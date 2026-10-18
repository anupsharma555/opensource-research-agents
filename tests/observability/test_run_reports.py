"""Local reports preserve exact attempt lineage and unknown provider usage."""

from __future__ import annotations

import hashlib
import json

import pytest

from ora.observability.evaluation import record_evaluation_observation
from ora.observability.run_reports import load_run_report
from ora.state import StateError, StateStore


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _put(store, kind, identifier, body):
    return store.transact(
        f"seed:{kind}:{identifier}", {}, lambda tx: tx.put(kind, identifier, body),
    )


def _ref(record):
    return {key: record[key] for key in ("kind", "id", "version")}


def _budget_row(number, status, *, request="a" * 64, response="b" * 64,
                actual_tokens=None, start=1001.0, end=1002.0):
    return {"number": number, "state": "observed", "status": status,
            "started_at": start, "ended_at": end,
            "request_sha256": request, "response_sha256": response,
            "actual_tokens": actual_tokens, "model": "openai/gpt-oss-120b"}


def _budget(rows):
    return {"schema": "ora.agent_validation_budget.v1", "started_at": 1000.0,
            "model_attempts": rows, "retrieval_attempts": [], "stop_reason": None}


def test_natural_report_joins_saved_receipt_to_exact_validation_ledger(tmp_path):
    case_dir = tmp_path / "natural"
    store = StateStore(case_dir / "state")
    question = "Check synthetic values https://private.example token=secret-value"
    _put(store, "mission_run", "run-a", {
        "mission_id": "mission-a", "question": question, "status": "completed",
    })
    _put(store, "agent_instance", "actor-a", {
        "mission_id": "mission-a", "run_id": "run-a",
    })
    call = _put(store, "agent_call", "call-a", {
        "mission_id": "mission-a", "instance_id": "actor-a", "task_id": "task-a",
    })
    intent = _put(store, "model_request_intent", "intent-a", {
        "mission_id": "mission-a", "call_ref": _ref(call),
        "request_sha256": "a" * 64,
    })
    _put(store, "model_response_receipt", "model-response:intent-a", {
        "mission_id": "mission-a", "intent_ref": _ref(intent),
        "request_sha256": "a" * 64, "response_sha256": "b" * 64,
        "status": "complete", "usage": {
            "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 40},
        }, "cache_observation": {"status": "reported_hit", "cached_tokens": 40},
    })
    budget = _budget([_budget_row(1, 200, actual_tokens=150)])
    _save(case_dir / "budget.json", budget)
    _save(case_dir / "case.json", {
        "run_id": "run-a", "question": question, "model": "openai/gpt-oss-120b",
        "credential_profile": "synthetic_profile",
    })
    _save(case_dir / "observation.json", {
        "run_id": "run-a", "outcome": "completed", "mission_status": "completed",
        "model_attempts": 1, "retrieval_attempts": 0, "elapsed_seconds": 2.5,
        "controller_elapsed_seconds": 2.0,
        "post_controller_finalization_seconds": 0.5,
    })
    record_evaluation_observation(store, "run-a", budget, "completed", 2)

    report = load_run_report(case_dir, kind="natural_agent")
    assert report["schema"] == "ora.local_run_report.v1"
    assert report["counts"]["model_http_attempts"] == 1
    assert report["counts"]["model_captured_responses"] == 1
    assert report["usage"]["input"]["reported_tokens"] == 100
    assert report["usage"]["cache_read"]["reported_tokens"] == 40
    assert report["usage"]["cache_fraction_reported_subset"]["value"] == 0.4
    assert report["usage"]["cache_fraction_reported_subset"]["coverage_status"] == "complete"
    assert report["elapsed_seconds"] == 2
    assert report["timing"]["observed_window_basis"] == "evaluation_observation_controller"
    assert report["timing"]["observed_window_seconds"] == 2
    assert report["timing"]["runner_total_elapsed_seconds"] == 2.5
    assert report["timing"]["post_controller_finalization_seconds"] == 0.5
    assert report["timing"]["post_controller_finalization_basis"] == (
        "explicit_post_controller_to_runner_observation"
    )
    assert report["timing"]["measured_http_seconds_sum"] == 1
    assert report["attempts"][0]["http_duration_seconds"] == 1
    assert report["attempts"][0]["cache_observation"] == "reported_hit"
    assert "private.example" not in json.dumps(report)
    assert "secret-value" not in json.dumps(report)
    assert "response_sha256" not in json.dumps(report)

    observation = json.loads((case_dir / "observation.json").read_text())
    del observation["controller_elapsed_seconds"]
    del observation["post_controller_finalization_seconds"]
    _save(case_dir / "observation.json", observation)
    legacy = load_run_report(case_dir, kind="natural_agent")
    assert legacy["timing"]["runner_total_elapsed_seconds"] == 2.5
    assert legacy["timing"]["post_controller_finalization_basis"] == (
        "legacy_post_shutdown_minus_controller_stop"
    )
    observation["elapsed_seconds"] = 1.9
    _save(case_dir / "observation.json", observation)
    with pytest.raises(StateError, match="precedes its saved controller stop"):
        load_run_report(case_dir, kind="natural_agent")
    observation["elapsed_seconds"] = 2.5
    observation["controller_elapsed_seconds"] = 2.0
    observation["post_controller_finalization_seconds"] = 0.4
    _save(case_dir / "observation.json", observation)
    with pytest.raises(StateError, match="timing disagrees"):
        load_run_report(case_dir, kind="natural_agent")
    observation["post_controller_finalization_seconds"] = 0.5
    _save(case_dir / "observation.json", observation)

    budget["model_attempts"][0]["actual_tokens"] = 151
    _save(case_dir / "budget.json", budget)
    with pytest.raises(StateError, match="ledger disagree"):
        load_run_report(case_dir, kind="natural_agent")


def test_direct_wire_probe_keeps_reported_zero_distinct_from_unknown(tmp_path):
    case_dir = tmp_path / "probe"
    budget = _budget([_budget_row(1, 200, actual_tokens=120)])
    _save(case_dir / "budget.json", budget)
    _save(case_dir / "RESULT.json", {
        "diagnostic": "exact_request_cache_wire_probe",
        "credential_profile": "synthetic_profile",
        "request_sha256": "a" * 64, "response_sha256": "b" * 64,
        "outcome": "complete", "normalized_usage": {
            "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 0},
        }, "cache_observation": {"status": "reported_zero", "cached_tokens": 0},
    })
    report = load_run_report(case_dir, kind="api_diagnostic")
    assert report["outcome_status"] == "single_response_observed"
    assert report["counts"]["model_usage_unknown_attempts"] == 0
    assert report["usage"]["cache_read"]["reported_tokens"] == 0
    assert report["usage"]["cache_fraction_reported_subset"]["value"] == 0
    assert report["usage"]["cache_fraction_reported_subset"]["reported_miss_calls"] == 1
    assert report["attempts"][0]["cache_observation"] == "reported_zero"
    assert report["local_prefix_reuse"]["status"] == "request_unavailable"
    assert report["local_prefix_reuse"]["reason"] == "no_saved_request_bytes"
    assert report["local_prefix_reuse"]["estimated_repeated_message_tokens"] is None
    assert report["timing"]["observed_window_basis"] == "ledger_first_start_to_last_attempt_end"
    assert report["timing"]["observed_window_seconds"] == 2
    assert report["timing"]["measured_http_seconds_sum"] == 1


def test_incomplete_pair_preserves_failed_403_and_single_later_response(tmp_path):
    parent = tmp_path / "pair"
    continuation = parent / "continuation1"
    first_budget = _budget([_budget_row(1, 403, actual_tokens=None)])
    _save(parent / "budget.json", first_budget)
    _save(parent / "RESULT.json", {
        "diagnostic": "identical_tool_free_synthetic_pair",
        "credential_profile": "synthetic_profile", "request_sha256": "a" * 64,
        "diagnostic_error_type": "HTTPError", "calls": [],
    })
    next_budget = _budget([_budget_row(1, 200, actual_tokens=110, start=1100, end=1102)])
    next_budget["started_at"] = 1099
    _save(continuation / "budget.json", next_budget)
    _save(continuation / "RESULT.json", {
        "diagnostic": "run_four_continuation_with_existing_adapter",
        "parent_attempts": 1,
        "parent_budget_sha256": hashlib.sha256((parent / "budget.json").read_bytes()).hexdigest(),
        "request_sha256": "a" * 64, "response_sha256": "b" * 64,
        "outcome": "complete", "normalized_usage": {
            "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
        }, "cache_observation": {"status": "not_reported"},
    })
    report = load_run_report(parent, kind="api_diagnostic", continuation_dir=continuation)
    assert report["outcome_status"] == "incomplete_pair"
    assert report["counts"] == {
        "model_http_attempts": 2, "model_captured_responses": 1,
        "model_usage_unknown_attempts": 1, "retrieval_http_attempts": 0,
        "retrieval_recorded_attempts": None,
    }
    assert report["usage"]["input"]["reported_tokens"] == 100
    assert report["usage"]["input"]["unknown_attempts"] == 1
    assert report["usage"]["cache_read"]["reported_tokens"] is None
    assert report["usage"]["cache_fraction_reported_subset"]["value"] is None
    assert report["local_prefix_reuse"]["status"] == "request_unavailable"
    assert [row["http_status"] for row in report["attempts"]] == [403, 200]
    assert [row["input_tokens"] for row in report["attempts"]] == [None, 100]
    assert report["elapsed_seconds"] == 102
    assert report["timing"]["observed_window_seconds"] == 102
    assert report["timing"]["measured_http_seconds_sum"] == 3
    assert report["timing"]["between_model_attempt_gap_seconds_sum"] == 98
    assert [row["http_duration_seconds"] for row in report["attempts"]] == [1, 2]

    with pytest.raises(StateError, match="explicit continuation"):
        load_run_report(parent, kind="api_diagnostic")
    next_budget["model_attempts"][0]["request_sha256"] = "c" * 64
    _save(continuation / "budget.json", next_budget)
    with pytest.raises(StateError, match="Diagnostic result and HTTP ledger disagree"):
        load_run_report(parent, kind="api_diagnostic", continuation_dir=continuation)
