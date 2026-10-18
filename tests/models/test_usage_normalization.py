"""Provider usage validation preserves optional-field absence and zero."""

import json

import pytest

from ora.models import CallConfiguration, GroqSelectedAdapter
from ora.models.usage import normalize_groq_usage, observe_groq_cache


def test_groq_usage_keeps_reported_zero_and_valid_breakdowns():
    raw = {
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 0, "other": "ignored"},
        "completion_tokens_details": {"reasoning_tokens": 5},
        "prompt_time": 0.1,
    }
    assert normalize_groq_usage(raw) == {
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 5},
        "prompt_time": 0.1,
    }
    assert normalize_groq_usage({
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
    }) == {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


def test_groq_usage_rejects_invalid_totals_and_skips_invalid_optional_details():
    assert normalize_groq_usage({"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 1}) is None
    assert normalize_groq_usage({
        "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
        "prompt_tokens_details": {"cached_tokens": 11},
        "completion_tokens_details": {"reasoning_tokens": -1},
    }) == {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}


@pytest.mark.parametrize(("detail", "expected"), [
    ({"cached_tokens": 40}, {"status": "reported_hit", "cached_tokens": 40}),
    ({"cached_tokens": 0}, {"status": "reported_zero", "cached_tokens": 0}),
    ({}, {"status": "not_reported"}),
    ({"cached_tokens": None}, {"status": "malformed"}),
    ({"cached_tokens": True}, {"status": "malformed"}),
    ({"cached_tokens": 101}, {"status": "malformed"}),
    ({"cached_tokens": -1}, {"status": "malformed"}),
])
def test_cache_observation_distinguishes_reported_absent_and_malformed(detail, expected):
    raw = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
           "prompt_tokens_details": detail}
    assert observe_groq_cache(raw) == expected


def test_cache_observation_unavailable_without_valid_usage():
    assert observe_groq_cache(None) == {"status": "unavailable"}
    assert observe_groq_cache({"prompt_tokens": True}) == {"status": "unavailable"}
    assert observe_groq_cache({
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
    }) == {"status": "not_reported"}
    assert observe_groq_cache({
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "prompt_tokens_details": None,
    }) == {"status": "not_reported"}


@pytest.mark.parametrize(("cached", "expected_observation"), [
    (40, {"status": "reported_hit", "cached_tokens": 40}),
    (0, {"status": "reported_zero", "cached_tokens": 0}),
    (None, {"status": "not_reported"}),
    ("invalid", {"status": "malformed"}),
])
def test_streamed_final_usage_keeps_cache_field_quality(
    tmp_path, cached, expected_observation,
):
    adapter = GroqSelectedAdapter(tmp_path)
    settings = CallConfiguration("synthetic-role", "a" * 64,
                                 "Synthetic instructions.", stream=True)
    tools = ({"type": "function", "function": {
        "name": "read", "description": "Read fixture", "parameters": {
            "type": "object", "properties": {"key": {"type": "string"}},
            "required": ["key"], "additionalProperties": False,
        },
    }},)
    prepared = adapter.prepare(
        settings, call_id="call-one", attempt_id="attempt-one",
        task_category="collect", phase="collect",
        messages=[{"role": "user", "content": "Synthetic question"}], tools=tools,
    )
    assert prepared.body["stream_options"] == {"include_usage": True}
    provider_usage = {"prompt_tokens": 100, "completion_tokens": 20,
                      "total_tokens": 120,
                      "completion_tokens_details": {"reasoning_tokens": 5}}
    if cached is not None:
        provider_usage["prompt_tokens_details"] = {"cached_tokens": cached}
    chunks = [
        {"model": "openai/gpt-oss-120b", "choices": [{
            "delta": {"content": "Synthetic answer", "reasoning": "private reasoning"},
            "finish_reason": "stop",
        }]},
        {"model": "openai/gpt-oss-120b", "choices": [], "usage": provider_usage},
    ]
    raw = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
           + "data: [DONE]\n\n").encode()
    outcome = adapter.decode_stream(prepared, raw)
    assert outcome.status == "complete"
    expected_usage = {
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "completion_tokens_details": {"reasoning_tokens": 5},
    }
    if type(cached) is int:
        expected_usage["prompt_tokens_details"] = {"cached_tokens": cached}
    assert outcome.usage == expected_usage
    assert outcome.cache_observation == expected_observation
    assert "private reasoning" not in repr(outcome)
