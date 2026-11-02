"""Save a small, exact-run observation when a validation controller stops."""

from __future__ import annotations

import hashlib
import math
import re

from ora.state import StateConflict, StateError, StateStore
from ora.state.store import canonical

KIND = "evaluation_observation"
SCHEMA = "ora.evaluation_observation.v1"
BASIS = "agent_validation_transport_ledger"
CAPTURE_MODE = "ledger_reconciled"
_OUTCOMES = frozenset({
    "completed", "observation_window_ended", "campaign_phase_cap",
    "mission_cancelled", "mission_failed",
    "operator_credential_hold", "long_provider_wait", "preparation_failure",
    "controller_error", "case_closed", "budget_lock_busy",
    "budget_identity_mismatch", "unresolved_attempt", "model_attempt_cap",
    "retrieval_attempt_cap", "token_cap", "reported_token_cap",
    "unknown_model_usage", "model_http_error", "endpoint_not_https",
    "endpoint_invalid", "model_endpoint_mismatch", "model_request_invalid",
    "model_request_mismatch", "response_capture_limit", "uncertain_transport",
    "provider_daily_quota_wait", "other",
})


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _outcome(value: str) -> str:
    if not isinstance(value, str):
        raise StateError("Evaluation outcome must be text")
    if value.startswith("controller_error_"):
        return "controller_error"
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        raise StateError("Evaluation outcome must be a bounded safe label")
    return value if value in _OUTCOMES else "other"


def _validated_token_ceiling_revisions(prior: dict) -> list[dict]:
    """Validate the explicit grant chain already present in a cumulative ledger."""
    revisions = prior.get("token_ceiling_revisions", [])
    if type(revisions) is not list or ("token_ceiling_revisions" in prior and not revisions):
        raise StateError("Continuation has an invalid token ceiling revision history")
    previous = None
    for revision in revisions:
        if (type(revision) is not dict
                or set(revision) != {"schema", "prior_limit_tokens", "additional_tokens",
                                     "new_limit_tokens", "source_ledger_sha256"}
                or revision["schema"] != "ora.validation_token_ceiling_revision.v1"
                or any(type(revision[key]) is not int or revision[key] < 1 for key in (
                    "prior_limit_tokens", "additional_tokens", "new_limit_tokens"))
                or revision["new_limit_tokens"] != (revision["prior_limit_tokens"]
                                                    + revision["additional_tokens"])
                or previous is not None and revision["prior_limit_tokens"] != previous
                or type(revision["source_ledger_sha256"]) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", revision["source_ledger_sha256"])):
            raise StateError("Continuation has an invalid token ceiling revision history")
        previous = revision["new_limit_tokens"]
    if revisions and previous != prior.get("limits", {}).get("tokens"):
        raise StateError("Continuation token ceiling history disagrees with its limit")
    return revisions


def combine_validation_ledgers(
    prior: dict, phase: dict, *, token_ceiling_revision: dict | None = None,
) -> dict:
    """Derive a complete HTTP ledger while retaining each phase's original file."""
    for ledger in (prior, phase):
        if (not isinstance(ledger, dict)
                or ledger.get("schema") != "ora.agent_validation_budget.v1"
                or any(not isinstance(ledger.get(key), list)
                       for key in ("model_attempts", "retrieval_attempts"))
                or any(type(ledger.get(key)) is not int or ledger[key] < 0
                       for key in ("planned_tokens", "peak_planned_tokens"))
                or any(any(type(item) is not dict or item.get("number") != index
                           for index, item in enumerate(ledger[key], 1))
                       for key in ("model_attempts", "retrieval_attempts"))):
            raise StateError("Continuation needs two exact phase ledger shapes")
    prior_limits, phase_limits = prior.get("limits"), phase.get("limits")
    if not isinstance(prior_limits, dict) or not isinstance(phase_limits, dict):
        raise StateError("Continuation needs saved aggregate and phase limits")
    revisions = _validated_token_ceiling_revisions(prior)
    if prior_limits.get("accounting_only") is True:
        if phase_limits != prior_limits or token_ceiling_revision is not None:
            raise StateError("Continuation cannot change the accounting mode or limits")
    else:
        for ledger in (prior, phase):
            attempts = ledger["model_attempts"]
            if (any(type(item.get("planned_tokens")) is not int
                    or item["planned_tokens"] < 0
                    or item.get("actual_tokens") is not None
                    and (type(item["actual_tokens"]) is not int
                         or item["actual_tokens"] < 0)
                    for item in attempts)
                    or sum(item["actual_tokens"] if item.get("actual_tokens") is not None
                           else item["planned_tokens"] for item in attempts)
                    != ledger["planned_tokens"]):
                raise StateError("Continuation cannot lose a measured or unknown-use reservation")
        base = {key: prior_limits.get(key) for key in ("model", "tokens", "retrieval")}
        optional = {"allow_vision", "allow_qwen_text", "qwen_estimate_policy"}
        if (not {"model", "tokens", "retrieval"} <= set(prior_limits)
                or set(prior_limits) - {"model", "tokens", "retrieval", *optional}
                or "allow_qwen_text" in prior_limits
                and prior_limits["allow_qwen_text"] is not True
                or "qwen_estimate_policy" in prior_limits
                and (prior_limits["qwen_estimate_policy"] != "native_when_supported"
                     or prior_limits.get("allow_qwen_text") is not True)
                or type(base["tokens"]) is not int or base["tokens"] < 1
                or type(base["retrieval"]) is not int or base["retrieval"] < 0
                or base["model"] is not None
                and (type(base["model"]) is not int or base["model"] < 1)):
            raise StateError("Continuation needs valid finite aggregate limits")
        effective_tokens = base["tokens"]
        if token_ceiling_revision is not None:
            revision = token_ceiling_revision
            if (type(revision) is not dict
                    or set(revision) != {"schema", "prior_limit_tokens", "additional_tokens",
                                         "new_limit_tokens", "source_ledger_sha256"}
                    or revision.get("schema") != "ora.validation_token_ceiling_revision.v1"
                    or type(revision.get("prior_limit_tokens")) is not int
                    or revision["prior_limit_tokens"] != base["tokens"]
                    or type(revision.get("additional_tokens")) is not int
                    or revision["additional_tokens"] < 1
                    or type(revision.get("new_limit_tokens")) is not int
                    or revision["new_limit_tokens"] != (base["tokens"]
                                                        + revision["additional_tokens"])
                    or revision.get("source_ledger_sha256")
                    != hashlib.sha256(canonical(prior).encode()).hexdigest()):
                raise StateError("Continuation token ceiling increase lacks an exact grant")
            effective_tokens = revision["new_limit_tokens"]
        remaining_tokens = effective_tokens - prior["planned_tokens"]
        remaining_model = (None if base["model"] is None else
                           base["model"] - len(prior["model_attempts"]))
        remaining_retrieval = base["retrieval"] - len(prior["retrieval_attempts"])
        expected = {"model": remaining_model, "tokens": remaining_tokens,
                    "retrieval": remaining_retrieval}
        for key in optional:
            if key in prior_limits:
                expected[key] = prior_limits[key]
        if (remaining_tokens < 1 or remaining_retrieval < 0
                or remaining_model is not None and remaining_model < 1
                or phase_limits != expected
                or remaining_model is not None
                and len(phase["model_attempts"]) > remaining_model
                or len(phase["retrieval_attempts"]) > remaining_retrieval
                or prior["peak_planned_tokens"] > base["tokens"]
                or phase["peak_planned_tokens"] > remaining_tokens):
            raise StateError("Continuation phase does not preserve the finite cumulative limits")
        if (phase["planned_tokens"] > remaining_tokens
                and (not phase.get("stop_reason")
                     or not any(item.get("actual_tokens") is not None
                                and item["actual_tokens"] > item["planned_tokens"]
                                for item in phase["model_attempts"]))):
            raise StateError("Continuation exceeds its finite allowance without an observed overrun")
    merged = {**prior, "stop_reason": phase.get("stop_reason"),
              "planned_tokens": prior["planned_tokens"] + phase["planned_tokens"],
              "peak_planned_tokens": max(
                  prior["peak_planned_tokens"],
                  prior["planned_tokens"] + phase["peak_planned_tokens"],
              )}
    if token_ceiling_revision is not None:
        merged["limits"] = {**prior_limits, "tokens": effective_tokens}
        merged["token_ceiling_revisions"] = [*revisions, token_ceiling_revision]
    for key in ("model_attempts", "retrieval_attempts"):
        merged[key] = [{**item, "number": index} for index, item in enumerate(
            [*prior[key], *phase[key]], 1
        )]
    return merged


def record_evaluation_observation(
    store: StateStore, run_id: str, budget: dict, outcome: str,
    elapsed_seconds: float,
) -> dict:
    """Persist a sanitized controller stop without altering mission completion.

    The ledger hash identifies the pre-close stop-decision snapshot used for
    these attempt counts. Dispatched callbacks may still settle afterward, so
    this is not a final provider-usage or billing receipt. Only the hash, never
    ledger entries or provider details, enters run state.
    """
    run = store.get("mission_run", run_id)
    if run is None or not isinstance(run["body"].get("mission_id"), str):
        raise StateError("Evaluation needs an existing mission run")
    if (not isinstance(budget, dict)
            or budget.get("schema") != "ora.agent_validation_budget.v1"
            or any(not isinstance(budget.get(key), list)
                   for key in ("model_attempts", "retrieval_attempts"))):
        raise StateError("Evaluation needs the saved validation ledger shape")
    if (type(elapsed_seconds) not in {int, float}
            or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0):
        raise StateError("Evaluation elapsed time must be finite and nonnegative")
    try:
        digest = hashlib.sha256(canonical(budget).encode()).hexdigest()
    except (TypeError, ValueError) as exc:
        raise StateError("Evaluation ledger cannot be fingerprinted") from exc
    mission_id = run["body"]["mission_id"]
    record_id = f"evaluation-observation:{run_id}"
    body = {
        "schema": SCHEMA, "basis": BASIS, "mission_id": mission_id,
        "run_id": run_id, "outcome": _outcome(outcome),
        "capture_mode": CAPTURE_MODE,
        "observation_stopped": True,
        "model_http_attempts": len(budget["model_attempts"]),
        "retrieval_http_attempts": len(budget["retrieval_attempts"]),
        "elapsed_seconds": float(elapsed_seconds),
        "ledger_sha256": digest,
    }
    existing = store.get(KIND, record_id)
    if existing is not None:
        if existing["body"] != body:
            raise StateConflict("Evaluation observation already exists with different inputs")
        return existing

    def save(tx):
        prior = tx.get(KIND, record_id)
        if prior is not None:
            if prior["body"] != body:
                raise StateConflict("Evaluation observation changed concurrently")
            return {"observation_ref": _ref(prior)}
        saved = tx.put(KIND, record_id, body)
        return {"observation_ref": _ref(saved)}

    result = store.transact(
        f"evaluation-observation:{run_id}:{digest}",
        {"run_id": run_id, "ledger_sha256": digest, "outcome": body["outcome"]},
        save, mission_id=mission_id,
    )
    return store.get(**result["observation_ref"])


def advance_evaluation_observation(
    store: StateStore, run_id: str, prior_ref: dict, cumulative_budget: dict,
    outcome: str, phase_elapsed_seconds: float,
) -> dict:
    """Explicitly advance one stopped run's safe observation after a copied phase."""
    record_id = f"evaluation-observation:{run_id}"
    if (type(prior_ref) is not dict or set(prior_ref) != {"kind", "id", "version"}
            or prior_ref["kind"] != KIND or prior_ref["id"] != record_id
            or type(prior_ref["version"]) is not int or prior_ref["version"] < 1):
        raise StateError("Continuation needs the exact prior observation reference")
    if (not isinstance(cumulative_budget, dict)
            or cumulative_budget.get("schema") != "ora.agent_validation_budget.v1"
            or any(not isinstance(cumulative_budget.get(key), list)
                   for key in ("model_attempts", "retrieval_attempts"))
            or type(phase_elapsed_seconds) not in {int, float}
            or not math.isfinite(phase_elapsed_seconds) or phase_elapsed_seconds < 0):
        raise StateError("Continuation needs a valid cumulative ledger and phase elapsed time")
    safe_outcome = _outcome(outcome)
    run = store.get("mission_run", run_id)
    prior = store.get(**prior_ref)
    if (run is None or prior is None
            or prior["body"].get("schema") != SCHEMA
            or prior["body"].get("run_id") != run_id
            or prior["body"].get("mission_id") != run["body"].get("mission_id")
            or prior["body"].get("basis") != BASIS
            or prior["body"].get("capture_mode") != CAPTURE_MODE):
        raise StateError("Prior observation does not belong to this exact run")
    previous = prior["body"]
    if (len(cumulative_budget["model_attempts"]) < previous["model_http_attempts"]
            or len(cumulative_budget["retrieval_attempts"])
            < previous["retrieval_http_attempts"]):
        raise StateError("Continuation cannot reduce prior HTTP attempt counts")
    cumulative_digest = hashlib.sha256(canonical(cumulative_budget).encode()).hexdigest()
    body = {**previous,
            "outcome": safe_outcome,
            "model_http_attempts": len(cumulative_budget["model_attempts"]),
            "retrieval_http_attempts": len(cumulative_budget["retrieval_attempts"]),
            "elapsed_seconds": previous["elapsed_seconds"] + float(phase_elapsed_seconds),
            "ledger_sha256": cumulative_digest}
    head = store.get(KIND, record_id)
    if head is not None and head["version"] == prior_ref["version"] + 1 and head["body"] == body:
        return head
    if head != prior:
        raise StateConflict("Continuation observation changed after the pinned prior version")

    def save(tx):
        current = tx.get(KIND, record_id)
        if current != prior:
            raise StateConflict("Continuation observation changed before advancement")
        saved = tx.put(KIND, record_id, body, expected_version=prior_ref["version"])
        return {"observation_ref": _ref(saved)}

    result = store.transact(
        f"evaluation-advance:{run_id}:{prior_ref['version']}:{cumulative_digest}",
        {"prior_ref": prior_ref, "cumulative_ledger_sha256": cumulative_digest,
         "outcome": safe_outcome, "phase_elapsed_seconds": float(phase_elapsed_seconds)},
        save, mission_id=run["body"]["mission_id"],
    )
    return store.get(**result["observation_ref"])


def read_evaluation_observation(store: StateStore, run_id: str) -> dict | None:
    """Return only a valid exact-run controller observation's safe fields."""
    run = store.get("mission_run", run_id)
    saved = store.get(KIND, f"evaluation-observation:{run_id}")
    if run is None or saved is None:
        return None
    body = saved["body"]
    keys = {
        "schema", "basis", "mission_id", "run_id", "outcome",
        "capture_mode", "observation_stopped", "model_http_attempts",
        "retrieval_http_attempts", "elapsed_seconds", "ledger_sha256",
    }
    try:
        safe_outcome = _outcome(body.get("outcome"))
    except StateError:
        return None
    if (set(body) != keys or body["schema"] != SCHEMA or body["basis"] != BASIS
            or body["capture_mode"] != CAPTURE_MODE
            or body["mission_id"] != run["body"].get("mission_id")
            or body["run_id"] != run_id or body["observation_stopped"] is not True
            or safe_outcome != body["outcome"]
            or any(type(body[key]) is not int or body[key] < 0
                   for key in ("model_http_attempts", "retrieval_http_attempts"))
            or type(body["elapsed_seconds"]) not in {int, float}
            or not math.isfinite(body["elapsed_seconds"])
            or body["elapsed_seconds"] < 0
            or not isinstance(body["ledger_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", body["ledger_sha256"])):
        return None
    return {key: body[key] for key in keys}
