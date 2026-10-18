"""Exercise the live Groq boundary with synthetic responses and no network access."""

from __future__ import annotations

import io
import json
import time
import urllib.error
from pathlib import Path

import pytest

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.live import provider as live
from ora.live.engine import ResearchHost


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.body = json.dumps(payload).encode()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.body


def response(
    *, content: str | None = "Answer", tool_calls: list[dict] | None = None,
    model: str = live.MODEL, finish_reason: str = "stop",
) -> dict:
    return {
        "model": model,
        "choices": [{"message": {
            "role": "assistant", "content": content, "tool_calls": tool_calls,
            "reasoning": "private synthetic reasoning must not escape",
        }, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300,
                  "private_usage": "must not escape"},
        "private_response": "must not escape",
    }


def test_operator_primary_hold_blocks_legacy_provider_before_key_or_network(tmp_path, monkeypatch):
    config = tmp_path / ".local/groq-credential-profiles.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"version": 1, "profiles": {"primary": {"enabled": False}}}))
    provider = live.GroqProvider(tmp_path)

    def unexpected(*args, **kwargs):
        pytest.fail("A held profile must not read a key or dispatch a request")

    monkeypatch.setattr(provider, "_key", unexpected)
    monkeypatch.setattr(live.urllib.request, "urlopen", unexpected)
    with pytest.raises(live.CredentialUnavailable, match="paused by operator"):
        provider.complete([{"role": "user", "content": "Fixture question"}])


def test_serial_tool_call_round_trip_and_safe_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-token")
    provider = live.GroqProvider(tmp_path)
    calls = [{"id": "call_1", "type": "function", "function": {
        "name": "source_read", "arguments": '{"source_id":"A1"}'}}]
    sent: list[dict] = []
    replies = [
        response(content=None, tool_calls=calls, finish_reason="tool_calls"),
        response(content="Supported answer"),
    ]

    def fake_open(request, timeout):
        assert request.full_url == live.URL
        assert request.get_method() == "POST"
        assert timeout == 120
        assert request.get_header("User-agent") == live.USER_AGENT
        assert request.get_header("Authorization") == "Bearer synthetic-test-token"
        sent.append(json.loads(request.data))
        return FakeResponse(replies.pop(0))

    monkeypatch.setattr(live.urllib.request, "urlopen", fake_open)
    tools = [{"type": "function", "function": {"name": "source_read", "description": "Read source",
              "parameters": {"type": "object", "properties": {"source_id": {"type": "string"}}}}}]
    first_messages = [{"role": "system", "content": "Work from evidence."},
                      {"role": "user", "content": "Review A1."}]
    first = provider.complete(first_messages, tools)
    assert first == {
        "content": None, "tool_calls": calls,
        "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
        "cache_observation": {"status": "not_reported"},
        "model": live.MODEL, "elapsed_seconds": first["elapsed_seconds"],
        "finish_reason": "tool_calls", "estimated_usd": 0.000135,
    }
    assert first["elapsed_seconds"] >= 0
    next_messages = [*first_messages, {"role": "assistant", "content": first["content"],
                                      "tool_calls": first["tool_calls"],
                                      "reasoning": "must be discarded"},
                     {"role": "tool", "tool_call_id": "call_1", "name": "source_read",
                      "content": "A1 source text"}]
    second = provider.complete(next_messages, tools)
    assert second["content"] == "Supported answer"
    assert second["tool_calls"] == []
    assert sent[0]["model"] == live.MODEL
    assert sent[0]["reasoning_effort"] == "high"
    assert sent[0]["include_reasoning"] is False
    assert sent[0]["max_completion_tokens"] == 8192
    assert sent[0]["stream"] is False
    assert sent[0]["parallel_tool_calls"] is False
    assert sent[0]["tool_choice"] == "auto"
    assert sent[0]["tools"] == tools
    assert sent[1]["messages"][-2:] == [
        {"role": "assistant", "content": None, "tool_calls": calls},
        {"role": "tool", "tool_call_id": "call_1", "name": "source_read",
         "content": "A1 source text"},
    ]
    assert "reasoning" not in json.dumps(sent[1]["messages"])
    assert "synthetic-test-token" not in json.dumps(sent)
    assert "private" not in json.dumps(first)


def test_legacy_provider_preserves_reported_cache_and_reasoning_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-token")
    raw = response()
    raw["usage"]["prompt_tokens_details"] = {"cached_tokens": 40, "other": "omit"}
    raw["usage"]["completion_tokens_details"] = {"reasoning_tokens": 30}
    monkeypatch.setattr(live.urllib.request, "urlopen",
                        lambda _request, timeout: FakeResponse(raw))
    result = live.GroqProvider(tmp_path).complete([{"role": "user", "content": "Question"}])
    assert result["usage"] == {
        "prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300,
        "prompt_tokens_details": {"cached_tokens": 40},
        "completion_tokens_details": {"reasoning_tokens": 30},
    }
    assert result["cache_observation"] == {"status": "reported_hit", "cached_tokens": 40}
    assert "private" not in json.dumps(result)


def test_estimate_is_key_free_and_respects_configured_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    provider = live.GroqProvider(tmp_path, max_completion_tokens=1024)
    messages = [{"role": "user", "content": "Question"}]
    estimate = provider.estimate_max_cost(messages)
    assert 0 < estimate < 0.01
    assert live.GroqProvider(tmp_path, 2048).estimate_max_cost(messages) > estimate
    with pytest.raises(live.ProviderError, match="credential is unavailable") as error:
        provider.complete(messages)
    assert error.value.status == 503
    assert error.value.retry_after is None


def test_builtin_provider_tools_are_not_admitted(tmp_path: Path) -> None:
    provider = live.GroqProvider(tmp_path)
    with pytest.raises(ValueError, match="only named local function tools"):
        provider.estimate_max_cost(
            [{"role": "user", "content": "Question"}], [{"type": "browser_search"}]
        )


def test_explicit_local_credential_loading_only_on_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    (tmp_path / ".env").write_text('GROQ_API_KEY="synthetic-file-token"\n')
    provider = live.GroqProvider(tmp_path)
    provider.estimate_max_cost([{"role": "user", "content": "Question"}])

    def fake_open(request, timeout):
        assert timeout == 120
        assert request.get_header("Authorization") == "Bearer synthetic-file-token"
        return FakeResponse(response())

    monkeypatch.setattr(live.urllib.request, "urlopen", fake_open)
    assert provider.complete([{"role": "user", "content": "Question"}])["content"] == "Answer"


def test_http_failure_is_sanitized_and_never_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-token")
    count = 0

    def fake_open(_request, _timeout=None, **_kwargs):
        nonlocal count
        count += 1
        raise urllib.error.HTTPError(
            live.URL, 429, "secret provider message", {"Retry-After": "4.5"},
            io.BytesIO(b"secret provider response"),
        )

    monkeypatch.setattr(live.urllib.request, "urlopen", fake_open)
    with pytest.raises(live.ProviderError) as error:
        live.GroqProvider(tmp_path).complete([{"role": "user", "content": "Question"}])
    assert count == 1
    assert error.value.status == 429
    assert error.value.retry_after == 4.5
    assert "secret" not in str(error.value)
    assert "retry" in str(error.value)


@pytest.mark.parametrize("failure", [TimeoutError("private timeout"),
                                      urllib.error.URLError("private URL")])
def test_transport_failure_is_sanitized_and_never_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-token")
    count = 0

    def fake_open(_request, timeout):
        nonlocal count
        count += 1
        assert timeout == 120
        raise failure

    monkeypatch.setattr(live.urllib.request, "urlopen", fake_open)
    with pytest.raises(live.ProviderError) as error:
        live.GroqProvider(tmp_path).complete([{"role": "user", "content": "Question"}])
    assert count == 1
    assert error.value.status is None
    assert "private" not in str(error.value)


def test_model_mismatch_is_rejected_without_response_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-token")
    monkeypatch.setattr(live.urllib.request, "urlopen",
                        lambda _request, timeout: FakeResponse(response(model="other-private-model")))
    with pytest.raises(live.ProviderError, match="different or missing model") as error:
        live.GroqProvider(tmp_path).complete([{"role": "user", "content": "Question"}])
    assert error.value.status == 502
    assert "other-private-model" not in str(error.value)


def test_truncated_completion_retains_usage_and_finish_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-token")
    monkeypatch.setattr(live.urllib.request, "urlopen",
                        lambda _request, timeout: FakeResponse(response(
                            content="Partial text", finish_reason="length")))
    result = live.GroqProvider(tmp_path, 1024).complete(
        [{"role": "user", "content": "Question"}])
    assert result["finish_reason"] == "length"
    assert result["content"] == "Partial text"
    assert result["usage"]["total_tokens"] == 300
    assert result["estimated_usd"] > 0


def test_invalid_completion_shape_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-token")
    invalid = response()
    invalid["usage"]["completion_tokens"] = "private invalid value"
    monkeypatch.setattr(live.urllib.request, "urlopen",
                        lambda _request, timeout: FakeResponse(invalid))
    with pytest.raises(live.ProviderError, match="invalid token usage") as error:
        live.GroqProvider(tmp_path).complete([{"role": "user", "content": "Question"}])
    assert error.value.status == 502
    assert "private" not in str(error.value)


def test_host_peer_continuation_and_followup_use_valid_groq_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross the real host/provider boundary with a synthetic HTTP transport."""
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-token")
    sent: list[dict] = []

    def fake_open(request, timeout):
        assert timeout == 120
        body = json.loads(request.data)
        sent.append(body)
        assert all(message.get("tool_calls") != [] for message in body["messages"])
        agent = "Sage" if "You are Sage" in body["messages"][0]["content"] else "Mira"
        initial = json.loads(body["messages"][1]["content"])
        if initial["target"]:
            assert initial["target"]["record"]["body"] == "Revised answer"
            return FakeResponse(response(content="Follow-up answer"))
        if agent == "Mira":
            return FakeResponse(response(content="Peer critique"))
        latest = body["messages"][-1]
        if latest["role"] == "user" and "peerResponse" in latest["content"]:
            continuation = json.loads(latest["content"])
            assert continuation["priorAnswer"] == "Preliminary answer"
            assert continuation["peerResponse"] == "Peer critique"
            return FakeResponse(response(content="Revised answer"))
        if len(body["messages"]) == 2:
            call = {"id": "call_peer", "type": "function", "function": {
                "name": "ask_peer", "arguments": json.dumps({
                    "agent_id": "agent-critique", "question": "Check this answer."
                })}}
            return FakeResponse(response(content=None, tool_calls=[call],
                                         finish_reason="tool_calls"))
        assert body["messages"][-1]["role"] == "tool"
        return FakeResponse(response(content="Preliminary answer"))

    monkeypatch.setattr(live.urllib.request, "urlopen", fake_open)
    host = ResearchHost(Path(__file__).resolve().parents[2], tmp_path, live.GroqProvider(tmp_path))

    def settle(run_id: str):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = host.state(run_id)
            status = next(item.status for item in state.runs if item.id == run_id)
            if status in {"completed", "waiting"}:
                return state, status
            time.sleep(0.01)
        pytest.fail("synthetic run did not settle")

    try:
        started = host.start(LiveStartRequest(
            request_id="provider-integration-start", question="Review the saved evidence",
            evidence_text="Synthetic evidence.", agent_ids=["agent-synthesis"],
        ))
        state, status = settle(started.run_id)
        assert status == "completed", state.error
        assert len(host.calls(started.run_id)) == 4
        report = next(artifact for artifact in state.snapshot.artifacts
                      if artifact.id == state.snapshot.mission.report_artifact_id)
        assert report.body == "Revised answer"

        followed = host.followup(LiveFollowupRequest(
            request_id="provider-integration-follow", parent_run_id=started.run_id,
            parent_snapshot_id=state.snapshot.snapshot_id,
            target_id=report.id, target_version=report.version,
            question="Revisit this answer", agent_ids=["agent-synthesis"],
        ))
        follow_state, follow_status = settle(followed.run_id)
        assert follow_status == "completed", follow_state.error
        assert len(host.calls(followed.run_id)) == 1
        assert len(sent) == 5
    finally:
        host.close()
