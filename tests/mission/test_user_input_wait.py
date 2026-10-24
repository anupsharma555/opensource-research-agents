"""A host-authenticated amendment can reach a peer during an explicit wait."""

from __future__ import annotations

import json
import time

from ora.contracts.models import LiveStartRequest, LiveUserInputRequest
from tests.mission.test_service import serialized_peer_name, service, tool


def _drive_until(host, run_id, predicate, *, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Explicit wait did not reach the expected saved boundary")


def test_user_input_interrupts_explicit_peer_wait_without_answering_peer(
    tmp_path, monkeypatch,
):
    turns = {"Sage": 0, "Mxra": 0}
    packets = []
    user_text = "Please review the synthetic comparison's denominator before concluding."
    saved = {}

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        role = serialized_peer_name(packet)
        turns[role] += 1
        if role == "Mxra":
            raise AssertionError("The peer is intentionally waiting in this offline case")
        step = turns[role]
        if step == 1:
            return tool("peer_request", {
                "target_instance_id": "agent-critique",
                "question": "Independently inspect the synthetic comparison.",
            }, step)
        if step == 2:
            assert packet["messages"][-2]["name"] == "peer_request"
            return tool("request_capability", {"tool_id": "wait_for_dependencies"}, step)
        if step == 3:
            context = json.loads(packet["messages"][-1]["content"])
            disposition = context["request_dispositions"][0]
            saved["request_id"] = disposition["ref"]["id"]
            target = disposition["recipient_control_ref"]
            return tool("wait_for_dependencies", {
                "dependencies": [{
                    "kind": "peer_request",
                    "request_id": disposition["ref"]["id"],
                    "request_version": disposition["ref"]["version"],
                    "target_control_id": target["id"],
                    "target_control_version": target["version"],
                }],
                "reason": "Await independent review while preserving the unanswered request.",
            }, step)
        assert step == 4, "Unexpected extra requester call"
        assert user_text in json.dumps(packet["messages"])
        context = json.loads(packet["messages"][-1]["content"])
        assert context["request_dispositions"][0]["status"] == "unanswered"
        saved["delivered_packet"] = packet
        return tool("discover_capabilities", {}, step)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="user-input-peer-wait", question="Inspect a synthetic comparison",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        critique = host._control(run.run_id, "agent-critique")
        host._update_control(
            critique, phase="retry_at", retry_at=time.time() + 3600,
            waiting_reason="Fixture holds independent review",
        )
        _drive_until(host, run.run_id, lambda: (
            turns["Sage"] >= 3
            and bool(host._control(run.run_id, "agent-synthesis")["body"].get("wait_for"))
        ))
        requester = host._control(run.run_id, "agent-synthesis")
        assert requester["body"]["phase"] == "waiting"
        assert host.store.get("mail_request", saved["request_id"])["body"]["status"] == "unanswered"
        critique_before = host._control(run.run_id, "agent-critique")
        task = host.store.get("board_proposal", requester["body"]["task_id"])
        receipt = host.submit_user_input(LiveUserInputRequest(
            request_id="user-amendment-during-wait", run_id=run.run_id,
            task_id=task["id"], target_agent_id=requester["body"]["actor_id"],
            expected_task_version=task["version"], kind="direction", text=user_text,
        ))
        assert receipt.status == "queued_for_next_boundary"
        _drive_until(host, run.run_id, lambda: "delivered_packet" in saved)
        assert host.store.get("mail_request", saved["request_id"])["body"]["status"] == "unanswered"
        assert host._control(run.run_id, "agent-critique") == critique_before
        assert turns["Mxra"] == 0
        assert len(packets) == 4
    finally:
        host.close()
