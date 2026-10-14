"""Offline admission estimator: exact bytes, narrow scope and safe fallback."""

from __future__ import annotations

import json
import math

import pytest

from ora.models import token_estimate
from ora.state.store import canonical


def request_bytes(*, text: str = "Read the checked source.") -> bytes:
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
        "reasoning_effort": "high", "include_reasoning": True,
        "max_completion_tokens": 8192, "stream": False,
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
    assert not (tmp_path / "must-not-be-created").exists()


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
