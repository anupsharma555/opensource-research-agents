"""Exercise model-selected note and peer actions through the offline mission loop."""

from __future__ import annotations

import json
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.coordination.executor import WorkSpec
from ora.mission.service import LocalToolRejection
from tests.mission.test_service import answer, drain, serialized_peer_name, service, tool


def _last_tool(request: dict) -> tuple[str, dict]:
    message = next(item for item in reversed(request["messages"]) if item["role"] == "tool")
    return message["name"], json.loads(message["content"])


def _drive_until_peer_request_linked(host, run_id: str) -> dict:
    for _ in range(300):
        host._drive_run(host._run(run_id))
        requests = host.store.records("mail_request")
        control = host._control(run_id, "agent-synthesis")
        if requests and any(
            (item.get("request_ref") or {}).get("id") == requests[0]["id"]
            for item in control["body"].get("peer_action_obligations", [])
        ):
            return requests[0]
        if any(item["body"].get("phase") == "model_response_lost"
               for item in host.store.records("executor_work")):
            raise AssertionError("Injected model transport failed before peer request linkage")
        time.sleep(0.01)
    raise AssertionError("Accepted peer request was not linked to the prior local rejection")


def test_notes_follow_new_source_window_and_survive_service_restart(tmp_path, monkeypatch):
    requests: list[dict] = []
    source_ref = None
    next_window = None

    def transport(request, *, timeout):
        nonlocal next_window
        body = json.loads(request.data)
        requests.append(body)
        step = len(requests)
        if step == 1:
            return tool("read_source_window", {
                "source_id": source_ref["id"], "source_version": source_ref["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 80,
                "selection_mode": "replace",
            }, step)
        name, result = _last_tool(body)
        if step == 2:
            assert name == "read_source_window" and result["next_request"]
            next_window = result["next_request"]
            return tool("request_capability", {"tool_id": "write_note"}, step)
        if step == 3:
            assert name == "request_capability"
            assert "write_note" in {item["function"]["name"] for item in body["tools"]}
            return tool("write_note", {
                "note_id": "allocation-observation", "category": "hypothesis",
                "text": "The first window suggests a candidate allocation description.",
                "expected_version": 0,
            }, step)
        if step == 4:
            assert name == "write_note" and result["status"] == "saved"
            assert body["model"] == requests[0]["model"]
            assert body["reasoning_effort"] == requests[0]["reasoning_effort"]
            return tool("read_source_window", next_window, step)
        if step == 5:
            assert name == "read_source_window" and result["passages"]
            assert result["passages"][0]["locator"].endswith("chars=80:160")
            return tool("write_note", {
                "note_id": "allocation-observation", "category": "contradiction",
                "text": "The later window conflicts with the candidate; retain both versions.",
                "expected_version": 1,
            }, step)
        if step == 6:
            assert name == "write_note" and result["note_ref"]["version"] == 2
            return tool("request_capability", {"tool_id": "read_note"}, step)
        if step == 7:
            assert name == "request_capability"
            return tool("read_note", {"note_id": "allocation-observation"}, step)
        if step == 8:
            assert name == "read_note" and result["note"]["version"] == 2
            assert result["note"]["body"]["category"] == "contradiction"
            return tool("request_capability", {"tool_id": "search_notes"}, step)
        if step == 9:
            assert name == "request_capability"
            return tool("search_notes", {"query": "later window conflicts"}, step)
        assert step == 10 and name == "search_notes"
        assert [(note["id"], note["version"]) for note in result["notes"]] == [
            ("allocation-observation", 2)
        ]
        return answer("The two saved observations conflict; further checking is needed.", step)

    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(
        request_id="note-continuation", question="Compare a synthetic source in windows",
        evidence_text="A synthetic source with a first view and a conflicting later view. " * 20,
        agent_ids=["agent-synthesis"],
    ))
    control = host._control(run.run_id, "agent-synthesis")
    source_ref = control["body"]["source_refs"][0]
    for _ in range(300):
        host._drive_run(host._run(run.run_id))
        note = host.store.get("working_note", "allocation-observation")
        control = host._control(run.run_id, "agent-synthesis")
        if note and control["body"]["phase"] == "ready_model":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("First model-selected note was not saved before restart")
    assert len(requests) == 3
    first_ref = ref(note)
    host.close()

    resumed = service(tmp_path, monkeypatch, transport)
    try:
        drain(resumed, run.run_id, limit=30)
        assert resumed.state(run.run_id).snapshot.mission.status == "completed"
        assert len(requests) == 10
        current = resumed.store.get("working_note", "allocation-observation")
        prior = resumed.store.get("working_note", "allocation-observation", 1)
        assert current["version"] == 2 and ref(prior) == first_ref
        assert prior["body"]["category"] == "hypothesis"
        assert current["body"]["category"] == "contradiction"
        assert prior["body"]["input_refs"] == current["body"]["input_refs"] == [source_ref]
        windows = resumed.store.records("source_read_window")
        assert {item["body"]["char_offset"] for item in windows} == {0, 80}
        assert all(item["body"]["copy_ref"] == source_ref for item in windows)
        assert {body["model"] for body in requests} == {requests[0]["model"]}
    finally:
        resumed.close()


def test_forged_peer_actor_argument_is_local_then_authorized_request_succeeds(
    tmp_path, monkeypatch
):
    counts = {"synthesis": 0, "critique": 0}
    seen_results = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        role = "critique" if serialized_peer_name(body) == "Mxra" else "synthesis"
        counts[role] += 1
        step = counts[role]
        if role == "synthesis" and step == 1:
            return tool("peer_request", {
                "target_instance_id": "agent-critique",
                "question": "Check the synthetic interpretation.",
                "requester_id": "forged-peer-identity",
            }, 1)
        if role == "synthesis" and step == 2:
            name, result = _last_tool(body)
            assert name == "peer_request" and result["status"] == "rejected"
            assert result["external_action"] == "none"
            seen_results.append(result)
            return tool("peer_request", {
                "target_instance_id": "agent-critique",
                "question": "Check the synthetic interpretation.",
            }, 2)
        return answer(f"{role} offers a bounded synthetic assessment.", 10 + step)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="peer-actor-override", question="Compare independent synthetic views",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        request = _drive_until_peer_request_linked(host, run.run_id)
        assert seen_results and seen_results[0]["field"] == "arguments"
        requests = host.store.records("mail_request")
        assert requests == [request]
        synthesis = host._control(run.run_id, "agent-synthesis")["body"]["actor_id"]
        critique = host._control(run.run_id, "agent-critique")["body"]["actor_id"]
        assert requests[0]["body"]["requester_id"] == synthesis
        assert requests[0]["body"]["recipient_id"] == critique
        assert "forged-peer-identity" not in json.dumps(requests[0]["body"])
    finally:
        host.close()


def test_absent_peer_can_be_invited_then_receive_multiline_request_under_same_model(
    tmp_path, monkeypatch
):
    host = None
    run = None
    seen_models = []
    synthesis_turns = 0
    question = "Check the synthetic comparison.\nName an alternative.\nState uncertainty."

    def transport(request, *, timeout):
        nonlocal synthesis_turns
        body = json.loads(request.data)
        if "Sage" not in body["messages"][0]["content"]:
            return answer("The invitation remains a separate peer decision.", 100)
        seen_models.append((body["model"], body["reasoning_effort"]))
        synthesis_turns += 1
        if synthesis_turns == 1:
            instructions = body["messages"][0]["content"]
            assert "peer_request targets registered peers" in instructions
            assert "invite absent roles first; invitation is not acceptance" in instructions
            assert "invite_peer" in instructions
            assert "agent-critique (Mxra)" not in instructions
            assert {"peer_request", "request_capability"} <= {
                item["function"]["name"] for item in body["tools"]
            }
            return tool("peer_request", {
                "target_instance_id": "Mxra", "question": question,
            }, 1)
        name, result = _last_tool(body)
        if synthesis_turns == 2:
            assert name == "peer_request" and result["status"] == "rejected"
            assert result["external_action"] == "none"
            assert result["current_recipients"] == []
            assert {item["alias"] for item in result["configured_role_matches"]} == {
                "agent-critique"
            }
            assert not host.store.records("mail_request")
            assert not host.store.records("mail_message")
            assert not host.store.records("mail_outbox")
            return tool("request_capability", {"tool_id": "invite_peer"}, 2)
        if synthesis_turns == 3:
            assert name == "request_capability"
            assert "invite_peer" in {
                item["function"]["name"] for item in body["tools"]
            }
            control = host._control(run.run_id, "agent-synthesis")
            assert control["body"]["source_refs"] == []
            instructions = body["messages"][0]["content"]
            assert "recipient_alias must be an exact configured alias" in instructions
            assert "agent-critique (Mxra)" in instructions
            proposal = host.store.get("board_proposal", control["body"]["task_id"])
            return tool("invite_peer", {
                "proposal_id": proposal["id"],
                "proposal_version": proposal["version"],
                "recipient_alias": "agent-critique",
                "objective": "Review the synthetic comparison",
                "expected_artifact": "Independent methods note",
            }, 3)
        if synthesis_turns == 4:
            assert name == "invite_peer"
            assert host.store.records("board_invitation")
            return tool("peer_request", {
                "target_instance_id": "agent-critique", "question": question,
            }, 4)
        return answer("The peer request was saved for independent review.", 10 + synthesis_turns)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="peer-invitation-repair", question="Compare synthetic views",
            agent_ids=["agent-synthesis"],
        ))
        request = _drive_until_peer_request_linked(host, run.run_id)
        recipient = host.store.get("agent_instance", request["body"]["recipient_id"], 1)
        assert request["body"]["question"] == question
        assert recipient["body"]["role_id"] == "independent_methods_reviewer_critic"
        assert recipient["body"]["run_id"] == run.run_id
        assert len(host.store.records("mail_message")) == 1
        assert len(host.store.records("mail_outbox")) == 1
        assert synthesis_turns >= 4 and len(set(seen_models)) == 1
    finally:
        host.close()


def test_ambiguous_invited_alias_is_local_without_mail_dispatch(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("Idle", 1))
    try:
        run = host.start(LiveStartRequest(
            request_id="ambiguous-peer-alias", question="Compare synthetic views",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        proposal = host.store.get("board_proposal", control["body"]["task_id"])
        spec = WorkSpec(
            work_id="peer-argument-check", mission_id=control["body"]["mission_id"],
            run_id=run.run_id, proposal_ref=ref(proposal),
            actor_id=control["body"]["actor_id"], configuration_ref={}, input_refs=(),
            handler_id="mission-tool", handler_version="1", kind="compute",
            lane=control["body"]["actor_id"],
        )
        with pytest.raises(LocalToolRejection) as absent:
            host._peer_request(spec, {
                "target_instance_id": "agent-critique", "question": "Check the comparator",
            })
        assert absent.value.details["current_recipients"] == []
        for index in (1, 2):
            invited = host.work.invite(
                f"invite-critic-{index}", run_ref=ref(host._run(run.run_id)),
                actor_id=spec.actor_id, source_control_ref=ref(control),
                proposal_ref=ref(proposal), recipient_alias="agent-critique",
                objective="Review the synthetic comparison",
                expected_artifact="Independent note",
                invitation_id=f"critic-invitation-{index}",
            )
            assert invited["status"] == "accepted"
        with pytest.raises(LocalToolRejection) as ambiguous:
            host._peer_request(spec, {
                "target_instance_id": "agent-critique", "question": "Check the comparator",
            })
        assert "ambiguous" in ambiguous.value.condition
        assert len(ambiguous.value.details["current_recipients"]) == 2
        assert not host.store.records("mail_request")
        assert not host.store.records("mail_message")
        assert not host.store.records("mail_outbox")
        recipient = ambiguous.value.details["current_recipients"][0]
        saved, _refs = host._peer_request(spec, {
            "target_instance_id": recipient["display_name"],
            "question": "Check the comparator\nand state the limit",
        })
        assert saved["status"] == "request_saved"
        assert saved["recipient_id"] == recipient["target_instance_id"]
        assert len(host.store.records("mail_request")) == 1
    finally:
        host.close()
