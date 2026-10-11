"""Dispatch-time asyncio limits are saved without changing work identity."""

from __future__ import annotations

import asyncio

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ProposalBoard, ref
from ora.coordination.capacity import CapacityPolicy, CapacityScheduler, QuotaLimit
from ora.coordination.executor import ConcurrentExecutor, WorkResult
from ora.coordination.mailbox import PeerMailbox
from ora.coordination.traces import TraceJournal
from ora.state import StateStore
from ora.state.store import StateConflict
from tests.coordination.test_executor import foundation, propose, spec
from tests.mission.test_service import answer, service


def _reopened_executor(tmp_path, *, handler, lease_seconds: float):
    store = StateStore(tmp_path)
    board = ProposalBoard(store)
    capacity = CapacityScheduler(
        store,
        CapacityPolicy(
            permits={"model": 2, "retrieval": 1, "compute": 1},
            quotas={"provider": QuotaLimit(60, requests=10, tokens=100)},
        ),
    )
    executor = ConcurrentExecutor(
        store,
        board,
        PeerMailbox(store),
        capacity,
        TraceJournal(store),
        {("fixture", "1"): handler},
        owner="reopened-host",
        lease_seconds=lease_seconds,
    )
    return store, executor


def test_legacy_pending_work_records_actual_reopened_lease_and_replays(tmp_path):
    calls = []

    async def callback(work):
        calls.append(work.work_id)
        return WorkResult(payload={"result": "accepted"}, used_tokens=1)

    _, board, _, first, actors, configs = foundation(
        tmp_path,
        handlers={("fixture", "1"): callback},
        lease_seconds=5,
    )
    proposal = propose(board, actors[0], "runtime-restart")
    work_spec = spec("runtime-restart", proposal, actors[0], configs[0])
    submitted = first.submit(work_spec)
    assert "runtime_segments" not in submitted["body"]
    old_spec = submitted["body"]["spec"]

    reopened_store, reopened = _reopened_executor(
        tmp_path,
        handler=callback,
        lease_seconds=9,
    )
    asyncio.run(reopened.run_ready("mission"))
    current = reopened_store.get("executor_work", submitted["id"])
    assert current["body"]["phase"] == "completed"
    assert current["body"]["spec"] == old_spec
    assert calls == ["runtime-restart"]
    segment = current["body"]["runtime_segments"][0]
    assert segment["runtime"] == "custom_asyncio"
    assert segment["executor_lease_seconds"] == 9
    assert segment["executor_callback_timeout_seconds"] is None
    assert segment["executor_callback_timeout_applicability"] == "none_configured"
    assert segment["sdk_step_limit"] is None
    assert segment["sdk_step_limit_applicability"] == "not_applicable"
    assert segment["sdk_recursion_limit"] is None
    assert segment["sdk_recursion_limit_applicability"] == "not_applicable"
    assert segment["attempt_id"] == work_spec.attempt_id
    cap = reopened_store.get("capacity_operation", reopened._capacity_id(work_spec))
    assert cap["body"]["attempts"][segment["capacity_attempt_number"] - 1]["status"] == "completed"
    assert segment["capacity_generation"] == cap["body"]["generation"]
    assert reopened_store.get(**segment["capacity_dispatch_ref"])["body"]["phase"] == "dispatched"
    assert ref(first.submit(work_spec)) == ref(current)
    asyncio.run(reopened.run_ready("mission"))
    assert calls == ["runtime-restart"]
    assert reopened_store.get("executor_work", submitted["id"]) == current


def test_metadata_failure_before_callback_is_known_no_effect(tmp_path, monkeypatch):
    calls = []

    async def callback(work):
        calls.append(work.work_id)
        return WorkResult(payload={"result": "unexpected"}, used_tokens=1)

    store, board, _, executor, actors, configs = foundation(
        tmp_path,
        handlers={("fixture", "1"): callback},
        lease_seconds=7,
    )
    proposal = propose(board, actors[0], "runtime-write-failure")
    work_spec = spec("runtime-write-failure", proposal, actors[0], configs[0])
    submitted = executor.submit(work_spec)
    original = store.transact

    def reject_metadata(operation_id, *args, **kwargs):
        if operation_id.startswith("executor-runtime-segment:"):
            raise StateConflict("injected metadata interruption")
        return original(operation_id, *args, **kwargs)

    monkeypatch.setattr(store, "transact", reject_metadata)
    asyncio.run(executor.run_ready("mission"))
    assert calls == []
    assert "runtime_segments" not in store.get("executor_work", submitted["id"])["body"]
    cap = store.get("capacity_operation", executor._capacity_id(work_spec))
    assert all(item["status"] == "temporary_no_effect" for item in cap["body"]["attempts"])
    assert store.get("board_proposal", proposal["id"])["body"]["status"] == "open"
    assert (
        next(item for item in board.open_work("mission") if item["proposal_ref"] == proposal)[
            "current_claim"
        ]
        is None
    )


def test_expired_dispatch_fences_callback_and_leaves_sibling_runnable(tmp_path, monkeypatch):
    now = [100.0]
    calls = []

    async def callback(work):
        calls.append(work.work_id)
        return WorkResult(payload={"result": work.work_id}, used_tokens=1)

    store, board, _, executor, actors, configs = foundation(
        tmp_path,
        handlers={("fixture", "1"): callback},
        clock=lambda: now[0],
        lease_seconds=5,
    )
    expired_proposal = propose(board, actors[0], "expiring-work")
    expired_spec = spec("expiring-work", expired_proposal, actors[0], configs[0])
    expired_work = executor.submit(expired_spec)
    original = store.transact
    delayed = []

    def delay_metadata(operation_id, *args, **kwargs):
        if operation_id.startswith("executor-runtime-segment:") and not delayed:
            now[0] += 6
            delayed.append(True)
        return original(operation_id, *args, **kwargs)

    monkeypatch.setattr(store, "transact", delay_metadata)
    asyncio.run(executor.run_ready("mission"))
    assert delayed and calls == []
    assert "runtime_segments" not in store.get("executor_work", expired_work["id"])["body"]
    expired_cap = store.get("capacity_operation", executor._capacity_id(expired_spec))
    assert expired_cap["body"]["phase"] == "uncertain"
    assert (
        next(
            item for item in board.open_work("mission") if item["proposal_ref"] == expired_proposal
        )["current_claim"]
        is None
    )

    sibling_proposal = propose(board, actors[1], "healthy-sibling")
    sibling_spec = spec("healthy-sibling", sibling_proposal, actors[1], configs[1])
    executor.submit(sibling_spec)
    asyncio.run(executor.run_ready("mission"))
    assert calls == ["healthy-sibling"]
    assert store.get("board_proposal", sibling_proposal["id"])["body"]["status"] == ("completed")


def test_revoked_board_claim_blocks_callback_before_metadata_commit(tmp_path, monkeypatch):
    calls = []

    async def callback(work):
        calls.append(work.work_id)
        return WorkResult(payload={"result": "unexpected"}, used_tokens=1)

    store, board, _, executor, actors, configs = foundation(
        tmp_path,
        handlers={("fixture", "1"): callback},
        lease_seconds=30,
    )
    proposal = propose(board, actors[0], "revoked-before-callback")
    work_spec = spec("revoked-before-callback", proposal, actors[0], configs[0])
    submitted = executor.submit(work_spec)
    original = store.transact
    revised = []

    def revoke_before_metadata(operation_id, *args, **kwargs):
        if operation_id.startswith("executor-runtime-segment:") and not revised:
            changed = board.revise(
                "revoke-before-runtime-record",
                mission_id="mission",
                proposal_id=proposal["id"],
                actor_id=actors[0],
                expected_version=proposal["version"],
                reason="The selected task changed before callback",
            )
            assert changed["status"] == "accepted"
            revised.append(True)
        return original(operation_id, *args, **kwargs)

    monkeypatch.setattr(store, "transact", revoke_before_metadata)
    asyncio.run(executor.run_ready("mission"))
    assert revised and calls == []
    assert "runtime_segments" not in store.get("executor_work", submitted["id"])["body"]
    cap = store.get("capacity_operation", executor._capacity_id(work_spec))
    assert cap["body"]["attempts"][-1]["status"] == "temporary_no_effect"
    assert (
        next(
            item
            for item in board.open_work("mission")
            if item["proposal_ref"]["id"] == proposal["id"]
        )["current_claim"]
        is None
    )


def test_accepted_segment_survives_release_interruption_without_duplicate(tmp_path, monkeypatch):
    calls = []

    async def callback(work):
        calls.append(work.work_id)
        return WorkResult(
            payload={"result": "accepted"}, used_tokens=1, disposition="continue_task"
        )

    store, board, _, executor, actors, configs = foundation(
        tmp_path,
        handlers={("fixture", "1"): callback},
        lease_seconds=11,
    )
    proposal = propose(board, actors[0], "runtime-accepted")
    work_spec = spec("runtime-accepted", proposal, actors[0], configs[0])
    submitted = executor.submit(work_spec)

    def interrupt_release(*_args):
        raise StateConflict("injected post-acceptance interruption")

    monkeypatch.setattr(executor, "_release_board", interrupt_release)
    asyncio.run(executor.run_ready("mission"))
    accepted = store.get("executor_work", submitted["id"])
    assert accepted["body"]["phase"] == "completed"
    assert accepted["body"]["runtime_segments"][0]["executor_lease_seconds"] == 11
    assert len(store.records("executor_artifact")) == 1
    assert calls == ["runtime-accepted"]
    reopened_store, reopened = _reopened_executor(
        tmp_path,
        handler=callback,
        lease_seconds=23,
    )
    asyncio.run(reopened.run_ready("mission"))
    assert calls == ["runtime-accepted"]
    assert reopened_store.get("executor_work", submitted["id"]) == accepted
    assert len(reopened_store.records("executor_artifact")) == 1


def test_mission_model_segment_pins_service_lease_and_attempt(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda _request, *, timeout: answer("Done.", 1))
    try:
        run = host.start(
            LiveStartRequest(
                request_id="runtime-segment-mission",
                question="A bounded answer",
                agent_ids=["agent-synthesis"],
            )
        )
        control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        pending = host._control(run.run_id, "agent-synthesis")
        work = host._work_for(pending)
        assert "runtime_segments" not in work["body"]
        asyncio.run(host.executor.run_ready(control["body"]["mission_id"]))
        accepted = host.store.get("executor_work", work["id"])
        segment = accepted["body"]["runtime_segments"][0]
        assert segment["executor_lease_seconds"] == 180
        assert (
            segment["attempt_id"]
            == host.store.get(**pending["body"]["call_ref"])["body"]["attempt_id"]
        )
        assert segment["sdk_step_limit_applicability"] == "not_applicable"
        assert accepted["body"]["result_ref"] is not None
    finally:
        host.close()
