"""Explicit accounting-only evaluation still records and stops uncertain effects."""

import io
import json
import urllib.error

import pytest

from ora.live.provider import URL
from ora.retrieval.agent_validation_budget import AgentBudget, BudgetStop
from tests.retrieval.test_agent_validation_budget import Response, request


def test_unlimited_aggregate_requires_opt_in_and_persists_identity(tmp_path):
    path = tmp_path / "ledger.json"
    with pytest.raises(ValueError, match="explicit authorization"):
        AgentBudget(path, model_limit=None, token_limit=None, retrieval_limit=None)
    with pytest.raises(ValueError, match="explicit authorization"):
        AgentBudget(path, accounting_only=True)
    with pytest.raises(ValueError, match="explicit authorization"):
        AgentBudget(path, model_limit=None, token_limit=55_000, retrieval_limit=4)

    sent = []

    def observed(_self, url, **_kwargs):
        sent.append(url)
        if isinstance(url, str):
            return Response(b"synthetic source")
        return Response(json.dumps({"usage": {"total_tokens": 8000}}).encode())

    options = dict(model_limit=None, token_limit=None, retrieval_limit=None,
                   accounting_only=True)
    with AgentBudget(path, **options) as budget:
        for _ in range(9):
            budget.open(observed, object(), request())
        for _ in range(5):
            budget.open(observed, object(), "https://example.org/synthetic")
        assert len(budget.data["model_attempts"]) == 9
        assert len(budget.data["retrieval_attempts"]) == 5
        assert budget.data["planned_tokens"] == 72_000
    with AgentBudget(path, **options) as budget:
        budget.open(observed, object(), request())
        assert budget.data["model_attempts"][-1]["number"] == 10
    with pytest.raises(BudgetStop, match="budget_identity_mismatch"):
        with AgentBudget(path):
            pass
    assert len(sent) == 15


def test_accounting_only_stops_unknown_usage_and_long_429_without_retry(tmp_path):
    options = dict(model_limit=None, token_limit=None, retrieval_limit=None,
                   accounting_only=True)
    calls = []

    def unknown(_self, _url, **_kwargs):
        calls.append(1)
        return Response(b'{"choices":[]}')

    with AgentBudget(tmp_path / "unknown.json", **options) as budget:
        budget.open(unknown, object(), request())
        with pytest.raises(BudgetStop, match="unknown_model_usage"):
            budget.open(unknown, object(), request())
    assert len(calls) == 1

    def long_wait(_self, _url, **_kwargs):
        calls.append(1)
        raise urllib.error.HTTPError(URL, 429, "rate limit",
                                     {"Retry-After": "3600"}, io.BytesIO(b"private"))

    path = tmp_path / "hold.json"
    with AgentBudget(path, **options) as budget:
        with pytest.raises(urllib.error.HTTPError):
            budget.open(long_wait, object(), request())
        assert budget.data["stop_reason"] == "long_provider_wait"
        assert budget.data["model_attempts"][0]["state"] == "rate_limited"
        assert budget.data["planned_tokens"] > 0
    with pytest.raises(BudgetStop, match="long_provider_wait"):
        with AgentBudget(path, **options):
            pass
    assert len(calls) == 2
    assert "private" not in path.read_text()


def test_known_retrieval_404_remains_a_recoverable_tool_result(tmp_path):
    options = dict(model_limit=None, token_limit=None, retrieval_limit=None,
                   accounting_only=True)
    calls = []

    def missing(_self, _url, **_kwargs):
        calls.append("missing")
        raise urllib.error.HTTPError("https://example.org/missing", 404, "Not Found",
                                     {}, io.BytesIO(b"not found"))

    def alternate(_self, _url, **_kwargs):
        calls.append("alternate")
        return Response(b"permitted alternate")

    with AgentBudget(tmp_path / "ledger.json", **options) as budget:
        with pytest.raises(urllib.error.HTTPError):
            budget.open(missing, object(), "https://example.org/missing")
        assert budget.data["stop_reason"] is None
        assert budget.open(alternate, object(), "https://example.org/alternate").read() == b"permitted alternate"
        assert [row["status"] for row in budget.data["retrieval_attempts"]] == [404, 200]
    assert calls == ["missing", "alternate"]


def test_accounting_only_classifies_daily_429_but_allows_minute_wait(tmp_path):
    options = dict(model_limit=None, token_limit=None, retrieval_limit=None,
                   accounting_only=True)

    def limited(retry_after, message):
        def send(_self, _url, **_kwargs):
            raw = json.dumps({"error": {"message": message}}).encode()
            raise urllib.error.HTTPError(URL, 429, "rate limit",
                                         {"Retry-After": str(retry_after)}, io.BytesIO(raw))
        return send

    with AgentBudget(tmp_path / "minute.json", **options) as budget:
        with pytest.raises(urllib.error.HTTPError):
            budget.open(limited(60, "too many requests this minute"), object(), request())
        assert budget.data["stop_reason"] is None
        assert budget.data["model_attempts"][0]["state"] == "rate_limited"

    with AgentBudget(tmp_path / "daily.json", **options) as budget:
        with pytest.raises(urllib.error.HTTPError):
            budget.open(limited(60, "TPD Limit: 100000, Used: 99999, Requested: 500"),
                        object(), request())
        assert budget.data["stop_reason"] == "provider_daily_quota_wait"
        assert budget.data["model_attempts"][0]["quota_classification"] == "provider_daily_limit"
