"""Local structural prompt reuse, separate from provider-reported cache usage.

This inspects exact saved request bytes but returns counts only. Structural
input totals include canonical messages and tool definitions, not every HTTP
request field or provider token accounting. A shared local token prefix is not
proof that Groq served any input from its private cache.

Changed-tool overlap separately tokenizes each complete named definition. Its
sum is not interchangeable with tokens for the whole JSON tool array and is
never added to the structural reuse numerator.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Iterable

from ora.models.token_estimate import TokenEstimateUnavailable, encode_context_token_ids
from ora.state.store import canonical

SCHEMA = "ora.local_prefix_reuse.v1"
BASIS = "local_o200k_canonical_request_structure.v1"
STRUCTURAL_BASIS = "local_o200k_canonical_messages_and_whole_tool_schema.v1"
TOOL_DEFINITION_OVERLAP_BASIS = (
    "local_o200k_exact_named_individual_tool_definitions_changed_arrays.v1"
)
MAX_REQUEST_BYTES = 1_000_000
_SHA = re.compile(r"[a-f0-9]{64}")
_SCOPE_KEYS = (
    "run_id", "actor_id", "task_id", "provider", "model_id",
    "credential_profile",
)


def _scope(candidate: dict) -> tuple[str, ...] | None:
    values = tuple(candidate.get(key) for key in _SCOPE_KEYS)
    return (values if all(isinstance(value, str) and 0 < len(value) <= 256
                          for value in values) else None)


def _identity(candidate: dict) -> tuple[tuple[str, ...], str] | None:
    scope = _scope(candidate)
    call_id = candidate.get("call_id")
    if (scope is None or not isinstance(call_id, str)
            or not 0 < len(call_id) <= 256):
        return None
    return scope, call_id


def _individual_tool_definitions(
    tools: list,
) -> tuple[dict[str, tuple[str, int]] | None, int | None, str]:
    """Measure exact whole definitions on their own nonadditive token basis."""
    definitions = {}
    total = 0
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if (not isinstance(tool, dict) or tool.get("type") != "function"
                or not isinstance(function, dict)
                or not isinstance(name, str) or not name or name in definitions
                or not isinstance(function.get("parameters"), dict)):
            return None, None, "invalid_or_duplicate_tool_definition"
        encoded = canonical(tool)
        try:
            tokens = len(encode_context_token_ids(encoded))
        except TokenEstimateUnavailable:
            return None, None, "tokenizer_unavailable"
        definitions[name] = encoded, tokens
        total += tokens
    return definitions, total, "available"


def _structural_measurements(
    inputs: list[tuple[dict, tuple[dict, tuple[str, ...], datetime, datetime] | None,
                       tuple[int, ...] | None]],
) -> dict:
    """Count exact local input structure without treating gaps as zero reuse."""
    identities = Counter(identity for candidate, _, _ in inputs
                         if (identity := _identity(candidate)) is not None)
    previous: dict[tuple[str, ...], tuple[str, dict, datetime,
                                           tuple[int, ...] | None, int | None,
                                           dict | None]] = {}
    broken_scopes: set[tuple[str, ...]] = set()
    rows = []
    measured_calls = measured_pairs = unavailable = overlap_skipped = 0
    denominator = repeated = 0
    changed_schema_pairs = changed_schema_unavailable = 0
    changed_schema_denominator = changed_schema_repeated = 0
    for candidate, parsed, message_ids in inputs:
        scope = parsed[1] if parsed is not None else _scope(candidate)
        safe_call_id = (_identity(candidate) or (None, None))[1]
        row = {
            "call_id": safe_call_id,
            "scope": dict(zip(_SCOPE_KEYS, scope)) if scope is not None else None,
            "prior_call_id": None,
            "status": "unavailable", "reason": "invalid_request",
            "message_tokens": None,
            "unchanged_leading_message_tokens": None,
            "tool_schema_tokens": None,
            "unchanged_whole_schema_tokens": None,
            "local_structural_input_tokens": None,
            "repeated_local_structural_input_tokens": None,
            "repeated_input_fraction": None,
            "current_tool_definition_count": None,
            "current_individual_tool_definition_tokens": None,
            "exact_shared_tool_definition_count": None,
            "exact_shared_tool_definition_tokens": None,
            "exact_shared_tool_definition_fraction": None,
            "tool_definition_overlap_status": "not_comparable",
        }
        if parsed is None or scope is None or safe_call_id is None:
            if scope is not None:
                previous.pop(scope, None)
                broken_scopes.add(scope)
            if parsed is not None:
                row["reason"] = "invalid_call_identity"
            unavailable += 1
            rows.append(row)
            continue
        request, _, dispatched, responded = parsed
        if identities[(scope, safe_call_id)] > 1:
            previous.pop(scope, None)
            broken_scopes.add(scope)
            row["reason"] = "duplicate_call_id"
            unavailable += 1
            rows.append(row)
            continue
        tool_tokens = None
        if not request["has_tools"]:
            tool_tokens = 0
        else:
            try:
                tool_tokens = len(encode_context_token_ids(request["tools"]))
            except TokenEstimateUnavailable:
                pass
        row["message_tokens"] = len(message_ids) if message_ids is not None else None
        row["tool_schema_tokens"] = tool_tokens
        definitions, individual_tokens, definition_status = _individual_tool_definitions(
            request["tool_items"]
        )
        row["tool_definition_overlap_status"] = definition_status
        if definitions is not None:
            row["current_tool_definition_count"] = len(definitions)
            row["current_individual_tool_definition_tokens"] = individual_tokens
            row["tool_definition_overlap_status"] = "not_comparable_request"
        if message_ids is not None and tool_tokens is not None:
            row["local_structural_input_tokens"] = len(message_ids) + tool_tokens
        prior = previous.get(scope)
        if prior is not None:
            row["prior_call_id"] = prior[0]
        if message_ids is None or tool_tokens is None:
            row["reason"] = "tokenizer_unavailable"
            unavailable += 1
        elif prior is None:
            if scope in broken_scopes:
                row["reason"] = "prior_request_unavailable"
                unavailable += 1
                broken_scopes.discard(scope)
            else:
                row.update(status="baseline_no_predecessor", reason="first_in_scope",
                           unchanged_leading_message_tokens=0,
                           unchanged_whole_schema_tokens=0,
                           repeated_local_structural_input_tokens=0,
                           repeated_input_fraction=0.0)
                measured_calls += 1
                denominator += row["local_structural_input_tokens"]
                if definitions is not None:
                    row["tool_definition_overlap_status"] = "baseline_no_predecessor"
        elif prior[2] > dispatched:
            row.update(status="overlap_skipped", reason="prior_response_after_dispatch")
            overlap_skipped += 1
            if definitions is not None:
                row["tool_definition_overlap_status"] = "overlap_skipped"
        elif prior[3] is None or prior[4] is None:
            row["reason"] = "prior_tokenizer_unavailable"
            unavailable += 1
        else:
            leading = _prefix_length(prior[3], message_ids)
            stable_tools = tool_tokens if prior[1]["tools"] == request["tools"] else 0
            repeated_input = leading + stable_tools
            total = row["local_structural_input_tokens"]
            row.update(status="measured", reason="comparable_prior_in_scope",
                       unchanged_leading_message_tokens=leading,
                       unchanged_whole_schema_tokens=stable_tools,
                       repeated_local_structural_input_tokens=repeated_input,
                       repeated_input_fraction=repeated_input / total if total else None)
            measured_calls += 1
            measured_pairs += 1
            denominator += total
            repeated += repeated_input
            if prior[1]["tools"] != request["tools"]:
                if definitions is None or prior[5] is None:
                    row["tool_definition_overlap_status"] = (
                        definition_status if definitions is None
                        else "prior_tool_definitions_unavailable"
                    )
                    changed_schema_unavailable += 1
                else:
                    shared = [tokens for name, (encoded, tokens) in definitions.items()
                              if name in prior[5] and prior[5][name][0] == encoded]
                    shared_tokens = sum(shared)
                    row["exact_shared_tool_definition_count"] = len(shared)
                    row["exact_shared_tool_definition_tokens"] = shared_tokens
                    row["exact_shared_tool_definition_fraction"] = (
                        shared_tokens / individual_tokens if individual_tokens else None
                    )
                    row["tool_definition_overlap_status"] = "measured_changed_schema"
                    changed_schema_pairs += 1
                    changed_schema_denominator += individual_tokens
                    changed_schema_repeated += shared_tokens
            elif definitions is not None and prior[5] is not None:
                row["tool_definition_overlap_status"] = "unchanged_whole_schema"
        previous[scope] = (
            safe_call_id, request, responded, message_ids, tool_tokens, definitions,
        )
        rows.append(row)
    coverage = ("unavailable" if not measured_calls else
                "partial" if unavailable or overlap_skipped else
                "baseline_only" if not measured_pairs else "complete")
    return {
        "structural_basis": STRUCTURAL_BASIS,
        "tool_definition_overlap_basis": TOOL_DEFINITION_OVERLAP_BASIS,
        "structural_scope_keys": list(_SCOPE_KEYS),
        "structural_input_calls": rows,
        "structural_candidate_calls": len(rows),
        "structural_measured_calls": measured_calls,
        "structural_measured_followup_pairs": measured_pairs,
        "structural_unavailable_calls": unavailable,
        "structural_overlap_skipped_calls": overlap_skipped,
        "structural_coverage_status": coverage,
        "local_structural_input_tokens": denominator if measured_calls else None,
        "repeated_local_structural_input_tokens": repeated if measured_pairs else None,
        "local_structural_input_reuse_fraction": (
            repeated / denominator if measured_pairs and denominator else None),
        "changed_schema_measured_pairs": changed_schema_pairs,
        "changed_schema_unavailable_pairs": changed_schema_unavailable,
        "changed_schema_current_individual_definition_tokens": (
            changed_schema_denominator if changed_schema_pairs else None),
        "changed_schema_exact_shared_definition_tokens": (
            changed_schema_repeated if changed_schema_pairs else None),
        "changed_schema_exact_shared_definition_fraction": (
            changed_schema_repeated / changed_schema_denominator
            if changed_schema_pairs and changed_schema_denominator else None),
    }


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
            "tool_items": tools, "has_tools": bool(tools)}, scope, dispatched, responded


def analyze_local_prefix_reuse(requests: Iterable[dict]) -> dict:
    """Compare each eligible captured request with its prior completed peer turn.

    The caller must select exact, terminally captured model intents with valid
    dispatch/response lineage. Candidate keys are ``request_bytes``,
    ``request_sha256``, ``call_id``, ``dispatch_created_at``,
    ``response_created_at``, and the six scope keys above. Only a prior response
    saved before the next dispatch can supply a comparison. The structural
    denominator includes first-in-scope requests at zero local reuse; missing
    and overlapping comparisons are excluded and lower coverage. A baseline
    does not establish a provider cold-cache miss. Nothing here estimates
    provider cache hits, credits, or latency savings.
    """
    candidates = list(requests)
    candidates.sort(key=lambda item: (
        (_instant(item.get("dispatch_created_at"))
         if isinstance(item, dict) else None)
        or datetime.min.replace(tzinfo=timezone.utc),
        (item.get("call_id") if isinstance(item, dict)
         and isinstance(item.get("call_id"), str) else ""),
    ))
    identities = Counter(identity for candidate in candidates
                         if isinstance(candidate, dict)
                         and (identity := _identity(candidate)) is not None)
    previous: dict[tuple[str, ...], tuple[dict, datetime, tuple[int, ...] | None]] = {}
    eligible_requests = eligible_pairs = measured_pairs = 0
    unsupported_requests = overlap_skipped_pairs = stable_tools = 0
    repeated_chars = current_chars = repeated_tokens = current_tokens = 0
    stable_tool_tokens = 0
    tokenizer_missing = False
    structural_inputs = []
    for candidate in candidates:
        parsed = _request(candidate)
        identity = _identity(candidate) if isinstance(candidate, dict) else None
        if parsed is None or identity is None or identities[identity] > 1:
            structural_inputs.append((candidate if isinstance(candidate, dict) else {},
                                      parsed, None))
            unsupported_requests += 1
            if isinstance(candidate, dict):
                scope = _scope(candidate)
                if scope is not None:
                    previous.pop(scope, None)
            continue
        request, scope, dispatched, responded = parsed
        eligible_requests += 1
        try:
            message_ids = encode_context_token_ids(request["messages"])
        except TokenEstimateUnavailable:
            message_ids = None
            tokenizer_missing = True
        structural_inputs.append((candidate, parsed, message_ids))
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
        **_structural_measurements(structural_inputs),
    }


def unavailable_local_prefix_reuse() -> dict:
    """Describe a diagnostic without retained exact request bytes."""
    return {**analyze_local_prefix_reuse(()), "status": "request_unavailable"}
