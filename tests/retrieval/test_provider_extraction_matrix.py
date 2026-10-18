"""Offline provider-shape matrix: candidate excerpts never become checked text."""

import pytest

from ora.retrieval.normalize import normalize_response
from ora.retrieval.providers import available_routes, map_response


def _normalized(route: str, payload: dict):
    spec = available_routes()[route]
    request = {
        "mission_id": "synthetic-mission", "task_id": "synthetic-task",
        "request_id": "synthetic-request", "attempt_id": "synthetic-attempt",
        "agent_instance_id": "synthetic-agent", "route": route,
        "provider": spec["provider"], "index": spec["index"],
        "operation": spec["operation"], "query": "synthetic topic",
        "filters": {"mode": "fast"} if route == "exa.search" else {},
        "requested_at": "2026-01-01T00:00:00Z",
        "requested_window": {"limit": 10},
    }
    return normalize_response(request, map_response(route, payload),
                              adapter="opaque_cursor", retrieved_at="2026-01-01T00:01:00Z")


@pytest.mark.parametrize("route", ["exa.search", "tavily.search", "searxng.search"])
def test_provider_candidate_is_excerpt_only_and_not_checked_source(route):
    item = {"id": "candidate" if route == "exa.search" else None,
            "url": "https://example.org/first", "title": "First"}
    item.update({"highlights": ["Candidate excerpt."]} if route == "exa.search"
                else {"content": "Candidate excerpt."})
    result = _normalized(route, {"results": [item]})
    assert result["outcome"] == "success"
    source = result["items"][0]
    assert source["discovery_excerpt"] == "Candidate excerpt."
    assert source["content"] is None and source["content_retained"] is False
    assert source["access_surface"] == "teaser"
    assert source["original_url"] == "https://example.org/first"


@pytest.mark.parametrize("route", ["exa.search", "tavily.search", "searxng.search"])
def test_provider_explicit_restriction_withholds_excerpt(route):
    item = {"id": "candidate" if route == "exa.search" else None,
            "url": "https://example.org/restricted", "title": "Restricted",
            "explicit_prohibition_detected": True}
    item.update({"highlights": ["Do not use this candidate."]} if route == "exa.search"
                else {"content": "Do not use this candidate."})
    result = _normalized(route, {"results": [item]})
    assert result["items"][0]["rights_status"] == "restricted"
    assert result["items"][0]["discovery_excerpt"] is None
    assert result["items"][0]["content"] is None


@pytest.mark.parametrize("route", ["exa.search", "tavily.search", "searxng.search"])
def test_provider_empty_and_malformed_item_are_distinct(route):
    empty = _normalized(route, {"results": []})
    assert empty["outcome"] == "empty" and empty["items"] == []
    malformed = _normalized(route, {"results": [{"title": "No source identity"}]})
    assert malformed["outcome"] == "item_failure"
    assert malformed["items"] == [] and len(malformed["item_errors"]) == 1


@pytest.mark.parametrize("route", ["exa.search", "tavily.search", "searxng.search"])
def test_duplicate_provider_urls_remain_distinct_observations_until_later_grouping(route):
    items = []
    for index in (1, 2):
        item = {"id": f"candidate-{index}" if route == "exa.search" else None,
                "url": "https://example.org/same", "title": f"Version {index}"}
        item.update({"highlights": [f"Excerpt {index}."]} if route == "exa.search"
                    else {"content": f"Excerpt {index}."})
        items.append(item)
    result = _normalized(route, {"results": items})
    assert result["outcome"] == "success" and len(result["items"]) == 2
    assert [item["discovery_excerpt"] for item in result["items"]] == [
        "Excerpt 1.", "Excerpt 2.",
    ]
    assert all(item["content"] is None for item in result["items"])


@pytest.mark.parametrize("route", ["exa.search", "tavily.search", "searxng.search"])
def test_provider_error_stays_diagnostic_without_candidate_text(route):
    spec = available_routes()[route]
    request = {
        "mission_id": "synthetic-mission", "task_id": "synthetic-task",
        "request_id": "synthetic-request", "attempt_id": "synthetic-attempt",
        "agent_instance_id": "synthetic-agent", "route": route,
        "provider": spec["provider"], "index": spec["index"],
        "operation": spec["operation"], "query": "synthetic topic",
        "filters": {}, "requested_at": "2026-01-01T00:00:00Z",
        "requested_window": {"limit": 10},
    }
    result = normalize_response(request, {"status": "error", "error_code": "http",
                                          "error": "synthetic provider detail"},
                                adapter="opaque_cursor", retrieved_at="2026-01-01T00:01:00Z")
    assert result["outcome"] == "provider_failure" and result["items"] == []
    assert "synthetic provider detail" not in str(result)


def test_exa_contents_text_is_not_a_retained_source_without_rights_grant():
    result = _normalized("exa.contents", {"results": [{
        "id": "content-1", "url": "https://example.org/full", "title": "Full",
        "text": "Long provider-returned text.",
    }]})
    item = result["items"][0]
    assert item["access_surface"] == "passage" and item["origin"] == "original_source"
    assert item["content"] is None and item["content_retained"] is False
    assert item["content_unretained_reason"] == "rights_not_confirmed"
    assert item["discovery_excerpt"] is None


def test_exa_contents_summary_is_provider_generated_not_source_evidence():
    result = _normalized("exa.contents", {"results": [{
        "id": "content-2", "url": "https://example.org/summary", "title": "Summary",
        "summary": "Generated synopsis.",
    }]})
    item = result["items"][0]
    assert item["access_surface"] == "provider_summary"
    assert item["origin"] == "provider_generated"
    assert item["content"] is None and item["discovery_excerpt"] is None


def test_exa_contents_status_failure_keeps_good_result_separate():
    result = _normalized("exa.contents", {"results": [{
        "id": "content-3", "url": "https://example.org/available", "text": "Available text.",
    }], "statuses": [{"status": "error", "id": "missing"}]})
    assert result["outcome"] == "partial"
    assert len(result["items"]) == 1 and len(result["item_errors"]) == 1
    assert result["items"][0]["original_url"] == "https://example.org/available"
