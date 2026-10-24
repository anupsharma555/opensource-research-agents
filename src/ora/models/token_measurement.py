"""Keep local estimates, provider admission demand and reported usage distinct.

Only numeric fields from a matching Groq token-rate error are retained. The
provider's pre-inference admission demand is not prompt usage or a charge.
"""

from __future__ import annotations

import re


def _count(value):
    return value if type(value) is int and value >= 0 else None


def read_token_measurement(receipt: dict | None) -> dict:
    """Read only known numeric measurement fields; legacy records stay unknown."""
    saved = (receipt or {}).get("token_measurement")
    if not isinstance(saved, dict) or saved.get("schema") != "ora.model.token_measurement.v1":
        return {}
    return {key: value for key, value in saved.items() if key in {
        "local_input_estimate", "reported_input_tokens", "provider_admission_tokens",
        "provider_admission_limit_tokens",
    } and _count(value) is not None}


def measure_tokens(*, estimate: dict | None, usage: dict, http_status: int | None,
                   provider_error: dict | None, model: str) -> dict:
    local = _count((estimate or {}).get("tokens"))
    reported = _count(usage.get("prompt_tokens"))
    requested = limit = None
    error = provider_error or {}
    message = error.get("message")
    if (http_status in {413, 429} and error.get("code") == "rate_limit_exceeded"
            and error.get("type") == "tokens" and isinstance(message, str)):
        match = re.search(
            r"^(?:Request too large|Rate limit reached) for model `" + re.escape(model)
            + r"`[^\n]*?on tokens per minute \(TPM\):\s*Limit\s+(\d{1,12}),\s*"
            + r"(?:Used\s+\d{1,12},\s*)?Requested\s+(\d{1,12})(?=[,.\s])",
            message,
        )
        if match and int(match[1]) > 0:
            limit, requested = int(match[1]), int(match[2])
    return {
        "schema": "ora.model.token_measurement.v1",
        "local_input_estimate": local,
        "local_estimate_basis": (estimate or {}).get("basis"),
        "reported_input_tokens": reported,
        "provider_admission_tokens": requested,
        "provider_admission_limit_tokens": limit,
        "provider_admission_status": "reported" if requested is not None else "not_reported",
        "usage_minus_local_estimate": (
            reported - local if reported is not None and local is not None else None
        ),
        "admission_minus_local_estimate": (
            requested - local if requested is not None and local is not None else None
        ),
    }
