"""Offline service proof that an unavailable peer wait cannot strand healthy work."""

from __future__ import annotations

import json
import socket
import time

import pytest

from ora.contracts.models import LiveBranchCancelRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.mission.waits import inspect_waits
from tests.mission.test_service import service, tool


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("socket connection")
        raise AssertionError("Offline communication check attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    yield
    assert attempts == []


def _drive_until(host, run_id: str, predicate) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Mission did not reach the saved communication checkpoint")


def _last_tool(body: dict) -> tuple[str, dict]:
    for message in reversed(body["messages"]):
        if message["role"] == "tool":
            return message["name"], json.loads(message["content"])
    return "", {}


def test_cancelled_optional_recipient_wakes_requester_while_required_peer_progress_survives(
    tmp_path, monkeypatch,
) -> None:
    """A saved wait ends when its recipient cannot answer; another branch stays accepted."""
    host = None
    run_id = None
    turns = {"Sage": 0, "Remy": 0}
    request_id = None
    observed = {"unavailable_input": False}

    def transport(request, *, timeout):
        nonlocal request_id
        body = json.loads(request.data)
        instruction = body["messages"][0]["content"]
        identities = [part for part in instruction.split("\n\n")
                      if part.startswith("You are ")]
        assert len(identities) == 1
        role = next(name for name in turns if identities[0].startswith(f"You are {name},"))
        turns[role] += 1
        step = turns[role]
        tools = {item["function"]["name"] for item in body["tools"]}
        if role == "Remy":
            assert step == 1 and "publish_result" in tools
            return tool("publish_result", {
                "title": "Independent synthetic data note",
                "text": "The synthetic data peer completed its own bounded check.",
                "citations": [], "limitations": ["Synthetic input only"],
            }, 100)
        if step == 1:
            assert "peer_request" in tools
            return tool("peer_request", {
                "target_instance_id": "agent-critique",
                "question": "Review the synthetic comparison independently.",
            }, 1)
        if step == 2:
            name, result = _last_tool(body)
            assert name == "peer_request" and result["status"] == "request_saved"
            request_id = result["request_id"]
            assert result["recipient_control_ref"]["id"] == host._control(
                run_id, "agent-critique"
            )["id"]
            assert "request_capability" in tools
            return tool("request_capability", {"tool_id": "wait_for_dependencies"}, 2)
        if step == 3:
            assert "wait_for_dependencies" in tools
            outgoing = host.store.get("mail_request", request_id)
            target = host._control(run_id, "agent-critique")
            return tool("wait_for_dependencies", {
                "dependencies": [{
                    "kind": "peer_request", "request_id": outgoing["id"],
                    "request_version": outgoing["version"],
                    "target_control_id": target["id"],
                    "target_control_version": target["version"],
                }],
                "reason": "Await the independent synthetic critique.",
            }, 3)
        assert step == 4 and "publish_result" in tools
        context = json.loads(body["messages"][-1]["content"])
        dispositions = {item["ref"]["id"]: item for item in context["request_dispositions"]}
        assert dispositions[request_id]["status"] == "recipient_unavailable_due_branch_cancel"
        assert not dispositions[request_id]["answer_refs"]
        assert host._control(run_id, "agent-data-management")["body"]["phase"] == "done"
        observed["unavailable_input"] = True
        return tool("publish_result", {
            "title": "Synthetic comparison with peer limit",
            "text": "The independent data check completed; critique was unavailable.",
            "citations": [], "limitations": ["Critique peer branch was cancelled"],
            "scope_completion": {
                "reason": "The requested critique cannot answer after its branch was cancelled.",
                "decisions": [],
            },
        }, 4)

    host = service(tmp_path, monkeypatch, transport)
    try:
        aliases = ["agent-synthesis", "agent-critique", "agent-data-management"]
        run = host.start(LiveStartRequest(
            request_id="unavailable-peer-wait-closure",
            question="Compare two independent synthetic checks.",
            evidence_text="A synthetic source passage for bounded comparison.",
            agent_ids=aliases,
            required_agent_ids=["agent-synthesis", "agent-data-management"],
        ))
        run_id = run.run_id
        original_source_refs = host._run(run_id)["body"]["source_refs"]
        for alias in aliases[1:]:
            host._update_control(host._control(run_id, alias), phase="retry_at",
                                 retry_at=time.time() + 3600,
                                 waiting_reason="Fixture holds sibling until requester waits")
        _drive_until(host, run_id, lambda: bool(
            host._control(run_id, "agent-synthesis")["body"].get("wait_for")
        ))
        requester = host._control(run_id, "agent-synthesis")
        assert requester["body"]["phase"] == "waiting"
        assert requester["body"]["wait_for"][0]["request_ref"]["id"] == request_id
        assert host.store.get("mail_request", request_id)["body"]["status"] == "unanswered"
        diagnosis = inspect_waits(host.store, requester["body"]["mission_id"])
        assert any(item["control_ref"] == ref(requester)
                   and item["kind"] == "explicit_peer_or_dependency_wait"
                   for item in diagnosis["normal_waits"])
        assert any(item["control_ref"]["id"] == host._control(
            run_id, "agent-critique"
        )["id"] and item["kind"] == "saved_retry_timer"
                   for item in diagnosis["normal_waits"])

        data = host._control(run_id, "agent-data-management")
        host._update_control(data, phase="ready_model", waiting_reason=None, retry_at=None)
        _drive_until(host, run_id, lambda: host._control(
            run_id, "agent-data-management"
        )["body"]["phase"] == "done")
        data_report = host._control(run_id, "agent-data-management")["body"]["report_ref"]
        assert data_report and host.store.get(**data_report)
        assert host._control(run_id, "agent-synthesis")["body"]["phase"] == "waiting"
        assert host._run(run_id)["body"]["status"] == "active"
        assert turns == {"Sage": 3, "Remy": 1}

        recipient = host._control(run_id, "agent-critique")
        task = host.store.get("board_proposal", recipient["body"]["task_id"])
        cancelled = host.cancel_branch(LiveBranchCancelRequest(
            request_id="cancel-unavailable-critique", run_id=run_id,
            task_id=task["id"], expected_task_version=task["version"],
            reason="Stop the optional synthetic critique branch.",
        ))
        assert cancelled.status == "cancelled"
        assert host.store.get("mail_request", request_id)["body"]["status"] == (
            "recipient_unavailable_due_branch_cancel"
        )
        _drive_until(host, run_id, lambda: host._run(run_id)["body"]["status"] == "completed")
        assert observed["unavailable_input"] is True
        assert turns == {"Sage": 4, "Remy": 1}
        assert host._control(run_id, "agent-synthesis")["body"]["phase"] == "done"
        assert host._control(run_id, "agent-data-management")["body"]["report_ref"] == data_report
        assert host._control(run_id, "agent-critique")["body"]["phase"] == "cancelled"
        assert host._run(run_id)["body"]["source_refs"] == original_source_refs
        requester = host._control(run_id, "agent-synthesis")
        assert host.store.get(**requester["body"]["report_ref"])["body"]["body"] == (
            "The independent data check completed; critique was unavailable."
        )
        assert len(host.store.records("mission_scope_disposition")) == 1
        assert len(host.store.records("mail_answer")) == 0
        assert len(host.store.records("mission_wait_decision")) == 1
        assert host.store.get("mail_request", request_id)["body"]["requester_wake_ref"]
        final_waits = inspect_waits(host.store, requester["body"]["mission_id"])
        assert not any(item["control_ref"]["id"] == requester["id"]
                       for group in ("normal_waits", "stranded_waits", "stale_waits")
                       for item in final_waits[group])
    finally:
        host.close()
