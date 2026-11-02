"""Pinned local Qwen template counts for adapter-shaped collection requests."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from ora.models import CallConfiguration, GroqSelectedAdapter
from ora.models import token_estimate as tokens
from ora.models.profiles import QWEN_MODEL
from ora.state.store import canonical

TOOL = {"type": "function", "function": {
    "name": "lookup", "description": "Read a synthetic checked value.",
    "parameters": {"type": "object", "properties": {"key": {"type": "string"}},
                   "required": ["key"], "additionalProperties": False},
}}
FINAL = {"type": "object", "properties": {"answer": {"type": "string"}},
         "required": ["answer"], "additionalProperties": False}


def prepared_body(tmp_path, *, messages: list[dict], phase: str = "collect",
                  reasoning_effort: str = "medium") -> dict:
    adapter = GroqSelectedAdapter(
        tmp_path, transport=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Tokenizer test must not invoke a provider")),
    )
    config = CallConfiguration(
        "synthetic-qwen", "a" * 64, "Use only synthetic source text.",
        model_id=QWEN_MODEL, include_reasoning=None, reasoning_format="parsed",
        reasoning_effort=reasoning_effort, max_completion_tokens=512,
    )
    return adapter.prepare(
        config, call_id="synthetic-call", attempt_id="synthetic-attempt",
        task_category="research", phase=phase, messages=messages,
        tools=(TOOL,) if phase == "collect" else (),
        schema_name="answer" if phase == "final" else None,
        schema=FINAL if phase == "final" else None,
    ).body


def history(*, public_content: str | None = None) -> list[dict]:
    return [
        {"role": "user", "content": "Read Café and 运动."},
        {"role": "assistant", "content": public_content, "tool_calls": [{
            "id": "synthetic-tool-1", "type": "function",
            "function": {"name": "lookup", "arguments": '{"key":"Cafe\\u0301"}'},
        }], "reasoning": "private synthetic reasoning must not replay"},
        {"role": "tool", "name": "lookup", "tool_call_id": "synthetic-tool-1",
         "content": '{"value":"α & β"}'},
        {"role": "user", "content": "What does that value mean?"},
    ]


@pytest.mark.parametrize("public_content", [None, "Public Café 运动 evidence before the call.", ""])
def test_native_basis_counts_full_sequential_history_without_request_mutation(
    tmp_path, public_content,
):
    body = prepared_body(tmp_path, messages=history(public_content=public_content))
    raw = canonical(body).encode()
    before = deepcopy(body)
    result = tokens.estimate_request_tokens(raw)
    assert result.basis == tokens.QWEN_TEMPLATE_BASIS
    assert result.asset_sha256 == tokens.QWEN_ASSET_SHA256
    assert result.fallback_reason is None
    assert tokens.estimate_request_tokens(raw, basis=tokens.QWEN_TEMPLATE_BASIS) == result
    assert tokens.estimate_qwen_stage_tokens(body) == result.tokens
    assert body == before and canonical(body).encode() == raw
    assert "private synthetic reasoning" not in raw.decode()
    if public_content:
        assert public_content in raw.decode()
    assert '"arguments":"{\\"key\\":' in raw.decode()


def test_low_basis_renders_pinned_low_directive_and_preserves_medium_replay(tmp_path):
    from jinja2 import Environment
    from tokenizers import Tokenizer

    medium = prepared_body(tmp_path, messages=history())
    medium_raw = canonical(medium).encode()
    assert tokens.estimate_request_tokens(
        medium_raw, basis=tokens.QWEN_TEMPLATE_BASIS,
    ).tokens == 343
    low = prepared_body(tmp_path, messages=history(), reasoning_effort="low")
    low_raw = canonical(low).encode()
    before = deepcopy(low)
    estimated = tokens.estimate_request_tokens(low_raw)
    assert estimated.basis == tokens.QWEN_TEMPLATE_LOW_BASIS
    assert estimated.asset_sha256 == tokens.QWEN_ASSET_SHA256
    assert tokens.estimate_request_tokens(
        low_raw, basis=tokens.QWEN_TEMPLATE_LOW_BASIS,
    ) == estimated
    assert tokens.estimate_qwen_stage_tokens(low) == estimated.tokens
    assert low == before and canonical(low).encode() == low_raw

    # Render with the same pinned source assets, independently of the
    # estimator's effort selector. The extra line is a template fact, not a
    # claim about Groq's private serialized count.
    template = Environment(autoescape=False).from_string(
        tokens.QWEN_TEMPLATE_PATH.read_text(),
    )
    tokenizer = Tokenizer.from_str(tokens.QWEN_TOKENIZER_PATH.read_text())
    rendered = template.render(
        messages=tokens._qwen_template_messages(low["messages"]),
        tools=tokens._qwen_template_tools(low["tools"]),
        add_generation_prompt=True, enable_thinking=True,
        reasoning_effort="low", raise_exception=lambda reason: (_ for _ in ()).throw(
            ValueError(reason)
        ), add_vision_id=False, preserve_thinking=False,
    )
    assert "Reasoning effort is set to low." in rendered
    assert estimated.tokens == len(tokenizer.encode(
        rendered, add_special_tokens=False,
    ).ids)
    assert estimated.tokens > 343
    assert tokens.estimate_request_tokens(
        medium_raw, basis=tokens.QWEN_TEMPLATE_BASIS,
    ).tokens == 343
    with pytest.raises(tokens.TokenEstimateUnavailable, match="unsupported_settings"):
        tokens.estimate_request_tokens(low_raw, basis=tokens.QWEN_TEMPLATE_BASIS)
    with pytest.raises(tokens.TokenEstimateUnavailable, match="unsupported_settings"):
        tokens.estimate_request_tokens(medium_raw, basis=tokens.QWEN_TEMPLATE_LOW_BASIS)


@pytest.mark.parametrize("effort", ["none", "high"])
def test_other_qwen_efforts_keep_explicit_surrogate_fallback(tmp_path, effort):
    body = prepared_body(tmp_path, messages=history(), reasoning_effort=effort)
    raw = canonical(body).encode()
    fallback = tokens.estimate_request_tokens(raw)
    assert fallback.basis == tokens.QWEN_SURROGATE_BASIS
    assert fallback.fallback_reason == "qwen_template_unsupported_settings"
    with pytest.raises(tokens.TokenEstimateUnavailable, match="unsupported_settings"):
        tokens.estimate_request_tokens(raw, basis=tokens.QWEN_TEMPLATE_LOW_BASIS)
    with pytest.raises(tokens.TokenEstimateUnavailable, match="unsupported_settings"):
        tokens.estimate_request_tokens(raw, basis=tokens.QWEN_TEMPLATE_BASIS)


def test_nfc_normalization_matches_official_tokenizer_for_decomposed_user_text(tmp_path):
    decomposed = prepared_body(tmp_path, messages=[
        {"role": "user", "content": "Compare Cafe\u0301 with checked evidence."},
    ])
    composed = deepcopy(decomposed)
    composed["messages"][-1]["content"] = "Compare Café with checked evidence."
    assert tokens.estimate_request_tokens(canonical(decomposed).encode()).tokens == (
        tokens.estimate_request_tokens(canonical(composed).encode()).tokens
    )


def test_old_surrogate_and_final_schema_fallback_remain_explicit(tmp_path):
    body = prepared_body(tmp_path, messages=history())
    raw = canonical(body).encode()
    old = tokens.estimate_request_tokens(raw, basis=tokens.QWEN_SURROGATE_BASIS)
    assert old.tokens == len(raw) + 128 * (len(body["messages"]) + len(body["tools"]) + 1)
    assert old.asset_sha256 is None
    final = prepared_body(tmp_path, messages=history(), phase="final")
    final_raw = canonical(final).encode()
    fallback = tokens.estimate_request_tokens(final_raw)
    assert fallback.basis == tokens.QWEN_SURROGATE_BASIS
    assert fallback.fallback_reason == "qwen_template_unsupported_settings"
    with pytest.raises(tokens.TokenEstimateUnavailable, match="unsupported_settings"):
        tokens.estimate_request_tokens(final_raw, basis=tokens.QWEN_TEMPLATE_BASIS)


def test_missing_asset_or_bad_tool_arguments_fail_closed_for_new_basis(tmp_path, monkeypatch):
    body = prepared_body(tmp_path, messages=history())
    raw = canonical(body).encode()
    low_raw = canonical(prepared_body(
        tmp_path, messages=history(), reasoning_effort="low",
    )).encode()
    monkeypatch.setattr(tokens, "QWEN_TOKENIZER_PATH", tmp_path / "missing-tokenizer.json")
    auto = tokens.estimate_request_tokens(raw)
    assert auto.basis == tokens.QWEN_SURROGATE_BASIS
    assert auto.fallback_reason == "qwen_asset_missing"
    with pytest.raises(tokens.TokenEstimateUnavailable, match="qwen_asset_missing"):
        tokens.estimate_request_tokens(raw, basis=tokens.QWEN_TEMPLATE_BASIS)
    assert tokens.estimate_request_tokens(low_raw).fallback_reason == "qwen_asset_missing"
    with pytest.raises(tokens.TokenEstimateUnavailable, match="qwen_asset_missing"):
        tokens.estimate_request_tokens(low_raw, basis=tokens.QWEN_TEMPLATE_LOW_BASIS)
    monkeypatch.undo()
    bad = deepcopy(body)
    bad["messages"][2]["tool_calls"][0]["function"]["arguments"] = '{"key":"a","key":"b"}'
    bad_raw = canonical(bad).encode()
    assert tokens.estimate_request_tokens(bad_raw).basis == tokens.QWEN_SURROGATE_BASIS
    with pytest.raises(tokens.TokenEstimateUnavailable, match="unsupported_shape"):
        tokens.estimate_request_tokens(bad_raw, basis=tokens.QWEN_TEMPLATE_BASIS)


@pytest.mark.parametrize("arguments", [
    '{"key":NaN}', '{"key":Infinity}', '{"key":-Infinity}',
    '{"key":{"nested":[1e999]}}',
])
def test_nonfinite_nested_tool_arguments_never_get_native_basis(tmp_path, arguments):
    body = prepared_body(tmp_path, messages=history())
    body["messages"][2]["tool_calls"][0]["function"]["arguments"] = arguments
    raw = canonical(body).encode()
    fallback = tokens.estimate_request_tokens(raw)
    assert fallback.basis == tokens.QWEN_SURROGATE_BASIS
    assert fallback.fallback_reason == "qwen_template_unsupported_shape"
    with pytest.raises(tokens.TokenEstimateUnavailable, match="unsupported_shape"):
        tokens.estimate_request_tokens(raw, basis=tokens.QWEN_TEMPLATE_BASIS)


def test_context_fragment_and_partial_stage_are_bounded_diagnostics(tmp_path):
    body = prepared_body(tmp_path, messages=history(public_content="Public tool preface 🧠."))
    fragment = canonical(body["messages"][1:])
    assert tokens.estimate_qwen_context_tokens(fragment) > 0
    with pytest.raises(tokens.TokenEstimateUnavailable, match="qwen_context_tokenization_failed"):
        tokens.estimate_qwen_context_tokens(json.dumps(body["messages"][1:], indent=2))
    partial = {**body, "messages": [], "tools": []}
    first = tokens.estimate_qwen_stage_tokens(partial)
    partial["messages"] = body["messages"][:1]
    second = tokens.estimate_qwen_stage_tokens(partial)
    partial["tools"] = body["tools"]
    third = tokens.estimate_qwen_stage_tokens(partial)
    assert 0 < first < second < third
    partial["messages"] = body["messages"][:3]
    assistant_prefix = tokens.estimate_qwen_stage_tokens(partial)
    partial["messages"] = body["messages"][:4]
    tool_prefix = tokens.estimate_qwen_stage_tokens(partial)
    assert third < assistant_prefix < tool_prefix
    assert tokens.estimate_qwen_stage_tokens(body) == tokens.estimate_request_tokens(
        canonical(body).encode()
    ).tokens
