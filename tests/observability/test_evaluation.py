"""Controller-stop observations are exact-run, minimal, and repeatable."""

from __future__ import annotations

import hashlib

import pytest

from ora.observability.evaluation import (
    KIND,
    advance_evaluation_observation,
    combine_validation_ledgers,
    read_evaluation_observation,
    record_evaluation_observation,
)
from ora.state import StateConflict, StateError, StateStore
from ora.state.store import canonical


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


def test_explicit_continuation_advances_cumulative_observation_without_rewriting_prior(tmp_path):
    store = StateStore(tmp_path)
    _run(store, "run-a", "mission-a")
    _run(store, "run-b", "mission-b")
    first = record_evaluation_observation(store, "run-a", _budget(), "model_http_error", 12.5)
    foreign = record_evaluation_observation(store, "run-b", _budget(), "model_http_error", 2)
    prior_ref = {key: first[key] for key in ("kind", "id", "version")}
    cumulative = {"schema": "ora.agent_validation_budget.v1",
             "model_attempts": [*_budget()["model_attempts"],
                                {"state": "observed", "private_marker": "PHASE_ONLY"}],
             "retrieval_attempts": [*_budget()["retrieval_attempts"],
                                    {"state": "observed"}, {"state": "observed"}],
             "stop_reason": None}
    with pytest.raises(StateError, match="exact prior observation"):
        advance_evaluation_observation(
            store, "run-a", {key: foreign[key] for key in prior_ref}, cumulative, "completed", 3.5,
        )
    second = advance_evaluation_observation(
        store, "run-a", prior_ref, cumulative, "completed", 3.5,
    )
    assert second["version"] == 2
    assert second["body"]["model_http_attempts"] == 3
    assert second["body"]["retrieval_http_attempts"] == 3
    assert second["body"]["elapsed_seconds"] == 16.0
    assert second["body"]["outcome"] == "completed"
    assert second["body"]["ledger_sha256"] == hashlib.sha256(canonical(cumulative).encode()).hexdigest()
    assert set(second["body"]) == set(first["body"])
    assert store.get(KIND, first["id"], 1) == first
    assert read_evaluation_observation(StateStore(tmp_path, read_only=True), "run-a") == second[
        "body"
    ]
    assert advance_evaluation_observation(
        store, "run-a", prior_ref, cumulative, "completed", 3.5,
    ) == second
    with pytest.raises(StateConflict, match="pinned prior version"):
        advance_evaluation_observation(store, "run-a", prior_ref, cumulative, "mission_failed", 3.5)
    with pytest.raises(StateConflict):
        record_evaluation_observation(store, "run-a", _budget(), "model_http_error", 12.5)
    with pytest.raises(StateError, match="valid cumulative ledger"):
        advance_evaluation_observation(store, "run-a", prior_ref, cumulative, "completed", -1)
    assert store.get("mission_run", "run-a")["body"]["status"] == "active"


def test_cumulative_ledger_chains_three_phases_without_changing_prior_rows():
    def phase(number, status, tokens):
        return {
            "schema": "ora.agent_validation_budget.v1",
            "limits": {"model": None, "tokens": None, "retrieval": None,
                       "accounting_only": True},
            "started_at": float(number),
            "model_attempts": [{"number": 1, "status": status,
                                "request_sha256": str(number) * 64}],
            "retrieval_attempts": [],
            "planned_tokens": tokens,
            "peak_planned_tokens": tokens,
            "stop_reason": "model_http_error" if status == 413 else None,
        }

    first, second, third = phase(1, 413, 17), phase(2, 413, 19), phase(3, 200, 23)
    two = combine_validation_ledgers(first, second)
    all_three = combine_validation_ledgers(two, third)
    assert [row["number"] for row in all_three["model_attempts"]] == [1, 2, 3]
    assert [row["request_sha256"] for row in all_three["model_attempts"]] == [
        str(index) * 64 for index in (1, 2, 3)
    ]
    assert all_three["planned_tokens"] == 59
    assert all_three["peak_planned_tokens"] == 59
    assert first["model_attempts"][0]["number"] == second["model_attempts"][0]["number"] == 1


def test_finite_continuation_uses_only_remaining_aggregate_allowance():
    prior = {
        "schema": "ora.agent_validation_budget.v1",
        "limits": {"model": None, "tokens": 100, "retrieval": 0},
        "started_at": 1.0,
        "model_attempts": [
            {"number": 1, "planned_tokens": 17, "actual_tokens": 10},
            {"number": 2, "planned_tokens": 20, "actual_tokens": None},
        ],
        "retrieval_attempts": [], "planned_tokens": 30,
        "peak_planned_tokens": 37, "stop_reason": "model_http_error",
    }
    phase = {
        "schema": "ora.agent_validation_budget.v1",
        "limits": {"model": None, "tokens": 70, "retrieval": 0},
        "started_at": 2.0,
        "model_attempts": [
            {"number": 1, "planned_tokens": 18, "actual_tokens": 8},
        ],
        "retrieval_attempts": [], "planned_tokens": 8,
        "peak_planned_tokens": 18, "stop_reason": None,
    }
    combined = combine_validation_ledgers(prior, phase)
    assert combined["limits"] == prior["limits"]
    assert combined["planned_tokens"] == 38  # 10 known + 20 unknown + 8 known
    assert combined["peak_planned_tokens"] == 48
    assert [row["number"] for row in combined["model_attempts"]] == [1, 2, 3]
    assert combined["model_attempts"][1]["actual_tokens"] is None
    assert prior["planned_tokens"] == 30 and phase["planned_tokens"] == 8
    for bad in (
        {**phase, "limits": {"model": None, "tokens": 100, "retrieval": 0}},
        {**phase, "limits": {"model": 1, "tokens": 70, "retrieval": 0}},
        {**phase, "limits": {"model": None, "tokens": 70, "retrieval": 1}},
        {**phase, "planned_tokens": 71},
        {**phase, "planned_tokens": 0},
    ):
        with pytest.raises(StateError):
            combine_validation_ledgers(prior, bad)
    with pytest.raises(StateError, match="unknown-use reservation"):
        combine_validation_ledgers({**prior, "planned_tokens": 10}, phase)

    overrun = {**phase, "model_attempts": [
        {"number": 1, "planned_tokens": 18, "actual_tokens": 75},
    ], "planned_tokens": 75, "stop_reason": "reported_token_cap"}
    observed = combine_validation_ledgers(prior, overrun)
    assert observed["planned_tokens"] == 105  # Provider truth remains reportable.
    assert observed["stop_reason"] == "reported_token_cap"
    assert combine_validation_ledgers(prior, {**overrun, "stop_reason": "model_http_error"})[
        "planned_tokens"] == 105  # A concurrent failed attempt may set the last stop.
    with pytest.raises(StateError, match="observed overrun"):
        combine_validation_ledgers(prior, {**overrun, "stop_reason": None})
    with pytest.raises(StateError, match="finite cumulative limits"):
        combine_validation_ledgers(prior, {**overrun, "peak_planned_tokens": 71})
    with pytest.raises(StateError, match="finite cumulative limits"):
        combine_validation_ledgers(prior, {**phase, "retrieval_attempts": [{"number": 1}]})


@pytest.mark.parametrize("native", [False, True])
def test_finite_qwen_policy_and_historical_charge_survive_ledger_combine(native):
    policy = {"qwen_estimate_policy": "native_when_supported"} if native else {}
    prior = {
        "schema": "ora.agent_validation_budget.v1",
        "limits": {"model": None, "tokens": 100, "retrieval": 0,
                   "allow_qwen_text": True, **policy},
        "model_attempts": [{"number": 1, "planned_tokens": 44,
                            "actual_tokens": 10,
                            "token_estimate_basis": ("native" if native else "surrogate")}],
        "retrieval_attempts": [], "planned_tokens": 10,
        "peak_planned_tokens": 44, "stop_reason": "observation_window_ended",
    }
    phase = {
        "schema": "ora.agent_validation_budget.v1",
        "limits": {"model": None, "tokens": 90, "retrieval": 0,
                   "allow_qwen_text": True, **policy},
        "model_attempts": [{"number": 1, "planned_tokens": 30, "actual_tokens": 8}],
        "retrieval_attempts": [], "planned_tokens": 8,
        "peak_planned_tokens": 30, "stop_reason": None,
    }
    old_row = dict(prior["model_attempts"][0])
    combined = combine_validation_ledgers(prior, phase)
    assert combined["limits"] == prior["limits"]
    assert combined["model_attempts"][0] == old_row
    assert combined["planned_tokens"] == 18
    assert prior["model_attempts"][0] == old_row
    for bad_limits in (
        {key: value for key, value in phase["limits"].items()
         if key != "qwen_estimate_policy"} if native else
        {**phase["limits"], "qwen_estimate_policy": "native_when_supported"},
        {key: value for key, value in phase["limits"].items()
         if key != "allow_qwen_text"},
        {**phase["limits"], "unknown_policy": True},
    ):
        with pytest.raises(StateError):
            combine_validation_ledgers(prior, {**phase, "limits": bad_limits})
