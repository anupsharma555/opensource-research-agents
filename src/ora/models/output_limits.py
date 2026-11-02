"""Extract numeric output-minute limits from exact Groq model rejections.

The provider message may contain an organization identifier. Never persist or
return it; only a caller-verified model and the numeric OTPM clause are retained.
"""

from __future__ import annotations

import re

_OTPM = re.compile(
    r"\boutput tokens per minute\s*\(OTPM\)\s*:\s*"
    r"Limit\s+([0-9][0-9,]*)(?:\s*,\s*Used\s+([0-9][0-9,]*))?"
    r"\s*,\s*Requested\s+([0-9][0-9,]*)(?=\D|$)",
    re.IGNORECASE,
)
_MAX_COUNT = 1_000_000_000


def _count(value: str | None, *, positive: bool) -> int | None:
    if value is None:
        return None
    if not re.fullmatch(r"\d{1,3}(?:,\d{3})*|\d+", value):
        return None
    number = int(value.replace(",", ""))
    if number > _MAX_COUNT or (positive and number == 0):
        return None
    return number


def output_limit_observation(
    http_status: object, provider_error: object, requested_model: str,
) -> dict | None:
    """Return an OTPM observation only for this model's exact 429 token error."""
    if (http_status != 429 or type(provider_error) is not dict
            or provider_error.get("type") != "tokens"
            or provider_error.get("code") != "rate_limit_exceeded"
            or not isinstance(requested_model, str) or not requested_model):
        return None
    message = provider_error.get("message")
    if not isinstance(message, str) or len(message) > 2048:
        return None
    if message.count(f"model `{requested_model}`") != 1:
        return None
    match = _OTPM.search(message)
    if match is None or _OTPM.search(message, match.end()) is not None:
        return None
    limit = _count(match.group(1), positive=True)
    used = _count(match.group(2), positive=False)
    requested = _count(match.group(3), positive=True)
    if limit is None or requested is None or (match.group(2) is not None and used is None):
        return None
    return {"scope": "output_tokens_per_minute", "limit_tokens": limit,
            "used_tokens": used, "requested_tokens": requested}
