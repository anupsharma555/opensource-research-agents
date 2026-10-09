"""Offline provider-shape examples; no provider endpoint or account is used."""

from __future__ import annotations

from ora.retrieval.normalize import normalize_response


def request(**changes):
    value = {
        "mission_id": "synthetic-mission",
        "task_id": "synthetic-task",
        "request_id": "search-a",
        "attempt_id": "try-a",
        "agent_instance_id": "synthetic-collector",
        "route": "synthetic-index",
        "provider": "simulated-count",
        "index": "synthetic-literature",
        "operation": "search",
        "query": "adult fictional intervention",
        "filters": {"year_from": 2020},
        "requested_at": "2026-10-08T10:00:00Z",
        "requested_window": {"offset": 0, "cursor": None, "limit": 20},
    }
    value.update(changes)
    return value


def test_conflicting_optional_metadata_preserves_both_observations():
    result = normalize_response(request(), {
        "status": "ok", "count": 1, "offset": 0,
        "items": [hit(1, publication_date="2024-01-01", scientific_metadata={
            "publication_date": "2024-02-01", "language": "en",
        })],
    }, adapter="count_offset", retrieved_at="2026-10-08T10:01:00Z")
    item = result["items"][0]
    assert item["scientific_metadata"]["publication_date"] == "2024-02-01"
    assert item["scientific_metadata"]["_mapping_conflicts"] == [{
        "field": "publication_date", "selected_from": "scientific_metadata",
        "alternative_from": "publication_date", "alternative_value": "2024-01-01",
    }]
    assert result["outcome"] == "success"


def hit(index: int, **changes):
    value = {
        "id": f"synthetic-{index}",
        "url": f"https://example.test/article/{index}",
        "title": f"Fictional report {index}",
        "content": f"Reported result {index}",
        "surface": "abstract",
        "origin": "original_source",
        "rights_status": "permitted",
        "access_status": "available",
    }
    value.update(changes)
    return value


def test_count_offset_keeps_first_twenty_and_reports_known_unseen_twenty_first():
    response = {
        "status": "ok", "count": 21, "offset": 0, "max_visible": 100,
        "items": [hit(i) for i in range(1, 21)], "usage": {"requests": 1},
    }
    result = normalize_response(
        request(), response, adapter="count_offset", retrieved_at="2026-10-08T10:01:00Z",
        continuation={"action": "continue", "reason": "A further result is available"},
        documented_usage=[{"unit": "provider_credit", "amount": 2}],
    )
    assert result["outcome"] == "success"
    assert result["window"]["returned_count"] == 20
    assert result["window"]["known_unseen_count"] == 1
    assert result["window"]["more_available"] == "yes"
    assert result["window"]["next_window"] == {"offset": 20, "limit": 20}
    assert result["items"][-1]["rank"] == 20
    assert result["usage"] == [
        {"unit": "requests", "amount": 1, "basis": "observed"},
        {"unit": "provider_credit", "amount": 2, "basis": "documented"},
    ]


def test_mixed_invalid_item_keeps_sibling_and_provider_summary_stays_separate():
    response = {
        "status": "ok", "count": 3, "offset": 0,
        "items": [hit(1), hit(2, surface="abstract", origin="provider_generated"),
                  hit(3, surface="provider_summary", origin="provider_generated")],
    }
    result = normalize_response(
        request(requested_window={"offset": 0, "limit": 3}), response,
        adapter="count_offset", retrieved_at="2026-10-08T10:01:00Z",
    )
    assert result["outcome"] == "partial"
    assert [item["provider_item_id"] for item in result["items"]] == ["synthetic-1", "synthetic-3"]
    assert result["items"][-1]["access_surface"] == "provider_summary"
    assert result["item_errors"] == [{
        "position": 1, "code": "invalid_item",
        "reason": "Provider summary cannot be original-source evidence",
    }]
    assert result["window"]["provider_stop_reason"] == "exhausted"


def test_cursor_unknown_total_and_agent_stop_do_not_claim_exhaustion():
    response = {
        "status": "ok", "results": [hit(1)], "total": None,
        "cursor": "page-a", "next_cursor": "page-b", "has_more": True,
    }
    result = normalize_response(
        request(provider="simulated-cursor", requested_window={"cursor": "page-a", "limit": 1}),
        response, adapter="opaque_cursor", retrieved_at="2026-10-08T10:01:00Z",
        continuation={"action": "switch_route", "reason": "Check a complementary index"},
    )
    assert result["window"]["provider_total"] is None
    assert result["window"]["known_unseen_count"] is None
    assert result["window"]["more_available"] == "yes"
    assert result["window"]["next_window"] == {"cursor": "page-b"}
    assert result["window"]["continuation"]["action"] == "switch_route"
    assert result["items"][0]["rank_scope"] == "page"
    cursor_total_without_position = normalize_response(
        request(provider="simulated-cursor", requested_window={"cursor": "page-a", "limit": 1}),
        {"status": "ok", "results": [hit(1)], "total": 21},
        adapter="opaque_cursor", retrieved_at="2026-10-08T10:01:00Z",
    )
    assert cursor_total_without_position["window"]["provider_total"] == 21
    assert cursor_total_without_position["window"]["known_unseen_count"] is None
    assert cursor_total_without_position["window"]["more_available"] == "unknown"


def test_fetch_uses_same_envelope_without_inventing_search_query_or_total():
    result = normalize_response(
        request(
            operation="fetch", query=None, requested_window={"offset": None, "limit": None},
        ),
        {"status": "ok", "items": [hit(1, surface="full_text")]},
        adapter="count_offset", retrieved_at="2026-10-08T10:01:00Z",
    )
    assert result["request"]["operation"] == "fetch"
    assert result["request"]["query"] is None
    assert result["window"]["provider_total"] is None
    assert result["items"][0]["access_surface"] == "full_text"
    assert result["outcome"] == "success"


def test_empty_item_failure_provider_failure_and_malformed_are_distinct():
    base = request(requested_window={"offset": 0, "limit": 2})
    cases = [
        ({"status": "ok", "count": 0, "offset": 0, "items": []}, "empty", "exhausted"),
        ({"status": "ok", "count": 1, "offset": 0, "items": [{"error": "bad item"}]},
         "item_failure", "exhausted"),
        ({"status": "error", "error_code": "route_unavailable", "error": "route unavailable"},
         "provider_failure", "error"),
        ({"status": "quota_wait"}, "provider_failure", "quota_wait"),
        (["malformed"], "malformed_payload", "error"),
    ]
    for response, outcome, stop in cases:
        result = normalize_response(
            base, response, adapter="count_offset", retrieved_at="2026-10-08T10:01:00Z",
        )
        assert (result["outcome"], result["window"]["provider_stop_reason"]) == (outcome, stop)
        assert result["items"] == []
        if outcome == "provider_failure":
            assert result["failure"]["category"] in {"provider_error", "quota_wait"}


def test_provider_exposure_limit_and_unknown_total_are_not_silent_exhaustion():
    limited = normalize_response(
        request(requested_window={"offset": 20, "limit": 20}),
        {"status": "ok", "count": 100, "offset": 20, "max_visible": 40,
         "items": [hit(i) for i in range(21, 41)]},
        adapter="count_offset", retrieved_at="2026-10-08T10:01:00Z",
    )
    assert limited["window"]["known_unseen_count"] == 60
    assert limited["window"]["more_available"] == "yes"
    assert limited["window"]["next_window"] is None
    assert limited["window"]["provider_stop_reason"] == "provider_limit"
    unknown = normalize_response(
        request(), {"status": "ok", "offset": 0, "items": [hit(1)]},
        adapter="count_offset", retrieved_at="2026-10-08T10:01:00Z",
    )
    assert unknown["window"]["provider_total"] is None
    assert unknown["window"]["more_available"] == "unknown"
    assert unknown["window"]["provider_stop_reason"] == "unknown"
    no_usable_next_window = normalize_response(
        request(), {"status": "ok", "count": 21, "has_more": True, "items": [hit(1)]},
        adapter="count_offset", retrieved_at="2026-10-08T10:01:00Z",
    )
    assert no_usable_next_window["window"]["more_available"] == "yes"
    assert no_usable_next_window["window"]["next_window"] is None
    assert no_usable_next_window["window"]["provider_stop_reason"] == "unknown"


def test_request_url_and_content_redaction_preserve_lineage_without_secret_values():
    secret = "synthetic-secret-value"
    raw = {
        "status": "ok", "count": 1, "offset": 0,
        "Authorization": f"Bearer {secret}", "providerTrace": "unmapped private trace sentinel",
        "items": [hit(1, url=f"https://user:pass@example.test/a?providerApiKey={secret}&topic=trial#frag",
                      content=f"Abstract text; Bearer {secret}")],
    }
    result = normalize_response(
        request(query=f"adult topic token={secret}", filters={
            "Authorization": f"Bearer {secret}", "providerApiKey": secret,
        }),
        raw, adapter="count_offset", retrieved_at="2026-10-08T10:01:00Z",
    )
    assert secret not in str(result)
    assert "pass@" not in str(result)
    assert result["items"][0]["url_redacted"] is True
    assert result["items"][0]["original_url_sha256"]
    assert "$.items[0].original_url" in result["redacted_paths"]
    assert "Authorization" not in result["request"]["filters"]
    assert "providerApiKey" not in result["request"]["filters"]
    assert "$.filters.Authorization" in result["redacted_paths"]
    assert {entry["field"] for entry in result["omitted_fields"]} == {
        "[sensitive-field]", "providerTrace",
    }
    assert "unmapped private trace sentinel" not in str(result)
    restricted = normalize_response(
        request(), {"status": "ok", "count": 1, "offset": 0,
                    "items": [hit(1, rights_status="restricted", content="Do not retain me")]},
        adapter="count_offset", retrieved_at="2026-10-08T10:01:00Z",
    )
    assert "Do not retain me" not in str(restricted)
    assert restricted["items"][0]["content"] is None
    assert restricted["items"][0]["content_unretained_reason"] == "rights_not_confirmed"


def test_secret_named_usage_unit_is_dropped_without_losing_valid_hits():
    result = normalize_response(
        request(),
        {"status": "ok", "count": 1, "offset": 0, "items": [hit(1)],
         "usage": {"provider_request": 1, "access_token": 42, "input_tokens": 30}},
        adapter="count_offset", retrieved_at="2026-10-08T10:01:00Z",
        documented_usage=[{"unit": "provider_credit", "amount": 2}],
    )
    assert result["outcome"] == "partial"
    assert result["window"]["window_errors"] == ["invalid_observed_usage"]
    assert result["usage"] == [
        {"unit": "provider_request", "amount": 1, "basis": "observed"},
        {"unit": "input_tokens", "amount": 30, "basis": "observed"},
        {"unit": "provider_credit", "amount": 2, "basis": "documented"},
    ]
    assert len(result["items"]) == 1
    assert "access_token" not in str(result["raw_projection"])


def test_explicit_scientific_fields_preserve_nested_values_and_report_omissions():
    secret = "synthetic-secret-value"
    source = hit(
        1,
        authors=[{"name": "Ada Example", "orcid": "0000-0000-0000-0000",
                  "email": "private@example.test"}],
        tables=[{"caption": "Reported outcomes", "columns": ["group", "n", "estimate"],
                 "rows": [["intervention", 20, 0.25], ["control", 19, None]],
                 "note": {"missing": False}}],
        scientific_metadata={"journal": "Simulated Studies", "year": 2026,
                             "providerApiKey": secret},
        structured_content={"sections": [{"name": "Results", "sample_size": 39}]},
        citation_count=12, vendor_debug_trace="unmapped trace",
    )
    result = normalize_response(
        request(), {"status": "ok", "offset": 0, "items": [source]},
        adapter="count_offset", retrieved_at="2026-10-08T10:01:00Z",
    )
    saved = result["items"][0]
    assert result["mapping_version"] == "reference-count-offset.scientific-v1"
    assert saved["scientific_metadata"]["authors"][0]["orcid"] == "0000-0000-0000-0000"
    assert saved["scientific_metadata"]["year"] == 2026
    assert saved["scientific_metadata"]["citation_count"] == 12
    assert saved["structured_content"]["tables"][0]["rows"] == [
        ["intervention", 20, 0.25], ["control", 19, None],
    ]
    assert saved["structured_content"]["sections"][0]["sample_size"] == 39
    assert {entry["field"] for entry in saved["omitted_fields"]} >= {
        "vendor_debug_trace", "scientific_metadata.[sensitive-field]",
        "authors[0].[sensitive-field]",
    }
    assert secret not in str(result)
    assert "private@example.test" not in str(result)


def test_rights_limited_table_is_omitted_without_losing_bibliographic_authors():
    result = normalize_response(
        request(),
        {"status": "ok", "offset": 0, "items": [hit(
            1, rights_status="restricted", authors=["Ada Example"],
            tables=[{"rows": [["restricted source text", 17]]}],
            scientific_metadata={"full_text": "restricted hidden text"},
        )]},
        adapter="count_offset", retrieved_at="2026-10-08T10:01:00Z",
    )
    item = result["items"][0]
    assert item["scientific_metadata"]["authors"] == ["Ada Example"]
    assert item["structured_content"] == {}
    assert {entry["field"]: entry["reason"] for entry in item["omitted_fields"]}["tables"] == "rights_not_confirmed"
    assert "restricted source text" not in str(result)
    assert "restricted hidden text" not in str(result)
    assert {entry["field"]: entry["reason"] for entry in item["omitted_fields"]}[
        "scientific_metadata.full_text"
    ] == "content_requires_structured_content"
