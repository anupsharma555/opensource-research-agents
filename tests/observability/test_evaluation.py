"""Controller-stop observations are exact-run, minimal, and repeatable."""

from __future__ import annotations

import pytest

from ora.observability.evaluation import (
    KIND,
    read_evaluation_observation,
    record_evaluation_observation,
)
from ora.state import StateConflict, StateError, StateStore


def _run(store: StateStore, run_id: str, mission_id: str) -> None:
    store.transact(
        f"seed:{run_id}", {}, lambda tx: tx.put("mission_run", run_id, {
            "mission_id": mission_id, "status": "active",
        }), mission_id=mission_id,
    )


def _budget() -> dict:
    return {
        "schema": "ora.agent_validation_budget.v1",
        "model_attempts": [{"state": "observed", "private_marker": "LOCAL_ONLY"},
                           {"state": "rate_limited"}],
        "retrieval_attempts": [{"state": "observed"}],
        "stop_reason": "model_attempt_cap",
    }


def test_posthoc_observation_is_sanitized_idempotent_and_does_not_close_run(tmp_path):
    store = StateStore(tmp_path)
    _run(store, "run-a", "mission-a")
    budget = _budget()
    first = record_evaluation_observation(
        store, "run-a", budget, "model_attempt_cap", 12.5,
    )
    second = record_evaluation_observation(
        store, "run-a", budget, "model_attempt_cap", 12.5,
    )
    assert first == second
    assert len(store.records(KIND)) == 1
    safe = read_evaluation_observation(StateStore(tmp_path, read_only=True), "run-a")
    assert safe == first["body"]
    assert safe["model_http_attempts"] == 2
    assert safe["retrieval_http_attempts"] == 1
    assert safe["outcome"] == "model_attempt_cap"
    assert safe["capture_mode"] == "ledger_reconciled"
    assert "LOCAL_ONLY" not in str(first)
    assert store.get("mission_run", "run-a")["body"]["status"] == "active"
    with pytest.raises(StateConflict):
        record_evaluation_observation(store, "run-a", budget, "completed", 12.5)


def test_outcome_and_reader_reject_untrusted_or_wrong_run_observations(tmp_path):
    store = StateStore(tmp_path)
    _run(store, "run-a", "mission-a")
    _run(store, "run-b", "mission-b")
    with pytest.raises(StateError):
        record_evaluation_observation(store, "run-a", _budget(), "private detail\n", 1)
    with pytest.raises(StateError):
        record_evaluation_observation(store, "run-a", _budget(), "completed", -1)
    assert read_evaluation_observation(store, "run-a") is None
    safe = record_evaluation_observation(
        store, "run-a", _budget(), "controller_error_ValueError", 1,
    )
    assert safe["body"]["outcome"] == "controller_error"
    assert read_evaluation_observation(store, "run-b") is None
    store.transact(
        "forge-cross-run-observation", {}, lambda tx: tx.put(
            KIND, "evaluation-observation:run-b", {**safe["body"], "run_id": "run-b"},
        ), mission_id="mission-b",
    )
    assert read_evaluation_observation(store, "run-b") is None


def test_unknown_bounded_outcome_is_not_exported_verbatim(tmp_path):
    store = StateStore(tmp_path)
    _run(store, "run-a", "mission-a")
    saved = record_evaluation_observation(
        store, "run-a", _budget(), "private_client_label", 1,
    )
    assert saved["body"]["outcome"] == "other"
    assert "private_client_label" not in str(saved)
