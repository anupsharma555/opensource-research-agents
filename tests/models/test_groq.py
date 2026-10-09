"""Synthetic protocol fixtures for the selected Groq model; no provider calls."""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest

from ora.models import (
    CallConfiguration,
    GroqSelectedAdapter,
    ModelPreflightError,
    SourceUse,
)

CONFIG_HASH = "a" * 64
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}
TOOLS = ({"type": "function", "function": {"name": "lookup", "description": "Read fixture",
           "parameters": {"type": "object", "properties": {"key": {"type": "string"}},
                          "required": ["key"], "additionalProperties": False}}},)


def config(budget: int = 8192) -> CallConfiguration:
    return CallConfiguration("role-config-v1", CONFIG_HASH, "Use supplied evidence.",
                             max_completion_tokens=budget)


def source(route: str = "direct") -> SourceUse:
    text = "Synthetic source text."
    return SourceUse("s1", 1, hashlib.sha256(text.encode()).hexdigest(), "permitted", True,
                     text=text, route=route)


def reply(*, content: str | None = None, calls: list | None = None,
          finish: str = "tool_calls", reasoning: str | None = "Returned reasoning") -> dict:
    message = {"role": "assistant", "content": content, "tool_calls": calls or []}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return {"id": "chatcmpl-fixture", "model": "openai/gpt-oss-120b",
            "x_groq": {"id": "req_fixture"},
            "choices": [{"message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 40, "total_tokens": 70,
                      "queue_time": 0.01, "completion_time": 0.2}}


def call(call_id: str, key: str) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": "lookup", "arguments": json.dumps({"key": key})}}


def prepare(adapter: GroqSelectedAdapter, *, messages: list | None = None,
            phase: str = "collect", sources: tuple = ()):
    return adapter.prepare(config(), call_id="c1", attempt_id="a1", task_category="collect",
                           phase=phase, messages=messages or [{"role": "user", "content": "Find A"}],
                           sources=sources, tools=TOOLS if phase == "collect" else (),
                           schema_name="answer" if phase == "final" else None,
                           schema=SCHEMA if phase == "final" else None)


def test_dependent_tool_turns_keep_settings_ids_and_returned_reasoning(tmp_path: Path) -> None:
    adapter = GroqSelectedAdapter(tmp_path)
    initial = prepare(adapter, sources=(source(),))
    first = adapter.decode(initial, reply(calls=[call("tool_1", "A")]))
    assert first.status == "tool_requested"
    assert first.executable_tool_calls[0]["id"] == "tool_1"
    assert first.reasoning == "Returned reasoning"
    assert "Returned reasoning" not in repr(first)
    assert first.reasoning_status == "returned"
    assert first.provider_request_id == "req_fixture"
    assert first.usage["completion_tokens"] == 40
    history = [{"role": "user", "content": "Find A"}, first.assistant_tool_message(),
               first.tool_result_message(content='{"next":"B"}')]
    second_prepared = adapter.prepare(
        config(), call_id="c2", attempt_id="a2", parent_call_id="c1",
        task_category="collect", phase="collect", messages=history, tools=TOOLS,
        sources=(source(),),
    )
    assert second_prepared.body["tools"] == initial.body["tools"]
    assert second_prepared.body["reasoning_effort"] == "high"
    assert second_prepared.body["include_reasoning"] is True
    assert second_prepared.body["parallel_tool_calls"] is False
    assert second_prepared.body["messages"][-2]["reasoning"] == "Returned reasoning"
    assert second_prepared.body["messages"][-1]["tool_call_id"] == "tool_1"
    second = adapter.decode(second_prepared, reply(calls=[call("tool_2", "B")]))
    history += [second.assistant_tool_message(), second.tool_result_message(content='{"value":2}')]
    third_prepared = adapter.prepare(
        config(), call_id="c3", attempt_id="a3", parent_call_id="c2",
        task_category="collect", phase="collect", messages=history, tools=TOOLS,
        sources=(source(),),
    )
    third = adapter.decode(third_prepared, reply(content="Two sources reviewed", finish="stop"))
    assert third.status == "complete"
    assert third.executable_tool_calls == ()
    assert [entry["tool_call_id"] for entry in third_prepared.body["messages"]
            if entry["role"] == "tool"] == ["tool_1", "tool_2"]
    assert [entry["role"] for entry in third_prepared.body["messages"][-4:]] == [
        "assistant", "tool", "assistant", "tool",
    ]
    assert first.request_sha256 != second.request_sha256 != third.request_sha256
    assert first.response_sha256 != third.response_sha256


def test_tool_error_can_lead_to_repaired_choice(tmp_path: Path) -> None:
    adapter = GroqSelectedAdapter(tmp_path)
    first = adapter.decode(prepare(adapter), reply(calls=[call("tool_1", "bad")]))
    messages = [{"role": "user", "content": "Find A"}, first.assistant_tool_message(),
                first.tool_result_message(content='{"is_error":true,"error":"missing"}')]
    next_call = prepare(adapter, messages=messages)
    second = adapter.decode(next_call, reply(calls=[call("tool_2", "good")]))
    assert json.loads(second.executable_tool_calls[0]["function"]["arguments"]) == {
        "key": "good",
    }


@pytest.mark.parametrize("route", ["direct", "cache", "peer"])
@pytest.mark.parametrize("use,allowed", [("restricted", False), ("unknown", False),
                                          ("permitted", False)])
def test_prohibited_source_copy_is_rejected_at_same_boundary(
    tmp_path: Path, route: str, use: str, allowed: bool,
) -> None:
    adapter = GroqSelectedAdapter(tmp_path)
    denied = replace(source(route), use=use, allowed_for_model=allowed)
    with pytest.raises(ModelPreflightError, match="not permitted"):
        prepare(adapter, sources=(denied,))


def test_direct_and_loop_call_share_source_manifest_and_body(tmp_path: Path) -> None:
    adapter = GroqSelectedAdapter(tmp_path)
    a = prepare(adapter, sources=(source(),))
    b = prepare(adapter, sources=(source(),))
    assert a.body == b.body
    assert a.manifest() == b.manifest()
    assert "Synthetic source text." in json.dumps(a.body)
    assert "source_packets" in a.body["messages"][1]["content"]


def test_unknown_copy_metadata_only_policy_never_sends_bytes(tmp_path: Path) -> None:
    adapter = GroqSelectedAdapter(tmp_path)
    metadata = replace(source("peer"), use="unknown", allowed_for_model=False,
                       unknown_policy="allow_metadata_only", text=None)
    prepared = prepare(adapter, sources=(metadata,))
    packet = json.loads(prepared.body["messages"][1]["content"])["source_packets"][0]
    assert set(packet) == {"id", "version", "sha256"}
    assert "Synthetic source text" not in json.dumps(prepared.body)
    assert prepared.evidence_refs[0]["unknown_policy"] == "allow_metadata_only"


@pytest.mark.parametrize("change", [
    {"model_id": "other"}, {"endpoint": "https://other.example/v1"},
    {"reasoning_effort": "medium"}, {"include_reasoning": False},
    {"max_completion_tokens": 0},
])
def test_selected_settings_fail_before_dispatch(tmp_path: Path, change: dict) -> None:
    adapter = GroqSelectedAdapter(tmp_path, transport=lambda *_a, **_kw: pytest.fail("network"))
    with pytest.raises(ModelPreflightError):
        adapter.prepare(replace(config(), **change), call_id="c", attempt_id="a",
                        task_category="collect", phase="collect",
                        messages=[{"role": "user", "content": "Question"}], tools=TOOLS)


def test_unsupported_reasoning_setting_is_known_at_preflight(tmp_path: Path) -> None:
    adapter = GroqSelectedAdapter(tmp_path, transport=lambda *_a, **_kw: pytest.fail("network"))
    with pytest.raises(ModelPreflightError) as rejected:
        adapter.prepare(replace(config(), reasoning_effort="xhigh"),
                        call_id="c", attempt_id="a", task_category="collect", phase="collect",
                        messages=[{"role": "user", "content": "Question"}], tools=TOOLS)
    assert rejected.value.reasoning_status == "unsupported"


def test_phase_and_schema_preflight(tmp_path: Path) -> None:
    adapter = GroqSelectedAdapter(tmp_path)
    with pytest.raises(ModelPreflightError, match="collection cannot"):
        adapter.prepare(config(), call_id="c", attempt_id="a", task_category="collect",
                        phase="collect", messages=[{"role": "user", "content": "Question"}],
                        tools=TOOLS, schema_name="answer", schema=SCHEMA)
    with pytest.raises(ModelPreflightError, match="no tools"):
        adapter.prepare(config(), call_id="c", attempt_id="a", task_category="final",
                        phase="final", messages=[{"role": "user", "content": "Question"}],
                        tools=TOOLS, schema_name="answer", schema=SCHEMA)
    with pytest.raises(ModelPreflightError, match="every property required"):
        adapter.prepare(config(), call_id="c", attempt_id="a", task_category="final",
                        phase="final", messages=[{"role": "user", "content": "Question"}],
                        schema_name="answer", schema={**SCHEMA, "required": []})
    with pytest.raises(ModelPreflightError, match="unqualified validation keyword"):
        adapter.prepare(config(), call_id="c", attempt_id="a", task_category="final",
                        phase="final", messages=[{"role": "user", "content": "Question"}],
                        schema_name="answer", schema={**SCHEMA, "minProperties": 1})
    final = prepare(adapter, phase="final")
    assert final.body["response_format"]["json_schema"]["strict"] is True
    assert "tools" not in final.body
    assert final.body["stream"] is False
    with pytest.raises(ModelPreflightError, match="cannot stream"):
        adapter.prepare(replace(config(), stream=True), call_id="c", attempt_id="a",
                        task_category="final", phase="final",
                        messages=[{"role": "user", "content": "Question"}],
                        schema_name="answer", schema=SCHEMA)


def stream_fixture(*, done: bool, complete_arguments: bool) -> bytes:
    chunks = [
        {"id": "stream-fixture", "model": "openai/gpt-oss-120b", "choices": [{
            "delta": {"reasoning": "Returned stream reasoning", "tool_calls": [{
                "index": 0, "id": "stream-tool", "type": "function",
                "function": {"name": "lookup", "arguments": '{"key":'},
            }]}, "finish_reason": None}]},
        {"id": "stream-fixture", "model": "openai/gpt-oss-120b", "choices": [{
            "delta": {"tool_calls": [{"index": 0, "function": {
                "arguments": '"A"}' if complete_arguments else '"A"',
            }}]}, "finish_reason": "tool_calls" if complete_arguments else None}]},
    ]
    if done:
        chunks.append({"id": "stream-fixture", "model": "openai/gpt-oss-120b",
                       "choices": [], "usage": {"prompt_tokens": 10,
                                                  "completion_tokens": 20,
                                                  "total_tokens": 30}})
    data = b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks)
    return data + (b"data: [DONE]\n\n" if done else b"")


def test_complete_stream_assembles_tool_arguments_before_exposure(tmp_path: Path) -> None:
    adapter = GroqSelectedAdapter(tmp_path)
    prepared = adapter.prepare(replace(config(), stream=True), call_id="c", attempt_id="a",
                               task_category="collect", phase="collect",
                               messages=[{"role": "user", "content": "Find A"}], tools=TOOLS)
    assert prepared.body["stream_options"] == {"include_usage": True}
    result = adapter.decode_stream(prepared, stream_fixture(done=True, complete_arguments=True))
    assert result.status == "tool_requested"
    assert result.reasoning == "Returned stream reasoning"
    assert json.loads(result.executable_tool_calls[0]["function"]["arguments"]) == {
        "key": "A",
    }
    assert result.usage["total_tokens"] == 30


def test_interrupted_stream_keeps_partial_artifact_without_tool_execution(tmp_path: Path) -> None:
    adapter = GroqSelectedAdapter(tmp_path)
    prepared = adapter.prepare(replace(config(), stream=True), call_id="c", attempt_id="a",
                               task_category="collect", phase="collect",
                               messages=[{"role": "user", "content": "Find A"}], tools=TOOLS)
    raw = stream_fixture(done=False, complete_arguments=False)
    result = adapter.decode_stream(prepared, raw)
    assert result.status == "partial" and result.error_kind == "interrupted_stream"
    assert result.reasoning_status == "truncated"
    assert result.partial_response == raw
    assert result.executable_tool_calls == ()


def test_interrupted_stream_without_reasoning_is_unknown(tmp_path: Path) -> None:
    adapter = GroqSelectedAdapter(tmp_path)
    prepared = adapter.prepare(replace(config(), stream=True), call_id="c", attempt_id="a",
                               task_category="collect", phase="collect",
                               messages=[{"role": "user", "content": "Find A"}], tools=TOOLS)
    raw = stream_fixture(done=False, complete_arguments=False).replace(
        b'"reasoning": "Returned stream reasoning", ', b"",
    )
    result = adapter.decode_stream(prepared, raw)
    assert result.status == "partial" and result.reasoning is None
    assert result.reasoning_status == "unknown"


def test_cutoff_and_malformed_calls_never_executable(tmp_path: Path) -> None:
    adapter = GroqSelectedAdapter(tmp_path)
    prepared = prepare(adapter)
    cutoff = adapter.decode(prepared, reply(content="partial", calls=[call("t1", "A")],
                                            finish="length"))
    assert cutoff.status == "partial"
    assert cutoff.reasoning_status == "truncated"
    assert cutoff.executable_tool_calls == ()
    with pytest.raises(ModelPreflightError):
        cutoff.assistant_tool_message()
    malformed = call("t1", "A")
    malformed["function"]["arguments"] = '{"key":'
    bad = adapter.decode(prepared, reply(calls=[malformed]))
    assert bad.status == "failed"
    assert bad.error_kind == "invalid_tool_calls"
    assert bad.executable_tool_calls == ()
    assert bad.usage["total_tokens"] == 70
    assert bad.reasoning == "Returned reasoning"
    assert bad.response_sha256 is not None


def test_missing_reasoning_and_final_schema_repair(tmp_path: Path) -> None:
    adapter = GroqSelectedAdapter(tmp_path)
    final = prepare(adapter, phase="final")
    missing = adapter.decode(final, reply(content='{"answer":"A"}', finish="stop",
                                          reasoning=None))
    assert missing.status == "complete"
    assert missing.reasoning is None and missing.reasoning_status == "absent"
    invalid = adapter.decode(final, reply(content='{"wrong":"A"}', finish="stop"))
    assert invalid.status == "partial" and invalid.error_kind == "invalid_final_schema"
    cutoff = adapter.decode(final, reply(content='{"answer":', finish="length"))
    assert cutoff.status == "partial" and cutoff.reasoning_status == "truncated"
    assert cutoff.content == '{"answer":'
    no_reasoning_cutoff = adapter.decode(final, reply(content='{"answer":', finish="length",
                                                      reasoning=None))
    assert no_reasoning_cutoff.status == "partial"
    assert no_reasoning_cutoff.reasoning_status == "unknown"
    repaired = adapter.prepare(config(16384), call_id="c2", attempt_id="a2",
                               parent_call_id="c1", task_category="final", phase="final",
                               messages=[{"role": "user", "content": "Retry with schema"}],
                               schema_name="answer", schema=SCHEMA)
    assert repaired.body["max_completion_tokens"] == 16384
    assert repaired.body["model"] == final.body["model"]


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.data = json.dumps(payload).encode()
        self.headers = {"x-ratelimit-remaining-tokens": "123", "private": "no"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _max_bytes):
        return self.data


def test_interrupted_response_keeps_restricted_partial_bytes_without_tool_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    response = FakeResponse(reply())
    response.data = b'{"model":"openai/gpt-oss-120b","choices":[{"message":{"tool_calls":['
    adapter = GroqSelectedAdapter(tmp_path, transport=lambda _request, timeout: response)
    outcome = adapter.invoke(prepare(adapter))
    assert outcome.status == "failed" and outcome.error_kind == "invalid_json"
    assert outcome.partial_response == response.data
    assert outcome.response_sha256 == hashlib.sha256(response.data).hexdigest()
    assert outcome.executable_tool_calls == ()
    assert outcome.reasoning_status == "unknown"
    assert "tool_calls" not in repr(outcome)


def test_transport_injection_captures_request_and_rate_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    sent = []

    def fake_open(request, timeout):
        sent.append((request, timeout))
        return FakeResponse(reply(content="Finished", finish="stop"))

    adapter = GroqSelectedAdapter(tmp_path, transport=fake_open)
    outcome = adapter.invoke(prepare(adapter))
    assert outcome.status == "complete"
    assert len(sent) == 1 and sent[0][1] == 120
    assert json.loads(sent[0][0].data)["include_reasoning"] is True
    assert outcome.rate_headers == {"x-ratelimit-remaining-tokens": "123"}
    assert outcome.timings["completion_time"] == 0.2
    assert "synthetic-token" not in str(outcome)


def test_http_error_is_typed_and_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    count = 0

    def failing(_request, timeout):
        nonlocal count
        count += 1
        raise urllib.error.HTTPError("https://api.groq.com", 429, "private", {
            "Retry-After": "2", "private": "no",
        }, io.BytesIO(b"private response"))

    adapter = GroqSelectedAdapter(tmp_path, transport=failing)
    outcome = adapter.invoke(prepare(adapter))
    assert count == 1 and outcome.status == "failed"
    assert outcome.error_kind == "http_error"
    assert outcome.retry_after_seconds == 2
    assert outcome.reasoning_status == "unknown"
    assert "private" not in str(outcome)


def test_no_credential_is_a_typed_failure_without_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    adapter = GroqSelectedAdapter(tmp_path, transport=lambda *_args, **_kwargs: pytest.fail("network"))
    outcome = adapter.invoke(prepare(adapter))
    assert outcome.status == "failed" and outcome.error_kind == "credential_unavailable"
    assert outcome.reasoning_status == "unknown"
