"""The actual corpus HTTP path shares durable evaluation allowance, without network."""

import io
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from ora.retrieval.agent_validation_budget import AgentBudget
from ora.retrieval.bheb import BhebCorpusService
from ora.state import StateStore
from tests.retrieval.test_bheb import _fixture, _response

SCOPE = {"mission_id": "budget-mission", "run_id": "budget-run",
         "actor_id": "budget-peer", "task_id": "budget-task"}


class Response(io.BytesIO):
    status = 200
    code = 200
    headers = {}


def _transport_guard(monkeypatch, budget, response, sent, *, failure=False):
    def fake_open(_opener, request, **kwargs):
        # This is the last boundary before network in the production HTTP path.
        # The attempt must already be durably charged, even when it later fails.
        ledger = json.loads(budget.path.read_text())
        assert ledger["retrieval_attempts"][-1]["state"] == "attempted"
        assert request.full_url == "https://api.openai.com/v1/vector_stores/vs_synthetic/search"
        assert kwargs["timeout"] == 8
        sent.append(json.loads(request.data)["query"])
        if failure:
            raise TimeoutError("synthetic transport timeout")
        return Response(response.body)

    def guarded(opener, request, **kwargs):
        return budget.open(fake_open, opener, request, **kwargs)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", guarded)


def test_exhausted_allowance_is_known_no_dispatch_and_survives_restart(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch, source=False)
    ledger_path = tmp_path / "budget.json"
    state_path = tmp_path / "state"
    sent = []
    with AgentBudget(ledger_path, retrieval_limit=1) as budget:
        _transport_guard(monkeypatch, budget, _response(attrs), sent)
        service = BhebCorpusService(config, StateStore(state_path))
        first = service.search(SCOPE, "first", "one question")
        assert first["status"] == "completed"
        assert len(budget.data["retrieval_attempts"]) == 1
    with AgentBudget(ledger_path, retrieval_limit=1) as budget:
        _transport_guard(monkeypatch, budget, _response(attrs), sent)
        reopened = BhebCorpusService(config, StateStore(state_path))
        reused = reopened.search(SCOPE, "reuse", "one question")
        assert reused["hits"] == first["hits"] and reused["reuse_status"] == "reused"
        denied = reopened.search(SCOPE, "second", "different question")
        assert denied["status"] == "not_dispatched"
        assert denied["diagnostics"]["attempt_count"] == 0
        assert denied["diagnostics"]["failure_condition"] == "retrieval_attempt_cap"
        assert len(budget.data["retrieval_attempts"]) == 1
        assert budget.data["model_attempts"] == []
        replay = reopened.search(SCOPE, "denied-replay", "different question")
        assert replay["status"] == "not_dispatched"
        assert reopened.search(SCOPE, "saved-after-denial", "one question")["hits"] == first["hits"]
    assert sent == ["one question"]
    saved = json.loads(ledger_path.read_text())
    assert saved["stop_reason"] == "retrieval_attempt_cap"
    assert "synthetic-test-key" not in ledger_path.read_text()


def test_concurrent_distinct_queries_share_one_remaining_http_attempt(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch, source=False)
    sent = []
    with AgentBudget(tmp_path / "budget.json", retrieval_limit=1) as budget:
        _transport_guard(monkeypatch, budget, _response(attrs), sent)
        service = BhebCorpusService(config, StateStore(tmp_path / "state"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda i: service.search(SCOPE, f"op-{i}", f"question {i}"), range(2)))
        assert sorted(row["status"] for row in results) == ["completed", "not_dispatched"]
        assert len(sent) == len(budget.data["retrieval_attempts"]) == 1
        assert budget.data["model_attempts"] == []


def test_failure_after_dispatch_stays_charged_and_uncertain(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch, source=False)
    sent = []
    with AgentBudget(tmp_path / "budget.json", retrieval_limit=2) as budget:
        _transport_guard(monkeypatch, budget, _response(attrs), sent, failure=True)
        service = BhebCorpusService(config, StateStore(tmp_path / "state"))
        uncertain = service.search(SCOPE, "timeout", "one question")
        assert uncertain["status"] == "uncertain"
        assert uncertain["diagnostics"]["attempt_count"] == 1
        assert budget.data["retrieval_attempts"][0]["state"] == "uncertain"
        assert budget.data["stop_reason"] == "uncertain_transport"
        assert service.search(SCOPE, "timeout-reuse", "one question")["status"] == "uncertain"
        denied = service.search(SCOPE, "other", "different question")
        assert denied["status"] == "not_dispatched"
        assert denied["diagnostics"]["attempt_count"] == 0
        assert len(budget.data["retrieval_attempts"]) == 1
    assert sent == ["one question"]
