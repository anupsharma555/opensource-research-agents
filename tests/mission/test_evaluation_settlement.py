"""Evaluation shutdown preserves callback acceptance and coherent state reads."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from ora.mission import evaluation_settlement
from ora.mission.dispatch import MissionDispatchDriver
from ora.state import StateStore


def _save(store, operation, action):
    return store.transact(operation, {}, action, mission_id="mission-fixture")


def test_intent_classification_uses_one_snapshot_during_receipt_commit(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    pointer = {"kind": "model_request_intent", "id": "intent-fixture", "version": 1}
    _save(store, "prepare-fixture", lambda tx: {
        "intent": tx.put("model_request_intent", pointer["id"], {
            "mission_id": "mission-fixture", "instance_id": "actor-fixture",
            "call_ref": {"id": "call-fixture", "version": 1},
            "request_sha256": "fixture-digest",
        }),
        "work": tx.put("executor_work", "work-fixture", {
            "phase": "pending", "spec": {"kind": "model", "call_id": "call-fixture",
                                             "call_version": 1},
        }),
        "marker": tx.put("model_dispatch_attempt", "model-dispatch:intent-fixture", {
            "status": "attempted_external_effect", "intent_ref": pointer,
            "request_sha256": "fixture-digest",
        }),
    })
    committed = threading.Event()
    writer_done = threading.Event()

    def writer():
        assert committed.wait(3)
        def settle(tx):
            work = tx.get("executor_work", "work-fixture")
            tx.put("executor_work", "work-fixture", {
                **work["body"], "phase": "completed",
            }, expected_version=work["version"])
            receipt = tx.put("model_response_receipt", "model-response:intent-fixture", {
                "intent_ref": pointer, "status": "completed",
                "request_sha256": "fixture-digest",
                "call_ref": {"id": "call-fixture", "version": 1},
                "mission_id": "mission-fixture",
            })
            marker = tx.get("model_dispatch_attempt", "model-dispatch:intent-fixture")
            return tx.put("model_dispatch_attempt", marker["id"], {
                **marker["body"], "status": "captured",
                "receipt_ref": {"kind": "model_response_receipt",
                                "id": receipt["id"], "version": receipt["version"]},
            }, expected_version=marker["version"])
        _save(store, "settle-fixture", settle)
        writer_done.set()

    thread = threading.Thread(target=writer)
    thread.start()
    real_connect = sqlite3.connect

    class InterleavedConnection:
        def __init__(self, connection):
            self.connection = connection
            self.interleaved = False

        def execute(self, statement, parameters=()):
            result = self.connection.execute(statement, parameters)
            if not self.interleaved and "JOIN heads" in statement:
                self.interleaved = True
                committed.set()
                assert writer_done.wait(3)
            return result

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(evaluation_settlement, "sqlite3", SimpleNamespace(
        connect=lambda *args, **kwargs: InterleavedConnection(
            real_connect(*args, **kwargs)),
    ))
    try:
        active, unsafe = evaluation_settlement.unsettled_peer_intents(
            store, {"actor-fixture"})
    finally:
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert [item["id"] for item in active] == ["intent-fixture"]
    assert unsafe == []
    active, unsafe = evaluation_settlement.unsettled_peer_intents(
        store, {"actor-fixture"})
    assert active == unsafe == []


@pytest.mark.parametrize(
    ("phase", "marker_status", "receipt_intent_id", "expected"), [
        ("pending", "captured", "intent-fixture", "active"),
        ("completed", "captured", "intent-fixture", "settled"),
        ("pending", "captured", "other-intent", "unsafe"),
        ("pending", "attempted_external_effect", "intent-fixture", "unsafe"),
        ("result_unaccepted", "captured", "intent-fixture", "unsafe"),
    ],
)
def test_captured_receipt_classification_checks_lineage_and_acceptance(
    tmp_path, phase, marker_status, receipt_intent_id, expected,
):
    store = StateStore(tmp_path)
    pointer = {"kind": "model_request_intent", "id": "intent-fixture", "version": 1}
    receipt_pointer = {"kind": "model_response_receipt",
                       "id": "model-response:intent-fixture", "version": 1}

    def prepare(tx):
        tx.put("model_request_intent", pointer["id"], {
            "mission_id": "mission-fixture", "instance_id": "actor-fixture",
            "call_ref": {"id": "call-fixture", "version": 1},
            "request_sha256": "fixture-digest",
        })
        tx.put("executor_work", "work-fixture", {
            "phase": phase, "spec": {"kind": "model", "call_id": "call-fixture",
                                     "call_version": 1},
        })
        tx.put("model_dispatch_attempt", "model-dispatch:intent-fixture", {
            "status": marker_status, "intent_ref": pointer,
            "request_sha256": "fixture-digest", "receipt_ref": receipt_pointer,
        })
        return tx.put("model_response_receipt", receipt_pointer["id"], {
            "intent_ref": {**pointer, "id": receipt_intent_id},
            "status": "completed", "request_sha256": "fixture-digest",
            "call_ref": {"id": "call-fixture", "version": 1},
            "mission_id": "mission-fixture",
        })

    _save(store, "prepare-captured-fixture", prepare)
    active, unsafe = evaluation_settlement.unsettled_peer_intents(
        store, {"actor-fixture"})
    status = "active" if active else "unsafe" if unsafe else "settled"
    assert status == expected


@pytest.mark.parametrize(("phase", "charge", "marker_hash", "expected"), [
    ("pending", 0, "fixture-digest", "active"),
    ("superseded", 0, "fixture-digest", "settled"),
    ("pending", 3, "fixture-digest", "unsafe"),
    ("pending", 0, "other-digest", "unsafe"),
])
def test_local_block_classification_requires_exact_zero_effect_capacity(
    tmp_path, phase, charge, marker_hash, expected,
):
    store = StateStore(tmp_path)
    pointer = {"kind": "model_request_intent", "id": "intent-fixture", "version": 1}
    call_ref = {"kind": "agent_call", "id": "call-fixture", "version": 1}

    def prepare(tx):
        tx.put("model_request_intent", pointer["id"], {
            "mission_id": "mission-fixture", "instance_id": "actor-fixture",
            "task_id": "task-fixture", "attempt_id": "attempt-fixture",
            "call_ref": call_ref, "request_sha256": "fixture-digest",
        })
        tx.put("executor_work", "work-fixture", {
            "phase": phase,
            "status_basis": "local_model_admission_blocked" if phase == "superseded" else None,
            "superseded_intent_ref": pointer if phase == "superseded" else None,
            "spec": {"kind": "model", "mission_id": "mission-fixture",
                     "actor_id": "actor-fixture", "task_id": "task-fixture",
                     "attempt_id": "attempt-fixture", "call_id": "call-fixture",
                     "call_version": 1, "proposal_ref": {"id": "task-fixture"}},
        })
        tx.put("model_dispatch_attempt", "model-dispatch:intent-fixture", {
            "status": "blocked_before_external_effect", "intent_ref": pointer,
            "call_ref": call_ref, "mission_id": "mission-fixture",
            "request_sha256": marker_hash,
        })
        return tx.put("capacity_operation", "capacity-fixture", {
            "mission_id": "mission-fixture", "task_id": "task-fixture",
            "attempt_id": "attempt-fixture", "call_id": "call-fixture",
            "request_token_estimate": {"intent_ref": pointer},
            "phase": "failed", "reservation": False,
            "external_active": False, "owner": None,
            "attempts": [{"status": "terminal_no_effect", "charged_tokens": charge,
                          "usage_known": True}],
        })

    _save(store, "prepare-blocked-fixture", prepare)
    active, unsafe = evaluation_settlement.unsettled_peer_intents(store, {"actor-fixture"})
    status = "active" if active else "unsafe" if unsafe else "settled"
    assert status == expected


def test_dispatch_drain_waits_for_callback_and_blocks_new_drains():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class Executor:
        def __init__(self):
            self.work = {"id": "work-fixture", "version": 1,
                         "body": {"phase": "pending", "spec": {"kind": "model"}}}
            self.board = SimpleNamespace(ready_work=lambda _mission: [])
            self.runs = 0

        def _work(self, _mission):
            return [self.work]

        async def run_ready(self, _mission):
            self.runs += 1

            def callback():
                started.set()
                assert release.wait(3)
                self.work["body"]["phase"] = "completed"
                finished.set()

            await asyncio.to_thread(callback)
            return {"observed": [], "next_wake": None}

    executor = Executor()
    driver = MissionDispatchDriver(executor, poll_interval=0.01)
    try:
        driver.poll("mission-fixture")
        assert started.wait(3)
        drained = {}
        thread = threading.Thread(target=lambda: drained.setdefault(
            "value", driver.drain_active(timeout=3)))
        thread.start()
        driver.poll("mission-fixture")
        assert thread.is_alive()
        assert executor.runs == 1
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert drained["value"] is True
        assert finished.is_set()
    finally:
        release.set()
        assert driver.close(timeout=3)
