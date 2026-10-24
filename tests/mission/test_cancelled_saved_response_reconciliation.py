"""A cancelled task still accounts for an exact captured model response after reopen."""

from __future__ import annotations

import asyncio
import socket
import threading

import pytest

from ora.contracts.models import LiveBranchCancelRequest, LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import answer, service


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("socket")
        raise AssertionError("Recovery made a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    yield
    assert not attempts


def _saved_cancelled_response(
    tmp_path, monkeypatch, *, mismatch=None, marker_status="captured", save_receipt=True,
):
    calls = []

    def transport(*_args, **_kwargs):
        calls.append("provider")
        raise AssertionError("Recovery recalled the model")

    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(
        request_id="cancelled-saved-response", question="Synthetic cancelled research task",
        agent_ids=["agent-synthesis"], required_agent_ids=["agent-synthesis"],
    ))
    control = host._control(run.run_id, "agent-synthesis")
    assert host._schedule_model(control)
    work = host._work_for(host._control(run.run_id, "agent-synthesis"))
    spec = host.executor._spec_from(work)
    cap_id = host.executor._capacity_id(spec)
    assert cap_id in host.executor._prepare(spec.mission_id)
    admitted = host.capacity.admit(
        host.executor.owner, "model", mission_id=spec.mission_id,
        eligible_operation_ids=frozenset({cap_id}), lease_seconds=host.executor.lease_seconds,
    )
    assert admitted is not None
    host.capacity.dispatched(cap_id, host.executor.owner, admitted["generation"])
    intent = host.store.get("model_request_intent", f"model-intent:{spec.call_id}:{spec.call_version}")
    assert intent is not None
    call_ref = {"kind": "agent_call", "id": spec.call_id, "version": spec.call_version}
    intent_body = intent["body"]
    receipt_body = {
        "schema": "ora.model_response_receipt.v1",
        "mission_id": spec.mission_id, "task_id": spec.proposal_ref["id"],
        "attempt_id": spec.attempt_id, "call_ref": call_ref,
        "configuration_ref": spec.configuration_ref, "intent_ref": ref(intent),
        "request_sha256": intent_body["request_sha256"],
        "credential_profile": intent_body["credential_profile"],
        "quota_domain": intent_body["quota_domain"],
        "usage": {"total_tokens": 11}, "status": "complete",
        "response_sha256": "c" * 64, "returned_model": "fixture-model",
        "finish_reason": "stop", "synthetic": True,
    }
    if mismatch:
        receipt_body[mismatch] = "mismatch"

    def save(tx):
        receipt = tx.put(
            "model_response_receipt", f"model-response:{intent['id']}", receipt_body,
        ) if save_receipt else None
        tx.put("model_dispatch_attempt", f"model-dispatch:{intent['id']}", {
            "schema": "ora.model_dispatch_attempt.v1", "mission_id": spec.mission_id,
            "intent_ref": ref(intent), "call_ref": call_ref,
            "request_sha256": intent_body["request_sha256"],
            "credential_profile": intent_body["credential_profile"],
            "quota_domain": intent_body["quota_domain"], "status": marker_status,
            "receipt_ref": ref(receipt) if receipt else None,
        })
        return {"receipt_ref": ref(receipt) if receipt else None}

    host.store.transact("save-synthetic-cancelled-response", {}, save, mission_id=spec.mission_id)
    host.capacity.cancel(cap_id, reason="branch_cancel")
    saved_run = host._run(run.run_id)
    host._save("mission_run", run.run_id, {**saved_run["body"], "status": "cancelled"},
               mission_id=spec.mission_id)
    host.close()
    return run.run_id, spec, cap_id, calls, transport


def test_reopen_accounts_exact_saved_response_once_without_accepting_work(tmp_path, monkeypatch):
    run_id, spec, cap_id, calls, transport = _saved_cancelled_response(tmp_path, monkeypatch)
    reopened = service(tmp_path, monkeypatch, transport)
    try:
        before_head = reopened.store.get("mission_snapshot_head", run_id)
        assert reopened._drive_run(reopened._run(run_id)) == (True, None)
        after_head = reopened.store.get("mission_snapshot_head", run_id)
        assert after_head["version"] > before_head["version"]
        assert after_head["body"]["snapshot_ref"] != before_head["body"]["snapshot_ref"]
        for _ in range(2):
            assert reopened._drive_run(reopened._run(run_id)) == (False, None)
        assert reopened.store.get("mission_snapshot_head", run_id) == after_head
        cap = reopened.capacity.get(cap_id)
        assert cap["phase"] == "known_unaccepted"
        assert cap["reservation"] is False
        assert cap["attempts"][-1]["charged_tokens"] == 11
        assert cap["attempts"][-1]["usage_known"] is True
        observations = reopened.store.records("executor_unaccepted_result")
        assert len(observations) == 1
        assert observations[0]["body"]["capacity_reconciled"] is True
        assert reopened.store.get("executor_work", reopened.executor._work_record_id(spec))["body"]["phase"] == "result_unaccepted"
        assert reopened.store.records("executor_artifact") == []
        assert reopened.store.records("executor_receipt") == []
        assert calls == []
    finally:
        reopened.close()


@pytest.mark.parametrize("mismatch", ["request_sha256", "attempt_id", "credential_profile"])
def test_mismatched_saved_response_stays_uncertain(tmp_path, monkeypatch, mismatch):
    run_id, spec, cap_id, calls, transport = _saved_cancelled_response(
        tmp_path, monkeypatch, mismatch=mismatch,
    )
    reopened = service(tmp_path, monkeypatch, transport)
    try:
        reopened._drive_run(reopened._run(run_id))
        cap = reopened.capacity.get(cap_id)
        assert cap["phase"] in {"dispatched", "uncertain"}
        assert cap["reservation"] is True
        assert cap["attempts"][-1]["usage_known"] is False
        assert reopened.store.records("executor_unaccepted_result") == []
        assert calls == []
    finally:
        reopened.close()


def test_uncaptured_dispatch_marker_stays_uncertain(tmp_path, monkeypatch):
    run_id, _, cap_id, calls, transport = _saved_cancelled_response(
        tmp_path, monkeypatch, marker_status="attempted_external_effect",
    )
    reopened = service(tmp_path, monkeypatch, transport)
    try:
        reopened._drive_run(reopened._run(run_id))
        assert reopened.capacity.get(cap_id)["reservation"] is True
        assert reopened.store.records("executor_unaccepted_result") == []
        assert calls == []
    finally:
        reopened.close()


def test_missing_saved_response_stays_uncertain(tmp_path, monkeypatch):
    run_id, _, cap_id, calls, transport = _saved_cancelled_response(
        tmp_path, monkeypatch, marker_status="attempted_external_effect",
        save_receipt=False,
    )
    reopened = service(tmp_path, monkeypatch, transport)
    try:
        reopened._drive_run(reopened._run(run_id))
        assert reopened.capacity.get(cap_id)["reservation"] is True
        assert reopened.store.records("executor_unaccepted_result") == []
        assert calls == []
    finally:
        reopened.close()


def test_prior_unaccepted_observation_is_reconciled_without_duplicate(tmp_path, monkeypatch):
    run_id, spec, cap_id, calls, transport = _saved_cancelled_response(tmp_path, monkeypatch)
    interim = service(tmp_path, monkeypatch, transport)
    try:
        work = interim.store.get("executor_work", interim.executor._work_record_id(spec))
        receipt = interim.store.get(
            "model_response_receipt",
            f"model-response:model-intent:{spec.call_id}:{spec.call_version}",
        )

        def save(tx):
            observation = tx.put("executor_unaccepted_result", "prior-cancelled-observation", {
                "mission_id": spec.mission_id, "work_id": spec.work_id,
                "observed_receipt_ref": ref(receipt), "external_effect": "known",
                "capacity_reconciled": False, "used_tokens": 11,
            })
            tx.put("executor_work", work["id"], {
                **work["body"], "phase": "result_unaccepted",
                "result_ref": ref(observation),
            }, expected_version=work["version"])
            return {"observation_ref": ref(observation)}

        interim.store.transact("save-prior-unaccounted-observation", {}, save,
                               mission_id=spec.mission_id)
    finally:
        interim.close()

    reopened = service(tmp_path, monkeypatch, transport)
    try:
        for _ in range(3):
            reopened._drive_run(reopened._run(run_id))
        observations = reopened.store.records("executor_unaccepted_result")
        assert len(observations) == 1
        assert observations[0]["id"] == "prior-cancelled-observation"
        assert observations[0]["body"]["capacity_reconciled"] is True
        assert reopened.capacity.get(cap_id)["attempts"][0]["charged_tokens"] == 11
        assert calls == []
    finally:
        reopened.close()


def test_real_offline_capture_reconciles_after_cancelled_callback_is_lost(
    tmp_path, monkeypatch,
):
    started = threading.Event()
    release = threading.Event()
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        started.set()
        assert release.wait(5)
        return answer("Late synthetic answer.", 1)

    host = service(tmp_path, monkeypatch, transport)
    worker = None
    try:
        run = host.start(LiveStartRequest(
            request_id="cancelled-real-capture",
            question="Inspect a synthetic research task",
            agent_ids=["agent-synthesis"], required_agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)

        def interrupted_observation(*_args, **_kwargs):
            raise RuntimeError("Synthetic interruption after response capture")

        monkeypatch.setattr(host.executor, "_record_unaccepted", interrupted_observation)
        worker = threading.Thread(
            target=lambda: asyncio.run(host.executor.run_ready(control["body"]["mission_id"])),
            daemon=True,
        )
        worker.start()
        assert started.wait(5)
        proposal = host.store.get("board_proposal", control["body"]["task_id"])
        cancelled = host.cancel_branch(LiveBranchCancelRequest(
            request_id="cancel-real-capture", run_id=run.run_id,
            task_id=proposal["id"], expected_task_version=proposal["version"],
            reason="Stop this synthetic branch",
        ))
        assert cancelled.status == "cancellation_requested"
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert len(host.store.records("model_response_receipt")) == 1
        assert host.store.records("executor_unaccepted_result") == []
    finally:
        release.set()
        if worker is not None:
            worker.join(5)
        host.close()

    reopened = service(
        tmp_path, monkeypatch,
        lambda *_args, **_kwargs: pytest.fail("Recovery recalled the model"),
    )
    try:
        for _ in range(3):
            reopened._drive_run(reopened._run(run.run_id))
        observations = reopened.store.records("executor_unaccepted_result")
        assert len(observations) == 1
        assert observations[0]["body"]["used_tokens"] == 11
        assert observations[0]["body"]["capacity_reconciled"] is True
        work = reopened.executor._work(control["body"]["mission_id"])[0]
        cap = reopened.capacity.get(reopened.executor._capacity_id(
            reopened.executor._spec_from(work),
        ))
        assert cap["phase"] == "known_unaccepted"
        assert cap["attempts"][0]["charged_tokens"] == 11
        assert reopened.store.records("executor_artifact") == []
        assert len(calls) == 1
    finally:
        reopened.close()
