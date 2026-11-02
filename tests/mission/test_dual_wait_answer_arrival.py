"""A reciprocal wait accepts one answer, then reopens only its requester once."""

from __future__ import annotations

import json
import socket
import threading
import time

from ora.contracts.models import LiveStartRequest
from tests.mission.test_service import serialized_peer_name, service, tool


def _drive_until(host, run_id: str, predicate) -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Reciprocal answer arrival did not reach its saved boundary")


def _context(packet: dict) -> dict:
    return json.loads(packet["messages"][-1]["content"])


def _last_tool(packet: dict) -> tuple[str, dict]:
    for message in reversed(packet["messages"]):
        if message["role"] == "tool":
            return message["name"], json.loads(message["content"])
    return "", {}


def test_answer_arrival_reopens_its_requester_without_fabricating_opposite_reply_after_restart(
    tmp_path, monkeypatch,
) -> None:
    def no_network(*_args, **_kwargs):
        raise AssertionError("Offline reciprocal wait must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    host = None
    run_id = None
    turns = {"Sage": 0, "Mxra": 0}
    requests: dict[str, str] = {}
    seen = {"recipient_input": None, "cycle": False}
    producer = {"role": None}
    producer_lock = threading.Lock()
    both_waiting = threading.Event()
    wait_snapshot: dict[str, str] = {}
    peer_text = "A synthetic independent check finds only a limited conclusion."

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        role = serialized_peer_name(packet)
        other = "Mxra" if role == "Sage" else "Sage"
        turns[role] += 1
        step = turns[role]
        response_index = step if role == "Sage" else 100 + step
        if step == 1:
            return tool("peer_request", {
                "target_instance_id": "agent-critique" if role == "Sage" else "agent-synthesis",
                "question": f"Please check {role}'s synthetic claim independently.",
            }, response_index)
        if step == 2:
            name, result = _last_tool(packet)
            assert name == "peer_request" and result["status"] == "request_saved"
            requests[role] = result["request_id"]
            return tool("request_capability", {"tool_id": "wait_for_dependencies"}, response_index)
        if step == 3:
            outgoing = host.store.get("mail_request", requests[role])
            target = host._control(run_id, "agent-critique" if role == "Sage" else "agent-synthesis")
            return tool("wait_for_dependencies", {"dependencies": [{
                "kind": "peer_request", "request_id": outgoing["id"],
                "request_version": outgoing["version"],
                "target_control_id": target["id"],
                "target_control_version": target["version"],
            }], "reason": "Wait for the independent check."}, response_index)
        if "wait_needs_peer_replan" in json.dumps(packet["messages"]):
            assert both_waiting.wait(timeout=5)
            with producer_lock:
                if producer["role"] is None:
                    producer["role"] = role
                    seen["cycle"] = True
                    assert len(host.store.records("mission_wait_decision")) >= 2
                    return tool("publish_result", {
                        "title": "Limited independent response", "text": peer_text,
                        "citations": [], "limitations": ["Synthetic fixture"],
                        "answer_request_id": requests[other],
                        "scope_completion": {
                            "reason": "The reciprocal wait requires independent progress; answer the peer first.",
                            "decisions": [],
                        },
                    }, response_index)
        outgoing = host.store.get("mail_request", requests[role])
        if not outgoing["body"]["answer_refs"]:
            # A cycle replan can reach this peer first. That is not answer delivery.
            assert outgoing["body"]["status"] == "unanswered"
            target = host._control(run_id, "agent-critique" if role == "Sage" else "agent-synthesis")
            return tool("wait_for_dependencies", {"dependencies": [{
                "kind": "peer_request", "request_id": outgoing["id"],
                "request_version": outgoing["version"],
                "target_control_id": target["id"],
                "target_control_version": target["version"],
            }], "reason": "Still awaiting this exact peer answer."}, response_index)
        assert role != producer["role"]
        assert seen["recipient_input"] is None
        context = _context(packet)
        answer_ref = outgoing["body"]["answer_refs"][0]
        preview = next(item for item in context["peer_answer_previews"]
                       if item["answer_ref"] == answer_ref)
        assert preview["request_ref"]["id"] == outgoing["id"]
        assert preview["request_status"] in {"awaiting_review", "partial_awaiting_review"}
        assert preview["report_delivery"] == "complete_in_input"
        assert preview["question"] == f"Please check {role}'s synthetic claim independently."
        assert preview["report"]["body"] == peer_text
        assert host.store.get("mail_request", requests[other])["body"]["status"] == "unanswered"
        seen["recipient_input"] = role
        return tool("review_peer_answer", {
            "request_id": outgoing["id"], "answer_id": answer_ref["id"],
            "disposition": "answered", "reason": "The saved independent check answers this request.",
        }, response_index)

    host = service(tmp_path, monkeypatch, transport)
    real_update_control = host._update_control

    def observe_wait_transition(control, **changes):
        result = real_update_control(control, **changes)
        if run_id is not None and len(requests) == 2 and not both_waiting.is_set():
            current = {
                alias: host._control(run_id, agent_id)["body"]
                for alias, agent_id in (("Sage", "agent-synthesis"), ("Mxra", "agent-critique"))
            }
            if all(item["phase"] == "waiting" and item["wait_for"]
                   for item in current.values()):
                wait_snapshot.update({
                    role: current[role]["wait_for"][0]["request_ref"]["id"]
                    for role in current
                })
                both_waiting.set()
        return result

    host._update_control = observe_wait_transition
    try:
        run = host.start(LiveStartRequest(
            request_id="offline-reciprocal-answer-arrival",
            question="Exchange independent checks of a synthetic claim.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        run_id = run.run_id
        _drive_until(host, run_id, both_waiting.is_set)
        assert wait_snapshot == requests
        decisions = host.store.records("mission_wait_decision")
        assert {item["body"]["actor_id"] for item in decisions} == {
            host._control(run_id, alias)["body"]["actor_id"]
            for alias in ("agent-synthesis", "agent-critique")
        }
        assert all(item["body"]["wait_for"][0]["kind"] == "peer_request" for item in decisions)
        _drive_until(host, run_id, lambda: bool(host.store.records("mail_answer")))
        assert seen["cycle"]
        assert producer["role"] in {"Sage", "Mxra"}
        recipient_role = "Mxra" if producer["role"] == "Sage" else "Sage"
        assert len(host.store.records("mail_answer")) == 1
        assert seen["recipient_input"] is None
        recipient_control = host._control(
            run_id, "agent-synthesis" if recipient_role == "Sage" else "agent-critique",
        )
        if recipient_control["body"]["phase"] == "waiting":
            _drive_until(host, run_id, lambda: host._control(
                run_id, "agent-synthesis" if recipient_role == "Sage" else "agent-critique",
            )["body"]["phase"] == "ready_model")
            recipient_control = host._control(
                run_id, "agent-synthesis" if recipient_role == "Sage" else "agent-critique",
            )
        assert recipient_control["body"]["phase"] == "ready_model"
        feedback = recipient_control["body"]["feedback"]
        assert feedback["status"] == "wait_dependency_changed"
        assert feedback["changed"][0]["saved_input_ref"]["id"] == requests[recipient_role]
        assert feedback["changed"][0]["current_input_ref"]["id"] == requests[recipient_role]
        assert seen["recipient_input"] is None
        answered = host.store.get("mail_request", requests[recipient_role])
        assert len(answered["body"]["answer_refs"]) == 1
        assert host.store.get("mail_request", requests[producer["role"]])["body"]["status"] == "unanswered"
        answer_ref = answered["body"]["answer_refs"][0]
        saved_answer = host.store.get(**answer_ref)
        assert saved_answer["body"]["request_id"] == requests[recipient_role]
        report_ref = saved_answer["body"]["artifact_ref"]
        assert report_ref["kind"] == "report"
        assert host.store.get(**report_ref)["body"]["body"] == peer_text
    finally:
        host.close()

    # Reopen the same state; the answer must reach the requester once without
    # turning the opposite, unanswered request into a fabricated resolution.
    host = service(tmp_path, monkeypatch, transport)
    try:
        _drive_until(host, run_id, lambda: seen["recipient_input"] == recipient_role)
        assert len(host.store.records("mail_answer")) == 1
        assert host.store.get("mail_request", requests[producer["role"]])["body"]["status"] == "unanswered"
        _drive_until(host, run_id, lambda: host.store.get(
            "mail_request", requests[recipient_role],
        )["body"].get("accepted_answer_ref") == answer_ref)
        assert len(host.store.records("mail_answer")) == 1
        assert len(host.store.records("mission_wait_decision")) >= 2
        for _ in range(6):
            host._drive_run(host._run(run_id))
        assert len(host.store.records("mail_answer")) == 1
        assert seen["recipient_input"] == recipient_role
    finally:
        host.close()
