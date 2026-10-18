"""Search excerpts are bounded discovery aids, not retained source passages."""

from __future__ import annotations

import json

import pytest

from ora.retrieval.discovery import run_discovery_page
from ora.retrieval.normalize import normalize_response
from ora.retrieval.providers import HTTPResult, available_routes, map_response
from ora.state import StateStore


def request(route: str, *, attempt: str = "a1") -> dict:
    spec = available_routes()[route]
    return {
        "mission_id": "synthetic-mission", "task_id": "synthetic-task",
        "request_id": "synthetic-search", "attempt_id": attempt,
        "agent_instance_id": "synthetic-agent", "route": route,
        "provider": spec["provider"], "index": spec["index"],
        "operation": spec["operation"], "query": "synthetic evidence",
        "filters": {"mode": "fast"} if route == "exa.search" else {},
        "requested_at": "2026-10-15T10:00:00Z",
        "requested_window": {"limit": 1},
    }


def normalized(route: str, payload: dict) -> dict:
    return normalize_response(
        request(route), map_response(route, payload),
        adapter="opaque_cursor", retrieved_at="2026-10-15T10:01:00Z",
    )["items"][0]


def test_exa_highlight_survives_as_discovery_only_with_redaction_and_bound():
    secret = "synthetic-secret-value"
    excerpt = "Relevant first words. token=" + secret + "\x00\u200b " + "more " * 500
    item = normalized("exa.search", {"results": [{
        "id": "exa-item", "url": "https://example.org/source", "title": "Source",
        "highlights": [excerpt], "summary": "Generated answer must not replace highlights",
    }]})
    assert item["access_surface"] == "teaser"
    assert item["origin"] == "original_source"
    assert item["rights_status"] == "unknown"
    assert item["content"] is None and item["content_sha256"] is None
    assert item["content_retained"] is False
    assert item["discovery_excerpt"].startswith("Relevant first words. token=[REDACTED]")
    assert secret not in item["discovery_excerpt"]
    assert "Generated answer" not in str(item)
    assert "\x00" not in item["discovery_excerpt"]
    assert "\u200b" not in item["discovery_excerpt"]
    assert len(item["discovery_excerpt"]) <= 300
    assert "$.discovery_excerpt" in item["redacted_fields"]


@pytest.mark.parametrize("route", ["tavily.search", "searxng.search"])
def test_other_search_snippet_is_not_source_content(route):
    item = normalized(route, {"results": [{
        "url": "https://example.org/source", "title": "Source",
        "content": "A short provider snippet for selection.",
    }]})
    assert item["discovery_excerpt"] == "A short provider snippet for selection."
    assert item["content"] is None and item["content_retained"] is False
    assert item["rights_status"] == "unknown"


@pytest.mark.parametrize("route,payload", [
    ("exa.search", {"results": [{
        "id": "x", "url": "https://example.org/source", "highlights": ["Not for use"],
        "explicit_prohibition_detected": True,
    }]}),
    ("tavily.search", {"results": [{
        "url": "https://example.org/source", "content": "Not for use",
        "rights_status": "not_retained",
    }]}),
])
def test_explicit_provider_restriction_withholds_excerpt(route, payload):
    item = normalized(route, payload)
    assert item["rights_status"] == "restricted"
    assert item["discovery_excerpt"] is None


def test_missing_or_malformed_highlights_and_untrusted_routes_do_not_gain_excerpt():
    for highlights in (None, ["valid", 7], []):
        item = normalized("exa.search", {"results": [{
            "id": "x", "url": "https://example.org/source", "highlights": highlights,
        }]})
        assert item["discovery_excerpt"] is None
    raw = {"status": "ok", "results": [{
        "id": "x", "url": "https://example.org/source", "title": "Source",
        "surface": "teaser", "origin": "original_source", "rights_status": "unknown",
        "discovery_excerpt": "Should not cross into this route",
    }]}
    item = normalize_response(
        request("exa.contents"), raw, adapter="opaque_cursor",
        retrieved_at="2026-10-15T10:01:00Z",
    )["items"][0]
    assert item["discovery_excerpt"] is None


def test_saved_search_replays_same_excerpt_without_another_http_call(tmp_path):
    store = StateStore(tmp_path)
    calls = []
    payload = {"results": [{
        "id": "exa-item", "url": "https://example.org/source", "title": "Source",
        "highlights": ["A useful candidate, still uninspected."],
    }]}

    def transport(method, url, *, body=None, headers=None):
        calls.append((method, url))
        return HTTPResult(200, json.dumps(payload).encode(), {})

    first = run_discovery_page(store, request("exa.search"), credential="synthetic-key", transport=transport)
    again = run_discovery_page(store, request("exa.search"), credential="synthetic-key", transport=transport)
    assert len(calls) == 1
    assert first["hits"][0]["discovery_excerpt"] == "A useful candidate, still uninspected."
    assert again["hits"][0]["discovery_excerpt"] == first["hits"][0]["discovery_excerpt"]
    source = store.get(**first["hits"][0]["hit_ref"])["body"]["source"]
    assert source["content"] is None and source["rights_status"] == "unknown"
    assert source["content_retained"] is False


def test_changed_excerpt_creates_distinct_hit_observation(tmp_path):
    from ora.retrieval.receipts import record_delivery

    store = StateStore(tmp_path)
    pointers = []
    for index, excerpt in enumerate(('Earlier candidate excerpt', 'Changed candidate excerpt')):
        selected = request('exa.search')
        selected['attempt_id'] = f'attempt-{index}'
        mapped = map_response('exa.search', {'results': [{'id': 'same', 'url': 'https://example.org/same',
            'title': 'Same source', 'highlights': [excerpt]}]})
        result = record_delivery(store, selected, mapped, delivery_id=f'delivery-{index}',
            adapter='opaque_cursor', retrieved_at='2026-01-01T00:00:00Z')
        pointers.extend(result['accepted_hit_refs'])
    assert len(pointers) == 2 and pointers[0] != pointers[1]
    assert [store.get(**pointer)['body']['source']['discovery_excerpt'] for pointer in pointers] == [
        'Earlier candidate excerpt', 'Changed candidate excerpt']


def test_explicit_source_notice_withholds_excerpt_in_both_saved_surfaces(tmp_path):
    from ora.retrieval.receipts import record_delivery

    store = StateStore(tmp_path)
    mapped = map_response('exa.search', {'results': [{'id': 'restricted', 'url': 'https://example.org/restricted',
        'title': 'Restricted source', 'highlights': ['Do not retain this candidate excerpt']} ]})
    result = record_delivery(store, request('exa.search'), mapped, delivery_id='notice-delivery',
        adapter='opaque_cursor', retrieved_at='2026-01-01T00:00:00Z',
        source_use_notices={'restricted': {'explicit_prohibition_detected': True}})
    source = store.get(**result['accepted_hit_refs'][0])['body']['source']
    assert source['discovery_excerpt'] is None
    raw = store.get(**result['delivery_ref'])['body']
    assert 'Do not retain this candidate excerpt' not in store.read_blob(raw['blob']['sha256']).decode()
