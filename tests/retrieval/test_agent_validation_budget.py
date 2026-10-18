"""Offline admission and replay tests for the natural-run HTTP ledger."""

import io
import json
import urllib.error
import urllib.request

import pytest

from ora.live.provider import MODEL, URL
from ora.mission.figure_reading import VISION_MODEL
from ora.retrieval.agent_validation_budget import AgentBudget, BudgetStop


class Response(io.BytesIO):
    status = 200
    code = 200
    headers = {}
    url = URL

    def geturl(self):
        return self.url

    def info(self):
        return self.headers


def test_vision_requires_explicit_allowance_and_shares_model_attempt_cap(tmp_path):
    packet = {"model": VISION_MODEL, "stream": False, "reasoning_effort": "none",
              "max_completion_tokens": 1024, "messages": [{"role": "user", "content": [
                  {"type": "text", "text": "Read the synthetic axis label"},
                  {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 200_000}},
              ]}]}
    vision = urllib.request.Request(URL, data=json.dumps(packet).encode(), method="POST")
    sent = []

    def opener(_self, _url, **_kwargs):
        sent.append(1)
        return response(total=2400)

    with AgentBudget(tmp_path / "disabled.json") as budget:
        with pytest.raises(BudgetStop, match="model_request_mismatch"):
            budget.open(opener, object(), vision)
    assert not sent
    with AgentBudget(tmp_path / "enabled.json", allow_vision=True,
                     model_limit=1, token_limit=5000) as budget:
        budget.open(opener, object(), vision)
        entry = budget.data["model_attempts"][0]
        assert entry["model"] == VISION_MODEL and entry["model_role"] == "vision"
        assert 2048 < entry["estimated_input_tokens"] < 2400
        assert entry["actual_tokens"] == 2400
        with pytest.raises(BudgetStop, match="model_attempt_cap"):
            budget.open(opener, object(), request())
    assert len(sent) == 1
    assert "A" * 100 not in (tmp_path / "enabled.json").read_text()


def request(*, native=False, cap=100, secret="private prompt"):
    body = {"model": MODEL, "messages": [{"role": "user", "content": secret}],
            "max_completion_tokens": cap, "reasoning_effort": "low" if native else "high",
            "include_reasoning": True, "stream": False,
            "tools": [{"type": "browser_search"}] if native else []}
    return urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                  headers={"Authorization": "Bearer private-key"})


def response(total=20):
    return Response(json.dumps({"model": MODEL, "usage": {"total_tokens": total},
                                "choices": [{"message": {"content": "private answer"}}]}).encode())


def test_actual_usage_releases_unused_reservation_and_preserves_secrets(tmp_path):
    path = tmp_path / "ledger.json"
    calls = []

    def opener(_self, _url, **_kwargs):
        calls.append(1)
        return response()

    with AgentBudget(path, token_limit=650, model_limit=3) as budget:
        for _ in range(3):
            with budget.open(opener, object(), request(cap=200)) as result:
                assert b"private answer" in result.read()
        assert budget.data["planned_tokens"] == 60
        assert budget.data["peak_planned_tokens"] > budget.data["planned_tokens"]
    assert len(calls) == 3
    saved = path.read_text()
    assert "private prompt" not in saved
    assert "private answer" not in saved
    assert "private-key" not in saved


def test_token_and_call_caps_block_before_dispatch(tmp_path):
    sent = []

    def opener(_self, _url, **_kwargs):
        sent.append(1)
        return response()

    path = tmp_path / "tokens.json"
    with AgentBudget(path, token_limit=10) as budget:
        with pytest.raises(BudgetStop, match="token_cap"):
            budget.open(opener, object(), request(cap=100))
    assert not sent

    path = tmp_path / "calls.json"
    with AgentBudget(path, model_limit=1) as budget:
        budget.open(opener, object(), request())
        with pytest.raises(BudgetStop, match="model_attempt_cap"):
            budget.open(opener, object(), request())
    assert len(sent) == 1


def test_default_eight_model_and_four_retrieval_attempts(tmp_path):
    sent = []

    def opener(_self, value, **_kwargs):
        sent.append(value)
        return response() if isinstance(value, urllib.request.Request) else Response(b"source")

    with AgentBudget(tmp_path / "ledger.json") as budget:
        for _ in range(8):
            budget.open(opener, object(), request())
        for _ in range(4):
            budget.open(opener, object(), "https://example.org/source")
        with pytest.raises(BudgetStop, match="model_attempt_cap"):
            budget.open(opener, object(), request())
    assert len(sent) == 12


def test_native_browser_request_keeps_full_tools_and_marks_internal_count_unknown(tmp_path):
    observed = []

    def opener(_self, value, **_kwargs):
        observed.append(json.loads(value.data))
        return response()

    with AgentBudget(tmp_path / "ledger.json") as budget:
        budget.open(opener, object(), request(native=True))
        entry = budget.data["model_attempts"][0]
        assert entry["native_browser_search"] is True
        assert entry["provider_internal_search_attempts"] == "unknown"
    assert observed[0]["tools"] == [{"type": "browser_search"}]
    assert observed[0]["reasoning_effort"] == "low"


def test_retrieval_redirect_recursion_counts_each_open(tmp_path):
    path = tmp_path / "ledger.json"
    seen = []
    with AgentBudget(path, retrieval_limit=2) as budget:
        def opener(_self, url, **_kwargs):
            seen.append(url)
            if len(seen) == 1:
                return budget.open(opener, object(), "https://example.org/redirect")
            return Response(b"source")

        assert budget.open(opener, object(), "https://example.org/start").read() == b"source"
        with pytest.raises(BudgetStop, match="retrieval_attempt_cap"):
            budget.open(opener, object(), "https://example.org/third")
    assert len(seen) == 2


def test_zero_retrieval_allowance_rejects_before_http(tmp_path):
    called = []

    def opener(*_args, **_kwargs):
        called.append(True)
        return Response(b"unexpected")

    with AgentBudget(tmp_path / "ledger.json", retrieval_limit=0,
                     model_limit=1, token_limit=1000) as budget:
        with pytest.raises(BudgetStop, match="retrieval_attempt_cap"):
            budget.open(opener, object(), "https://example.org/source")
        assert budget.data["retrieval_attempts"] == []
    assert called == []

    with AgentBudget(tmp_path / "native-ledger.json", retrieval_limit=0,
                     model_limit=1, token_limit=10000) as budget:
        with pytest.raises(BudgetStop, match="retrieval_attempt_cap"):
            budget.open(opener, object(), request(native=True))
        assert budget.data["model_attempts"] == []
        assert budget.data["retrieval_attempts"] == []
    assert called == []


def test_uncertain_transport_is_durable_and_cannot_restart(tmp_path):
    path = tmp_path / "ledger.json"

    def failed(_self, _url, **_kwargs):
        raise TimeoutError()

    with AgentBudget(path) as budget:
        with pytest.raises(BudgetStop, match="uncertain_transport"):
            budget.open(failed, object(), "https://example.org/source")
    with pytest.raises(BudgetStop, match="uncertain_transport"):
        with AgentBudget(path):
            pytest.fail("replayed uncertain request")


def test_missing_model_usage_latches_before_next_request(tmp_path):
    path = tmp_path / "ledger.json"
    sent = []

    def no_usage(_self, _url, **_kwargs):
        sent.append(1)
        return Response(b'{"model":"openai/gpt-oss-120b","choices":[]}')

    with AgentBudget(path) as budget:
        budget.open(no_usage, object(), request())
        with pytest.raises(BudgetStop, match="unknown_model_usage"):
            budget.open(no_usage, object(), request())
    assert len(sent) == 1
    with pytest.raises(BudgetStop, match="unknown_model_usage"):
        with AgentBudget(path):
            pytest.fail("replayed unknown usage")


def test_429_preserves_error_body_and_unknown_usage_reservation(tmp_path):
    raw = b'{"error":"private rate limit"}'

    def limited(_self, _url, **_kwargs):
        raise urllib.error.HTTPError(URL, 429, "Too Many Requests", {"Retry-After": "2"},
                                     io.BytesIO(raw))

    with AgentBudget(tmp_path / "ledger.json") as budget:
        with pytest.raises(urllib.error.HTTPError) as raised:
            budget.open(limited, object(), request())
        assert raised.value.read() == raw
        assert budget.data["model_attempts"][0]["state"] == "rate_limited"
        assert budget.data["planned_tokens"] > 0
        assert "actual_tokens" not in budget.data["model_attempts"][0]
        assert budget.data["stop_reason"] is None
    assert "private rate limit" not in (tmp_path / "ledger.json").read_text()
