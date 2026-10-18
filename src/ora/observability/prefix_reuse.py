"""Local structural prompt reuse, separate from provider-reported cache usage.

This inspects exact saved request bytes but returns counts only. A shared local
token prefix is not proof that Groq served any input from its private cache.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Iterable

from ora.models.token_estimate import TokenEstimateUnavailable, encode_context_token_ids
from ora.state.store import canonical

SCHEMA = "ora.local_prefix_reuse.v1"
BASIS = "local_o200k_canonical_request_structure.v1"
MAX_REQUEST_BYTES = 1_000_000
_SHA = re.compile(r"[a-f0-9]{64}")
_SCOPE_KEYS = (
    "run_id", "actor_id", "task_id", "provider", "model_id",
    "credential_profile",
)


def _prefix_length(left: str | tuple[int, ...], right: str | tuple[int, ...]) -> int:
    index = 0
    for first, second in zip(left, right):
        if first != second:
            break
        index += 1
    return index


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value)
        return instant if instant.utcoffset() is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _request(candidate: dict) -> tuple[dict, tuple[str, ...], datetime, datetime] | None:
    if not isinstance(candidate, dict):
        return None
    raw = candidate.get("request_bytes")
    digest = candidate.get("request_sha256")
    scope = tuple(candidate.get(key) for key in _SCOPE_KEYS)
    dispatched = _instant(candidate.get("dispatch_created_at"))
    responded = _instant(candidate.get("response_created_at"))
    if (type(raw) is not bytes or not 0 < len(raw) <= MAX_REQUEST_BYTES
            or not isinstance(digest, str) or not _SHA.fullmatch(digest)
            or hashlib.sha256(raw).hexdigest() != digest
            or any(not isinstance(value, str) or not value or len(value) > 256
                   for value in scope)
            or not isinstance(candidate.get("call_id"), str)
            or not candidate["call_id"]
            or dispatched is None or responded is None or responded < dispatched):
        return None
    try:
        body = json.loads(raw)
        messages = body.get("messages") if isinstance(body, dict) else None
        tools = body.get("tools", []) if isinstance(body, dict) else None
        if (not isinstance(body, dict) or canonical(body).encode("utf-8") != raw
                or body.get("model") != candidate["model_id"]
                or not isinstance(messages, list) or not messages
                or not all(isinstance(message, dict)
                           and type(message.get("content")) in {str, type(None)}
                           and "image_url" not in message
                           for message in messages)
                or not isinstance(tools, list)):
            return None
        messages = canonical(messages)
        tool_definitions = canonical(tools)
    except (UnicodeError, ValueError, TypeError, OverflowError):
        return None
    return {"messages": messages, "tools": tool_definitions,
            "has_tools": bool(tools)}, scope, dispatched, responded


def analyze_local_prefix_reuse(requests: Iterable[dict]) -> dict:
    """Compare each eligible captured request with its prior completed peer turn.

    The caller must select exact, terminally captured model intents with valid
    dispatch/response lineage. Candidate keys are ``request_bytes``,
    ``request_sha256``, ``call_id``, ``dispatch_created_at``,
    ``response_created_at``, and the six scope keys above. Only a prior response
    saved before the next dispatch can supply a comparison. Nothing here
    estimates provider cache hits, credits, or latency savings.
    """
    candidates = list(requests)
    candidates.sort(key=lambda item: (
        (_instant(item.get("dispatch_created_at"))
         if isinstance(item, dict) else None)
        or datetime.min.replace(tzinfo=timezone.utc),
        (item.get("call_id") if isinstance(item, dict)
         and isinstance(item.get("call_id"), str) else ""),
    ))
    previous: dict[tuple[str, ...], tuple[dict, datetime, tuple[int, ...] | None]] = {}
    eligible_requests = eligible_pairs = measured_pairs = 0
    unsupported_requests = overlap_skipped_pairs = stable_tools = 0
    repeated_chars = current_chars = repeated_tokens = current_tokens = 0
    stable_tool_tokens = 0
    tokenizer_missing = False
    for candidate in candidates:
        parsed = _request(candidate)
        if parsed is None:
            unsupported_requests += 1
            if isinstance(candidate, dict):
                scope = tuple(candidate.get(key) for key in _SCOPE_KEYS)
                if all(isinstance(value, str) for value in scope):
                    previous.pop(scope, None)
            continue
        request, scope, dispatched, responded = parsed
        eligible_requests += 1
        try:
            message_ids = encode_context_token_ids(request["messages"])
        except TokenEstimateUnavailable:
            message_ids = None
            tokenizer_missing = True
        prior = previous.get(scope)
        if prior is not None:
            previous_request, prior_response, prior_ids = prior
            if prior_response > dispatched:
                overlap_skipped_pairs += 1
            else:
                eligible_pairs += 1
                current_chars += len(request["messages"])
                repeated_chars += _prefix_length(
                    previous_request["messages"], request["messages"])
                if request["tools"] == previous_request["tools"]:
                    stable_tools += 1
                    if request["has_tools"]:
                        try:
                            stable_tool_tokens += len(encode_context_token_ids(request["tools"]))
                        except TokenEstimateUnavailable:
                            tokenizer_missing = True
                if prior_ids is not None and message_ids is not None:
                    measured_pairs += 1
                    current_tokens += len(message_ids)
                    repeated_tokens += _prefix_length(prior_ids, message_ids)
        previous[scope] = request, responded, message_ids
    measured = measured_pairs > 0
    return {
        "schema": SCHEMA, "basis": BASIS,
        "tool_stability_basis": "exact_canonical_tool_definitions_only",
        "status": ("measured" if measured else "tokenizer_unavailable"
                   if eligible_pairs and tokenizer_missing else "request_unavailable"
                   if unsupported_requests and not eligible_requests else "no_eligible_pair"),
        "eligible_requests": eligible_requests,
        "eligible_pairs": eligible_pairs,
        "measured_pairs": measured_pairs,
        "unsupported_requests": unsupported_requests,
        "overlap_skipped_pairs": overlap_skipped_pairs,
        "stable_tool_schema_pairs": stable_tools,
        "repeated_leading_message_characters": repeated_chars,
        "current_message_characters": current_chars,
        "message_prefix_character_fraction": (
            repeated_chars / current_chars if current_chars else None),
        "estimated_repeated_message_tokens": repeated_tokens if measured else None,
        "estimated_current_message_tokens": current_tokens if measured else None,
        "estimated_message_prefix_token_fraction": (
            repeated_tokens / current_tokens if current_tokens else None),
        "estimated_stable_tool_schema_tokens": (
            stable_tool_tokens if eligible_pairs and not tokenizer_missing else None),
    }


def unavailable_local_prefix_reuse() -> dict:
    """Describe a diagnostic without retained exact request bytes."""
    return {**analyze_local_prefix_reuse(()), "status": "request_unavailable"}
