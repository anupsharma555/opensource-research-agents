"""Offline admission estimator: exact bytes, narrow scope and safe fallback."""

from __future__ import annotations

import json
import math

import pytest

from ora.models import token_estimate
from ora.state.store import canonical


def request_bytes(*, text: str = "Read the checked source.", output_cap: object = 8192,
                  effort: str = "high") -> bytes:
    return canonical({
        "model": "openai/gpt-oss-120b",
        "messages": [
            {"role": "developer", "content": "Work from exact retained evidence."},
            {"role": "user", "content": text},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "read_source_window", "arguments": "{}"},
            }]},
            {"role": "tool", "name": "read_source_window", "tool_call_id": "call-1",
             "content": '{"text":"One checked sentence."}'},
        ],
        "reasoning_effort": effort, "include_reasoning": True,
        "max_completion_tokens": output_cap, "stream": False,
        "tools": [{"type": "function", "function": {
            "name": "publish_result", "description": "Save a result",
            "parameters": {"type": "object", "properties": {}},
        }}],
        "tool_choice": "auto", "parallel_tool_calls": False,
    }).encode()


def _legacy(raw: bytes) -> int:
    return math.ceil(len(raw) / 4 * 1.25)


def test_exact_canonical_request_and_context_text_use_verified_local_asset(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "must-not-be-created"))

    def no_network(*_args, **_kwargs):
        raise AssertionError("Estimator attempted network access")

    monkeypatch.setattr("urllib.request.urlopen", no_network)
    raw = request_bytes(text='研究 🧠 "\\\n" <|start|>')
    estimate = token_estimate.estimate_request_tokens(raw)
    assert estimate.basis == token_estimate.TOKENIZER_BASIS
    assert estimate.asset_sha256 == token_estimate.ASSET_SHA256
    assert estimate.fallback_reason is None
    assert estimate.tokens == token_estimate.estimate_context_tokens(raw.decode())
    assert token_estimate.estimate_context_tokens("<|start|>") > 1
    assert token_estimate.estimate_request_tokens(
        raw, basis=token_estimate.TOKENIZER_BASIS,
    ) == estimate
    assert token_estimate.estimate_request_tokens(
        raw, basis=token_estimate.TOKENIZER_BASIS_V1,
    ).tokens == estimate.tokens + 128
    assert token_estimate.estimate_request_tokens(
        raw, basis=token_estimate.TOKENIZER_BASIS_V2,
    ).tokens == estimate.tokens
    assert token_estimate.estimate_request_tokens(
        raw, basis=token_estimate.TOKENIZER_BASIS_V3,
    ).tokens == estimate.tokens
    assert not (tmp_path / "must-not-be-created").exists()


def test_new_medium_uses_pinned_tokenizer_while_saved_v3_remains_high_only():
    medium = request_bytes(effort="medium")
    current = token_estimate.estimate_request_tokens(medium)
    assert current.basis == token_estimate.TOKENIZER_BASIS
    assert current.fallback_reason is None
    assert current.asset_sha256 == token_estimate.ASSET_SHA256
    assert current.tokens == token_estimate.estimate_context_tokens(medium.decode())
    assert token_estimate.estimate_request_tokens(
        medium, basis=token_estimate.TOKENIZER_BASIS,
    ) == current
    with pytest.raises(token_estimate.TokenEstimateUnavailable,
                       match="unsupported_reasoning_or_stream"):
        token_estimate.estimate_request_tokens(
            medium, basis=token_estimate.TOKENIZER_BASIS_V3,
        )
    historical = request_bytes(effort="high")
    old_count = token_estimate.estimate_request_tokens(
        historical, basis=token_estimate.TOKENIZER_BASIS_V3,
    )
    assert old_count.basis == token_estimate.TOKENIZER_BASIS_V3
    assert old_count.tokens == token_estimate.estimate_request_tokens(historical).tokens


def test_native_unfamiliar_and_noncanonical_requests_keep_legacy_basis():
    raw = request_bytes()
    body = json.loads(raw)
    candidates = []
    candidates.append(raw.replace(b'"model":"openai/gpt-oss-120b"', b'"model":"other"'))
    candidates.append(raw.replace(b'"reasoning_effort":"high"', b'"reasoning_effort":"low"'))
    candidates.append(raw.replace(b'"stream":false', b'"stream":true'))
    candidates.append(b'{"model": "openai/gpt-oss-120b"}')
    body["tools"].append({"type": "browser_search"})
    candidates.append(canonical(body).encode())
    body["tools"].pop()
    body["messages"][2]["content"] = "Some accompanying prose."
    candidates.append(canonical(body).encode())
    body["messages"][2]["content"] = None
    body["tools"][0]["function"]["name"] = "browser_search"
    candidates.append(canonical(body).encode())
    body["tools"][0]["function"]["name"] = "publish_result"
    body["messages"][-1]["extra"] = "unfamiliar"
    candidates.append(canonical(body).encode())
    body["messages"][-1]["role"] = []
    candidates.append(canonical(body).encode())
    for candidate in candidates:
        result = token_estimate.estimate_request_tokens(candidate)
        assert result.basis == token_estimate.LEGACY_BASIS
        assert result.tokens == _legacy(candidate)
        assert result.asset_sha256 is None and result.fallback_reason
        with pytest.raises(token_estimate.TokenEstimateUnavailable):
            token_estimate.estimate_request_tokens(
                candidate, basis=token_estimate.TOKENIZER_BASIS,
            )
        with pytest.raises(token_estimate.TokenEstimateUnavailable):
            token_estimate.estimate_request_tokens(
                candidate, basis=token_estimate.TOKENIZER_BASIS_V1,
            )
    assert token_estimate.estimate_request_tokens(
        raw, basis=token_estimate.LEGACY_BASIS,
    ).tokens == _legacy(raw)


def test_missing_or_corrupt_asset_falls_back_only_for_new_auto_calls(
    tmp_path, monkeypatch,
):
    raw = request_bytes()
    missing = tmp_path / "missing.tiktoken"
    monkeypatch.setattr(token_estimate, "ASSET_PATH", missing)
    auto = token_estimate.estimate_request_tokens(raw)
    assert auto.basis == token_estimate.LEGACY_BASIS
    assert auto.fallback_reason == "asset_missing" and auto.tokens == _legacy(raw)
    with pytest.raises(token_estimate.TokenEstimateUnavailable, match="asset_missing"):
        token_estimate.estimate_request_tokens(raw, basis=token_estimate.TOKENIZER_BASIS)
    with pytest.raises(token_estimate.TokenEstimateUnavailable, match="asset_missing"):
        token_estimate.estimate_request_tokens(raw, basis=token_estimate.TOKENIZER_BASIS_V1)
    with pytest.raises(token_estimate.TokenEstimateUnavailable, match="asset_missing"):
        token_estimate.estimate_context_tokens("source text")
    missing.write_bytes(b"corrupt rank data")
    auto = token_estimate.estimate_request_tokens(raw)
    assert auto.basis == token_estimate.LEGACY_BASIS
    assert auto.fallback_reason == "asset_hash_mismatch"
    with pytest.raises(token_estimate.TokenEstimateUnavailable, match="asset_hash_mismatch"):
        token_estimate.estimate_request_tokens(raw, basis=token_estimate.TOKENIZER_BASIS)


@pytest.mark.parametrize("output_cap", [1, 768, 1536, 2048, 8192, 65536])
def test_bounded_output_caps_count_exact_bytes_without_repricing_old_bases(output_cap):
    raw = request_bytes(output_cap=output_cap)
    current = token_estimate.estimate_request_tokens(raw)
    assert current.basis == token_estimate.TOKENIZER_BASIS
    assert current.fallback_reason is None
    assert current.tokens == token_estimate.estimate_context_tokens(raw.decode())
    assert token_estimate.estimate_request_tokens(
        raw, basis=token_estimate.TOKENIZER_BASIS_V3,
    ).tokens == current.tokens
    for basis, allowance in (
        (token_estimate.TOKENIZER_BASIS_V1, 128),
        (token_estimate.TOKENIZER_BASIS_V2, 0),
    ):
        if output_cap == 8192:
            assert token_estimate.estimate_request_tokens(raw, basis=basis).tokens == (
                current.tokens + allowance
            )
        else:
            with pytest.raises(token_estimate.TokenEstimateUnavailable,
                               match="unsupported_reasoning_or_stream"):
                token_estimate.estimate_request_tokens(raw, basis=basis)
    # A historically saved byte estimate remains reproducible after the fix.
    assert token_estimate.estimate_request_tokens(
        raw, basis=token_estimate.LEGACY_BASIS,
    ).tokens == _legacy(raw)


@pytest.mark.parametrize("output_cap", [True, False, 0, -1, 65537, 8192.0, "2048", None])
def test_invalid_output_caps_keep_fallback_and_fail_strict_replay(output_cap):
    raw = request_bytes(output_cap=output_cap)
    result = token_estimate.estimate_request_tokens(raw)
    assert result.basis == token_estimate.LEGACY_BASIS
    assert result.fallback_reason == "unsupported_completion_budget"
    assert result.tokens == _legacy(raw)
    with pytest.raises(token_estimate.TokenEstimateUnavailable,
                       match="unsupported_completion_budget"):
        token_estimate.estimate_request_tokens(raw, basis=token_estimate.TOKENIZER_BASIS)


def test_v2_keeps_historical_shape_and_asset_failure_behavior(tmp_path, monkeypatch):
    # The old gate used numeric equality. Preserve it for exact old receipts;
    # current adapter and v3 still reject floating-point output settings.
    raw = request_bytes(output_cap=8192.0)
    old = token_estimate.estimate_request_tokens(raw, basis=token_estimate.TOKENIZER_BASIS_V2)
    assert old.tokens == token_estimate.estimate_context_tokens(raw.decode())
    monkeypatch.setattr(token_estimate, "ASSET_PATH", tmp_path / "missing.tiktoken")
    with pytest.raises(token_estimate.TokenEstimateUnavailable, match="asset_missing"):
        token_estimate.estimate_request_tokens(raw, basis=token_estimate.TOKENIZER_BASIS_V2)
