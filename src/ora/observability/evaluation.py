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
    "completed", "observation_window_ended", "mission_cancelled", "mission_failed",
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
