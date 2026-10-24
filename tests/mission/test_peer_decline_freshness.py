"""A saved peer decline makes only its requester's unsubmitted input stale."""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from ora.mission.peer_freshness import changed_peer_input
from ora.state import Transaction
from tests.mission.test_peer_queued_freshness import _spec, _start_queued
from tests.mission.test_service import service


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("socket connection")
        raise AssertionError("Offline decline freshness attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    yield
    assert attempts == []


def _decline_request(host, run_id: str, label: str) -> dict:
    sent, _ = host._peer_request(_spec(host, run_id, "agent-synthesis", label), {
        "target_instance_id": "agent-critique",
        "question": "Can you check this synthetic finding independently?",
    })
    assert sent["status"] == "request_saved"
    mission_id = host._control(run_id, "agent-synthesis")["body"]["mission_id"]
    host._deliver_pending(mission_id)
    current = host.store.get("mail_request", sent["request_id"])
    declined = host.mailbox.decline(
        f"decline-{label}", mission_id=mission_id,
        request_id=current["id"],
        recipient_id=host._control(run_id, "agent-critique")["body"]["actor_id"],
        reason="The synthetic comparator is unavailable",
        expected_version=current["version"],
    )
    assert declined["status"] == "accepted"
    return declined


def _prepared_packet(host, control: dict) -> dict:
    call_ref = control["body"]["call_ref"]
    intent = host.store.get(
        "model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}"
    )
    return json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))


def test_queued_decline_refreshes_only_originating_requester(tmp_path, monkeypatch) -> None:
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: pytest.fail(
        "Queued stale call must not reach model transport"
    ))
    try:
        run, pending, intent, work, spec, cap_id = _start_queued(
            host, "queued-decline-freshness",
        )
        mission_id = pending["body"]["mission_id"]
        sent, _ = host._peer_request(
            _spec(host, run.run_id, "agent-synthesis", "decline-queued-request"),
            {"target_instance_id": "agent-critique",
             "question": "Can you check this synthetic finding independently?"},
        )
        assert sent["status"] == "request_saved"
        host._deliver_pending(mission_id)
        assert host._schedule_model(host._control(run.run_id, "agent-critique"))
        other = host._control(run.run_id, "agent-critique")
        other_work = host._work_for(other)
        current = host.store.get("mail_request", sent["request_id"])
        declined = host.mailbox.decline(
            "decline-queued-request", mission_id=mission_id,
            request_id=current["id"],
            recipient_id=other["body"]["actor_id"],
            reason="The synthetic comparator is unavailable",
            expected_version=current["version"],
        )
        assert declined["status"] == "accepted"
        inclusion = host.store.get("context_inclusion", f"context:{intent['id']}", 1)
        db = host.store._connect()
        try:
            reader = Transaction(host.store, db)
            assert changed_peer_input(
                reader, db, mission_id=mission_id, run_id=run.run_id,
                task_id=pending["body"]["task_id"], actor_id=pending["body"]["actor_id"],
                event_through=inclusion["body"]["event_through"],
            )["request_ref"] == declined["request_ref"]
            assert changed_peer_input(
                reader, db, mission_id=mission_id, run_id=run.run_id,
                task_id="unrelated-synthetic-task", actor_id=pending["body"]["actor_id"],
                event_through=inclusion["body"]["event_through"],
            ) is None
        finally:
            db.close()
        assert host._supersede_queued_peer_input(other) is False
        assert host.store.get("executor_work", other_work["id"])["body"]["phase"] == "pending"
        other_run, other_pending, _, other_run_work, _, _ = _start_queued(
            host, "unrelated-run-decline-freshness",
        )
        assert other_run.run_id != run.run_id
        assert host._supersede_queued_peer_input(other_pending) is False
        assert host.store.get("executor_work", other_run_work["id"])["body"][
            "phase"
        ] == "pending"
        assert host._supersede_queued_peer_input(pending)
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "superseded"
        assert host.store.get("agent_attempt", spec.attempt_id)["body"]["status"] == "superseded"
        assert host.store.get("capacity_operation", cap_id)["body"]["phase"] == "canceled"
        assert host.store.get("model_request_intent", intent["id"]) == intent
        assert host.store.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}") is None
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        successor = host._control(run.run_id, "agent-synthesis")
        assert successor["body"]["call_ref"] != pending["body"]["call_ref"]
        index = json.loads(_prepared_packet(host, successor)["messages"][-1]["content"])
        disposition = next(item for item in index["request_dispositions"]
                           if item["ref"]["id"] == current["id"])
        assert disposition["status"] == "declined"
        assert disposition["answer_refs"] == []
    finally:
        host.close()


def test_decline_at_dispatch_boundary_stops_old_request_without_provider_call(
    tmp_path, monkeypatch,
) -> None:
    calls = []

    def no_transport(request, *, timeout):
        calls.append(json.loads(request.data))
        raise AssertionError("Declined peer input must stop before provider transport")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run, pending, intent, work, spec, cap_id = _start_queued(
            host, "dispatch-decline-freshness",
        )
        original = host.session.invoke_capture
        declined = {}

        def decline_after_claim(intent_ref, **kwargs):
            declined.update(_decline_request(host, run.run_id, "dispatch-race"))
            return original(intent_ref, **kwargs)

        monkeypatch.setattr(host.session, "invoke_capture", decline_after_claim)
        asyncio.run(host.executor.run_ready(pending["body"]["mission_id"]))
        assert declined["request_ref"]["kind"] == "mail_request"
        capacity = host.store.get("capacity_operation", cap_id)["body"]
        assert capacity["phase"] == "failed"
        assert capacity["attempts"][-1]["status"] == "terminal_no_effect"
        assert capacity["attempts"][-1]["charged_tokens"] == 0
        assert host.store.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}") is None
        assert host.store.get("model_response_receipt", f"model-response:{intent['id']}") is None
        assert calls == []
    finally:
        host.close()

    resumed = service(tmp_path, monkeypatch, no_transport)
    try:
        control = resumed._control(run.run_id, "agent-synthesis")
        assert resumed._supersede_claimed_peer_input(control)
        assert resumed.store.get("executor_work", work["id"])["body"]["phase"] == "superseded"
        assert resumed.store.get("agent_attempt", spec.attempt_id)["body"]["status"] == "superseded"
        assert resumed._schedule_model(resumed._control(run.run_id, "agent-synthesis"))
        successor = resumed._control(run.run_id, "agent-synthesis")
        assert successor["body"]["call_ref"] != pending["body"]["call_ref"]
        index = json.loads(_prepared_packet(resumed, successor)["messages"][-1]["content"])
        assert any(item["ref"]["id"] == declined["request_ref"]["id"]
                   and item["status"] == "declined"
                   for item in index["request_dispositions"])
        assert calls == []
    finally:
        resumed.close()
