"""Read-only, sanitized summaries of explicitly selected local validation cases.

These reports are local presentation data, not mission wire contracts. Provider
usage comes from exact saved receipts or a recognized direct-diagnostic result;
the validation ledger establishes actual HTTP attempts and failed-call unknowns.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ora.state import StateError, StateStore
from ora.state.store import canonical

from .evaluation import read_evaluation_observation
from .export import _task_label
from .metrics import _usage, _usage_total, build_context_metrics, read_cache_observation
from .prefix_reuse import unavailable_local_prefix_reuse

SCHEMA = "ora.local_run_report.v1"
_BUDGET_SCHEMA = "ora.agent_validation_budget.v1"
_SHA = re.compile(r"[a-f0-9]{64}")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
_MAX_JSON_BYTES = 1_000_000


def _json(path: Path) -> dict:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            raise StateError("Local report input is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise StateError("Local report input is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise StateError("Local report input must be an object")
    return value


def _nonnegative(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _time(value: object) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise StateError("Validation ledger has an invalid timestamp")
    return float(value)


def _hash(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _label(value: object, *, model: bool = False) -> str:
    pattern = _MODEL if model else _LABEL
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise StateError("Local report has an invalid model or profile label")
    return value


def _status(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        raise StateError("Local report has an invalid status")
    return value


def _budget(path: Path) -> dict:
    budget = _json(path)
    if (budget.get("schema") != _BUDGET_SCHEMA
            or not isinstance(budget.get("model_attempts"), list)
            or not isinstance(budget.get("retrieval_attempts"), list)):
        raise StateError("Unsupported validation ledger shape")
    _time(budget.get("started_at"))
    for kind in ("model_attempts", "retrieval_attempts"):
        for index, row in enumerate(budget[kind], 1):
            if (not isinstance(row, dict) or row.get("number") != index
                    or not isinstance(row.get("state"), str)
                    or type(row.get("status")) is not int
                    or not 100 <= row["status"] <= 599):
                raise StateError("Unsupported validation attempt shape")
            if kind == "model_attempts" and (
                not isinstance(row.get("request_sha256"), str)
                or not _SHA.fullmatch(row["request_sha256"])
            ):
                raise StateError("Model attempt needs an exact request hash")
            _time(row.get("started_at"))
            if row.get("ended_at") is not None:
                if _time(row["ended_at"]) < _time(row["started_at"]):
                    raise StateError("Validation attempt ends before it starts")
    return budget


def _row(index: int, phase: str, saved: dict, usage: dict | None,
         cache_observation: dict | None = None, response_status: str | None = None) -> dict:
    usage = usage or _usage(None, unresolved=True)
    observed = cache_observation or {"status": "unavailable"}
    return {
        "index": index, "phase": phase, "state": _status(saved["state"]),
        "http_status": saved["status"], "response_status": response_status,
        "http_duration_seconds": (
            round(_time(saved["ended_at"]) - _time(saved["started_at"]), 3)
            if saved.get("ended_at") is not None else None
        ),
        "input_tokens": usage["prompt_tokens"].get("value"),
        "output_tokens": usage["completion_tokens"].get("value"),
        "reasoning_tokens": usage["reasoning"].get("value"),
        "cache_read_tokens": usage["cache_read"].get("value"),
        "cache_observation": observed["status"],
        "usage_status": usage["total_tokens"]["status"],
        "_usage": usage,
    }


def _finalize(*, case_dir: Path, kind: str, task_label: str, profile: str,
              model: str, outcome: str, mission_status: str | None,
              budgets: list[dict], rows: list[dict], captured: int,
              retrieval_recorded: int | None, evidence: str,
              evaluation_elapsed: float | None = None,
              runner_total_elapsed: float | None = None,
              finalization_basis: str | None = None,
              local_prefix_reuse: dict | None = None) -> dict:
    starts = [_time(item["started_at"]) for item in budgets]
    all_attempts = [row for budget in budgets
                    for key in ("model_attempts", "retrieval_attempts")
                    for row in budget[key]]
    ends = [row.get("ended_at") for row in all_attempts]
    elapsed = (max(_time(value) for value in ends) - min(starts)
               if ends and all(value is not None for value in ends) else None)
    if (evaluation_elapsed is not None and elapsed is not None
            and evaluation_elapsed + 0.01 < elapsed):
        raise StateError("Evaluation duration is shorter than its HTTP ledger window")
    durations = [_time(row["ended_at"]) - _time(row["started_at"])
                 for row in all_attempts if row.get("ended_at") is not None]
    model_attempts = sorted((row for budget in budgets for row in budget["model_attempts"]),
                            key=lambda row: row["started_at"])
    between_model_gap = (
        sum(max(0.0, _time(right["started_at"]) - _time(left["ended_at"]))
            for left, right in zip(model_attempts, model_attempts[1:]))
        if all(row.get("ended_at") is not None for row in model_attempts) else None
    )
    observed_window = (evaluation_elapsed if evaluation_elapsed is not None else elapsed)
    timing_basis = ("evaluation_observation_controller" if evaluation_elapsed is not None
                    else "ledger_first_start_to_last_attempt_end")
    totals = _usage_total([{"usage": row["_usage"]} for row in rows])
    cache = totals["cache_fraction"]
    fields = {}
    for source, target in (("prompt_tokens", "input"), ("completion_tokens", "output"),
                           ("reasoning", "reasoning"), ("cache_read", "cache_read")):
        item = totals[source]
        fields[target] = {
            "reported_tokens": item["tokens"],
            "reported_attempts": item["reported_calls"],
            "unknown_attempts": item["unknown_calls"],
            "not_reported_attempts": item["not_reported_calls"],
            "invalid_attempts": item["invalid_calls"],
            "legacy_uncertain_attempts": item["legacy_uncertain_calls"],
        }
    public_rows = [{key: value for key, value in row.items() if key != "_usage"}
                   for row in rows]
    return {
        "schema": SCHEMA,
        "report_id": _hash([SCHEMA, kind, str(case_dir.resolve())])[:16],
        "kind": kind, "task_label": task_label,
        "started_at_utc": datetime.fromtimestamp(min(starts), timezone.utc).isoformat(),
        "profile_label": _label(profile), "model_label": _label(model, model=True),
        "outcome_status": _status(outcome), "mission_status": mission_status,
        "counts": {
            "model_http_attempts": len(rows),
            "model_captured_responses": captured,
            "model_usage_unknown_attempts": sum(
                row["usage_status"] != "reported" for row in public_rows),
            "retrieval_http_attempts": sum(len(item["retrieval_attempts"]) for item in budgets),
            "retrieval_recorded_attempts": retrieval_recorded,
        },
        "usage": {
            **fields,
            "cache_fraction_reported_subset": {
                key: cache[key] for key in (
                    "value", "coverage_status", "covered_calls", "total_calls",
                    "eligible_input_coverage_fraction", "reported_hit_calls",
                    "reported_miss_calls",
                )
            },
            "input_fraction_reported_subset": totals["token_mix"]["input_fraction"],
            "output_fraction_reported_subset": totals["token_mix"]["output_fraction"],
            "reasoning_fraction_reported_subset": totals["reasoning_fraction"]["value"],
        },
        "elapsed_seconds": round(elapsed, 3) if elapsed is not None else None,
        "timing": {
            "observed_window_seconds": (
                round(observed_window, 3) if observed_window is not None else None
            ),
            "observed_window_basis": timing_basis,
            "runner_total_elapsed_seconds": (
                round(runner_total_elapsed, 3) if runner_total_elapsed is not None else None
            ),
            "runner_total_basis": (
                "runner_observation_after_shutdown" if runner_total_elapsed is not None else None
            ),
            "post_controller_finalization_seconds": (
                round(runner_total_elapsed - evaluation_elapsed, 3)
                if runner_total_elapsed is not None and evaluation_elapsed is not None else None
            ),
            "post_controller_finalization_basis": finalization_basis,
            "ledger_window_seconds": round(elapsed, 3) if elapsed is not None else None,
            "first_ledger_started_at_utc": datetime.fromtimestamp(
                min(starts), timezone.utc).isoformat(),
            "last_attempt_ended_at_utc": (datetime.fromtimestamp(
                max(_time(value) for value in ends), timezone.utc).isoformat()
                if elapsed is not None else None),
            "measured_http_seconds_sum": round(sum(durations), 3) if durations else None,
            "measured_http_attempts": len(durations),
            "unmeasured_http_attempts": len(all_attempts) - len(durations),
            "between_model_attempt_gap_seconds_sum": (
                round(between_model_gap, 3) if between_model_gap is not None else None
            ),
        },
        "attempts": public_rows,
        "local_prefix_reuse": local_prefix_reuse or {
            **unavailable_local_prefix_reuse(), "reason": "no_saved_request_bytes",
        },
        "evidence_basis": evidence,
    }


def _runner_timing(observation: dict, controller_elapsed: float) -> tuple[float, str]:
    """Keep the pre-close stop and post-shutdown observation as distinct clocks."""
    total = observation.get("elapsed_seconds")
    if (type(total) not in {int, float} or not math.isfinite(total)
            or total < controller_elapsed):
        raise StateError("Runner observation precedes its saved controller stop")
    has_controller = "controller_elapsed_seconds" in observation
    has_finalization = "post_controller_finalization_seconds" in observation
    if has_controller != has_finalization:
        raise StateError("Runner observation has incomplete timing fields")
    if not has_controller:
        return float(total), "legacy_post_shutdown_minus_controller_stop"
    controller = observation["controller_elapsed_seconds"]
    finalization = observation["post_controller_finalization_seconds"]
    if (type(controller) not in {int, float} or not math.isfinite(controller)
            or controller != controller_elapsed
            or type(finalization) not in {int, float} or not math.isfinite(finalization)
            or finalization < 0 or finalization != total - controller):
        raise StateError("Runner timing disagrees with its saved controller stop")
    return float(total), "explicit_post_controller_to_runner_observation"


def _natural(case_dir: Path) -> dict:
    case = _json(case_dir / "case.json")
    observation = _json(case_dir / "observation.json")
    budget = _budget(case_dir / "budget.json")
    run_id = case.get("run_id")
    if not isinstance(run_id, str) or observation.get("run_id") != run_id:
        raise StateError("Natural case and observation have different runs")
    store = StateStore(case_dir / "state", read_only=True)
    run = store.get("mission_run", run_id)
    if (run is None or run["body"].get("question") != case.get("question")
            or observation.get("mission_status") != run["body"].get("status")):
        raise StateError("Natural case does not match its saved run")
    evaluation = read_evaluation_observation(store, run_id)
    if (evaluation is None or evaluation["ledger_sha256"] != _hash(budget)
            or evaluation["outcome"] != observation.get("outcome")
            or evaluation["model_http_attempts"] != len(budget["model_attempts"])
            or evaluation["retrieval_http_attempts"] != len(budget["retrieval_attempts"])
            or observation.get("model_attempts") != len(budget["model_attempts"])
            or observation.get("retrieval_attempts") != len(budget["retrieval_attempts"])):
        raise StateError("Natural case and exact validation ledger disagree")
    runner_total_elapsed, finalization_basis = _runner_timing(
        observation, evaluation["elapsed_seconds"],
    )
    metrics = build_context_metrics(store, run_id)
    if metrics["mission_id"] != run["body"].get("mission_id"):
        raise StateError("Natural metrics have another mission")
    if any(call["kind"] == "figure" for call in metrics["calls"]):
        raise StateError("Natural case includes figure usage outside this report's scope")
    available = []
    for call in metrics["calls"]:
        if call["kind"] != "model" or call.get("intent_id") is None:
            continue
        receipt = store.get("model_response_receipt", "model-response:" + call["intent_id"])
        available.append((call, receipt))
    rows, captured = [], 0
    for index, saved in enumerate(budget["model_attempts"], 1):
        candidates = [(call, receipt) for call, receipt in available
                      if call["request_sha256"] == saved["request_sha256"]
                      and (saved.get("response_sha256") is None or receipt is not None
                           and receipt["body"].get("response_sha256") == saved["response_sha256"])]
        if saved["status"] == 200:
            if (not isinstance(saved.get("response_sha256"), str)
                    or not _SHA.fullmatch(saved["response_sha256"])
                    or len(candidates) != 1 or candidates[0][0]["status"] != "captured"):
                raise StateError("Successful budget attempt lacks one exact response receipt")
            call, receipt = candidates[0]
            usage = call["usage"]
            if (saved.get("actual_tokens") != usage["total_tokens"].get("value")
                    or receipt["body"].get("request_sha256") != saved["request_sha256"]):
                raise StateError("Budget and response token or request lineage disagree")
            available.remove((call, receipt))
            captured += 1
            rows.append(_row(index, "run", saved, usage, call["cache_observation"],
                             _status(call["response_status"])))
        else:
            if _nonnegative(saved.get("actual_tokens")) is not None:
                raise StateError("Failed budget attempt has unsupported known usage")
            rows.append(_row(index, "run", saved, None))
    if any(call["status"] == "captured" for call, _receipt in available):
        raise StateError("Captured response is absent from the validation ledger")
    return _finalize(
        case_dir=case_dir, kind="natural_agent", task_label=_task_label(case["question"]),
        profile=case.get("credential_profile"), model=case.get("model"),
        outcome=evaluation["outcome"], mission_status=_status(run["body"]["status"]),
        budgets=[budget], rows=rows, captured=captured,
        retrieval_recorded=metrics["retrieval"]["recorded_attempt_count"],
        evidence="exact_run_receipts_and_validation_ledger",
        evaluation_elapsed=evaluation["elapsed_seconds"],
        runner_total_elapsed=runner_total_elapsed,
        finalization_basis=finalization_basis,
        local_prefix_reuse=metrics["local_prefix_reuse"],
    )


def _diagnostic_response(result: dict, saved: dict) -> tuple[dict, dict]:
    if (result.get("request_sha256") != saved.get("request_sha256")
            or not isinstance(saved.get("response_sha256"), str)
            or not _SHA.fullmatch(saved["response_sha256"])
            or result.get("response_sha256") != saved.get("response_sha256")
            or saved.get("status") != 200 or saved.get("state") != "observed"):
        raise StateError("Diagnostic result and HTTP ledger disagree")
    usage = result.get("normalized_usage")
    if not isinstance(usage, dict):
        raise StateError("Diagnostic has no recognized normalized usage")
    total = _nonnegative(usage.get("total_tokens"))
    prompt = _nonnegative(usage.get("prompt_tokens"))
    completion = _nonnegative(usage.get("completion_tokens"))
    if (total is None or prompt is None or completion is None
            or total != prompt + completion or total != saved.get("actual_tokens")):
        raise StateError("Diagnostic usage and HTTP ledger disagree")
    raw_usage = result.get("raw_usage")
    if raw_usage is not None:
        if (not isinstance(raw_usage, dict)
                or any(raw_usage.get(key) != usage[key]
                       for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
                or result.get("raw_prompt_details_present") is not (
                    "prompt_tokens_details" in raw_usage
                )):
            raise StateError("Raw and normalized diagnostic usage disagree")
    body = {"usage": usage, "cache_observation": result.get("cache_observation")}
    observed = read_cache_observation(body)
    return _usage(body, unresolved=False), observed


def _diagnostic(case_dir: Path, continuation_dir: Path | None) -> dict:
    result = _json(case_dir / "RESULT.json")
    budget = _budget(case_dir / "budget.json")
    label = result.get("diagnostic")
    if label == "exact_request_cache_wire_probe":
        if continuation_dir is not None or len(budget["model_attempts"]) != 1:
            raise StateError("Wire probe has an unsupported continuation or attempt count")
        saved = budget["model_attempts"][0]
        usage, observed = _diagnostic_response(result, saved)
        rows = [_row(1, "probe", saved, usage, observed,
                     _status(result.get("outcome")))]
        outcome = "single_response_observed"
        captured = 1
        budgets = [budget]
        task_label = "Cache wire probe"
    elif label == "identical_tool_free_synthetic_pair":
        if continuation_dir is None or not isinstance(result.get("calls"), list):
            raise StateError("Synthetic pair needs its explicit continuation")
        continuation = _json(continuation_dir / "RESULT.json")
        next_budget = _budget(continuation_dir / "budget.json")
        if (continuation.get("diagnostic") != "run_four_continuation_with_existing_adapter"
                or len(budget["model_attempts"]) != 1
                or len(next_budget["model_attempts"]) != 1
                or result["calls"]
                or result.get("diagnostic_error_type") != "HTTPError"
                or budget["model_attempts"][0]["status"] != 403
                or budget["model_attempts"][0].get("actual_tokens") is not None
                or continuation.get("parent_attempts") != 1
                or continuation.get("parent_budget_sha256")
                != hashlib.sha256((case_dir / "budget.json").read_bytes()).hexdigest()
                or result.get("request_sha256") != continuation.get("request_sha256")
                or result.get("request_sha256") != budget["model_attempts"][0].get("request_sha256")):
            raise StateError("Synthetic pair failure and continuation do not match")
        saved = next_budget["model_attempts"][0]
        usage, observed = _diagnostic_response(continuation, saved)
        rows = [_row(1, "initial", budget["model_attempts"][0], None),
                _row(2, "continuation", saved, usage, observed,
                     _status(continuation.get("outcome")))]
        outcome = "incomplete_pair"
        captured = 1
        budgets = [budget, next_budget]
        task_label = "Synthetic exact-repeat check"
    else:
        raise StateError("Unsupported direct diagnostic result shape")
    if any(item["retrieval_attempts"] for item in budgets):
        raise StateError("Direct diagnostic unexpectedly contains retrieval attempts")
    model = budgets[0]["model_attempts"][0].get("model")
    if any(row.get("model") != model for item in budgets for row in item["model_attempts"]):
        raise StateError("Diagnostic attempts use different models")
    return _finalize(
        case_dir=case_dir, kind="api_diagnostic", task_label=task_label,
        profile=result.get("credential_profile"), model=model,
        outcome=outcome, mission_status=None, budgets=budgets, rows=rows,
        captured=captured, retrieval_recorded=None,
        evidence="recognized_direct_result_and_exact_http_ledger",
    )


def load_run_report(
    case_dir: Path, *, kind: Literal["natural_agent", "api_diagnostic"],
    continuation_dir: Path | None = None,
) -> dict:
    """Return a sanitized report from one explicitly selected local case.

    This never scans for cases, writes state, sends a request, or exports traces.
    A direct diagnostic's continuation must be selected explicitly as well.
    """
    case_dir = Path(case_dir)
    if not case_dir.is_dir():
        raise StateError("Selected local case directory is unavailable")
    if kind == "natural_agent":
        if continuation_dir is not None:
            raise StateError("Natural case does not use a diagnostic continuation")
        return _natural(case_dir)
    if kind == "api_diagnostic":
        return _diagnostic(case_dir, Path(continuation_dir) if continuation_dir else None)
    raise StateError("Unknown local report kind")
