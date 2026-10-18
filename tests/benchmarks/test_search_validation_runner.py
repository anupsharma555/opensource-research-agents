"""Full provider runner boundaries with injected HTTP and no live credentials."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from ora.retrieval import discovery
from ora.retrieval.providers import HTTPResult
from ora.retrieval.validation_transport import ValidationStop

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("search_validation", ROOT / "scripts/validate_search.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
VALUES = {"EXA_API_KEY": "synthetic-exa", "TAVILY_API_KEY": "synthetic-tavily", "NCBI_API_KEY": "synthetic-ncbi"}


def reply(payload, status=200, headers=None):
    return HTTPResult(status, json.dumps(payload).encode(), headers or {})


def responses(calls):
    def opener(method, url, *, body, headers, timeout):
        calls.append((method, url, body))
        assert timeout <= 30
        if "esearch.fcgi" in url:
            assert parse_qs(urlsplit(url).query)["api_key"] == [VALUES["NCBI_API_KEY"]]
            return reply({"esearchresult": {"idlist": ["123"], "count": "1"}})
        if "esummary.fcgi" in url:
            assert parse_qs(urlsplit(url).query)["api_key"] == [VALUES["NCBI_API_KEY"]]
            return reply({"result": {"uids": ["123"], "123": {"uid": "123", "title": "Synthetic tree record"}}})
        if "api.crossref.org" in url:
            return reply({"message": {"items": [{"DOI": "10.0000/synthetic", "title": ["Synthetic canopy study"]}], "total-results": 1}})
        if "www.ebi.ac.uk" in url:
            return reply({"hitCount": 1, "resultList": {"result": [{"id": "123", "source": "MED", "title": "Synthetic tree record"}]}})
        if "tavily" in url:
            assert headers["Authorization"] == "Bearer " + VALUES["TAVILY_API_KEY"]
            assert body["include_answer"] is False and body["max_results"] == 3
            return reply({"results": [{"url": "https://example.org/trees", "title": "Synthetic trees", "content": "Synthetic discovery excerpt."}], "usage": {"credits": 1}})
        assert headers["x-api-key"] == VALUES["EXA_API_KEY"]
        if url.endswith("/contents"):
            assert body["urls"] == ["https://example.org/trees"]
            assert body["text"]["maxCharacters"] == 2000
            return reply({"results": [{"id": "selected", "url": "https://example.org/trees", "title": "Synthetic trees", "text": "Unqualified raw text must not leak."}]})
        assert body["type"] in {"fast", "auto"} and body["numResults"] == 3
        assert body["contents"] == {"highlights": True}
        return reply({"results": [{"id": "one", "url": "https://example.org/trees", "title": "Synthetic trees", "highlights": ["Candidate only."]}], "costDollars": {"total": 0.001}})
    return opener


def run(tmp_path, calls, *, selected=None, cap=8, opener=None, **kwargs):
    return runner.run_batch(tmp_path, "synthetic-batch", tmp_path / ".local/search-validation/case/state",
                            list(runner.CASES) if selected is None else selected,
                            max_attempts=cap, values=VALUES, opener=opener or responses(calls),
                            execution_mode="offline_fixture", **kwargs)


def test_default_command_and_provider_selection_cannot_call(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "run_batch", lambda *_a, **_kw: pytest.fail("Dry-run dispatched"))
    assert runner.main([]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["live"] is False and plan["planned_http_attempts"] == 8
    assert runner.main(["--provider", "pubmed"]) == 0
    assert [c["id"] for c in json.loads(capsys.readouterr().out)["cases"]] == ["pubmed"]
    assert not (tmp_path / ".local").exists()


def test_all_cases_use_real_adapters_and_eight_attempts(tmp_path):
    calls = []
    result = run(tmp_path, calls)
    assert result["stop_reason"] is None, result
    assert result["http_attempts"] == result["captured_responses"] == len(calls) == 8
    assert result["model_calls"] == result["model_tokens"] == 0
    assert all(c["status"] == "completed" and c["page_ref"] and c["delivery_ref"] for c in result["cases"])
    assert result["cases"][3]["authentication"] == "keyed_http_success"
    assert result["cases"][3]["attempts"] == 2
    # No new attempt when the same complete batch is reopened.
    again = run(tmp_path, calls)
    assert again["http_attempts"] == 8 and len(calls) == 8
    serialized = "\n".join(path.read_text() for path in tmp_path.rglob("*.json"))
    assert all(value not in serialized for value in VALUES.values())
    assert "Unqualified raw text must not leak." not in serialized


def test_cap_between_pubmed_legs_stops_before_next_request(tmp_path):
    calls = []
    result = run(tmp_path, calls, selected=[runner.CASES[3]], cap=1)
    assert len(calls) == 1 and result["stop_reason"] == "attempt_cap"
    assert result["cases"][0]["status"] == "stopped"
    with pytest.raises(ValidationStop, match="attempt_cap"):
        runner.run_batch(tmp_path, "synthetic-batch", tmp_path / ".local/search-validation/different/state",
                         [runner.CASES[3]], max_attempts=1, values=VALUES, opener=responses(calls))
    assert len(calls) == 1


@pytest.mark.parametrize("status,payload,reason", [
    (401, {}, "authentication_rejected"),
    (200, {}, "malformed_provider_response"),
])
def test_first_auth_or_shape_error_stops_batch(tmp_path, status, payload, reason):
    calls = []
    def opener(*args, **kwargs):
        calls.append(1)
        return reply(payload, status)
    result = run(tmp_path, calls, opener=opener)
    assert result["stop_reason"] == reason and len(calls) == 1
    assert result["captured_responses"] == 1
    assert all(c["status"] == "not_run" for c in result["cases"][1:])


def test_empty_and_404_continue_independent_cases(tmp_path):
    calls = []
    def opener(*args, **kwargs):
        calls.append(1)
        return reply({"results": []}, 404 if len(calls) == 1 else 200)
    result = run(tmp_path, calls, selected=list(runner.CASES[:3]), opener=opener)
    assert result["stop_reason"] is None and len(calls) == 3
    assert [c["status"] for c in result["cases"]] == ["http_404", "empty", "empty"]
    assert "not_evidence_of_scientific_absence" in result["cases"][0]["reason"]


@pytest.mark.parametrize("change", ["state", "key"])
def test_restart_cannot_switch_state_or_credential(tmp_path, change):
    calls = []
    run(tmp_path, calls, selected=[runner.CASES[0]])
    state = tmp_path / ".local/search-validation/case/state"
    values = dict(VALUES)
    if change == "state":
        state = tmp_path / ".local/search-validation/other/state"
    else:
        values["EXA_API_KEY"] = "synthetic-changed-key"
    with pytest.raises(ValidationStop, match="batch_state_directory_changed" if change == "state" else "batch_credentials_changed"):
        runner.run_batch(tmp_path, "synthetic-batch", state, [runner.CASES[0]],
                         values=values, opener=responses(calls), execution_mode="offline_fixture")
    assert len(calls) == 1


def test_contents_failure_is_not_an_empty_search(tmp_path):
    calls = []
    normal = responses(calls)
    def opener(method, url, **kwargs):
        if url.endswith("/contents"):
            calls.append((method, url, kwargs.get("body")))
            return reply({"results": [], "statuses": [{"id": "https://example.org/trees", "status": "error"}]})
        return normal(method, url, **kwargs)
    result = run(tmp_path, calls, selected=[runner.CASES[0], runner.CASES[-1]], opener=opener)
    assert result["stop_reason"] is None
    assert result["cases"][-1]["status"] == "source_unavailable"
    assert len(calls) == 2


def test_malformed_result_item_stops_before_another_provider(tmp_path):
    calls = []
    def opener(*args, **kwargs):
        calls.append(1)
        return reply({"results": [123]})
    result = run(tmp_path, calls, opener=opener)
    assert result["stop_reason"] == "malformed_provider_items" and len(calls) == 1


@pytest.mark.parametrize("start", [1792293000.1234562, 1792293000.1234568])
def test_known_rate_wait_uses_saved_reset_and_only_one_retry(tmp_path, monkeypatch, start):
    epoch = [start]
    sleeps, calls = [], []
    def sleep(seconds):
        sleeps.append(seconds)
        epoch[0] += seconds
    monkeypatch.setattr(discovery, "time", SimpleNamespace(time=lambda: epoch[0], sleep=sleep, perf_counter=lambda: epoch[0]))
    def opener(*args, **kwargs):
        calls.append(epoch[0])
        return reply({}, 429, {"retry-after": "2"})
    result = run(tmp_path, calls, selected=[runner.CASES[0]], opener=opener,
                 clock=lambda: epoch[0], sleep=sleep)
    assert len(calls) == 2 and calls[1] >= calls[0] + 2
    assert sum(sleeps) == pytest.approx(2)
    assert result["cases"][0]["status"] == "pending"
    ledger = json.loads((tmp_path / ".local/search-validation/batches/synthetic-batch.json").read_text())
    assert ledger["cases"]["exa-fast"]["retry_count"] == 1
    assert len(ledger["attempts"]) == 2
    assert result["stop_reason"] is None


def test_uncertain_transport_preserved_without_retry(tmp_path):
    calls = []
    def opener(*args, **kwargs):
        calls.append(1)
        raise TimeoutError("synthetic-private-error")
    result = run(tmp_path, calls, opener=opener)
    assert result["stop_reason"] == "uncertain_transport" and len(calls) == 1
    assert result["captured_responses"] == 0
    ledger = tmp_path / ".local/search-validation/batches/synthetic-batch.json"
    assert "synthetic-private-error" not in ledger.read_text()
    with pytest.raises(ValidationStop):
        run(tmp_path, calls, opener=opener)
    assert len(calls) == 1
