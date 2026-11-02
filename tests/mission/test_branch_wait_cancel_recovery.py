"""Offline proof of sibling progress and operation-level cancellation boundaries."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import answer, serialized_peer_name, service


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fence accidental connections for each case, including host cleanup."""
    attempts: list[str] = []

    def forbidden(*_args, **_kwargs):
        attempts.append("socket connection")
        raise AssertionError("Offline branch recovery attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    yield
    assert attempts == []


def _until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Expected saved mission progress did not occur")


def test_accepted_fast_report_survives_held_sibling_callback_and_host_reopen(
    tmp_path, monkeypatch,
):
    slow_started = threading.Event()
    release_slow = threading.Event()
    calls: list[str] = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        role = "slow" if serialized_peer_name(body) == "Mxra" else "fast"
        calls.append(role)
        if role == "slow":
            slow_started.set()
            assert release_slow.wait(8), "Controlled sibling was not released"
        return answer(f"Synthetic {role} contribution.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    reopened = None
    try:
        request = LiveStartRequest(
            request_id="held-independent-branch",
            question="Summarize two independent synthetic contributions",
            evidence_text="Synthetic source passage for independent review.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        )
        run = host.start(request)
        original = host._run(run.run_id)["body"]
        assert original["source_refs"]
        model_scopes = set()
        for agent_id in request.agent_ids:
            assert host._schedule_model(host._control(run.run_id, agent_id))
            work = host._work_for(host._control(run.run_id, agent_id))
            model_scopes.update(host.executor._spec_from(work).quota_scopes)
        assert len(model_scopes) == 1
        host.capacity.observe_headers(
            model_scopes.pop(), remaining_requests=100, remaining_tokens=50000,
            token_limit=50000, reset_after=60,
            observation_id="known-synthetic-branch-headroom",
        )

        def fast_accepted() -> bool:
            host._drive_run(host._run(run.run_id))
            return host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "done"

        _until(fast_accepted)
        assert slow_started.is_set() and not release_slow.is_set()
        fast = host._control(run.run_id, "agent-synthesis")
        slow = host._control(run.run_id, "agent-critique")
        fast_report = fast["body"]["report_ref"]
        assert fast_report and host.store.get(**fast_report)
        assert host.store.get("board_proposal", fast["body"]["task_id"])["body"]["status"] == "completed"
        assert host.store.get("agent_attempt", host.store.get(**fast["body"]["call_ref"])["body"]["attempt_id"])["body"]["status"] == "completed"
        assert slow["body"]["phase"] != "done"
        assert host._run(run.run_id)["body"]["status"] == "active"

        release_slow.set()

        def all_done() -> bool:
            host._drive_run(host._run(run.run_id))
            return host._run(run.run_id)["body"]["status"] == "completed"

        _until(all_done)
        assert calls.count("fast") == calls.count("slow") == 1
        host.close()
        reopened = service(tmp_path, monkeypatch, transport)
        completed = reopened._run(run.run_id)
        assert completed["body"]["status"] == "completed"
        assert completed["body"]["source_refs"] == original["source_refs"]
        assert completed["body"]["profile"]["modelSettings"] == original["profile"]["modelSettings"]
        assert completed["body"]["root_task_id"] == original["root_task_id"]
        assert reopened._control(run.run_id, "agent-synthesis")["body"]["report_ref"] == fast_report
        before = [(item["id"], item["version"]) for item in reopened.store.records("executor_work")]
        for _ in range(4):
            reopened._drive_run(completed)
        assert [(item["id"], item["version"]) for item in reopened.store.records("executor_work")] == before
        assert calls.count("fast") == calls.count("slow") == 1
    finally:
        release_slow.set()
        if reopened is not None:
            reopened.close()
        else:
            host.close()


def test_fixture_cancelled_mission_stays_closed_after_late_mail_and_reopen(tmp_path, monkeypatch):
    calls: list[dict] = []

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        return answer("Independent synthetic result.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    cancelled_request = LiveStartRequest(
        request_id="fixture-cancelled-mission",
        question="Synthetic work later cancelled by the fixture",
        evidence_text="Synthetic cancelled-run source.",
        agent_ids=["agent-synthesis", "agent-critique"],
        required_agent_ids=["agent-synthesis", "agent-critique"],
    )
    try:
        cancelled = host.start(cancelled_request)
        saved = host._run(cancelled.run_id)
        assert saved["body"]["source_refs"]
        # This is a documented fixture injection, not a supported user-facing
        # whole-mission cancellation operation.
        host._save(
            "mission_run", saved["id"], {**saved["body"], "status": "cancelled"},
            mission_id=saved["body"]["mission_id"],
        )
        frozen = host._run(cancelled.run_id)
        controls = host._controls(cancelled.run_id)
    finally:
        host.close()

    reopened = service(tmp_path, monkeypatch, transport)
    try:
        duplicate = reopened.start(cancelled_request)
        assert duplicate.reused and duplicate.run_id == cancelled.run_id
        first = reopened._control(cancelled.run_id, "agent-synthesis")
        second = reopened._control(cancelled.run_id, "agent-critique")
        late = reopened.mailbox.send_request(
            "late-after-fixture-cancel", mission_id=frozen["body"]["mission_id"],
            request_id="late-after-fixture-cancel",
            message_id="late-after-fixture-cancel-message",
            requester_id=first["body"]["actor_id"],
            recipient_id=second["body"]["actor_id"],
            question="Check a late synthetic claim", expected_artifact="Synthetic review",
            cause_ref=first["body"]["proposal_ref"],
            payload={"text": "Check a late synthetic claim"},
        )
        assert late["status"] == "accepted"
        for _ in range(4):
            assert reopened._drive_run(frozen) == (False, None)
            assert reopened._activate_peer_obligations(frozen) is False
            assert reopened._maybe_complete(frozen) is False
        assert reopened._run(cancelled.run_id) == frozen
        assert reopened._controls(cancelled.run_id) == controls
        assert not reopened.store.records("model_request_intent")

        independent = reopened.start(LiveStartRequest(
            request_id="independent-after-fixture-cancel",
            question="Provide a separate synthetic contribution",
            agent_ids=["agent-synthesis"],
            required_agent_ids=["agent-synthesis"],
        ))

        def independent_done() -> bool:
            reopened._drive_run(reopened._run(independent.run_id))
            return reopened._run(independent.run_id)["body"]["status"] == "completed"

        _until(independent_done)
        assert len(calls) == 1
        independent_control = reopened._control(independent.run_id, "agent-synthesis")
        assert independent_control["body"]["report_ref"]
        assert reopened._run(cancelled.run_id) == frozen
        assert reopened._run(cancelled.run_id)["body"]["profile"]["modelSettings"] == frozen["body"]["profile"]["modelSettings"]
        assert reopened._run(cancelled.run_id)["body"]["source_refs"] == frozen["body"]["source_refs"]
        assert reopened._run(cancelled.run_id)["body"]["root_task_id"] == frozen["body"]["root_task_id"]
        intents = reopened.store.records("model_request_intent")
        assert len(intents) == 1
        intent = intents[0]["body"]
        independent_call = reopened.store.get(**independent_control["body"]["call_ref"])
        assert independent_call is not None
        assert intent["mission_id"] == independent_control["body"]["mission_id"]
        assert intent["task_id"] == independent_control["body"]["task_id"]
        assert intent["instance_id"] == independent_control["body"]["actor_id"]
        assert intent["call_ref"] == ref(independent_call)
        for cancelled_control in controls:
            assert intent["mission_id"] != cancelled_control["body"]["mission_id"]
            assert intent["task_id"] != cancelled_control["body"]["task_id"]
            assert intent["instance_id"] != cancelled_control["body"]["actor_id"]
        assert reopened.store.get(**ref(reopened._run(independent.run_id)))
    finally:
        reopened.close()


def test_cancelled_queued_capacity_operation_does_not_block_sibling_after_reopen(
    tmp_path, monkeypatch,
):
    calls: list[dict] = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        assert serialized_peer_name(body) == "Sage"
        calls.append(body)
        return answer("Synthetic independent result after operation cancellation.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    try:
        host.capacity.observe_headers(
            "groq-selected", remaining_requests=100, remaining_tokens=50000,
            token_limit=50000, reset_after=60,
            observation_id="known-cancel-sibling-headroom",
        )
        run = host.start(LiveStartRequest(
            request_id="cancel-queued-operation-with-sibling",
            question="Prepare separate synthetic contributions",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        cancelled_control = host._control(run.run_id, "agent-critique")
        assert host._schedule_model(cancelled_control)
        cancelled_work = host._work_for(host._control(run.run_id, "agent-critique"))
        cancelled_spec = host.executor._spec_from(cancelled_work)
        capacity_id = host.executor._capacity_id(cancelled_spec)
        # Prepare the real executor queue without starting dispatch, then use
        # its supported operation-level cancellation API. This cancels one
        # queued call, not the mission or the peer's whole research task.
        assert capacity_id in host.executor._prepare(cancelled_spec.mission_id)
        assert host.capacity.cancel(capacity_id, reason="branch_cancel")["phase"] == "canceled"

        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))

        def sibling_done() -> bool:
            host._drive_run(host._run(run.run_id))
            return host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "done"

        _until(sibling_done)
        sibling = host._control(run.run_id, "agent-synthesis")
        assert sibling["body"]["report_ref"]
        assert host.store.get("board_proposal", sibling["body"]["task_id"])["body"]["status"] == "completed"
        assert host.capacity.get(capacity_id)["phase"] == "canceled"
        assert host._work_for(host._control(run.run_id, "agent-critique"))["body"]["phase"] == "pending"
        assert len(calls) == 1
    finally:
        host.close()

    reopened = service(tmp_path, monkeypatch, transport)
    try:
        cancelled_capacity_head = reopened.store.get("capacity_operation", capacity_id)
        assert cancelled_capacity_head["body"]["phase"] == "canceled"
        assert reopened._control(run.run_id, "agent-synthesis")["body"]["report_ref"] == sibling["body"]["report_ref"]
        before = [
            (item["id"], item["version"])
            for item in reopened.store.records("model_dispatch_attempt")
        ]
        reopened._drive_run(reopened._run(run.run_id))
        stable_controls = reopened._controls(run.run_id)
        stable_cancelled_work = reopened.store.get("executor_work", cancelled_work["id"])
        assert stable_cancelled_work["body"]["phase"] == "pending"
        for _ in range(4):
            reopened._drive_run(reopened._run(run.run_id))
        assert reopened.store.get("capacity_operation", capacity_id) == cancelled_capacity_head
        assert reopened._controls(run.run_id) == stable_controls
        assert reopened.store.get("executor_work", cancelled_work["id"]) == stable_cancelled_work
        assert [
            (item["id"], item["version"])
            for item in reopened.store.records("model_dispatch_attempt")
        ] == before
        assert len(calls) == 1
        assert reopened._run(run.run_id)["body"]["status"] == "active"
    finally:
        reopened.close()
