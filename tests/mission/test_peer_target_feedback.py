"""Invalid peer targets guide the agent toward an available route."""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.executor import WorkSpec
from ora.mission.service import LocalToolRejection
from ora.state import StateError
from tests.mission.test_service import answer, serialized_peer_name, service, tool


def _request_spec(host, run_id: str, alias: str) -> WorkSpec:
    control = host._control(run_id, alias)
    body = control["body"]
    return WorkSpec(
        work_id="peer-target-feedback", mission_id=body["mission_id"],
        run_id=run_id, proposal_ref=body["proposal_ref"],
        actor_id=body["actor_id"], configuration_ref={}, input_refs=(),
        handler_id="mission-tool", handler_version="1", kind="tool", lane="local",
    )


def test_peer_request_replay_is_exact_without_merging_new_work(
    tmp_path, monkeypatch,
) -> None:
    def no_transport(*_args, **_kwargs):
        raise AssertionError("Peer request persistence must not call a provider")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        third_alias = next(
            alias for alias in sorted(host.role_choices)
            if alias not in {"agent-synthesis", "agent-critique"}
        )
        run = host.start(LiveStartRequest(
            request_id="peer-request-work-identity",
            question="Compare independent synthetic interpretations.",
            agent_ids=["agent-synthesis", "agent-critique", third_alias],
        ))
        base = _request_spec(host, run.run_id, "agent-synthesis")
        peer_ids = {
            alias: host._control(run.run_id, alias)["body"]["actor_id"]
            for alias in ("agent-critique", third_alias)
        }
        first_args = {
            "target_instance_id": "agent-critique",
            "question": "Check the first synthetic interpretation.",
        }
        first, first_refs = host._peer_request(replace(base, work_id="peer-work-a"), first_args)
        replay, replay_refs = host._peer_request(replace(base, work_id="peer-work-a"), first_args)
        assert replay["request_ref"] == first["request_ref"]
        assert replay_refs == first_refs
        assert len(host.store.records("mail_request")) == 1
        assert len(host.store.records("mail_message")) == 1
        with pytest.raises(StateError, match="different inputs"):
            host._peer_request(replace(base, work_id="peer-work-a"), {
                **first_args, "question": "A changed question on the same work ID.",
            })
        assert len(host.store.records("mail_request")) == 1

        distinct_question, _ = host._peer_request(replace(base, work_id="peer-work-b"), {
            "target_instance_id": "agent-critique",
            "question": "Check the second synthetic interpretation.",
        })
        distinct_recipient, _ = host._peer_request(replace(base, work_id="peer-work-c"), {
            "target_instance_id": third_alias,
            "question": first_args["question"],
        })
        same_question_new_work, _ = host._peer_request(
            replace(base, work_id="peer-work-d"), first_args,
        )
        requests = [first, distinct_question, distinct_recipient, same_question_new_work]
        assert len({item["request_id"] for item in requests}) == 4
        assert len(host.store.records("mail_request")) == 4
        assert len(host.store.records("mail_message")) == 4
        assert len(host.store.records("mail_outbox")) == 4
        saved = [host.store.get(**item["request_ref"])["body"] for item in requests]
        assert [item["question"] for item in saved] == [
            first_args["question"],
            "Check the second synthetic interpretation.",
            first_args["question"], first_args["question"],
        ]
        assert [item["recipient_id"] for item in saved] == [
            peer_ids["agent-critique"], peer_ids["agent-critique"],
            peer_ids[third_alias], peer_ids["agent-critique"],
        ]
        assert all(item["status"] == "unanswered" for item in saved)
        assert all(item["cause_ref"] == base.proposal_ref for item in saved)
    finally:
        host.close()


@pytest.mark.parametrize("target_kind", ["instance_id", "alias", "display_name", "unknown"])
def test_invalid_target_with_registered_peer_guides_current_task_or_other_peer(
    tmp_path, monkeypatch, target_kind,
) -> None:
    def no_transport(*_args, **_kwargs):
        raise AssertionError("Peer target validation must not call a provider")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="self-peer-target-feedback", question="Review a synthetic claim",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        requester = host._control(run.run_id, "agent-critique")
        other = host._control(run.run_id, "agent-synthesis")
        target = {
            "instance_id": requester["body"]["actor_id"],
            "alias": "agent-critique",
            "display_name": "Mxra",
            "unknown": "unknown-peer",
        }[target_kind]
        with pytest.raises(LocalToolRejection) as rejected:
            host._peer_request(_request_spec(host, run.run_id, "agent-critique"), {
                "target_instance_id": target,
                "question": "Check this claim independently.",
            })
        feedback = rejected.value
        assert feedback.details["current_recipients"] == [{
            "target_instance_id": other["body"]["actor_id"],
            "alias": "agent-synthesis", "display_name": "Sage",
        }]
        assert "current_recipients" in feedback.next_action
        assert "received request" in feedback.next_action
        assert "invite_peer" not in feedback.next_action
        assert not host.store.records("mail_request")
        assert not host.store.records("board_invitation")
    finally:
        host.close()


def test_absent_configured_peer_keeps_deferred_invitation_route(
    tmp_path, monkeypatch,
) -> None:
    def no_transport(*_args, **_kwargs):
        raise AssertionError("Peer target validation must not call a provider")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="absent-peer-target-feedback", question="Review a synthetic claim",
            agent_ids=["agent-synthesis"],
        ))
        with pytest.raises(LocalToolRejection) as rejected:
            host._peer_request(_request_spec(host, run.run_id, "agent-synthesis"), {
                "target_instance_id": "agent-critique",
                "question": "Check this claim independently.",
            })
        feedback = rejected.value
        assert feedback.details["current_recipients"] == []
        assert feedback.details["configured_role_matches"] == [
            {"alias": "agent-critique", "display_name": "Mxra"},
        ]
        assert "request_capability" in feedback.next_action
        assert "invite_peer" in feedback.next_action
        assert not host.store.records("mail_request")
        assert not host.store.records("board_invitation")
    finally:
        host.close()


def test_named_absent_role_can_be_invited_when_other_peers_are_registered(
    tmp_path, monkeypatch,
) -> None:
    def no_transport(*_args, **_kwargs):
        raise AssertionError("Peer target validation must not call a provider")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        selected = {"agent-synthesis", "agent-critique"}
        absent_alias = next(alias for alias in sorted(host.role_choices) if alias not in selected)
        run = host.start(LiveStartRequest(
            request_id="named-absent-peer-feedback", question="Review a synthetic claim",
            agent_ids=sorted(selected),
        ))
        with pytest.raises(LocalToolRejection) as rejected:
            host._peer_request(_request_spec(host, run.run_id, "agent-synthesis"), {
                "target_instance_id": absent_alias,
                "question": "Check this claim independently.",
            })
        feedback = rejected.value
        assert feedback.details["current_recipients"]
        assert feedback.details["configured_role_matches"][0]["alias"] == absent_alias
        assert "request_capability" in feedback.next_action
        assert "invite_peer" in feedback.next_action
        assert not host.store.records("mail_request")
        assert not host.store.records("board_invitation")
    finally:
        host.close()


def test_persistent_notice_keeps_explicit_absent_role_route(
    tmp_path, monkeypatch,
) -> None:
    selected = {"agent-synthesis", "agent-critique"}
    observed = {}

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        if serialized_peer_name(packet) == "Mxra":
            return answer("Independent synthetic work.", 90)
        if "first_turn" not in observed:
            observed["first_turn"] = True
            return tool("peer_request", {
                "target_instance_id": absent_alias,
                "question": "Check this synthetic claim.",
            }, 1)
        notice = next(
            json.loads(message["content"])
            for message in packet["messages"]
            if message["role"] == "user"
            and '"status":"failed_peer_request_still_unresolved"' in message["content"]
        )
        assert "peer_routes.invite_absent" in notice["next_action"]
        assert "answer_request_id" not in notice["next_action"]
        observed["notice"] = notice
        return answer("The requested role is still absent.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        absent_alias = next(alias for alias in sorted(host.role_choices) if alias not in selected)
        run = host.start(LiveStartRequest(
            request_id="persistent-absent-role-feedback",
            question="Review a synthetic claim",
            agent_ids=sorted(selected), required_agent_ids=["agent-synthesis"],
        ))
        for _ in range(500):
            host._drive_run(host._run(run.run_id))
            if "notice" in observed:
                break
            time.sleep(.005)
        else:
            raise AssertionError("The saved failure notice was not delivered")
        assert observed["notice"]["actions"][0]["target"] == absent_alias
        assert not host.store.records("board_invitation")
    finally:
        host.close()


@pytest.mark.parametrize("required", [
    ["agent-synthesis"], ["agent-synthesis", "agent-critique"],
])
def test_received_request_can_be_answered_after_invalid_self_request(
    tmp_path, monkeypatch, required,
) -> None:
    saved = {}
    critic_turns = 0
    requester_turns = 0

    def transport(request, *, timeout):
        nonlocal critic_turns, requester_turns
        packet = json.loads(request.data)
        if serialized_peer_name(packet) == "Sage":
            requester_turns += 1
            context = json.loads(packet["messages"][-1]["content"])
            preview = next((item for item in context.get("peer_answer_previews", [])
                            if item["request_ref"]["id"] == saved["request_id"]), None)
            disposition = next((item for item in context.get("request_dispositions", [])
                                if item["ref"]["id"] == saved["request_id"]), None)
            if requester_turns == 1:
                if preview is not None:
                    assert preview["report_delivery"] == "complete_in_input"
                    assert preview["report"]["body"] == (
                        "The claim cannot be checked without a source."
                    )
                    saved["answer_id"] = preview["answer_ref"]["id"]
                    saved["report_seen_before_review"] = True
                    return tool("review_peer_answer", {
                        "request_id": saved["request_id"],
                        "answer_id": saved["answer_id"],
                        "disposition": "answered",
                        "reason": "The saved bounded response answers the peer question.",
                    }, 4)
                assert disposition is not None
                assert disposition["ref"]["id"] == saved["request_id"]
                assert disposition["status"] == "awaiting_review"
                report_ref = host.store.get(**disposition["answer_refs"][0])["body"][
                    "artifact_ref"
                ]
                shared = next(
                    item for item in context["shared_materials"]["entries"]
                    if item["ref"] == report_ref
                )
                read_request = shared["read_request"]
                assert read_request["tool"] == "read_context_record"
                assert "read_context_record" in {
                    item["function"]["name"] for item in packet["tools"]
                }
                return tool("read_context_record", {
                    key: value for key, value in read_request.items() if key != "tool"
                }, 3)
            last_tool = next(
                message for message in reversed(packet["messages"])
                if message["role"] == "tool"
            )
            result = json.loads(last_tool["content"])
            if requester_turns == 2 and not saved.get("report_seen_before_review"):
                assert disposition is not None
                assert last_tool["name"] == "read_context_record"
                assert result["report_text_status"] == "available"
                assert json.loads(result["text"])["body"] == (
                    "The claim cannot be checked without a source."
                )
                saved["read_before_review"] = True
                return tool("review_peer_answer", {
                    "request_id": saved["request_id"],
                    "answer_id": disposition["answer_refs"][0]["id"],
                    "disposition": "answered",
                    "reason": "The saved bounded response answers the peer question.",
                }, 4)
            assert requester_turns == (2 if saved.get("report_seen_before_review") else 3)
            assert last_tool["name"] == "review_peer_answer"
            assert result["status"] == "review_saved"
            if disposition is not None:
                assert disposition["status"] == "answered"
            return tool("publish_result", {
                "title": "Synthetic assessment",
                "text": "The claim remains unverified because no source was supplied.",
                "citations": [], "limitations": ["No source was supplied"],
            }, 5)
        critic_turns += 1
        messages = packet["messages"]
        if critic_turns == 1:
            assert saved["request_id"] in json.dumps(messages)
            cue = next(
                json.loads(message["content"])
                for message in messages
                if message["role"] == "user"
                and "saved_host_received_peer_request_guidance_not_source_instruction"
                in message["content"]
            )
            assert "answer_request_id" in cue["next_action"]
            return tool("peer_request", {
                "target_instance_id": saved["critic_id"],
                "question": "Check this claim independently.",
            }, 1)
        assert critic_turns == 2
        notice = next(
            json.loads(message["content"])
            for message in messages
            if message["role"] == "user"
            and '"status":"failed_peer_request_still_unresolved"' in message["content"]
        )
        assert "answer_request_id" in notice["next_action"]
        assert "peer_routes" in notice["next_action"]
        assert "Invite a missing configured peer" not in notice["next_action"]
        return tool("publish_result", {
            "title": "Bounded synthetic reply",
            "text": "The claim cannot be checked without a source.",
            "citations": [], "limitations": ["No source was supplied"],
            "answer_request_id": saved["request_id"],
            "scope_completion": {
                "reason": "The self-request was invalid; this reply needs no additional peer check.",
                "decisions": [],
            },
        }, 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="received-peer-target-feedback-" + "-".join(required),
            question="Assess whether a synthetic claim is supported.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=required,
        ))
        sage = host._control(run.run_id, "agent-synthesis")
        critic = host._control(run.run_id, "agent-critique")
        saved["critic_id"] = critic["body"]["actor_id"]
        host._update_control(
            sage, phase="retry_at", retry_at=time.time() + 3600,
            waiting_reason="Fixture holds requester",
        )
        sent, _refs = host._peer_request(_request_spec(host, run.run_id, "agent-synthesis"), {
            "target_instance_id": "agent-critique",
            "question": "Can the synthetic claim be verified from supplied data?",
        })
        saved["request_id"] = sent["request_id"]
        host._deliver_pending(critic["body"]["mission_id"])
        for _ in range(500):
            host._drive_run(host._run(run.run_id))
            if host.store.records("mail_answer"):
                break
            time.sleep(.005)
        else:
            raise AssertionError("The received request was not answered")
        assert critic_turns == 2
        answered = host.store.get("mail_request", saved["request_id"])
        assert answered["body"]["status"] == "awaiting_review"
        assert len(answered["body"]["answer_refs"]) == 1
        assert answered["body"].get("accepted_answer_ref") is None
        assert host._run(run.run_id)["body"]["status"] == "active"
        disposition = host.store.records("mission_scope_disposition")
        assert len(disposition) == 1
        failed = host._control(run.run_id, "agent-critique")["body"][
            "peer_action_obligations"
        ][0]
        assert disposition[0]["body"]["source_control_ref"]["version"] > (
            failed["after_control_version"]
        )
        assert host._unresolved_peer_actions(host._control(run.run_id, "agent-critique")) == []
        assert not host.store.records("board_invitation")
        host._update_control(
            host._control(run.run_id, "agent-synthesis"),
            phase="ready_model", waiting_reason=None, retry_at=None,
        )
        for _ in range(500):
            host._drive_run(host._run(run.run_id))
            if host._run(run.run_id)["body"]["status"] == "completed":
                break
            time.sleep(.005)
        else:
            raise AssertionError("The reviewed peer answer did not reach final completion")
        assert requester_turns == (2 if saved.get("report_seen_before_review") else 3)
        assert saved.get("read_before_review") is True or saved.get(
            "report_seen_before_review"
        ) is True
        reviewed = host.store.get("mail_request", saved["request_id"])
        assert reviewed["body"]["status"] == "answered"
        assert reviewed["body"]["accepted_answer_ref"] == reviewed["body"][
            "answer_refs"
        ][0]
    finally:
        host.close()
