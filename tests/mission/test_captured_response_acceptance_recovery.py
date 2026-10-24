"""A saved, charged model reply can finish local acceptance after callback loss."""

from __future__ import annotations

import socket
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import service, tool


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Captured recovery opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def _drive_until(host, run_id, predicate, *, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(
        f"Saved model state was not reached: "
        f"{[(item['id'], item['body'].get('phase')) for item in host.store.records('executor_work')]} "
        f"{[(item['id'], item['body'].get('waiting_reason')) for item in host.store.records('mission_task_control')]}"
    )


def _unaccepted(tmp_path, monkeypatch, *, evidence_text=None):
    calls = []

    def transport(_request, *, timeout):
        calls.append(timeout)
        return tool("request_capability", {"tool_id": "read_plan"}, len(calls))

    host = service(tmp_path, monkeypatch, transport)
    original = host.executor._accept

    def interrupted(*_args, **_kwargs):
        raise RuntimeError("Synthetic callback acceptance interrupted")

    host.executor._accept = interrupted
    host.model_recovery.accept_captured_response = lambda **_kwargs: {"status": "not_eligible"}
    started = host.start(
        LiveStartRequest(
            request_id="saved-reply-acceptance",
            question="Examine a synthetic question",
            evidence_text=evidence_text or "",
            agent_ids=["agent-synthesis"],
        )
    )
    _drive_until(
        host,
        started.run_id,
        lambda: (
            any(
                item["body"].get("phase") == "result_unaccepted"
                for item in host.store.records("executor_work")
            )
            and all(
                item["current_claim"] is None
                for item in host.board.open_work(host._run(started.run_id)["body"]["mission_id"])
            )
        ),
    )
    host.executor._accept = original
    control = host._control(started.run_id, "agent-synthesis")
    work = host._work_for(control)
    spec = host.executor._spec_from(work)
    cap_id = host.executor._capacity_id(spec)
    response = host.store.get(
        "model_response_receipt",
        f"model-response:model-intent:{spec.call_id}:{spec.call_version}",
    )
    assert response is not None
    assert host.store.get("capacity_operation", cap_id)["body"]["phase"] == "known_unaccepted"
    host.close()
    return started.run_id, spec, cap_id, ref(response), calls, transport


def test_reopen_accepts_exact_captured_reply_once(tmp_path, monkeypatch):
    run_id, spec, cap_id, response_ref, calls, transport = _unaccepted(tmp_path, monkeypatch)
    host = service(tmp_path, monkeypatch, transport)
    try:
        before = host.store.get("capacity_operation", cap_id)
        _drive_until(
            host,
            run_id,
            lambda: (
                host.store.get("executor_work", host.executor._work_record_id(spec))["body"][
                    "phase"
                ]
                == "completed"
            ),
        )
        work = host.store.get("executor_work", host.executor._work_record_id(spec))
        artifact = host.store.get(**work["body"]["result_ref"])
        assert response_ref in artifact["body"]["output_refs"]
        assert len(calls) == 1
        after = host.store.get("capacity_operation", cap_id)
        assert after == before  # No duplicate quota settlement.
        assert len(host.store.records("executor_artifact")) == 1
        assert len(host.store.records("board_outcome")) == 1
        original_artifact_ref = work["body"]["result_ref"]
        host._drive_run(host._run(run_id))
        assert len(calls) == 1
        assert (
            host.store.get("executor_work", work["id"])["body"]["result_ref"]
            == original_artifact_ref
        )
    finally:
        host.close()


def test_revoked_source_keeps_captured_response_unaccepted(tmp_path, monkeypatch):
    run_id, spec, _cap_id, _response_ref, calls, transport = _unaccepted(
        tmp_path,
        monkeypatch,
        evidence_text="Synthetic source paragraph for permission testing.",
    )
    host = service(tmp_path, monkeypatch, transport)
    try:
        assert spec.input_refs
        pointer = spec.input_refs[0]
        use = host.store.resolve("source_use.copy", f"{pointer['id']}:{pointer['version']}")
        assert use is not None

        def revoke(tx):
            current = tx.get("source_use", use["id"])
            return ref(
                tx.put(
                    "source_use",
                    use["id"],
                    {
                        **current["body"],
                        "allowed_uses": ["storage"],
                        "policy_status": "revoked",
                    },
                    expected_version=current["version"],
                )
            )

        host.store.transact("revoke-synthetic-source", {}, revoke, mission_id=spec.mission_id)
        host._drive_run(host._run(run_id))
        assert len(calls) == 1
        assert not host.store.records("executor_artifact")
        assert (
            host.store.get("executor_work", host.executor._work_record_id(spec))["body"]["phase"]
            == "result_unaccepted"
        )
        assert "source-use" in host._control(run_id, "agent-synthesis")["body"]["waiting_reason"]
    finally:
        host.close()


@pytest.mark.parametrize("change", ["cancel_run", "revise_task", "lose_capture"])
def test_ineligible_saved_work_cannot_be_accepted(tmp_path, monkeypatch, change):
    run_id, spec, cap_id, _response_ref, calls, transport = _unaccepted(
        tmp_path,
        monkeypatch,
    )
    host = service(tmp_path, monkeypatch, transport)
    try:

        def change_state(tx):
            if change == "cancel_run":
                current = tx.get("mission_run", run_id)
                tx.put(
                    "mission_run",
                    run_id,
                    {**current["body"], "status": "cancelled"},
                    expected_version=current["version"],
                )
            elif change == "revise_task":
                current = tx.get("board_proposal", spec.proposal_ref["id"])
                tx.put(
                    "board_proposal",
                    current["id"],
                    {**current["body"], "next_action": "Revised synthetic task"},
                    expected_version=current["version"],
                )
            else:
                marker_id = f"model-dispatch:model-intent:{spec.call_id}:{spec.call_version}"
                current = tx.get("model_dispatch_attempt", marker_id)
                tx.put(
                    "model_dispatch_attempt",
                    marker_id,
                    {**current["body"], "status": "attempted_external_effect", "receipt_ref": None},
                    expected_version=current["version"],
                )
            return {"changed": change}

        host.store.transact(
            f"synthetic-ineligible-{change}", {}, change_state, mission_id=spec.mission_id
        )
        control = host._control(run_id, "agent-synthesis")
        outcome = host.model_recovery.accept_captured_response(
            control_ref=ref(control),
            work_ref=ref(host._work_for(control)),
            validate_current_tx=lambda tx: host._validate_captured_model_tx(tx, spec),
        )
        assert outcome["status"] == "not_eligible"
        assert (
            host.store.get("executor_work", host.executor._work_record_id(spec))["body"]["phase"]
            == "result_unaccepted"
        )
        assert host.store.get("capacity_operation", cap_id)["body"]["phase"] == "known_unaccepted"
        assert not host.store.records("executor_artifact")
        assert len(calls) == 1
    finally:
        host.close()


def test_unknown_dispatched_response_stays_reserved_without_capture(tmp_path, monkeypatch):
    calls = []

    def transport(*_args, **_kwargs):
        calls.append("provider")
        raise AssertionError("Unknown-response test called the provider")

    host = service(tmp_path, monkeypatch, transport)
    try:
        started = host.start(
            LiveStartRequest(
                request_id="unknown-response-held",
                question="Synthetic research question",
                agent_ids=["agent-synthesis"],
            )
        )
        control = host._control(started.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        control = host._control(started.run_id, "agent-synthesis")
        work = host._work_for(control)
        spec = host.executor._spec_from(work)
        cap_id = host.executor._capacity_id(spec)
        assert cap_id in host.executor._prepare(spec.mission_id)
        admitted = host.capacity.admit(
            host.executor.owner,
            "model",
            mission_id=spec.mission_id,
            eligible_operation_ids=frozenset({cap_id}),
            lease_seconds=host.executor.lease_seconds,
        )
        assert admitted is not None
        host.capacity.dispatched(cap_id, host.executor.owner, admitted["generation"])
        intent = host.store.get(
            "model_request_intent",
            f"model-intent:{spec.call_id}:{spec.call_version}",
        )

        def mark(tx):
            tx.put(
                "model_dispatch_attempt",
                f"model-dispatch:{intent['id']}",
                {
                    "schema": "ora.model_dispatch_attempt.v1",
                    "mission_id": spec.mission_id,
                    "intent_ref": ref(intent),
                    "call_ref": control["body"]["call_ref"],
                    "request_sha256": intent["body"]["request_sha256"],
                    "status": "attempted_external_effect",
                },
            )
            return {"marked": True}

        host.store.transact(
            "synthetic-unknown-model-dispatch", {}, mark, mission_id=spec.mission_id
        )
        host.capacity.timeout(cap_id, host.executor.owner, admitted["generation"])
        outcome = host.model_recovery.accept_captured_response(
            control_ref=ref(control),
            work_ref=ref(work),
            validate_current_tx=lambda tx: host._validate_captured_model_tx(tx, spec),
        )
        assert outcome["status"] == "not_eligible"
        capacity = host.store.get("capacity_operation", cap_id)["body"]
        assert capacity["phase"] == "uncertain" and capacity["reservation"] is True
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "pending"
        assert not host.store.records("executor_artifact")
        assert not calls
    finally:
        host.close()
