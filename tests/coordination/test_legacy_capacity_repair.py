"""Bound an old queued model estimate by exact lineage and provider ceilings."""

from __future__ import annotations

import hashlib

import pytest

from ora.coordination.capacity import CapacityPolicy, CapacityScheduler, QuotaLimit
from ora.state.store import StateStore, canonical


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def legacy_work(tmp_path, *, request_size: int):
    clock = Clock()
    store = StateStore(tmp_path, clock=clock)
    scheduler = CapacityScheduler(
        store, CapacityPolicy(
            permits={"model": 1, "retrieval": 0, "compute": 0},
            quotas={"scope": QuotaLimit(60)},
        ),
    )
    raw = b"x" * request_size
    blob = store.put_blob(raw)
    work_id = "old-model-work"
    work_record_id = "executor-work:" + hashlib.sha256(
        canonical(["mission", work_id]).encode()
    ).hexdigest()
    capacity_id = "executor-cap:" + hashlib.sha256(
        canonical(["mission", work_id]).encode()
    ).hexdigest()

    def save_fixture(tx):
        call = tx.put("agent_call", "old-call", {
            "mission_id": "mission", "instance_id": "actor", "task_id": "task",
            "attempt_id": "attempt", "configuration_id": "configuration",
        })
        tx.put("executor_work", work_record_id, {
            "mission_id": "mission", "phase": "pending", "result_ref": None,
            "spec": {
                "kind": "model", "mission_id": "mission", "run_id": "run",
                "work_id": work_id, "call_id": call["id"],
                "call_version": call["version"], "attempt_id": "attempt",
                "actor_id": "actor", "quota_scopes": ["scope"],
                "proposal_ref": {"kind": "board_proposal", "id": "task", "version": 1},
                "configuration_ref": {
                    "kind": "agent_configuration", "id": "configuration", "version": 1,
                },
            },
        })
        tx.put("model_request_intent", "model-intent:old-call:1", {
            "call_ref": {"kind": "agent_call", "id": call["id"], "version": call["version"]},
            "mission_id": "mission", "task_id": "task", "attempt_id": "attempt",
            "parent_call_ref": None,
            "request_sha256": hashlib.sha256(raw).hexdigest(), "blob": blob,
        })
        return {}

    store.transact("old-model-fixture", {}, save_fixture)
    scheduler.enqueue(
        capacity_id, task_id="task", lane="actor", kind="model",
        quota_scopes=("scope",), estimated_tokens=8192,
        call_id="old-call", mission_id="mission", run_id="run",
        attempt_id="attempt", budget_version="configuration",
    )

    def remove_new_estimate_marker(tx):
        current = tx.get("capacity_operation", capacity_id)
        body = dict(current["body"])
        body.pop("estimate_basis")
        tx.put("capacity_operation", capacity_id, body, expected_version=current["version"])
        return {}

    store.transact("old-estimate-fixture", {}, remove_new_estimate_marker)
    assert len(scheduler.reestimate_legacy_pending_models()) == 1
    scheduler.observe_headers(
        "scope", remaining_requests=100, remaining_tokens=100,
        token_limit=8000, reset_after_tokens=60, observed_at=clock(),
    )
    return scheduler, clock, capacity_id, work_record_id


def revise(scheduler, kind, record_id, update):
    def save(tx):
        current = tx.get(kind, record_id)
        tx.put(kind, record_id, update(current["body"]),
               expected_version=current["version"])
        return {}

    scheduler.store.transact(f"fixture-revise:{kind}:{record_id}", {}, save)


def test_exact_legacy_overlimit_is_replan_candidate_but_never_admitted(tmp_path):
    scheduler, clock, capacity_id, work_id = legacy_work(tmp_path, request_size=30000)
    before = scheduler.store.get("capacity_operation", capacity_id)
    status = scheduler.legacy_overlimit_status(capacity_id, work_id)
    assert status["status"] == "replan_allowed"
    assert status["estimated_tokens"] == 9375
    assert status["per_request_limit"] == 8000
    assert status["limit_evidence_ref"]["kind"] == "capacity_quota"
    assert status["capacity_ref"] == {
        "kind": "capacity_operation", "id": capacity_id, "version": before["version"]
    }
    assert status["intent_ref"]["id"] == "model-intent:old-call:1"
    assert scheduler.store.get("capacity_operation", capacity_id) == before
    assert scheduler.admit("host", "model") is None
    clock.advance(61)
    assert scheduler.admit("host", "model") is None
    assert scheduler.get(capacity_id)["phase"] == "waiting"
    assert scheduler.get(capacity_id)["attempts"] == []


def test_legacy_request_within_ceiling_keeps_ordinary_capacity_wait(tmp_path):
    scheduler, clock, capacity_id, work_id = legacy_work(tmp_path, request_size=20000)
    status = scheduler.legacy_overlimit_status(capacity_id, work_id)
    assert status["status"] == "ordinary_capacity_path"
    assert status["estimated_tokens"] == 6250
    assert status["per_request_limit"] == 8000
    assert status["limit_evidence_ref"]["kind"] == "capacity_quota"
    assert scheduler.admit("host", "model") is None  # Current headroom is only 100.
    clock.advance(61)
    admitted = scheduler.admit("host", "model")
    assert admitted and admitted["operation_id"] == capacity_id


@pytest.mark.parametrize("boundary", ["claimed", "work_mismatch", "dispatch", "estimate_mismatch"])
def test_legacy_overlimit_requires_unclaimed_exact_lineage(tmp_path, boundary):
    scheduler, _clock, capacity_id, work_id = legacy_work(tmp_path, request_size=30000)
    if boundary == "claimed":
        revise(scheduler, "capacity_operation", capacity_id, lambda body: {
            **body, "phase": "admitted", "owner": "other-host",
            "reservation": True, "local_active": True,
        })
        reason = "claimed_or_external_effect_uncertain"
    elif boundary == "work_mismatch":
        revise(scheduler, "executor_work", work_id, lambda body: {
            **body, "spec": {**body["spec"], "call_id": "different-call"},
        })
        reason = "work_lineage_mismatch"
    elif boundary == "dispatch":
        scheduler.store.transact(
            "fixture-uncertain-dispatch", {},
            lambda tx: tx.put(
                "model_dispatch_attempt", "model-dispatch:model-intent:old-call:1",
                {"status": "uncertain"},
            ),
        )
        reason = "external_effect_uncertain"
    else:
        revise(scheduler, "capacity_operation", capacity_id, lambda body: {
            **body, "estimated_tokens": 9500,
        })
        reason = "estimate_mismatch"
    status = scheduler.legacy_overlimit_status(capacity_id, work_id)
    assert status["status"] == "unsafe"
    assert status["reason"] == reason


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_legacy_overlimit_with_unreadable_intent_is_unsafe(tmp_path, damage):
    scheduler, _clock, capacity_id, work_id = legacy_work(tmp_path, request_size=30000)
    intent = scheduler.store.get("model_request_intent", "model-intent:old-call:1")
    path = scheduler.store.blobs.path(intent["body"]["blob"]["sha256"])
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt synthetic fixture")
    status = scheduler.legacy_overlimit_status(capacity_id, work_id)
    assert status["status"] == "unsafe"
    assert status["reason"] == "intent_blob_unverifiable"
    assert scheduler.get(capacity_id)["attempts"] == []
