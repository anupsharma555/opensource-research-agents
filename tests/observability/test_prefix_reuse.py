"""Structural prefix reuse never becomes a provider cache-hit claim."""

from __future__ import annotations

import hashlib
import json

from ora.models.token_estimate import TokenEstimateUnavailable, encode_context_token_ids
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
    assert result["structural_basis"] == "local_o200k_canonical_messages_and_whole_tool_schema.v1"
    assert result["structural_scope_keys"] == [
        "run_id", "actor_id", "task_id", "provider", "model_id", "credential_profile",
    ]
    baseline, followup = result["structural_input_calls"]
    assert baseline["status"] == "baseline_no_predecessor"
    assert baseline["prior_call_id"] is None
    assert baseline["repeated_local_structural_input_tokens"] == 0
    assert baseline["repeated_input_fraction"] == 0
    assert followup["status"] == "measured" and followup["prior_call_id"] == "call-1"
    assert followup["scope"]["credential_profile"] == "primary"
    assert followup["message_tokens"] == len(encode_context_token_ids(
        canonical(json.loads(second["request_bytes"])["messages"])))
    assert followup["tool_schema_tokens"] == len(encode_context_token_ids(
        canonical(json.loads(second["request_bytes"])["tools"])))
    assert followup["unchanged_whole_schema_tokens"] == followup["tool_schema_tokens"]
    assert followup["repeated_local_structural_input_tokens"] == (
        followup["unchanged_leading_message_tokens"]
        + followup["unchanged_whole_schema_tokens"])
    assert result["local_structural_input_tokens"] == sum(
        row["local_structural_input_tokens"] for row in result["structural_input_calls"])
    assert result["local_structural_input_reuse_fraction"] == (
        followup["repeated_local_structural_input_tokens"]
        / result["local_structural_input_tokens"])
    assert result["structural_coverage_status"] == "complete"


def test_one_request_and_unavailable_direct_diagnostic_are_not_zero_cache():
    single = analyze_local_prefix_reuse([_candidate(1, "One request.")])
    assert single["status"] == "no_eligible_pair"
    assert single["estimated_repeated_message_tokens"] is None
    assert single["estimated_message_prefix_token_fraction"] is None
    assert single["structural_coverage_status"] == "baseline_only"
    assert single["structural_input_calls"][0]["repeated_input_fraction"] == 0
    assert single["local_structural_input_tokens"] > 0
    assert single["local_structural_input_reuse_fraction"] is None
    direct = unavailable_local_prefix_reuse()
    assert direct["status"] == "request_unavailable"
    assert direct["eligible_pairs"] == 0
    assert direct["estimated_repeated_message_tokens"] is None
    assert direct["structural_coverage_status"] == "unavailable"
    assert direct["local_structural_input_reuse_fraction"] is None


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
    assert result["structural_input_calls"][1]["tool_schema_tokens"] == 0
    assert result["structural_input_calls"][1]["unchanged_whole_schema_tokens"] == 0


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
    rows = result["structural_input_calls"]
    assert rows[0]["status"] == "baseline_no_predecessor"
    assert rows[1]["status"] == "overlap_skipped"
    assert rows[1]["prior_call_id"] == "call-1"
    assert rows[1]["repeated_input_fraction"] is None
    assert rows[2]["status"] == "measured" and rows[2]["prior_call_id"] == "call-2"
    assert all(row["status"] == "baseline_no_predecessor" for row in rows[3:])
    assert result["structural_coverage_status"] == "partial"
    assert result["structural_overlap_skipped_calls"] == 1
    assert result["changed_schema_measured_pairs"] == 0


def test_identical_requests_in_different_runs_are_separate_baselines():
    first = _candidate(1, "Same input")
    second = _candidate(2, "Same input")
    second["run_id"] = "run-b"
    result = analyze_local_prefix_reuse([first, second])
    assert [row["status"] for row in result["structural_input_calls"]] == [
        "baseline_no_predecessor", "baseline_no_predecessor",
    ]
    assert result["eligible_pairs"] == result["measured_pairs"] == 0
    assert result["changed_schema_measured_pairs"] == 0
    assert result["changed_schema_exact_shared_definition_fraction"] is None


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
    rows = result["structural_input_calls"]
    assert [row["status"] for row in rows] == [
        "baseline_no_predecessor", "unavailable", "unavailable",
    ]
    assert rows[2]["reason"] == "prior_request_unavailable"
    assert result["structural_coverage_status"] == "partial"
    assert result["local_structural_input_reuse_fraction"] is None
    assert result["changed_schema_measured_pairs"] == 0


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
    assert result["structural_measured_calls"] == 0
    assert result["structural_unavailable_calls"] == 2
    assert result["local_structural_input_tokens"] is None


def test_exact_same_messages_and_tools_count_once_with_first_request_denominator():
    first = _candidate(1, "Same complete input")
    second = _candidate(2, "Same complete input", tool_choice="required")
    result = analyze_local_prefix_reuse([first, second])
    baseline, followup = result["structural_input_calls"]
    assert followup["unchanged_leading_message_tokens"] == followup["message_tokens"]
    assert followup["unchanged_whole_schema_tokens"] == followup["tool_schema_tokens"]
    assert followup["repeated_input_fraction"] == 1.0
    assert baseline["local_structural_input_tokens"] == followup[
        "local_structural_input_tokens"]
    assert result["local_structural_input_reuse_fraction"] == .5
    assert result["structural_measured_followup_pairs"] == 1


def test_changed_tool_schema_has_no_whole_schema_reuse():
    first = _candidate(1, "Same message")
    second = _candidate(2, "Same message", tools=[{
        "type": "function", "function": {"name": "other_tool",
                                          "parameters": {"type": "object"}},
    }])
    result = analyze_local_prefix_reuse([first, second])
    followup = result["structural_input_calls"][1]
    assert followup["status"] == "measured"
    assert followup["unchanged_leading_message_tokens"] == followup["message_tokens"]
    assert followup["tool_schema_tokens"] > 0
    assert followup["unchanged_whole_schema_tokens"] == 0
    assert followup["repeated_input_fraction"] < 1


def test_changed_tool_array_reports_exact_definition_overlap_separately():
    first = _candidate(1, "Same message")
    original_tool = json.loads(first["request_bytes"])["tools"][0]
    added_tool = {"type": "function", "function": {
        "name": "added_tool", "parameters": {"type": "object"},
    }}
    second = _candidate(2, "Same message", tools=[original_tool, added_tool])
    result = analyze_local_prefix_reuse([first, second])
    followup = result["structural_input_calls"][1]
    shared_tokens = len(encode_context_token_ids(canonical(original_tool)))
    added_tokens = len(encode_context_token_ids(canonical(added_tool)))

    assert result["tool_definition_overlap_basis"].endswith("changed_arrays.v1")
    assert followup["unchanged_whole_schema_tokens"] == 0
    assert followup["tool_definition_overlap_status"] == "measured_changed_schema"
    assert followup["current_tool_definition_count"] == 2
    assert followup["current_individual_tool_definition_tokens"] == (
        shared_tokens + added_tokens
    )
    assert followup["exact_shared_tool_definition_count"] == 1
    assert followup["exact_shared_tool_definition_tokens"] == shared_tokens
    assert followup["exact_shared_tool_definition_fraction"] == (
        shared_tokens / (shared_tokens + added_tokens)
    )
    assert result["changed_schema_measured_pairs"] == 1
    assert result["changed_schema_current_individual_definition_tokens"] == (
        shared_tokens + added_tokens
    )
    assert result["changed_schema_exact_shared_definition_tokens"] == shared_tokens
    assert result["changed_schema_exact_shared_definition_fraction"] == (
        shared_tokens / (shared_tokens + added_tokens)
    )
    assert result["local_structural_input_reuse_fraction"] < .5
    assert "cache_read" not in str(result)


def test_reordered_exact_definitions_overlap_without_whole_array_equality():
    first_tool = {"type": "function", "function": {
        "name": "first_tool", "parameters": {"type": "object"},
    }}
    second_tool = {"type": "function", "function": {
        "name": "second_tool", "parameters": {"type": "object"},
    }}
    result = analyze_local_prefix_reuse([
        _candidate(1, "Same", tools=[first_tool, second_tool]),
        _candidate(2, "Same", tools=[second_tool, first_tool]),
    ])
    followup = result["structural_input_calls"][1]
    assert followup["unchanged_whole_schema_tokens"] == 0
    assert followup["exact_shared_tool_definition_count"] == 2
    assert followup["exact_shared_tool_definition_fraction"] == 1


def test_duplicate_tool_names_leave_overlap_unavailable_without_double_credit():
    tool = {"type": "function", "function": {
        "name": "same_name", "parameters": {"type": "object"},
    }}
    result = analyze_local_prefix_reuse([
        _candidate(1, "Same", tools=[tool]),
        _candidate(2, "Same", tools=[tool, tool]),
    ])
    followup = result["structural_input_calls"][1]
    assert followup["status"] == "measured"  # Existing structural measure remains.
    assert followup["unchanged_whole_schema_tokens"] == 0
    assert followup["tool_definition_overlap_status"] == (
        "invalid_or_duplicate_tool_definition"
    )
    assert followup["exact_shared_tool_definition_tokens"] is None
    assert result["changed_schema_measured_pairs"] == 0
    assert result["changed_schema_unavailable_pairs"] == 1
    assert result["changed_schema_exact_shared_definition_fraction"] is None


def test_duplicate_call_ids_in_same_scope_are_unavailable_without_poisoning_others():
    first = _candidate(1, "First")
    second = _candidate(2, "Second")
    first["call_id"] = second["call_id"] = "duplicate"
    third = _candidate(3, "Third")
    fourth = _candidate(4, "Third again")
    distinct_scope = _candidate(5, "Independent", actor="actor-b")
    distinct_scope["call_id"] = "duplicate"
    result = analyze_local_prefix_reuse([
        first, second, third, fourth, distinct_scope,
    ])
    rows = result["structural_input_calls"]
    assert [row["status"] for row in rows] == [
        "unavailable", "unavailable", "unavailable", "measured",
        "baseline_no_predecessor",
    ]
    assert [row["reason"] for row in rows[:3]] == [
        "duplicate_call_id", "duplicate_call_id", "prior_request_unavailable",
    ]
    assert rows[3]["prior_call_id"] == "call-3"
    assert rows[4]["prior_call_id"] is None
    assert result["structural_unavailable_calls"] == 3
    assert result["structural_coverage_status"] == "partial"
    assert result["eligible_requests"] == 3
    assert result["eligible_pairs"] == result["measured_pairs"] == 1
    assert result["unsupported_requests"] == 2


def test_duplicate_identity_with_bad_hash_does_not_certify_a_baseline():
    good = _candidate(1, "Synthetic private request")
    tampered = {**_candidate(2, "Different private request"),
                "call_id": good["call_id"], "request_sha256": "0" * 64}
    result = analyze_local_prefix_reuse([good, tampered])
    assert result["eligible_requests"] == 0
    assert result["unsupported_requests"] == 2
    assert result["estimated_message_prefix_token_fraction"] is None
    assert [row["status"] for row in result["structural_input_calls"]] == [
        "unavailable", "unavailable",
    ]
    assert result["structural_input_calls"][0]["reason"] == "duplicate_call_id"
    assert result["local_structural_input_tokens"] is None
    assert "Synthetic private request" not in str(result)


def test_captured_partial_response_still_measures_its_exact_request():
    first = _candidate(1, "Shared request")
    second = _candidate(2, "Shared request")
    second["response_status"] = "partial"
    result = analyze_local_prefix_reuse([first, second])
    assert result["structural_input_calls"][1]["status"] == "measured"
    assert result["structural_input_calls"][1]["repeated_input_fraction"] == 1.0
