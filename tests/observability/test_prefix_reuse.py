"""Structural prefix reuse never becomes a provider cache-hit claim."""

from __future__ import annotations

import hashlib
import json

from ora.models.token_estimate import TokenEstimateUnavailable
from ora.observability.prefix_reuse import (
    analyze_local_prefix_reuse,
    unavailable_local_prefix_reuse,
)
from ora.state.store import canonical


def _candidate(number: int, content: str, *, actor: str = "actor-a",
               task: str = "task-a", provider: str = "groq",
               profile: str = "primary", model: str = "fixture-model",
               tools: list | None = None, dispatch: int | None = None,
               response: int | None = None, tool_choice: str = "auto") -> dict:
    body = {
        "model": model,
        "messages": [{"role": "developer", "content": "Shared synthetic instruction."},
                     {"role": "user", "content": content}],
        "tools": ([{"type": "function", "function": {"name": "synthetic_tool",
                                                  "parameters": {"type": "object"}}}]
                  if tools is None else tools),
        "tool_choice": tool_choice,
    }
    raw = canonical(body).encode()
    return {
        "request_bytes": raw, "request_sha256": hashlib.sha256(raw).hexdigest(),
        "call_id": f"call-{number}", "run_id": "run-a", "actor_id": actor,
        "task_id": task, "provider": provider, "model_id": model,
        "credential_profile": profile,
        "dispatch_created_at": f"2026-10-17T00:00:{dispatch if dispatch is not None else number:02d}+00:00",
        "response_created_at": f"2026-10-17T00:00:{response if response is not None else dispatch if dispatch is not None else number:02d}+00:00",
    }


def test_repeated_prefix_is_local_structure_with_separate_tool_stability():
    first = _candidate(1, "Describe the shared synthetic passage.")
    second = _candidate(2, "Describe the shared synthetic passage. Add a limit.",
                        tool_choice="required")
    result = analyze_local_prefix_reuse([second, first])
    assert result["status"] == "measured"
    assert result["eligible_requests"] == 2 and result["eligible_pairs"] == 1
    assert result["measured_pairs"] == 1
    assert result["estimated_repeated_message_tokens"] > 0
    assert 0 < result["estimated_message_prefix_token_fraction"] < 1
    assert 0 < result["message_prefix_character_fraction"] < 1
    assert result["stable_tool_schema_pairs"] == 1
    assert result["estimated_stable_tool_schema_tokens"] > 0
    assert result["tool_stability_basis"] == "exact_canonical_tool_definitions_only"
    assert "Shared synthetic instruction" not in str(result)
    assert "Describe the shared" not in str(result)
    assert "cache_read" not in str(result)


def test_one_request_and_unavailable_direct_diagnostic_are_not_zero_cache():
    single = analyze_local_prefix_reuse([_candidate(1, "One request.")])
    assert single["status"] == "no_eligible_pair"
    assert single["estimated_repeated_message_tokens"] is None
    assert single["estimated_message_prefix_token_fraction"] is None
    direct = unavailable_local_prefix_reuse()
    assert direct["status"] == "request_unavailable"
    assert direct["eligible_pairs"] == 0
    assert direct["estimated_repeated_message_tokens"] is None


def test_tool_free_requests_have_stable_empty_definitions_without_token_charge():
    first = _candidate(1, "A repeated text prompt", tools=[])
    second = _candidate(2, "A repeated text prompt, continued", tools=[])
    body = json.loads(first["request_bytes"])
    body.pop("tools")
    first["request_bytes"] = canonical(body).encode()
    first["request_sha256"] = hashlib.sha256(first["request_bytes"]).hexdigest()
    result = analyze_local_prefix_reuse([first, second])
    assert result["status"] == "measured"
    assert result["stable_tool_schema_pairs"] == 1
    assert result["estimated_stable_tool_schema_tokens"] == 0


def test_scope_model_profile_provider_and_causal_response_boundary():
    base = _candidate(1, "Same message", response=5)
    overlapping = _candidate(2, "Same message plus work", dispatch=3, response=6)
    later = _candidate(3, "Same message plus work again", dispatch=7, response=8)
    different = [
        _candidate(4, "Same message", actor="actor-b", dispatch=9),
        _candidate(5, "Same message", task="task-b", dispatch=10),
        _candidate(6, "Same message", provider="other", dispatch=11),
        _candidate(7, "Same message", profile="backup", dispatch=12),
        _candidate(8, "Same message", model="other-model", dispatch=13),
    ]
    result = analyze_local_prefix_reuse([*different, later, overlapping, base])
    assert result["eligible_requests"] == 8
    assert result["overlap_skipped_pairs"] == 1
    assert result["eligible_pairs"] == result["measured_pairs"] == 1


def test_invalid_exact_request_breaks_comparison_without_exposing_bytes():
    first = _candidate(1, "Private synthetic input")
    tampered = {**_candidate(2, "Private synthetic input changed"),
                "request_sha256": "0" * 64}
    third = _candidate(3, "Private synthetic input again")
    result = analyze_local_prefix_reuse([first, tampered, third])
    assert result["unsupported_requests"] == 1
    assert result["eligible_requests"] == 2
    assert result["eligible_pairs"] == 0
    assert result["status"] == "no_eligible_pair"
    assert "Private synthetic input" not in str(result)


def test_image_content_and_malformed_sort_id_are_unsupported():
    image = _candidate(1, "Synthetic image question")
    body = json.loads(image["request_bytes"])
    body["messages"][1]["content"] = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    image["request_bytes"] = canonical(body).encode()
    image["request_sha256"] = hashlib.sha256(image["request_bytes"]).hexdigest()
    malformed = {**_candidate(2, "Ordinary text"), "call_id": 2}
    result = analyze_local_prefix_reuse([malformed, image])
    assert result["unsupported_requests"] == 2
    assert result["eligible_requests"] == 0
    assert result["status"] == "request_unavailable"


def test_response_before_pre_http_dispatch_marker_is_not_comparable():
    impossible = _candidate(1, "Synthetic text", dispatch=5, response=4)
    result = analyze_local_prefix_reuse([impossible])
    assert result["unsupported_requests"] == 1
    assert result["eligible_requests"] == 0


def test_tokenizer_unavailable_keeps_character_measure_but_no_token_proxy(monkeypatch):
    def unavailable(_value):
        raise TokenEstimateUnavailable("asset_missing")

    monkeypatch.setattr("ora.observability.prefix_reuse.encode_context_token_ids",
                        unavailable)
    result = analyze_local_prefix_reuse([
        _candidate(1, "Shared text"), _candidate(2, "Shared text extended"),
    ])
    assert result["status"] == "tokenizer_unavailable"
    assert result["eligible_pairs"] == 1 and result["measured_pairs"] == 0
    assert result["message_prefix_character_fraction"] > 0
    assert result["estimated_repeated_message_tokens"] is None
    assert result["estimated_message_prefix_token_fraction"] is None
