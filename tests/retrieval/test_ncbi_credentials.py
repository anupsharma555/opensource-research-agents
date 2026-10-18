"""Synthetic NCBI key and pacing checks; no provider request is made."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from ora.retrieval.discovery import _admit_ncbi, run_discovery_page
from ora.retrieval.providers import HTTPResult, available_routes, build_call
from ora.state import StateStore

KEY = "synthetic-ncbi-key"


def request(route: str, *, attempt: str = "a1", resume: dict | None = None) -> dict:
    route_spec = available_routes()[route]
    selected = {
        "mission_id": "synthetic-mission", "task_id": "synthetic-task",
        "request_id": "synthetic-" + route, "attempt_id": attempt,
        "agent_instance_id": "synthetic-agent", "route": route,
        "provider": route_spec["provider"], "index": route_spec["index"],
        "operation": route_spec["operation"],
        "query": "123" if route != "pubmed.search" else "synthetic study",
        "filters": {}, "requested_at": "2026-10-15T10:00:00Z",
        "requested_window": {"offset": 0, "limit": 1},
    }
    if resume is not None:
        selected["resume_esearch_ref"] = resume
    return selected


class FakeHTTP:
    def __init__(self, *responses: HTTPResult):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict | None, dict]] = []

    def __call__(self, method, url, *, body=None, headers=None):
        self.calls.append((method, url, body, headers))
        return self.responses.pop(0)


def packet(body: dict, status: int = 200) -> HTTPResult:
    return HTTPResult(status, json.dumps(body).encode(), {})


@pytest.mark.parametrize("route,endpoint", [
    ("pubmed.search", "esearch.fcgi"),
    ("pubmed.pmid", "esummary.fcgi"),
    ("pubmed.abstract", "efetch.fcgi"),
])
def test_optional_key_is_sent_on_each_pubmed_route(route, endpoint):
    _, keyed_url, _, _ = build_call(request(route), KEY)
    _, public_url, _, _ = build_call(request(route))
    assert urlsplit(keyed_url).path.endswith(endpoint)
    assert parse_qs(urlsplit(keyed_url).query)["api_key"] == [KEY]
    assert "api_key" not in parse_qs(urlsplit(public_url).query)


def test_search_and_summary_use_key_but_saved_receipts_redact_it(tmp_path, monkeypatch):
    import ora.retrieval.discovery as discovery

    monkeypatch.setattr(discovery.time, "sleep", lambda _: None)
    http = FakeHTTP(
        packet({"esearchresult": {"count": "1", "idlist": ["123"]}}),
        packet({"result": {"uids": ["123"], "123": {
            "uid": "123", "title": "Synthetic title",
        }}}),
    )
    store = StateStore(tmp_path)
    result = run_discovery_page(store, request("pubmed.search"), credential=KEY, transport=http)
    assert result["outcome"] == "success"
    assert [urlsplit(call[1]).path.rsplit("/", 1)[-1] for call in http.calls] == [
        "esearch.fcgi", "esummary.fcgi",
    ]
    assert all(parse_qs(urlsplit(call[1]).query)["api_key"] == [KEY] for call in http.calls)
    assert KEY not in json.dumps(result)
    assert KEY not in json.dumps(store.get(**result["page_ref"]))
    assert KEY not in json.dumps(store.get(**result["delivery_ref"]))
    assert KEY not in json.dumps(store.get(**result["esearch_ref"]))
    assert "REDACTED" in result["http_call"]["url"]


def test_resumed_summary_uses_key_without_repeating_esearch(tmp_path, monkeypatch):
    import ora.retrieval.discovery as discovery

    monkeypatch.setattr(discovery.time, "sleep", lambda _: None)
    store = StateStore(tmp_path)
    first_http = FakeHTTP(
        packet({"esearchresult": {"count": "1", "idlist": ["123"]}}),
        packet({}, status=500),
    )
    first = run_discovery_page(store, request("pubmed.search"), credential=KEY, transport=first_http)
    assert first["esearch_ref"]
    resumed_http = FakeHTTP(packet({"result": {"uids": ["123"], "123": {
        "uid": "123", "title": "Synthetic title",
    }}}))
    resumed = run_discovery_page(
        store, request("pubmed.search", attempt="a2", resume=first["esearch_ref"]),
        credential=KEY, transport=resumed_http,
    )
    assert resumed["outcome"] == "success"
    assert len(resumed_http.calls) == 1
    assert urlsplit(resumed_http.calls[0][1]).path.endswith("esummary.fcgi")
    assert parse_qs(urlsplit(resumed_http.calls[0][1]).query)["api_key"] == [KEY]
    assert KEY not in json.dumps(resumed)


def test_efetch_key_is_redacted_even_on_error(tmp_path):
    http = FakeHTTP(packet({}, status=401))
    result = run_discovery_page(
        StateStore(tmp_path), request("pubmed.abstract"), credential=KEY, transport=http,
    )
    assert parse_qs(urlsplit(http.calls[0][1]).query)["api_key"] == [KEY]
    assert result["outcome"] == "provider_failure"
    assert KEY not in json.dumps(result)


def test_local_ncbi_reservations_separate_public_and_per_key_pools(tmp_path, monkeypatch):
    import ora.retrieval.discovery as discovery

    monkeypatch.setattr(discovery.time, "time", lambda: 1000.0)
    store = StateStore(tmp_path)
    assert _admit_ncbi(store)["wait_seconds"] == 0
    assert _admit_ncbi(store)["wait_seconds"] == pytest.approx(0.36)
    assert _admit_ncbi(store, KEY)["wait_seconds"] == 0
    assert _admit_ncbi(store, KEY)["wait_seconds"] == pytest.approx(0.1)
    assert _admit_ncbi(store, "another-synthetic-key")["wait_seconds"] == 0
    assert _admit_ncbi(store, KEY, requests_per_second=2)["wait_seconds"] == pytest.approx(0.2)
    assert _admit_ncbi(store, requests_per_second=2)["wait_seconds"] == pytest.approx(0.72)


@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_invalid_local_ncbi_limit_is_rejected_before_dispatch(tmp_path, value):
    store = StateStore(tmp_path)
    with pytest.raises(ValueError, match="NCBI requests per second"):
        run_discovery_page(
            store, request("pubmed.pmid"), credential=KEY,
            ncbi_requests_per_second=value, transport=FakeHTTP(),
        )
