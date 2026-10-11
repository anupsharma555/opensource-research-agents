"""The mission supervisor can advance fast peers while other callbacks run."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from ora.agents import AgentRegistry
from ora.coordination.board import ProposalBoard, ref
from ora.coordination.capacity import CapacityPolicy, CapacityScheduler, QuotaLimit
from ora.coordination.executor import ConcurrentExecutor, WorkResult, WorkSpec
from ora.coordination.mailbox import PeerMailbox
from ora.coordination.traces import TraceJournal
from ora.mission.dispatch import MissionDispatchDriver
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[2]


def _setup(tmp_path, callback):
    store = StateStore(tmp_path)
    board = ProposalBoard(store)
    capacity = CapacityScheduler(
        store,
        CapacityPolicy(
            permits={"model": 3, "retrieval": 1, "compute": 1},
            quotas={"provider": QuotaLimit(60, requests=10, tokens=100)},
        ),
    )
    executor = ConcurrentExecutor(
        store, board, PeerMailbox(store), capacity, TraceJournal(store),
        {("fixture", "1"): callback}, owner="dispatch-test", lease_seconds=30,
    )
    registry = AgentRegistry(store, ROOT)

    def submit(mission: str, name: str):
        actor = registry.register(
            f"actor-{name}", mission_id=mission, run_id=f"run-{mission}",
            role_id="evidence_preparation_synthesis", display_name=f"Peer {name}",
        )["id"]
        config = store.transact(
            f"config-{name}", {"actor": actor},
            lambda tx: tx.put(
                "agent_configuration", f"config-{name}",
                {"mission_id": mission, "instance_id": actor,
                 "configuration_sha256": f"fixture-{name}"},
            ), mission_id=mission,
        )
        proposal = board.propose(
            f"propose-{name}", mission_id=mission, proposal_id=f"proposal-{name}",
            actor_id=actor, objective=f"Inspect {name}",
            expected_artifact="Fixture receipt", next_action="Run callback",
        )["proposal_ref"]

        def next_step(work_id: str):
            return executor.submit(WorkSpec(
                work_id=work_id, mission_id=mission, run_id=f"run-{mission}",
                proposal_ref=proposal, actor_id=actor, configuration_ref=ref(config),
                input_refs=(), handler_id="fixture", handler_version="1", kind="model",
                lane=actor, quota_scopes=("provider",), estimated_tokens=1,
                attempt_id=f"attempt-{work_id}",
            ))

        next_step(name)
        return next_step, proposal

    return store, board, capacity, executor, submit


def _until(driver, mission: str, predicate, *, timeout: float = 4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = driver.poll(mission)
        if predicate(state):
            return state
        time.sleep(0.01)
    raise AssertionError(f"Dispatch condition did not occur for {mission}")


def test_fast_next_step_and_other_mission_finish_before_slow_peer(tmp_path):
    slow_started = threading.Event()
    release_slow = threading.Event()
    fast_second_started = threading.Event()
    other_started = threading.Event()

    def callback(work):
        if work.work_id == "slow":
            slow_started.set()
            assert release_slow.wait(5), "controlled slow callback was not released"
        elif work.work_id == "fast":
            assert slow_started.wait(5), "controlled slow callback did not start"
        elif work.work_id == "fast-2":
            fast_second_started.set()
        elif work.work_id == "other":
            other_started.set()
        return WorkResult(
            disposition="continue_task" if work.work_id == "fast" else "completed",
            payload={"observed": work.work_id}, used_tokens=1,
        )

    store, board, _, executor, submit = _setup(tmp_path, callback)
    next_fast, fast_proposal = submit("mission-a", "fast")
    submit("mission-a", "slow")
    driver = MissionDispatchDriver(executor, poll_interval=0.01)
    try:
        def fast_completed(state):
            return any(
                store.get(**pointer)["body"]["spec"]["work_id"] == "fast"
                for pointer in state["completed_refs"]
            )

        first = _until(driver, "mission-a", fast_completed)
        assert slow_started.is_set()
        assert first["active"] and not release_slow.is_set()
        # Accepted work becomes visible before the separate claim-release
        # transaction. Wait for that observable handoff, not a fixed delay.
        _until(
            driver, "mission-a",
            lambda _: next(
                item for item in board.open_work("mission-a")
                if item["proposal_ref"]["id"] == fast_proposal["id"]
            )["current_claim"] is None,
        )
        next_fast("fast-2")
        _until(
            driver, "mission-a",
            lambda state: any(
                store.get(**pointer)["body"]["spec"]["work_id"] == "fast-2"
                for pointer in state["completed_refs"]
            ),
        )
        assert store.get("board_proposal", fast_proposal["id"])["body"]["status"] == "completed"
        submit("mission-b", "other")
        _until(
            driver, "mission-b",
            lambda state: any(
                store.get(**pointer)["body"]["spec"]["work_id"] == "other"
                for pointer in state["completed_refs"]
            ),
        )
        assert not release_slow.is_set()
        assert fast_second_started.is_set()
        assert other_started.is_set()
        assert not any(
            item["body"]["phase"] == "completed"
            for item in executor._work("mission-a")
            if item["body"]["spec"]["work_id"] == "slow"
        )
    finally:
        release_slow.set()
        assert driver.close(timeout=4)


def test_close_cancels_dispatched_callback_and_keeps_uncertain_effect(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def callback(work):
        started.set()
        assert release.wait(5), "controlled callback was not released"
        return WorkResult(payload={"observed": work.work_id}, used_tokens=1)

    store, _, capacity, executor, submit = _setup(tmp_path, callback)
    submit("mission-a", "slow")
    driver = MissionDispatchDriver(executor, poll_interval=0.01)
    try:
        _until(driver, "mission-a", lambda _: started.is_set())
        work = next(item for item in executor._work("mission-a")
                    if item["body"]["spec"]["work_id"] == "slow")
        spec = executor._spec_from(work)
        assert driver.close(timeout=4)
        assert capacity.get(executor._capacity_id(spec))["phase"] == "uncertain"
        assert store.get("board_proposal", spec.proposal_ref["id"])["body"]["status"] == "open"
        assert not store.records("executor_artifact")
    finally:
        release.set()
        driver.close(timeout=4)
