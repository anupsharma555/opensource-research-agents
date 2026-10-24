"""Private offline review probe for a real saved tool effect during branch cancellation."""

from __future__ import annotations

import asyncio
import json
import socket
import threading

import pytest

from ora.contracts.models import LiveBranchCancelRequest, LiveStartRequest
from tests.mission.test_service import service, tool


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("socket connection")
        raise AssertionError("Offline cancellation probe attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    yield
    assert attempts == []


def prepare_note_work(host, *, request_id: str):
    run = host.start(LiveStartRequest(
        request_id=request_id,
        question="Save one synthetic checkpoint",
        agent_ids=["agent-synthesis"],
    ))
    mission_id = host._run(run.run_id)["body"]["mission_id"]
    for expected_tool in ("request_capability", "write_note"):
        control = host._control(run.run_id, "agent-synthesis")
        assert control["body"]["phase"] == "ready_model"
        assert host._schedule_model(control)
        model_outcome = asyncio.run(host.executor.run_ready(mission_id))
        assert "completed" in model_outcome["work"].values(), model_outcome
        control = host._control(run.run_id, "agent-synthesis")
        assert host._advance(control)
        control = host._control(run.run_id, "agent-synthesis")
        assert control["body"]["phase"] == "ready_tool", (
            control["body"].get("waiting_reason"), model_outcome,
        )
        assert control["body"]["tool_call"]["name"] == expected_tool
        if expected_tool == "request_capability":
            assert host._schedule_tool(control)
            local_outcome = asyncio.run(host.executor.run_ready(mission_id))
            assert "completed" in local_outcome["work"].values(), local_outcome
            assert host._advance(host._control(run.run_id, "agent-synthesis"))
            continue
    assert control["body"]["tool_call"]["name"] == "write_note"
    assert host._schedule_tool(control)
    control = host._control(run.run_id, "agent-synthesis")
    work = host._work_for(control)
    assert work["body"]["spec"]["handler_id"] == "mission-tool"
    assert work["body"]["phase"] == "pending"
    return run, control, work, mission_id


def cancel(host, run, control, *, request_id: str):
    proposal = host.store.get("board_proposal", control["body"]["task_id"])
    return host.cancel_branch(LiveBranchCancelRequest(
        request_id=request_id,
        run_id=run.run_id,
        task_id=proposal["id"],
        expected_task_version=proposal["version"],
        reason="Stop this exact synthetic task",
    ))


def test_real_note_saved_then_cancelled_before_executor_acceptance(tmp_path, monkeypatch):
    model_calls = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        model_calls.append(packet)
        if len(model_calls) == 1:
            return tool("request_capability", {"tool_id": "write_note"}, 1)
        assert "write_note" in {item["function"]["name"] for item in packet["tools"]}
        return tool("write_note", {
            "note_id": "synthetic-cancelled-note",
            "category": "checkpoint",
            "text": "The local side effect happened before cancellation.",
            "expected_version": 0,
        }, 2)

    host = service(tmp_path, monkeypatch, transport)
    release = threading.Event()
    returned = threading.Event()
    invoked = []
    failures = []
    thread = None
    try:
        run, control, work, mission_id = prepare_note_work(host, request_id="held-real-note")
        original = host.executor.handlers[("mission-tool", "1")]

        def held(spec):
            invoked.append(spec.work_id)
            result = original(spec)
            assert host.store.get("working_note", "synthetic-cancelled-note") is not None
            returned.set()
            assert release.wait(10), "Held tool was not released"
            return result

        host.executor.handlers[("mission-tool", "1")] = held

        def execute():
            try:
                asyncio.run(host.executor.run_ready(mission_id))
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        assert returned.wait(10), "Real write_note effect did not reach the hold"
        note = host.store.get("working_note", "synthetic-cancelled-note")
        assert note is not None
        receipt = cancel(host, run, control, request_id="cancel-held-note")
        assert receipt.status == "cancellation_requested"
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "cancelled"
        assert host.store.get("board_proposal", control["body"]["task_id"])["body"]["status"] == "cancelled"
    finally:
        release.set()
        if thread is not None:
            thread.join(12)
        host.close()

    assert thread is not None and not thread.is_alive()
    assert not failures, failures
    assert invoked == [work["body"]["spec"]["work_id"]]
    assert len(model_calls) == 2

    reopened = service(tmp_path, monkeypatch, transport)
    try:
        note_after = reopened.store.get("working_note", "synthetic-cancelled-note")
        assert note_after == note
        assert len(reopened.store.records("working_note")) == 1
        observations = [
            item for item in reopened.store.records("executor_unaccepted_result")
            if item["body"]["work_id"] == invoked[0]
        ]
        assert len(observations) == 1
        assert observations[0]["body"]["reason_code"] == "board_rejected"
        assert observations[0]["body"]["capacity_reconciled"] is True
        assert reopened.store.get("executor_work", work["id"])["body"]["phase"] == "result_unaccepted"
        assert not any(
            item["body"].get("proposal_ref", {}).get("id") == control["body"]["task_id"]
            and item["body"].get("completes_task")
            for item in reopened.store.records("board_outcome")
        )
        before = {kind: [(item["id"], item["version"]) for item in reopened.store.records(kind)]
                  for kind in ("working_note", "executor_work", "executor_unaccepted_result",
                               "capacity_operation", "model_request_intent")}
        for _ in range(4):
            assert reopened._drive_run(reopened._run(run.run_id)) == (False, None)
        after = {kind: [(item["id"], item["version"]) for item in reopened.store.records(kind)]
                 for kind in before}
        assert after == before
        assert len(model_calls) == 2
    finally:
        reopened.close()


def test_cancel_before_real_note_handler_prevents_local_side_effect(tmp_path, monkeypatch):
    calls = []

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        if len(calls) == 1:
            return tool("request_capability", {"tool_id": "write_note"}, 1)
        return tool("write_note", {
            "note_id": "synthetic-never-written",
            "category": "checkpoint",
            "text": "This note should never be saved.",
            "expected_version": 0,
        }, 2)

    host = service(tmp_path, monkeypatch, transport)
    invoked = []
    try:
        run, control, work, mission_id = prepare_note_work(host, request_id="cancel-before-real-note")
        original = host.executor.handlers[("mission-tool", "1")]

        def observed(spec):
            invoked.append(spec.work_id)
            return original(spec)

        host.executor.handlers[("mission-tool", "1")] = observed
        receipt = cancel(host, run, control, request_id="cancel-before-note-handler")
        assert receipt.status == "cancelled"
        asyncio.run(host.executor.run_ready(mission_id))
        assert invoked == []
        assert host.store.get("working_note", "synthetic-never-written") is None
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "cancelled"
        assert len(calls) == 2
    finally:
        host.close()
