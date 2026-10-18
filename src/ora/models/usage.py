"""Validate provider usage without inventing values for absent details."""

from __future__ import annotations

import math
from typing import Any

_MISSING = object()


def normalize_groq_usage(raw: Any) -> dict[str, Any] | None:
    """Return supported Chat Completions usage, retaining reported zero values.

    A missing optional field stays absent. The caller owns the separate
    dispatch-status distinction between an unreported field and unknown usage.
    """
    if not isinstance(raw, dict):
        return None
    result: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = raw.get(key)
        if type(value) is not int or value < 0:
            return None
        result[key] = value
    for key in ("queue_time", "prompt_time", "completion_time", "total_time"):
        value = raw.get(key)
        if type(value) in {int, float} and math.isfinite(value) and value >= 0:
            result[key] = float(value)
    for section, detail, total in (
        ("prompt_tokens_details", "cached_tokens", "prompt_tokens"),
        ("completion_tokens_details", "reasoning_tokens", "completion_tokens"),
    ):
        breakdown = raw.get(section)
        value = breakdown.get(detail) if isinstance(breakdown, dict) else None
        if type(value) is int and 0 <= value <= result[total]:
            result[section] = {detail: value}
    return result


def observe_groq_cache(raw: Any) -> dict[str, str | int]:
    """Classify only Groq's reported cached-input field, without guessing a hit.

    A malformed optional detail does not invalidate otherwise usable token
    totals. No validated usage at all means cache observation is unavailable.
    """
    normalized = normalize_groq_usage(raw)
    if normalized is None:
        return {"status": "unavailable"}
    details = raw.get("prompt_tokens_details", _MISSING)
    if details is _MISSING or details is None:
        return {"status": "not_reported"}
    if not isinstance(details, dict):
        return {"status": "malformed"}
    cached = details.get("cached_tokens", _MISSING)
    if cached is _MISSING:
        return {"status": "not_reported"}
    if type(cached) is not int or not 0 <= cached <= normalized["prompt_tokens"]:
        return {"status": "malformed"}
    return {"status": "reported_hit" if cached else "reported_zero",
            "cached_tokens": cached}
