"""Offline Groq Qwen protocol checks; transport responses are synthetic."""

from __future__ import annotations

import io
import json
import urllib.error
from dataclasses import replace

import pytest

from ora.models import CallConfiguration, GroqSelectedAdapter, ModelPreflightError
from ora.models.profiles import QWEN_MODEL, model_supports_native_browser
from ora.models.token_estimate import (
    QWEN_SURROGATE_BASIS,
    QWEN_TEMPLATE_BASIS,
    TOKENIZER_BASIS,
    TokenEstimateUnavailable,
    estimate_request_tokens,
)

TOOLS = ({"type": "function", "function": {"name": "lookup", "parameters": {
    "type": "object", "properties": {"key": {"type": "string"}},
    "required": ["key"], "additionalProperties": False,
}}},)
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}


def configuration(**changes) -> CallConfiguration:
    return replace(CallConfiguration("qwen-config", "a" * 64, "Use supplied evidence.",
                                     model_id=QWEN_MODEL, include_reasoning=None,
                                     reasoning_format="parsed"), **changes)


def prepare(adapter, config=None, *, phase="collect", messages=None):
    return adapter.prepare(config or configuration(), call_id="call", attempt_id="attempt",
                           task_category="research", phase=phase,
                           messages=messages or [{"role": "user", "content": "Find A"}],
                           tools=TOOLS if phase == "collect" else (),
                           schema_name="answer" if phase == "final" else None,
                           schema=SCHEMA if phase == "final" else None)


def reply(*, model=QWEN_MODEL, content=None, calls=None, reasoning="private trace",
          finish="tool_calls", usage=None):
    return {"id": "chat-qwen", "model": model, "x_groq": {"id": "req-qwen"},
            "choices": [{"message": {"role": "assistant", "content": content,
                                     "reasoning": reasoning, "tool_calls": calls or []},
                         "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 13, "completion_tokens": 17,
                               "total_tokens": 30}}


def tool_call():
    return {"id": "tool-1", "type": "function",
            "function": {"name": "lookup", "arguments": '{"key":"A"}'}}


class Response:
    headers = {"x-ratelimit-remaining-tokens": "123"}

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _max_bytes):
        return self.payload


def test_qwen_text_tools_usage_and_private_history(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    sent = []

    def transport(request, timeout):
        sent.append(json.loads(request.data))
        return Response(reply(calls=[tool_call()]))

    adapter = GroqSelectedAdapter(tmp_path, transport=transport)
    first = prepare(adapter)
    assert [message["role"] for message in first.body["messages"]] == ["system", "user"]
    assert first.manifest()["model_id"] == QWEN_MODEL
    assert first.body["reasoning_format"] == "parsed"
    assert "include_reasoning" not in first.body
    outcome = adapter.invoke(first)
    assert len(sent) == 1 and sent[0] == first.body
    assert outcome.status == "tool_requested"
    assert outcome.returned_model == QWEN_MODEL
    assert outcome.reasoning == "private trace"
    assert outcome.reasoning_status == "returned"
    assert outcome.usage["total_tokens"] == 30
    assert outcome.cache_observation["status"] == "not_reported"
    assert outcome.executable_tool_calls == (tool_call(),)
    history = [{"role": "user", "content": "Find A"}, outcome.assistant_tool_message(),
               outcome.tool_result_message(content='{"answer":"A"}')]
    assert "reasoning" not in history[1]
    history[1]["reasoning"] = "private trace"
    second = prepare(adapter, messages=history)
    assert "reasoning" not in second.body["messages"][-2]


def test_qwen_final_text_and_wrong_returned_model(tmp_path):
    adapter = GroqSelectedAdapter(tmp_path)
    final = prepare(adapter, phase="final")
    assert final.body["response_format"]["type"] == "json_schema"
    assert "tools" not in final.body
    complete = adapter.decode(final, reply(content='{"answer":"A"}', finish="stop",
                                            reasoning=None))
    assert complete.status == "complete" and complete.reasoning_status == "absent"
    mismatch = adapter.decode(final, reply(model="openai/gpt-oss-120b",
                                            content='{"answer":"A"}', finish="stop"))
    assert mismatch.status == "failed" and mismatch.error_kind == "model_mismatch"
    assert mismatch.returned_model == "openai/gpt-oss-120b"
    assert mismatch.executable_tool_calls == ()


@pytest.mark.parametrize("changes", [
    {"include_reasoning": True}, {"reasoning_format": None},
    {"reasoning_format": "raw"}, {"reasoning_effort": "xhigh"},
    {"max_completion_tokens": 16385},
])
def test_qwen_rejects_unsupported_settings_before_transport(tmp_path, changes):
    adapter = GroqSelectedAdapter(tmp_path, transport=lambda *_a, **_k: pytest.fail("transport"))
    with pytest.raises(ModelPreflightError):
        prepare(adapter, configuration(**changes))


def test_qwen_rejects_gpt_oss_native_browser_search(tmp_path):
    adapter = GroqSelectedAdapter(tmp_path)
    with pytest.raises(ModelPreflightError, match="browser_search is unavailable"):
        adapter.prepare(configuration(), call_id="call", attempt_id="attempt",
                        task_category="research", phase="collect",
                        messages=[{"role": "user", "content": "Find A"}],
                        tools=({"type": "browser_search"},))


@pytest.mark.parametrize(("model_id", "expected"), [
    ("openai/gpt-oss-120b", True), (QWEN_MODEL, False), ("unknown", False),
    (None, False),
])
def test_native_browser_eligibility_is_explicit(model_id, expected):
    assert model_supports_native_browser(model_id) is expected


@pytest.mark.parametrize("role", ["system", "developer"])
def test_qwen_rejects_caller_instruction_roles_before_transport(tmp_path, role):
    adapter = GroqSelectedAdapter(tmp_path, transport=lambda *_a, **_k: pytest.fail("transport"))
    with pytest.raises(ModelPreflightError, match="only leading system message"):
        prepare(adapter, messages=[{"role": role, "content": "Second instruction"},
                                   {"role": "user", "content": "Find A"}])


def test_gpt_oss_retains_developer_request_role(tmp_path):
    adapter = GroqSelectedAdapter(tmp_path)
    prepared = prepare(adapter, CallConfiguration("gpt-config", "a" * 64,
                                                  "Use supplied evidence."))
    assert [message["role"] for message in prepared.body["messages"]] == ["developer", "user"]


def test_qwen_error_is_typed_without_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    calls = []

    def transport(_request, timeout):
        calls.append(timeout)
        raise urllib.error.HTTPError("https://api.groq.com", 400, "synthetic", {},
                                     io.BytesIO(b'{"error":{"type":"invalid_request_error",'
                                                b'"message":"synthetic rejection"}}'))

    adapter = GroqSelectedAdapter(tmp_path, transport=transport)
    result = adapter.invoke(prepare(adapter))
    assert len(calls) == 1 and result.status == "failed"
    assert result.error_kind == "http_error" and result.http_status == 400
    assert result.executable_tool_calls == ()


def test_qwen_new_template_estimate_and_old_surrogate_are_replayable(tmp_path):
    prepared = prepare(GroqSelectedAdapter(tmp_path))
    raw = json.dumps(prepared.body, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode()
    estimated = estimate_request_tokens(raw)
    assert estimated.basis == QWEN_TEMPLATE_BASIS
    assert estimated.asset_sha256 is not None
    assert estimated.fallback_reason is None
    assert estimate_request_tokens(raw, basis=QWEN_TEMPLATE_BASIS) == estimated
    historical = estimate_request_tokens(raw, basis=QWEN_SURROGATE_BASIS)
    assert historical.basis == QWEN_SURROGATE_BASIS
    assert historical.asset_sha256 is None
    assert historical.fallback_reason == "qwen_tokenizer_unavailable"
    assert historical.tokens == len(raw) + 128 * (len(prepared.body["messages"])
                                                 + len(prepared.body["tools"]) + 1)
    with pytest.raises(TokenEstimateUnavailable, match="unsupported_request_shape"):
        estimate_request_tokens(raw, basis=TOKENIZER_BASIS)


def test_qwen_name_in_gpt_oss_prompt_does_not_change_estimate_basis(tmp_path):
    adapter = GroqSelectedAdapter(tmp_path)
    prepared = prepare(
        adapter, CallConfiguration("gpt-config", "a" * 64, "Use supplied evidence."),
        messages=[{"role": "user", "content": "Compare qwen/qwen3.8-27b by name"}],
    )
    raw = json.dumps(prepared.body, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode()
    assert estimate_request_tokens(raw).basis == TOKENIZER_BASIS
