"""One model-selected peer turn keeps independent effects and report boundaries."""

from __future__ import annotations

import json
import socket
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.mission.peer_turn import link_peer_reply
from ora.mission.report_lineage import report_text_access
from ora.retrieval.providers import HTTPResult
from tests.mission.test_service import _search_spec, serialized_peer_name, service, tool


def _drive_until(host, run_id: str, predicate) -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Peer turn did not reach its saved boundary")


def _send(host, run_id: str, request_id: str, sender: str, recipient: str) -> None:
    owner = host._control(run_id, sender)
    target = host._control(run_id, recipient)
    mission_id = owner["body"]["mission_id"]
    sent = host.mailbox.send_request(
        f"send-{request_id}", mission_id=mission_id,
        request_id=request_id, message_id=f"message-{request_id}",
        requester_id=owner["body"]["actor_id"],
        recipient_id=target["body"]["actor_id"],
        question=f"Synthetic question {request_id}",
        expected_artifact="Short independent report",
        cause_ref=owner["body"]["proposal_ref"],
        payload={"question": f"Synthetic question {request_id}"},
        task_ref=owner["body"]["proposal_ref"],
    )
    assert sent["status"] == "accepted"
    host.mailbox.mark_delivered(f"deliver-{request_id}", mission_id=mission_id,
                                message_id=f"message-{request_id}")


@pytest.mark.parametrize(("bad_review", "restart", "malformed_followup"), [
    ("missing_answer", False, False), ("missing_answer", False, True),
    ("missing_answer", True, False), ("missing_answer", True, True),
    ("malformed_version", False, False), ("malformed_version", False, True),
    ("malformed_version", True, False), ("malformed_version", True, True),
    (None, True, False),
])
def test_peer_turn_reply_review_and_followup_persist_independently(
    tmp_path, monkeypatch, bad_review, restart, malformed_followup,
):
    def no_network(*_args, **_kwargs):
        raise AssertionError("Synthetic peer turn opened a socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    turns = {"Sage": 0, "Mxra": 0}
    seen = {}
    host = None

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        role = serialized_peer_name(packet)
        turns[role] += 1
        step = turns[role]
        names = {item["function"]["name"] for item in packet["tools"]}
        if role == "Sage" and step == 1:
            return tool("publish_result", {
                "title": "Sage synthetic answer", "text": "Synthetic answer to Mxra.",
                "citations": [], "limitations": [], "answer_request_id": "to-sage",
            }, 1)
        if role == "Mxra" and step == 1:
            assert "peer_turn" in names
            answer_ref = host.store.get("mail_request", "to-sage")["body"]["answer_refs"][0]
            incoming = host.store.get("mail_request", "to-mxra")
            outgoing = host.store.get("mail_request", "to-sage")
            current = json.loads(packet["messages"][-1]["content"])
            incoming_card = current["incoming_request"]
            assert incoming_card["ref"] == {
                "kind": "mail_request", "id": incoming["id"],
                "version": incoming["version"],
            }
            assert incoming_card["question"] == "Synthetic question to-mxra"
            assert any(
                item["request_ref"]["id"] == outgoing["id"]
                and item["answer_ref"] == answer_ref
                for key in ("peer_answer_previews", "peer_answer_review_cues")
                for item in current.get(key, [])
            )
            seen["answer_ref"] = answer_ref
            return tool("peer_turn", {
                "report": {
                    "kind": "reply",
                    "request_id": incoming["id"], "request_version": incoming["version"],
                    "title": "Mxra synthetic reply", "text": "Synthetic answer to Sage.",
                    "citations": [], "limitations": [],
                },
                "review": {
                    "request_id": outgoing["id"],
                    "request_version": ("latest" if bad_review == "malformed_version"
                                        else outgoing["version"]),
                    "answer_id": ("missing-answer" if bad_review == "missing_answer"
                                  else answer_ref["id"]),
                    "answer_version": 1,
                    "disposition": ("answered" if bad_review is None else "partly_answered"),
                    "reason": ("The independent check resolves this question."
                               if bad_review is None else "A further check is needed."),
                },
                "followup_request": ["invalid"] if malformed_followup else {
                    "target_instance_id": "agent-synthesis",
                    "question": "Please independently check another synthetic point.",
                },
            }, 2)
        raise AssertionError(f"Unexpected {role} turn {step}")

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="bounded-peer-turn-independent",
            question="Exchange synthetic peer answers and ask one follow-up.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        _send(host, run.run_id, "to-sage", "agent-critique", "agent-synthesis")
        _send(host, run.run_id, "to-mxra", "agent-synthesis", "agent-critique")
        mxra = host._control(run.run_id, "agent-critique")
        host._update_control(mxra, phase="retry_at", retry_at=time.time() + 3600,
                             waiting_reason="Hold until Sage's answer is saved")
        _drive_until(host, run.run_id, lambda: bool(host.store.get(
            "mail_request", "to-sage")["body"]["answer_refs"]))
        original_advance = host._advance_tool

        def remember_bundle_control(control, work):
            if (control["body"].get("tool_call") or {}).get("name") == "peer_turn":
                seen["bundle_control"] = control
            return original_advance(control, work)

        host._advance_tool = remember_bundle_control
        host._update_control(host._control(run.run_id, "agent-critique"),
                             phase="ready_model", waiting_reason=None, retry_at=None)
        _drive_until(host, run.run_id, lambda: bool(host.store.get(
            "mail_request", "to-mxra")["body"]["answer_refs"]))
        request = host.store.get("mail_request", "to-mxra")
        assert len(request["body"]["answer_refs"]) == 1
        mxra = host._control(run.run_id, "agent-critique")
        assert mxra["body"]["phase"] != "done"
        assert mxra["body"].get("report_ref") is None
        followups = [item for item in host.store.records("mail_request")
                     if item["id"] not in {"to-sage", "to-mxra"}]
        assert len(followups) == (0 if malformed_followup else 1)
        if followups:
            assert followups[0]["body"]["requester_id"] == mxra["body"]["actor_id"]
        reviewed_request = host.store.get("mail_request", "to-sage")
        assert reviewed_request["body"]["status"] == (
            "answered" if bad_review is None else "awaiting_review"
        )
        if bad_review is None:
            assert reviewed_request["body"]["accepted_answer_ref"] == seen["answer_ref"]
        result = host.store.get("context_tool_result", f"tool-result:{mxra['body']['response_ref']['id']}")
        assert result is not None
        payload = json.loads(host.store.read_blob(result["body"]["blob"]["sha256"]))
        operations = json.loads(payload["content"])["operations"]
        assert operations["report"]["status"] == "reply_linked"
        assert operations["review"]["status"] == (
            "review_saved" if bad_review is None else "rejected"
        )
        assert operations["followup_request"]["status"] == (
            "rejected" if malformed_followup else "request_saved"
        )
        sage = host._control(run.run_id, "agent-synthesis")
        answer = host.store.get(**operations["report"]["answer_ref"])
        provisional = host.context._provisional_peer_answer_entry(
            request, answer, mission_id=mxra["body"]["mission_id"],
            actor_id=sage["body"]["actor_id"], task_id=sage["body"]["task_id"],
            pointer=operations["report"]["report_ref"],
        )
        assert provisional["type"] == "provisional_peer_answer_report"
        assert provisional["status"] == "peer_answer_text_available"
        if restart:
            host.close()
            host = service(tmp_path, monkeypatch, transport)
            assert len(host.store.records("mail_answer")) == 2
            assert len(host.store.records("mail_request")) == (
                2 if malformed_followup else 3
            )
            assert host.store.get("mail_request", "to-mxra") == request
            assert host.store.get("mail_request", "to-sage") == reviewed_request
            assert host.store.get(**operations["report"]["answer_ref"]) == answer
            assert [item for item in host.store.records("mail_request")
                    if item["id"] not in {"to-sage", "to-mxra"}] == followups
        assert link_peer_reply(host, seen["bundle_control"],
                               operations["report"]["report_ref"], "to-mxra",
                               seen["bundle_control"]["body"]["tool_call"]["arguments"][
                                   "report"]["request_version"])["answer_ref"] == (
            operations["report"]["answer_ref"])
        assert len(host.store.records("mail_answer")) == 2
    finally:
        host.close()


@pytest.mark.parametrize("valid_review,malformed_report,invalid_followup", [
    (True, False, False), (False, False, False), (True, True, False),
    (True, False, True),
])
def test_peer_turn_review_then_dependent_revision_finishes(tmp_path, monkeypatch,
                                                            valid_review, malformed_report,
                                                            invalid_followup):
    monkeypatch.setattr(socket.socket, "connect", lambda *_a, **_k: (
        (_ for _ in ()).throw(AssertionError("No network expected"))))
    turns = {"Sage": 0, "Mxra": 0}
    host = None

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        role = serialized_peer_name(packet)
        turns[role] += 1
        if role == "Sage" and turns[role] == 1:
            return tool("publish_result", {
                "title": "Sage reply", "text": "Synthetic independent reply.",
                "citations": [], "limitations": [], "answer_request_id": "to-sage",
            }, 11)
        if role == "Mxra" and turns[role] == 1:
            outgoing = host.store.get("mail_request", "to-sage")
            answer_ref = outgoing["body"]["answer_refs"][0]
            current = json.loads(packet["messages"][-1]["content"])
            visible = next(
                item for item in current["peer_answer_previews"]
                if item["request_ref"]["id"] == outgoing["id"]
                and item["answer_ref"] == answer_ref
            )
            assert visible["question"] == "Synthetic question to-sage"
            assert visible["report"]["body"] == "Synthetic independent reply."
            actions = {
                "review": {
                    "request_id": outgoing["id"], "request_version": outgoing["version"],
                    "answer_id": answer_ref["id"] if valid_review else "missing-answer",
                    "answer_version": answer_ref["version"],
                    "disposition": "answered", "reason": "The synthetic reply addressed the check.",
                },
                "report": ["invalid"] if malformed_report else {
                    "kind": "revision",
                    "title": "Mxra revised result", "text": "Synthetic revised conclusion.",
                    "citations": [], "limitations": [],
                },
            }
            if invalid_followup:
                actions["followup_request"] = ["invalid"]
            return tool("peer_turn", actions, 12)
        raise AssertionError(f"Unexpected {role} turn")

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="bounded-peer-turn-dependent",
            question="Review an independent synthetic answer and revise.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        _send(host, run.run_id, "to-sage", "agent-critique", "agent-synthesis")
        mxra = host._control(run.run_id, "agent-critique")
        host._update_control(mxra, phase="retry_at", retry_at=time.time() + 3600,
                             waiting_reason="Hold until Sage's answer is saved")
        _drive_until(host, run.run_id, lambda: bool(host.store.get(
            "mail_request", "to-sage")["body"]["answer_refs"]))
        host._update_control(host._control(run.run_id, "agent-critique"),
                             phase="ready_model", waiting_reason=None, retry_at=None)
        _drive_until(host, run.run_id, lambda: (
            host._control(run.run_id, "agent-critique")["body"]["phase"] == "done"
            if valid_review and not malformed_report and not invalid_followup else
            bool(host.store.records("context_tool_result"))
        ))
        reviewed = host.store.get("mail_request", "to-sage")
        assert reviewed["body"]["status"] == ("answered" if valid_review else
                                              "awaiting_review")
        mxra = host._control(run.run_id, "agent-critique")
        if valid_review and not malformed_report and not invalid_followup:
            assert reviewed["body"]["accepted_answer_ref"] == reviewed["body"]["answer_refs"][0]
            report = host.store.get(**mxra["body"]["report_ref"])
            assert report["body"]["body"] == "Synthetic revised conclusion."
            assert host.store.get("board_proposal", mxra["body"]["task_id"])["body"][
                "status"] == "completed"
            assert len(host.store.records("report")) == 2
        else:
            assert mxra["body"]["phase"] != "done"
            assert mxra["body"].get("report_ref") is None
            assert len(host.store.records("report")) == (2 if invalid_followup else 1)
            tool_result = host.store.get(
                "context_tool_result", f"tool-result:{mxra['body']['response_ref']['id']}",
            )
            payload = json.loads(host.store.read_blob(tool_result["body"]["blob"]["sha256"]))
            operations = json.loads(payload["content"])["operations"]
            assert operations["review"]["status"] == (
                "review_saved" if valid_review else "rejected"
            )
            assert operations["report"]["status"] == (
                "saved_not_final" if invalid_followup else
                "rejected" if malformed_report else "blocked_dependency"
            )
            if invalid_followup:
                assert operations["report"]["reason"] == (
                    "independent_followup_request_rejected"
                )
                assert operations["followup_request"]["status"] == "rejected"
                assert host.store.get("board_proposal", mxra["body"]["task_id"])[
                    "body"]["status"] == "open"
                assert len(mxra["body"]["peer_action_obligations"]) == 1
    finally:
        host.close()


def test_revoked_reply_stays_unlinked_while_independent_followup_survives(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(socket.socket, "connect", lambda *_a, **_k: (
        (_ for _ in ()).throw(AssertionError("No network expected"))))
    host = None
    revoked = False
    source_ref = None

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        assert serialized_peer_name(packet) == "Mxra"
        incoming = host.store.get("mail_request", "to-mxra")
        return tool("peer_turn", {
            "report": {
                "kind": "reply",
                "request_id": incoming["id"], "request_version": incoming["version"],
                "title": "Source-dependent reply", "text": "Synthetic source-dependent answer.",
                "citations": [], "limitations": [],
            },
            "followup_request": {
                "target_instance_id": "agent-synthesis",
                "question": "Check an independent synthetic follow-up.",
            },
        }, 21)

    host = service(tmp_path, monkeypatch, transport)
    host.discovery_transport = lambda *_a, **_k: HTTPResult(200, json.dumps({
        "hitCount": 1, "nextCursorMark": "*", "resultList": {"result": [{
            "source": "MED", "id": "9876", "pmid": "9876",
            "title": "Synthetic source", "abstractText": "Checked synthetic source text. " * 5,
        }]},
    }).encode(), {})
    try:
        run = host.start(LiveStartRequest(
            request_id="bounded-peer-turn-revoked",
            question="Answer a synthetic source-dependent peer request.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        _send(host, run.run_id, "to-mxra", "agent-synthesis", "agent-critique")
        sage = host._control(run.run_id, "agent-synthesis")
        host._update_control(sage, phase="retry_at", retry_at=time.time() + 3600,
                             waiting_reason="Hold while Mxra answers")
        mxra = host._control(run.run_id, "agent-critique")
        spec = _search_spec(host, run.run_id, suffix="peer-turn-revoked")
        spec = type(spec)(**{**spec.__dict__, "actor_id": mxra["body"]["actor_id"],
                             "proposal_ref": mxra["body"]["proposal_ref"]})
        found, _ = host._search(spec, {"route": "europe_pmc.search",
                                       "query": "synthetic", "limit": 1})
        inspected, output = host._inspect_hit(spec, {"hit_id": found["hits"][0]["hit_id"]})
        assert inspected["status"] == "inspected"
        source_ref = output[0]
        host._update_control(mxra, source_refs=[source_ref])
        original_peer_request = host._peer_request

        def revoke_before_work_acceptance(spec, args):
            nonlocal revoked
            if spec.work_id.endswith(":peer-turn:followup_request") and not revoked:
                assert len(host.store.records("report")) == 1
                use = host.store.resolve("source_use.copy",
                                         f"{source_ref['id']}:{source_ref['version']}")
                host.store.transact("revoke-peer-turn-source", {}, lambda tx: tx.put(
                    "source_use", use["id"], {
                        **use["body"], "policy_status": "revoked", "allowed_uses": [],
                    }, expected_version=use["version"]),
                    mission_id=spec.mission_id)
                revoked = True
            return original_peer_request(spec, args)

        host._peer_request = revoke_before_work_acceptance
        _drive_until(host, run.run_id, lambda: bool(host.store.records(
            "context_tool_result")))
        assert revoked
        assert host.store.get("mail_request", "to-mxra")["body"]["answer_refs"] == []
        assert len(host.store.records("mail_request")) == 2
        assert len(host.store.records("mail_answer")) == 0
        mxra = host._control(run.run_id, "agent-critique")
        assert mxra["body"]["phase"] != "done"
        assert mxra["body"].get("report_ref") is None
        report = host.store.records("report")[0]
        assert report_text_access(host.store, report)["status"] == (
            "data_unavailable_due_source_use"
        )
        tool_result = host.store.records("context_tool_result")[0]
        payload = json.loads(host.store.read_blob(tool_result["body"]["blob"]["sha256"]))
        operations = json.loads(payload["content"])["operations"]
        assert operations["report"]["status"] == "saved_not_linked"
        assert operations["followup_request"]["status"] == "request_saved"
    finally:
        host.close()


def test_old_run_without_peer_turn_pin_keeps_old_model_tool_view(tmp_path, monkeypatch):
    packets = []

    def transport(request, *, timeout):
        packets.append(json.loads(request.data))
        return tool("board_read", {}, 31)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="synthetic-old-peer-contract",
            question="Keep the saved peer tool contract.",
            agent_ids=["agent-synthesis"], required_agent_ids=["agent-synthesis"],
        ))
        saved = host._run(run.run_id)
        host.store.transact("simulate-peer-turn-unpinned-run", {}, lambda tx: tx.put(
            "mission_run", saved["id"], {
                key: value for key, value in saved["body"].items()
                if key != "peer_turn_tool_version"
            }, expected_version=saved["version"]),
            mission_id=saved["body"]["mission_id"])
        control = host._control(run.run_id, "agent-synthesis")
        catalog, _ = host._tool_registry(control)
        assert "peer_turn" not in catalog.tools
        _drive_until(host, run.run_id, lambda: bool(packets))
        assert "peer_turn" not in {
            item["function"]["name"] for item in packets[0]["tools"]
        }
        call = host.store.records("agent_call")[0]
        assert "peer_turn" not in call["body"]["permitted_tools"]
    finally:
        host.close()
