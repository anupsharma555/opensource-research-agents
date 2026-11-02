"""Keep local estimates, provider admission demand and reported usage distinct.

Only numeric fields from a matching Groq token-rate error are retained. The
provider's pre-inference admission demand is not prompt usage or a charge.
"""

from __future__ import annotations

import re

_COUNT_TEXT = r"[0-9]+(?:,[0-9]{3})*"
_ITPM = re.compile(
    r"\binput tokens per minute\s*\(ITPM\)\s*:\s*"
    rf"Limit\s+({_COUNT_TEXT})(?:\s*,\s*Used\s+({_COUNT_TEXT}))?"
    rf"\s*,\s*Requested\s+({_COUNT_TEXT})(?=[,.\s]|$)",
    re.IGNORECASE,
)
_MAX_PROVIDER_COUNT = 1_000_000_000


def _provider_count(value: str | None, *, positive: bool) -> int | None:
    if value is None or not re.fullmatch(r"\d{1,3}(?:,\d{3})*|\d+", value):
        return None
    count = int(value.replace(",", ""))
    if count > _MAX_PROVIDER_COUNT or positive and count == 0:
        return None
    return count


def input_limit_observation(
    http_status: object, provider_error: object, model: str,
) -> dict | None:
    """Read only a model-matched numeric ITPM refusal, never account text."""
    if (http_status != 413 or type(provider_error) is not dict
            or provider_error.get("type") != "tokens"
            or provider_error.get("code") != "rate_limit_exceeded"
            or not isinstance(model, str) or not model):
        return None
    message = provider_error.get("message")
    prefix = f"Request too large for model `{model}`"
    if (not isinstance(message, str) or len(message) > 2048
            or not message.startswith(prefix)
            or message.count(f"model `{model}`") != 1):
        return None
    match = _ITPM.search(message)
    if (match is None or message.lower().count(
            "input tokens per minute (itpm)") != 1):
        return None
    limit = _provider_count(match.group(1), positive=True)
    used = _provider_count(match.group(2), positive=False) if match.group(2) else None
    requested = _provider_count(match.group(3), positive=True)
    if (limit is None or requested is None or requested <= limit
            or match.group(2) and used is None):
        return None
    return {"scope": "input_tokens_per_minute", "limit_tokens": limit,
            "used_tokens": used, "requested_tokens": requested}


def _count(value):
    return value if type(value) is int and value >= 0 else None


def read_token_measurement(receipt: dict | None) -> dict:
    """Read only known numeric measurement fields; legacy records stay unknown."""
    saved = (receipt or {}).get("token_measurement")
    if not isinstance(saved, dict) or saved.get("schema") != "ora.model.token_measurement.v1":
        return {}
    result = {key: value for key, value in saved.items() if key in {
        "local_input_estimate", "reported_input_tokens", "provider_admission_tokens",
        "provider_admission_limit_tokens",
    } and _count(value) is not None}
    if saved.get("provider_admission_scope") in {
        "combined_tokens_per_minute", "input_tokens_per_minute",
    }:
        result["provider_admission_scope"] = saved["provider_admission_scope"]
    return result


def measure_tokens(*, estimate: dict | None, usage: dict, http_status: int | None,
                   provider_error: dict | None, model: str) -> dict:
    local = _count((estimate or {}).get("tokens"))
    reported = _count(usage.get("prompt_tokens"))
    requested = limit = scope = None
    error = provider_error or {}
    message = error.get("message")
    input_limit = input_limit_observation(http_status, error, model)
    if input_limit is not None:
        limit, requested = input_limit["limit_tokens"], input_limit["requested_tokens"]
        scope = input_limit["scope"]
    elif (http_status in {413, 429} and error.get("code") == "rate_limit_exceeded"
            and error.get("type") == "tokens" and isinstance(message, str)):
        match = re.search(
            r"^(?:Request too large|Rate limit reached) for model `" + re.escape(model)
            + r"`[^\n]*?on tokens per minute \(TPM\):\s*Limit\s+(\d{1,12}),\s*"
            + r"(?:Used\s+\d{1,12},\s*)?Requested\s+(\d{1,12})(?=[,.\s])",
            message,
        )
        if match and int(match[1]) > 0:
            limit, requested = int(match[1]), int(match[2])
            scope = "combined_tokens_per_minute"
    return {
        "schema": "ora.model.token_measurement.v1",
        "local_input_estimate": local,
        "local_estimate_basis": (estimate or {}).get("basis"),
        "reported_input_tokens": reported,
        "provider_admission_tokens": requested,
        "provider_admission_limit_tokens": limit,
        "provider_admission_scope": scope,
        "provider_admission_status": "reported" if requested is not None else "not_reported",
        "usage_minus_local_estimate": (
            reported - local if reported is not None and local is not None else None
        ),
        "admission_minus_local_estimate": (
            requested - local if requested is not None and local is not None else None
        ),
    }
