"""Focused synthetic checks for model expectations and saved timing evidence."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ora.coordination.board import ref
from ora.mission.latency import LatencyError, inspect_latency, read_expectation, save_expectation
from ora.state import StateConflict, StateStore


def stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def seed_expectation_inputs(store: StateStore) -> dict:
    def save(tx):
        tx.put(
            "mission_run",
            "run",
            {"mission_id": "m", "run_id": "run", "status": "active", "created_at": stamp(100)},
        )
        actor = tx.put("agent_instance", "peer", {"mission_id": "m", "run_id": "run"})
        task = tx.put("board_proposal", "task", {"mission_id": "m", "status": "open"})
        call = tx.put(
            "agent_call", "call-1", {"mission_id": "m", "instance_id": "peer", "task_id": "task"}
        )
        response = tx.put(
            "model_response_receipt",
            "response-1",
            {
                "mission_id": "m",
                "status": "tool_requested",
                "task_id": "task",
                "call_ref": ref(call),
            },
        )
        return {"actor": ref(actor), "task": ref(task), "response": ref(response)}

    return store.transact("seed", {}, save, mission_id="m")


def test_model_expectation_is_optional_revisable_and_source_pinned(tmp_path) -> None:
    store = StateStore(tmp_path, clock=lambda: 100.0)
    pointers = seed_expectation_inputs(store)
    assert read_expectation(store, "m", "peer", "task") is None
    first = save_expectation(
        store,
        "expect-1",
        mission_id="m",
        run_id="run",
        actor_ref=pointers["actor"],
        task_ref=pointers["task"],
        response_ref=pointers["response"],
        category="medium",
        basis="A focused source review is needed",
        expected_version=0,
    )
    assert first["version"] == 1
    assert read_expectation(store, "m", "peer", "task")["body"]["category"] == "medium"
    assert (
        save_expectation(
            store,
            "expect-1",
            mission_id="m",
            run_id="run",
            actor_ref=pointers["actor"],
            task_ref=pointers["task"],
            response_ref=pointers["response"],
            category="medium",
            basis="A focused source review is needed",
            expected_version=0,
        )
        == first
    )

    def second_response(tx):
        call = tx.put(
            "agent_call", "call-2", {"mission_id": "m", "instance_id": "peer", "task_id": "task"}
        )
        response = tx.put(
            "model_response_receipt",
            "response-2",
            {
                "mission_id": "m",
                "status": "complete",
                "task_id": "task",
                "call_ref": ref(call),
            },
        )
        return ref(response)

    response_2 = store.transact("second-response", {}, second_response, mission_id="m")
    with pytest.raises(LatencyError, match="new model response"):
        save_expectation(
            store,
            "bad-revision",
            mission_id="m",
            run_id="run",
            actor_ref=pointers["actor"],
            task_ref=pointers["task"],
            response_ref=pointers["response"],
            category="long",
            basis="More evidence needed",
            expected_version=1,
        )
    revised = save_expectation(
        store,
        "expect-2",
        mission_id="m",
        run_id="run",
        actor_ref=pointers["actor"],
        task_ref=pointers["task"],
        response_ref=response_2,
        category="long",
        basis="Two independent sources remain to be checked",
        expected_version=1,
    )
    assert revised["version"] == 2
    assert store.get(**first)["body"]["category"] == "medium"
    assert read_expectation(store, "m", "peer", "task")["body"]["response_ref"] == response_2
    with pytest.raises(StateConflict):
        save_expectation(
            store,
            "stale-revision",
            mission_id="m",
            run_id="run",
            actor_ref=pointers["actor"],
            task_ref=pointers["task"],
            response_ref=response_2,
            category="minimal",
            basis="Already checked",
            expected_version=0,
        )

    def revise_task(tx):
        old = tx.get("board_proposal", "task")
        return ref(tx.put("board_proposal", "task", old["body"], expected_version=1))

    store.transact("revise-task", {}, revise_task, mission_id="m")
    after_progress = read_expectation(store, "m", "peer", "task")
    assert after_progress["version"] == 2
    assert after_progress["task_version_mismatch"] is True
    assert after_progress["task_meaning_changed"] is False

    def change_objective(tx):
        old = tx.get("board_proposal", "task")
        return ref(
            tx.put(
                "board_proposal",
                "task",
                {
                    **old["body"],
                    "objective": "Check a new population",
                },
                expected_version=2,
            )
        )

    store.transact("change-objective", {}, change_objective, mission_id="m")
    assert read_expectation(store, "m", "peer", "task")["task_meaning_changed"] is True


def test_expectation_rejects_response_from_another_task(tmp_path) -> None:
    store = StateStore(tmp_path, clock=lambda: 100.0)
    pointers = seed_expectation_inputs(store)

    def other_response(tx):
        tx.put("board_proposal", "other-task", {"mission_id": "m", "status": "open"})
        call = tx.put(
            "agent_call",
            "other-call",
            {
                "mission_id": "m",
                "instance_id": "peer",
                "task_id": "other-task",
            },
        )
        response = tx.put(
            "model_response_receipt",
            "other-response",
            {
                "mission_id": "m",
                "status": "tool_requested",
                "task_id": "other-task",
                "call_ref": ref(call),
            },
        )
        return ref(response)

    unrelated = store.transact("other-response", {}, other_response, mission_id="m")
    with pytest.raises(LatencyError, match="matching model response"):
        save_expectation(
            store,
            "wrong-source",
            mission_id="m",
            run_id="run",
            actor_ref=pointers["actor"],
            task_ref=pointers["task"],
            response_ref=unrelated,
            category="long",
            basis="A second task is involved",
            expected_version=0,
        )
    assert read_expectation(store, "m", "peer", "task") is None


def test_latency_distinguishes_missing_measurements_from_zero_and_overlap(tmp_path) -> None:
    current = [100.0]
    store = StateStore(tmp_path, clock=lambda: current[0])
    seed_expectation_inputs(store)
    empty = inspect_latency(store, "run", now=100.0)
    assert empty["elapsed_seconds"] == 0
    assert empty["first_accepted_contribution_at"] is None
    assert empty["model_active_wall_seconds"] is None
    assert all(value is None for value in empty["capacity_wait_operation_sums_seconds"].values())
    assert empty["peer_wait_seconds"] is None

    def save(tx):
        tx.put(
            "capacity_operation",
            "cap-a",
            {
                "mission_id": "m",
                "task_id": "task",
                "kind": "model",
                "phase": "completed",
                "attempts": [{"at": 110.0, "completed_at": 130.0}],
                "queue_wait_seconds": 5,
                "quota_wait_seconds": 0,
                "provider_wait_seconds": 2,
                "retry_wait_seconds": 0,
            },
        )
        tx.put(
            "capacity_operation",
            "cap-b",
            {
                "mission_id": "m",
                "task_id": "task",
                "kind": "model",
                "phase": "completed",
                "attempts": [{"at": 120.0, "completed_at": 140.0}],
                "queue_wait_seconds": 7,
                "quota_wait_seconds": 3,
                "provider_wait_seconds": 0,
                "retry_wait_seconds": 0,
            },
        )
        tx.put(
            "capacity_operation",
            "cap-wait",
            {
                "mission_id": "m",
                "task_id": "task",
                "kind": "retrieval",
                "phase": "waiting",
                "attempts": [],
                "wait_reason": "provider",
                "wait_started_at": 145.0,
                "queue_wait_seconds": 1,
                "quota_wait_seconds": 0,
                "provider_wait_seconds": 0,
                "retry_wait_seconds": 0,
            },
        )
        outcome = tx.put(
            "board_outcome",
            "accepted",
            {
                "mission_id": "m",
                "proposal_ref": {"kind": "board_proposal", "id": "task", "version": 1},
                "actor_ref": {"kind": "agent_instance", "id": "peer", "version": 1},
                "result_refs": [{"kind": "report", "id": "contribution", "version": 1}],
            },
        )
        return ref(outcome)

    current[0] = 145.0
    outcome_ref = store.transact("observations", {}, save, mission_id="m")
    result = inspect_latency(store, "run", now=150.0)
    assert result["elapsed_seconds"] == 50
    assert result["seconds_to_first_accepted"] == 45
    assert result["first_accepted_outcome_ref"] == outcome_ref
    assert result["model_active_wall_seconds"] == 30
    assert result["model_active_operation_sum_seconds"] == 40
    assert result["capacity_wait_operation_sums_seconds"] == {
        "queue_wait_seconds": 13,
        "quota_wait_seconds": 3,
        "provider_wait_seconds": 7,
        "retry_wait_seconds": 0,
    }
    assert result["peer_wait_seconds"] is None
    task = inspect_latency(store, "run", task_id="task", now=150.0)
    assert task["elapsed_seconds"] == 50


def test_completed_run_elapsed_stops_and_open_attempt_is_not_completed_activity(tmp_path) -> None:
    current = [100.0]
    store = StateStore(tmp_path, clock=lambda: current[0])
    seed_expectation_inputs(store)

    def complete(tx):
        run = tx.get("mission_run", "run")
        tx.put(
            "mission_run",
            "run",
            {**run["body"], "status": "completed", "completed_at": stamp(130)},
            expected_version=1,
        )
        outcome = tx.put(
            "board_outcome",
            "final-outcome",
            {
                "mission_id": "m",
                "proposal_ref": {"kind": "board_proposal", "id": "task", "version": 1},
                "result_refs": [{"kind": "report", "id": "contribution", "version": 1}],
            },
        )
        task = tx.get("board_proposal", "task")
        tx.put(
            "board_proposal",
            "task",
            {
                **task["body"],
                "status": "completed",
                "outcome_ref": ref(outcome),
            },
            expected_version=1,
        )
        tx.put(
            "capacity_operation",
            "cap-open",
            {
                "mission_id": "m",
                "task_id": "task",
                "kind": "model",
                "phase": "uncertain",
                "attempts": [{"at": 120.0, "status": "dispatched"}],
                "queue_wait_seconds": 0,
                "quota_wait_seconds": 0,
                "provider_wait_seconds": 0,
                "retry_wait_seconds": 0,
            },
        )
        return {"saved": True}

    current[0] = 130.0
    store.transact("complete", {}, complete, mission_id="m")
    result = inspect_latency(store, "run", now=500.0)
    assert result["elapsed_seconds"] == 30
    assert inspect_latency(store, "run", task_id="task", now=500.0)["elapsed_seconds"] == 30
    assert result["model_active_wall_seconds"] is None
    assert result["model_attempts_without_closed_interval"] == 1
    assert result["capacity_wait_operation_sums_seconds"]["queue_wait_seconds"] == 0
