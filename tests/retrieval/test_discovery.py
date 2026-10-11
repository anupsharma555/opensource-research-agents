"""Synthetic provider packets prove discovery mechanics, not live route access."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from ora.retrieval.catalog import (
    list_venues,
    propose_venue,
    record_navigation,
    resolve_venue,
    revise_venue,
    seed_catalog,
    validate_venue,
)
from ora.retrieval.discovery import available_routes, run_discovery_page
from ora.retrieval.providers import HTTPResult, build_call
from ora.retrieval.receipts import MAX_RESPONSE_BYTES, record_delivery
from ora.state import StateStore


def request(
    route="crossref.search",
    *,
    query="digital psychotherapy",
    attempt="a1",
    filters=None,
    window=None,
):
    spec = available_routes()[route]
    return {
        "mission_id": "synthetic-mission",
        "task_id": "synthetic-task",
        "request_id": "synthetic-" + route,
        "attempt_id": attempt,
        "agent_instance_id": "synthetic-agent",
        "route": route,
        "provider": spec["provider"],
        "index": spec["index"],
        "operation": spec["operation"],
        "query": query,
        "filters": filters or {},
        "requested_at": "2026-10-09T10:00:00Z",
        "requested_window": window or {"offset": 0, "limit": 2},
    }


class FakeHTTP:
    def __init__(self, *packets):
        self.packets = list(packets)
        self.calls = []

    def __call__(self, method, url, *, body=None, headers=None):
        self.calls.append((method, url, body, headers))
        return self.packets.pop(0)


def packet(body, status=200, headers=None):
    return HTTPResult(status, json.dumps(body).encode(), headers or {})


def crossref_item(doi="10.1234/synthetic"):
    return {
        "DOI": doi,
        "URL": "https://doi.org/" + doi,
        "title": ["Synthetic trial"],
        "container-title": ["Example Journal"],
        "published": {"date-parts": [[2024, 5, 2]]},
        "abstract": "<jats:p>Synthetic abstract.</jats:p>",
    }


def test_very_simple_doi_fetch_and_missing_id_are_distinct(tmp_path):
    store = StateStore(tmp_path)
    found = run_discovery_page(
        store,
        request("crossref.doi", query=None, filters={"doi": "10.1234/synthetic"}),
        transport=FakeHTTP(packet({"message": crossref_item()})),
    )
    assert found["outcome"] == "success"
    assert found["hits"][0]["scientific_metadata"]["doi"] == "10.1234/synthetic"
    assert found["hits"][0]["access_surface"] == "abstract"
    assert found["hits"][0]["content_sha256"]
    assert (
        store.get(**found["hits"][0]["hit_ref"])["body"]["source"]["content"]
        == "Synthetic abstract."
    )
    assert found["hits"][0]["source_use_policy"]["copyright_license_status"] == "unverified"
    missing = run_discovery_page(
        store,
        request("crossref.doi", query=None, attempt="a2", filters={"doi": "10.1234/missing"}),
        transport=FakeHTTP(packet({}, status=404)),
    )
    assert missing["outcome"] == "empty"
    assert missing["hits"] == []


@pytest.mark.parametrize(
    "route,selected_id,payload,identifier_key,identifier_value",
    [
        (
            "pubmed.pmid",
            "26559241",
            {
                "result": {
                    "uids": ["26559241"],
                    "26559241": {"uid": "26559241", "title": "Synthetic record"},
                }
            },
            "pmid",
            "26559241",
        ),
        (
            "crossref.doi",
            "10.1234/synthetic",
            {"message": crossref_item()},
            "doi",
            "10.1234/synthetic",
        ),
    ],
)
def test_exact_fetch_accepts_selected_id_without_topic_query(
    tmp_path, route, selected_id, payload, identifier_key, identifier_value
):
    store = StateStore(tmp_path)
    fake = FakeHTTP(packet(payload))
    result = run_discovery_page(
        store,
        request(route, query="", filters={"selected_id": selected_id}),
        transport=fake,
    )
    assert result["outcome"] == "success"
    assert result["hits"][0]["scientific_metadata"][identifier_key] == identifier_value
    assert selected_id.replace("/", "%2F") in fake.calls[0][1]
    description = available_routes()[route]
    assert "selected_id" in description["query_description"]
    assert "abstract" in description["returned_content"]


def test_exact_fetch_rejects_conflicting_identifiers_and_search_does_not_reinterpret_id(tmp_path):
    with pytest.raises(ValueError, match="Conflicting pmid"):
        run_discovery_page(
            StateStore(tmp_path / "a"),
            request("pubmed.pmid", query="", filters={"pmid": "123", "selected_id": "456"}),
            transport=FakeHTTP(),
        )
    with pytest.raises(ValueError, match="only an alias"):
        run_discovery_page(
            StateStore(tmp_path / "b"),
            request("pubmed.search", filters={"selected_id": "123"}),
            transport=FakeHTTP(),
        )


def test_simple_query_translation_and_saved_replay(tmp_path):
    store = StateStore(tmp_path)
    req = request(filters={"from_date": "2020-01-01", "until_date": "2025-12-31"})
    fake = FakeHTTP(packet({"message": {"items": [crossref_item()], "total-results": 1}}))
    result = run_discovery_page(store, req, transport=fake)
    filters = parse_qs(urlsplit(fake.calls[0][1]).query)["filter"][0]
    assert "from-pub-date:2020-01-01" in filters
    assert "until-pub-date:2025-12-31" in filters
    assert result["window"]["more_available"] == "no"
    assert run_discovery_page(store, req, transport=FakeHTTP()) == result


def test_route_specific_filters_reach_only_the_selected_provider(tmp_path):
    europe = FakeHTTP(packet({"hitCount": 0, "resultList": {"result": []}}))
    run_discovery_page(
        StateStore(tmp_path / "europe"),
        request("europe_pmc.search", filters={"journal": "Example Journal",
                "from_date": "2020-01-01"}, window={"cursor": "*", "limit": 1}),
        transport=europe,
    )
    assert 'JOURNAL%3A%22Example+Journal%22' in europe.calls[0][1]
    assert "FIRST_PDATE" in europe.calls[0][1]

    crossref = FakeHTTP(packet({"message": {"items": [], "total-results": 0}}))
    run_discovery_page(
        StateStore(tmp_path / "crossref"),
        request(filters={"type": "journal-article", "until_date": "2025-12-31"}),
        transport=crossref,
    )
    assert "type%3Ajournal-article" in crossref.calls[0][1]

    exa = FakeHTTP(packet({"results": []}))
    run_discovery_page(
        StateStore(tmp_path / "exa"),
        request("exa.search", filters={"mode": "fast"}, window={"limit": 1}),
        credential="synthetic-key", transport=exa,
    )
    assert exa.calls[0][2]["type"] == "fast"


@pytest.mark.parametrize(
    "start,end,expected_start,expected_end",
    [
        ("2024-02-29", "2024-02-29", "2024-02-28T23:59:59.999999Z",
         "2024-03-01T00:00:00.000000Z"),
        ("2024-01-31", "2024-02-01", "2024-01-30T23:59:59.999999Z",
         "2024-02-02T00:00:00.000000Z"),
        ("2024-12-31", "2025-01-01", "2024-12-30T23:59:59.999999Z",
         "2025-01-02T00:00:00.000000Z"),
    ],
)
def test_exa_date_only_filters_cover_inclusive_utc_calendar_days(
    start, end, expected_start, expected_end,
):
    method, url, body, _headers = build_call(
        request("exa.search", filters={"from_date": start, "until_date": end},
                window={"limit": 1}),
        credential="synthetic-key",
    )
    assert (method, url) == ("POST", "https://api.exa.ai/search")
    assert body["startPublishedDate"] == expected_start
    assert body["endPublishedDate"] == expected_end


@pytest.mark.parametrize(
    "filters,field",
    [
        ({"until_date": "9999-12-31"}, "until_date"),
        ({"from_date": "0001-01-01"}, "from_date"),
    ],
)
def test_exa_unrepresentable_exclusive_date_boundary_is_explicit(filters, field):
    with pytest.raises(ValueError, match=field):
        build_call(request("exa.search", filters=filters, window={"limit": 1}),
                   credential="synthetic-key")


@pytest.mark.parametrize(
    "route, filters, window, error",
    [
        ("pubmed.search", {"journal": "Example"}, {"offset": 0, "limit": 1}, "Unsupported"),
        ("europe_pmc.search", {"from_date": "2025-01-01", "until_date": "2020-01-01"},
         {"cursor": "*", "limit": 1}, "from_date"),
        ("europe_pmc.search", {"journal": 'Example" OR ALL'},
         {"cursor": "*", "limit": 1}, "journal"),
        ("crossref.search", {"type": "journal-article,from-pub-date:1900"},
         {"offset": 0, "limit": 1}, "type"),
        ("exa.contents", {"url": "https://example.org/article",
                          "selection_reason": ""}, {"limit": 1}, "selection_reason"),
        ("exa.contents", {"id": "selected", "url": "https://example.org/article",
                          "selection_reason": "Inspect source"}, {"limit": 1}, "exactly one"),
        ("exa.contents", {"url": "http://example.org/article",
                          "selection_reason": "Inspect source"}, {"limit": 1}, "public HTTPS"),
    ],
)
def test_invalid_route_filters_fail_before_dispatch_or_http(tmp_path, route, filters, window, error):
    store = StateStore(tmp_path)
    transport = FakeHTTP()
    with pytest.raises(ValueError, match=error):
        run_discovery_page(
            store, request(route, query=None if route == "exa.contents" else "digital therapy",
                           filters=filters, window=window),
            credential="synthetic-key" if route.startswith("exa.") else None,
            transport=transport,
        )
    assert transport.calls == []
    assert store.records("discovery_page") == []


def test_intermediate_europe_pmc_cursor_and_identifiers(tmp_path):
    store = StateStore(tmp_path)
    raw = {
        "hitCount": 3,
        "nextCursorMark": "cursor-2",
        "resultList": {
            "result": [
                {
                    "source": "MED",
                    "id": "123",
                    "pmid": "123",
                    "pmcid": "PMC123",
                    "doi": "10.1234/synthetic",
                    "title": "Synthetic digital study",
                    "abstractText": "<p>Synthetic reported result.</p>",
                    "isOpenAccess": "Y",
                }
            ]
        },
    }
    req = request(
        "europe_pmc.search",
        window={"cursor": "*", "limit": 1},
        filters={"from_date": "2020-01-01", "until_date": "2025-12-31"},
    )
    fake = FakeHTTP(packet(raw))
    first = run_discovery_page(store, req, transport=fake)
    assert "FIRST_PDATE" in fake.calls[0][1]
    assert first["hits"][0]["scientific_metadata"]["pmcid"] == "PMC123"
    assert first["window"]["next_window"] == {"cursor": "cursor-2"}
    assert (
        store.get(**first["hits"][0]["hit_ref"])["body"]["source"]["content"]
        == "Synthetic reported result."
    )
    second = run_discovery_page(
        store,
        request("europe_pmc.search", attempt="a2", window={"cursor": "cursor-2", "limit": 1}),
        transport=FakeHTTP(
            packet({"hitCount": 3, "nextCursorMark": "cursor-2", "resultList": {"result": []}})
        ),
    )
    assert second["outcome"] == "empty"
    assert second["window"]["more_available"] == "no"


def test_closed_full_text_published_abstract_has_narrow_private_use(tmp_path):
    store = StateStore(tmp_path)
    abstract = "Synthetic published abstract: n=40 and the outcome was reported."
    result = run_discovery_page(
        store,
        request("europe_pmc.search", window={"cursor": "*", "limit": 1}),
        transport=FakeHTTP(
            packet(
                {
                    "hitCount": 1,
                    "resultList": {
                        "result": [
                            {
                                "source": "MED",
                                "id": "123",
                                "title": "Synthetic trial",
                                "abstractText": abstract,
                                "isOpenAccess": "N",
                                "doi": "10.1234/synthetic",
                            }
                        ]
                    },
                }
            )
        ),
    )
    hit = store.get(**result["hits"][0]["hit_ref"])["body"]
    policy = hit["source_use_policy"]
    assert hit["source"]["content"] == abstract
    assert hit["source"]["access_surface"] == "abstract"
    assert policy["decision"] == "private_research_use"
    assert policy["copyright_license_status"] == "unverified"
    assert policy["full_text_access"] == "not_inferred"
    assert policy["redistribution"] == "not_authorized_by_this_decision"
    assert policy["abstract_sha256"] == hit["source"]["content_sha256"]
    assert policy["provider_response_sha256"] == result["http_legs"][0]["sha256"]
    assert result["hits"][0]["source_use_policy"] == policy


def test_same_abstract_in_two_missions_keeps_separate_hit_provenance(tmp_path):
    store = StateStore(tmp_path)
    payload = {"message": crossref_item()}
    original = request("crossref.doi", query="", filters={"selected_id": "10.1234/synthetic"})
    another = {**original, "mission_id": "another-mission"}
    first = run_discovery_page(store, original, transport=FakeHTTP(packet(payload)))
    second = run_discovery_page(store, another, transport=FakeHTTP(packet(payload)))
    assert first["hits"][0]["hit_ref"] != second["hits"][0]["hit_ref"]
    assert store.get(**second["hits"][0]["hit_ref"])["body"]["mission_id"] == "another-mission"


def test_explicit_abstract_restriction_and_exa_text_do_not_gain_private_policy(tmp_path):
    store = StateStore(tmp_path)
    blocked = run_discovery_page(
        store,
        request("crossref.doi", query="", filters={"selected_id": "10.1234/synthetic"}),
        transport=FakeHTTP(
            packet(
                {
                    "message": {
                        **crossref_item(),
                        "rights_status": "restricted",
                    }
                }
            )
        ),
    )
    blocked_hit = store.get(**blocked["hits"][0]["hit_ref"])["body"]
    assert blocked_hit["source"]["access_surface"] == "abstract"
    assert blocked_hit["source"]["content"] is None
    assert blocked_hit["source_use_policy"]["decision"] == "no_text_use"
    assert blocked_hit["source_use_policy"]["source_notice"]["explicit_prohibition_detected"]
    exa = run_discovery_page(
        store,
        request(
            "exa.contents",
            query=None,
            attempt="exa-policy",
            filters={"id": "synthetic", "selection_reason": "Inspect this candidate"},
            window={"limit": 1},
        ),
        credential="synthetic-key",
        transport=FakeHTTP(
            packet(
                {
                    "results": [
                        {
                            "id": "synthetic",
                            "url": "https://example.org/article",
                            "text": "Candidate full text",
                        }
                    ]
                }
            )
        ),
    )
    assert store.get(**exa["hits"][0]["hit_ref"])["body"]["source_use_policy"] is None


def test_explicit_provider_copyright_notice_overrides_private_abstract_default(tmp_path):
    store = StateStore(tmp_path)
    result = run_discovery_page(
        store,
        request("europe_pmc.search", window={"cursor": "*", "limit": 1}),
        transport=FakeHTTP(
            packet(
                {
                    "hitCount": 1,
                    "resultList": {
                        "result": [
                            {
                                "source": "MED",
                                "id": "123",
                                "abstractText": "Synthetic trial abstract.",
                                "isOpenAccess": "N",
                                "copyrightStatement": "Copyright Example Publisher. No text and data mining.",
                            }
                        ]
                    },
                }
            )
        ),
    )
    hit = store.get(**result["hits"][0]["hit_ref"])["body"]
    assert hit["source"]["rights_status"] == "restricted"
    assert hit["source"]["content"] is None
    notice = hit["source_use_policy"]["source_notice"]
    assert notice["explicit_prohibition_detected"] is True
    assert notice["full_text_oa_reported"] == "N"
    assert hit["source_use_policy"]["allowed_uses"] == ["discovery"]


def test_crossref_license_url_is_saved_as_clue_not_abstract_grant(tmp_path):
    store = StateStore(tmp_path)
    item = {
        **crossref_item(),
        "license": [
            {
                "URL": "https://example.org/license",
                "content-version": "vor",
            }
        ],
    }
    result = run_discovery_page(
        store,
        request("crossref.doi", query="", filters={"selected_id": "10.1234/synthetic"}),
        transport=FakeHTTP(packet({"message": item})),
    )
    policy = result["hits"][0]["source_use_policy"]
    assert policy["source_notice"]["license_urls"] == ["https://example.org/license"]
    assert policy["copyright_license_status"] == "unverified"
    assert policy["redistribution"] == "not_authorized_by_this_decision"


def test_delivery_rejects_retained_abstract_against_explicit_source_prohibition(tmp_path):
    with pytest.raises(ValueError, match="prohibition conflicts"):
        record_delivery(
            StateStore(tmp_path),
            request("crossref.doi", query="", filters={"selected_id": "10.1234/synthetic"}),
            {
                "status": "ok",
                "items": [
                    {
                        "id": "10.1234/synthetic",
                        "url": "https://doi.org/10.1234/synthetic",
                        "title": "Synthetic",
                        "content": "A published abstract.",
                        "surface": "abstract",
                        "origin": "original_source",
                        "rights_status": "permitted",
                        "access_status": "available",
                    }
                ],
                "count": 1,
                "offset": 0,
            },
            delivery_id="conflicting-policy",
            adapter="count_offset",
            retrieved_at="2026-10-09T12:00:00Z",
            source_use_notices={"10.1234/synthetic": {"explicit_prohibition_detected": True}},
        )


def test_europe_pmc_missing_continuation_does_not_claim_another_page(tmp_path):
    store = StateStore(tmp_path)
    result = run_discovery_page(
        store,
        request("europe_pmc.search", window={"cursor": "*", "limit": 2}),
        transport=FakeHTTP(
            packet(
                {
                    "hitCount": 1,
                    "resultList": {
                        "result": [{"source": "MED", "id": "123", "title": "Synthetic article"}]
                    },
                }
            )
        ),
    )
    assert result["outcome"] == "success"
    assert result["window"]["next_window"] is None
    assert result["window"]["more_available"] == "no"
    incomplete = run_discovery_page(
        store,
        request("europe_pmc.search", attempt="a2", window={"cursor": "*", "limit": 1}),
        transport=FakeHTTP(
            packet(
                {
                    "hitCount": 3,
                    "resultList": {
                        "result": [
                            {"source": "MED", "id": "456", "title": "Another synthetic article"}
                        ]
                    },
                }
            )
        ),
    )
    assert incomplete["window"]["next_window"] is None
    assert incomplete["window"]["more_available"] == "unknown"


def test_advanced_partial_items_rate_wait_and_healthy_route(tmp_path):
    store = StateStore(tmp_path)
    exa = request("exa.search", filters={"mode": "auto"}, window={"limit": 2}, attempt="exa-1")
    partial = run_discovery_page(
        store,
        exa,
        credential="synthetic-key",
        transport=FakeHTTP(
            packet(
                {
                    "results": [
                        {"id": "source-1", "url": "https://example.org/one", "title": "One"},
                        {"id": 4, "url": "https://example.org/two"},
                    ],
                    "costDollars": {"total": 0.01},
                }
            )
        ),
    )
    assert partial["outcome"] == "partial"
    assert len(partial["hits"]) == 1 and len(partial["item_errors"]) == 1
    assert partial["hits"][0]["access_surface"] == "teaser"
    assert partial["window"]["more_available"] == "unknown"
    limited = run_discovery_page(
        store,
        request("exa.search", attempt="exa-2", window={"limit": 1}),
        credential="synthetic-key",
        transport=FakeHTTP(packet({}, status=429, headers={"retry-after": "60"})),
    )
    assert limited["outcome"] == "provider_failure"
    assert limited["retry_at"]
    withheld = run_discovery_page(
        store,
        request("exa.search", attempt="exa-3", window={"limit": 1}),
        credential="synthetic-key",
        transport=FakeHTTP(),
    )
    assert withheld["outcome"] == "quota_wait"
    healthy = run_discovery_page(
        store,
        request("crossref.search", attempt="scholarly"),
        transport=FakeHTTP(packet({"message": {"items": [crossref_item()], "total-results": 1}})),
    )
    assert healthy["outcome"] == "success"


def test_complex_pubmed_search_two_legs_and_explicit_followup(tmp_path):
    store = StateStore(tmp_path)
    search = packet({"esearchresult": {"count": "4", "idlist": ["111", "222"]}})
    summary = packet(
        {
            "result": {
                "uids": ["111", "222"],
                "111": {
                    "uid": "111",
                    "title": "Synthetic trial A",
                    "pubdate": "2017 Mar", "epubdate": "2016 Dec 23",
                    "articleids": [{"idtype": "doi", "value": "10.1234/a"}],
                },
                "222": {"uid": "222", "title": "Synthetic trial B"},
            }
        }
    )
    fake = FakeHTTP(search, summary)
    first = run_discovery_page(
        store,
        request(
            "pubmed.search", query="teletherapy AND anxiety", filters={"from_date": "2020-01-01"}
        ),
        transport=fake,
    )
    assert len(first["http_legs"]) == 2
    assert first["http_legs"][1]["admission_wait_ms"] > 0
    assert first["window"]["next_window"] == {"offset": 2, "limit": 2}
    assert first["hits"][0]["scientific_metadata"]["doi"] == "10.1234/a"
    assert first["hits"][0]["scientific_metadata"]["publication_date"] == "2017 Mar"
    assert first["hits"][0]["scientific_metadata"]["electronic_publication_date"] == "2016 Dec 23"
    followup = run_discovery_page(
        store,
        request(
            "pubmed.search",
            query="online CBT AND anxiety",
            attempt="a2",
            window={"offset": 2, "limit": 2},
        ),
        transport=FakeHTTP(packet({"esearchresult": {"count": "2", "idlist": []}})),
    )
    assert followup["outcome"] == "empty"
    assert first["hits"] and len(store.records("retrieval_delivery")) == 2


def test_pubmed_esummary_keeps_display_and_earlier_electronic_dates(tmp_path):
    summary = {"result": {"uids": ["12345"], "12345": {
        "uid": "12345", "title": "Synthetic trial", "pubdate": "2017 Mar",
        "epubdate": "2016 Dec 23",
    }}}
    result = run_discovery_page(
        StateStore(tmp_path / "both"),
        request("pubmed.pmid", query=None, filters={"pmid": "12345"},
                window={"limit": 1}),
        transport=FakeHTTP(packet(summary)),
    )
    assert result["outcome"] == "success"
    assert result["hits"][0]["scientific_metadata"] == {
        "pmid": "12345",
        "publication_date": "2017 Mar",
        "publication_date_basis": "pubmed.esummary.pubdate",
        "electronic_publication_date": "2016 Dec 23",
    }

    del summary["result"]["12345"]["epubdate"]
    missing = run_discovery_page(
        StateStore(tmp_path / "missing"),
        request("pubmed.pmid", query=None, filters={"pmid": "12345"},
                window={"limit": 1}),
        transport=FakeHTTP(packet(summary)),
    )
    assert missing["hits"][0]["scientific_metadata"] == {
        "pmid": "12345",
        "publication_date": "2017 Mar",
        "publication_date_basis": "pubmed.esummary.pubdate",
    }


def test_pubmed_saved_esearch_resumes_summary_after_wait_without_repeating_search(
    tmp_path, monkeypatch
):
    store = StateStore(tmp_path)
    selected = request(
        "pubmed.search", query="synthetic therapy comparison",
        filters={"from_date": "2020-01-01"},
    )
    search = packet({"esearchresult": {"count": "1", "idlist": ["111"]}})
    limited = FakeHTTP(search, packet({}, status=429, headers={"retry-after": "60"}))
    first = run_discovery_page(store, selected, transport=limited)
    search_ref = first["esearch_ref"]
    assert first["outcome"] == "provider_failure" and first["retry_at"]
    assert search_ref == {"kind": "discovery_esearch_leg", "id": search_ref["id"], "version": 1}
    assert store.get(**search_ref)["body"]["id_list"] == ["111"]
    assert len(limited.calls) == 2
    assert run_discovery_page(store, selected, transport=FakeHTTP())["esearch_ref"] == search_ref

    resumed_request = {**selected, "attempt_id": "a2", "resume_esearch_ref": search_ref}
    withheld = FakeHTTP()
    blocked = run_discovery_page(store, resumed_request, transport=withheld)
    assert blocked["outcome"] == "quota_wait" and blocked["esearch_ref"] == search_ref
    assert withheld.calls == []

    import ora.retrieval.discovery as discovery

    now = discovery.time.time()
    monkeypatch.setattr(discovery.time, "time", lambda: now + 61)
    summary = packet({"result": {"uids": ["111"], "111": {
        "uid": "111", "title": "Synthetic therapy trial",
    }}})
    resumed_http = FakeHTTP(summary)
    resumed = run_discovery_page(
        StateStore(tmp_path), resumed_request, transport=resumed_http,
    )
    assert resumed["outcome"] == "success"
    assert resumed["esearch_ref"] == resumed["resumed_from_esearch_ref"] == search_ref
    assert len(resumed_http.calls) == 1
    assert "esummary.fcgi" in resumed_http.calls[0][1]
    assert [leg["name"] for leg in resumed["http_legs"]] == ["pubmed.esummary"]
    envelope = store.get(**resumed["delivery_ref"])["body"]["envelope"]
    assert envelope["request"]["resumeEsearchRef"] == search_ref
    assert envelope["outcome"] == "success"

    fresh = FakeHTTP(search, summary)
    fresh_result = run_discovery_page(
        StateStore(tmp_path), {**selected, "attempt_id": "a3"}, transport=fresh,
    )
    assert fresh_result["outcome"] == "success"
    assert len(fresh.calls) == 2  # Omission explicitly chooses a fresh ESearch.


def test_interrupted_pubmed_summary_exposes_exact_resume_ref_and_rejects_drift(tmp_path):
    store = StateStore(tmp_path)
    selected = request("pubmed.search", query="synthetic trial methods")
    calls = []

    def crash_after_esearch(method, url, *, body=None, headers=None):
        calls.append(url)
        if len(calls) == 1:
            return packet({"esearchresult": {"count": "1", "idlist": ["333"]}})
        raise RuntimeError("synthetic interruption before summary capture")

    with pytest.raises(RuntimeError, match="synthetic interruption"):
        run_discovery_page(store, selected, transport=crash_after_esearch)
    uncertain = run_discovery_page(StateStore(tmp_path), selected, transport=FakeHTTP())
    assert uncertain["outcome"] == "uncertain_dispatch"
    search_ref = uncertain["esearch_ref"]
    assert store.get(**search_ref)["body"]["id_list"] == ["333"]
    for changed in (
        {"query": "different question"},
        {"requested_window": {"offset": 2, "limit": 2}},
        {"task_id": "another-task"},
        {"filters": {"from_date": "2021-01-01"}},
    ):
        bad = {**selected, "attempt_id": "a2", "resume_esearch_ref": search_ref, **changed}
        with pytest.raises(ValueError, match="does not match"):
            run_discovery_page(StateStore(tmp_path), bad, transport=FakeHTTP())
    with pytest.raises(ValueError, match="does not match"):
        run_discovery_page(StateStore(tmp_path), {
            **selected, "attempt_id": "a2", "resume_esearch_ref": {
                "kind": "discovery_esearch_leg", "id": "discovery-esearch-" + "0" * 32,
                "version": 1,
            },
        }, transport=FakeHTTP())
    with pytest.raises(ValueError, match="only valid for PubMed"):
        run_discovery_page(StateStore(tmp_path), {
            **request("crossref.search", attempt="a2"), "resume_esearch_ref": search_ref,
        }, transport=FakeHTTP())
    assert len(store.records("discovery_page")) == 1

    resumed = FakeHTTP(packet({"result": {"uids": ["333"], "333": {
        "uid": "333", "title": "Synthetic method trial",
    }}}))
    result = run_discovery_page(
        StateStore(tmp_path), {**selected, "attempt_id": "a2",
                                   "resume_esearch_ref": search_ref}, transport=resumed,
    )
    assert result["outcome"] == "success"
    assert len(resumed.calls) == 1 and "esummary.fcgi" in resumed.calls[0][1]


def test_pubmed_summary_reconciles_selected_ids_and_keeps_partial_siblings(tmp_path):
    store = StateStore(tmp_path)
    selected = request("pubmed.search", query="synthetic two-study query")
    search = packet({"esearchresult": {"count": "2", "idlist": ["111", "222"]}})
    one_summary = packet({"result": {"uids": ["111"], "111": {
        "uid": "111", "title": "Synthetic first trial",
    }}})
    partial = run_discovery_page(store, selected, transport=FakeHTTP(search, one_summary))
    assert partial["outcome"] == "partial"
    assert len(partial["hits"]) == 1
    assert partial["item_errors"][0]["position"] == 1
    assert partial["window"]["more_available"] == "no"
    assert partial["esearch_ref"] is not None

    other = request("pubmed.search", query="synthetic wrong-summary query", attempt="a2")
    wrong = packet({"result": {"uids": ["999"], "999": {
        "uid": "999", "title": "Unselected trial",
    }}})
    rejected = run_discovery_page(store, other, transport=FakeHTTP(search, wrong))
    assert rejected["outcome"] == "malformed_payload"
    assert rejected["accepted_hit_refs"] == []
    assert rejected["esearch_ref"] is not None


def test_ncbi_retry_after_is_shared_between_search_and_pmid(tmp_path):
    store = StateStore(tmp_path)
    limited = run_discovery_page(
        store,
        request("pubmed.search"),
        transport=FakeHTTP(packet({}, status=429, headers={"retry-after": "60"})),
    )
    assert limited["outcome"] == "provider_failure"
    direct = run_discovery_page(
        StateStore(tmp_path),
        request(
            "pubmed.pmid", query=None, filters={"pmid": "12345"}, window={"offset": 0, "limit": 1}
        ),
        transport=FakeHTTP(),
    )
    assert direct["outcome"] == "quota_wait"
    assert direct["wait_ref"] == limited["wait_ref"]


def test_exa_contents_keeps_selected_text_candidate_and_summary_distinct(tmp_path):
    store = StateStore(tmp_path)
    content = run_discovery_page(
        store,
        request(
            "exa.contents",
            query=None,
            filters={"url": "https://example.org/synthetic", "max_characters": 4000,
                     "max_age_hours": 24, "selection_reason": "Inspect exact candidate text"},
            window={"limit": 1},
        ),
        credential="synthetic-key",
        transport=FakeHTTP(
            packet(
                {
                    "results": [
                        {
                            "id": "exa-1",
                            "url": "https://example.org/synthetic",
                            "text": "Synthetic article text",
                            "summary": "Generated summary",
                        }
                    ],
                    "costDollars": {"total": 0.002},
                }
            )
        ),
    )
    assert content["hits"][0]["access_surface"] == "passage"
    assert content["hits"][0]["content_sha256"]
    assert content["selection"] == {
        "selected_id": None,
        "selected_url": "https://example.org/synthetic",
        "reason": "Inspect exact candidate text",
        "reason_status": "explicit",
        "max_characters": 4000,
        "max_age_hours": 24,
        "freshness_basis": "explicit_max_age_hours",
    }
    saved_delivery = store.get(**content["delivery_ref"])["body"]["envelope"]
    assert saved_delivery["request"]["filters"]["selection_reason"] == "Inspect exact candidate text"
    assert store.get(**content["page_ref"])["body"]["result"]["selection"] == content["selection"]
    assert run_discovery_page(store, request(
        "exa.contents", query=None,
        filters={"url": "https://example.org/synthetic", "max_characters": 4000,
                 "max_age_hours": 24, "selection_reason": "Inspect exact candidate text"},
        window={"limit": 1},
    ), credential="synthetic-key", transport=FakeHTTP()) == content
    assert store.get(**content["hits"][0]["hit_ref"])["body"]["source"]["content"] is None
    generated = run_discovery_page(
        store,
        request(
            "exa.contents",
            query=None,
            attempt="a2",
            filters={"url": "https://example.org/summary",
                     "selection_reason": "Check whether candidate has attributable text"},
            window={"limit": 1},
        ),
        credential="synthetic-key",
        transport=FakeHTTP(
            packet(
                {
                    "results": [
                        {"url": "https://example.org/summary", "summary": "Generated summary"}
                    ]
                }
            )
        ),
    )
    assert generated["hits"][0]["access_surface"] == "provider_summary"
    assert generated["hits"][0]["origin"] == "provider_generated"


def test_exa_selected_url_without_rationale_keeps_one_call_and_marks_absence(tmp_path):
    store = StateStore(tmp_path)
    transport = FakeHTTP(packet({"results": []}))
    result = run_discovery_page(
        store,
        request("exa.contents", query=None,
                filters={"url": "https://example.org/known-article"},
                window={"limit": 1}),
        credential="synthetic-key", transport=transport,
    )
    assert len(transport.calls) == 1
    assert result["selection"]["reason"] == "not supplied"
    assert result["selection"]["reason_status"] == "not_supplied"
    assert result["selection"]["freshness_basis"] == "provider_default_unqualified"
    assert store.get(**result["page_ref"])["body"]["result"]["selection"] == result["selection"]


def test_exa_contents_item_status_failure_keeps_successful_sibling(tmp_path):
    store = StateStore(tmp_path)
    result = run_discovery_page(
        store,
        request(
            "exa.contents", query=None,
            filters={"id": "synthetic-selected", "selection_reason": "Inspect selected source"},
            window={"limit": 1}
        ),
        credential="synthetic-key",
        transport=FakeHTTP(
            packet(
                {
                    "results": [
                        {
                            "id": "synthetic-selected",
                            "url": "https://example.org/one",
                            "text": "Source candidate",
                        }
                    ],
                    "statuses": [
                        {"id": "synthetic-selected", "status": "success", "source": "cached"},
                        {"id": "synthetic-failed", "status": "error"},
                    ],
                }
            )
        ),
    )
    assert result["outcome"] == "partial"
    assert len(result["hits"]) == 1 and len(result["item_errors"]) == 1
    assert result["provider_meta"]["statuses"][1]["id"] == "synthetic-failed"


def test_catalog_seed_types_unlisted_lead_rights_and_version_history(tmp_path):
    store = StateStore(tmp_path)
    seeds = seed_catalog()["entries"]
    assert len(seeds) == len({x["id"] for x in seeds}) == 25
    assert {x["type"] for x in seeds} == {"journal", "conference", "repository", "registry"}
    apa = next(x for x in seeds if x["name"].startswith("American Psychiatric Association Annual"))
    assert "written permission" in apa["access_note"]
    proposal = propose_venue(
        store,
        name="Synthetic Specialist Journal",
        kind="journal",
        url="https://example.org/journal",
        mission_id="synthetic-mission",
        rationale="Relevant intervention reports",
        proposed_at="2026-10-09T12:00:00Z",
    )
    assert proposal["entry"]["links"][0]["qualification"] == "discovered"
    assert list_venues(store, text="Synthetic Specialist Journal")
    navigation = record_navigation(
        store,
        venue_id=apa["id"],
        url=apa["links"][0]["url"],
        outcome="terms_pending",
        checked_at="2026-10-09T12:00:00Z",
        alternate_url="https://example.org/permitted-copy",
    )
    assert navigation["navigation_ref"]
    jmir = next(x for x in seeds if x["name"] == "JMIR Mental Health")
    assert jmir["links"][0]["resource_year"] == 2026
    revised = revise_venue(
        store,
        jmir["id"],
        entry={
            **jmir,
            "links": jmir["links"]
            + [
                {
                    "url": "https://mental.jmir.org/2027",
                    "type": "journal_volume",
                    "last_checked": "2026-10-09",
                    "qualification": "discovered",
                }
            ],
        },
        expected_version=1,
        reason="New year lead",
    )
    assert revised["entry"]["version"] == 2
    assert resolve_venue(store, jmir["id"])["links"][0] == jmir["links"][0]
    assert store.get("venue_entry", jmir["id"], 1)["body"]["previous_entry_sha256"]


def test_catalog_rejects_false_resource_type_and_impossible_checked_date():
    item = seed_catalog()["entries"][0]
    with pytest.raises(ValueError, match="does not match"):
        validate_venue({**item, "links": [{**item["links"][0], "type": "conference_program"}]})
    with pytest.raises(ValueError, match="real checked date"):
        validate_venue({**item, "links": [{**item["links"][0], "last_checked": "2026-13-09"}]})


def test_catalog_navigation_rejects_untrusted_date_and_http_status(tmp_path):
    store = StateStore(tmp_path)
    entry = seed_catalog()["entries"][0]
    fields = dict(
        venue_id=entry["id"],
        url=entry["links"][0]["url"],
        outcome="navigable",
    )
    with pytest.raises(ValueError, match="timezone"):
        record_navigation(store, **fields, checked_at="2026-10-09T12:00:00")
    with pytest.raises(ValueError, match="actual status code"):
        record_navigation(store, **fields, checked_at="2026-10-09T12:00:00Z", status_code=999)


@pytest.mark.parametrize(
    "route,filter_name",
    [
        ("europe_pmc.search", "from_date"),
        ("crossref.search", "from_date"),
        ("pubmed.search", "from_date"),
    ],
)
def test_invalid_date_filter_is_rejected_before_http(tmp_path, route, filter_name):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        run_discovery_page(
            StateStore(tmp_path),
            request(
                route,
                filters={filter_name: "yesterday"},
                window={"cursor": "*", "limit": 2}
                if route == "europe_pmc.search"
                else {"offset": 0, "limit": 2},
            ),
            transport=FakeHTTP(),
        )
    with pytest.raises(ValueError, match="real YYYY-MM-DD"):
        run_discovery_page(
            StateStore(tmp_path),
            request(
                route,
                filters={filter_name: "2026-13-09"},
                window={"cursor": "*", "limit": 2}
                if route == "europe_pmc.search"
                else {"offset": 0, "limit": 2},
            ),
            transport=FakeHTTP(),
        )


def test_exa_requires_selected_key_and_does_not_persist_it(tmp_path):
    store = StateStore(tmp_path)
    req = request("exa.search", window={"limit": 1})
    with pytest.raises(ValueError, match="credential"):
        run_discovery_page(store, req, transport=FakeHTTP())
    assert store.records("discovery_page") == []
    run_discovery_page(
        store, req, credential="synthetic-key", transport=FakeHTTP(packet({"results": []}))
    )
    assert b"synthetic-key" not in store.db_path.read_bytes()


def test_invalid_request_time_is_rejected_before_dispatch_or_http(tmp_path):
    store = StateStore(tmp_path)
    bad = {**request(), "requested_at": "2026-10-09T10:00:00"}
    with pytest.raises(ValueError, match="timezone"):
        run_discovery_page(store, bad, transport=FakeHTTP())
    assert store.records("discovery_page") == []


def test_route_window_and_content_bounds_reject_silent_translation(tmp_path):
    store = StateStore(tmp_path)
    with pytest.raises(ValueError, match="cursor window"):
        run_discovery_page(
            store,
            request("europe_pmc.search", window={"offset": 0, "limit": 2}),
            transport=FakeHTTP(),
        )
    with pytest.raises(ValueError, match="offset window"):
        run_discovery_page(
            store,
            request("crossref.search", window={"cursor": "opaque", "limit": 2}),
            transport=FakeHTTP(),
        )
    with pytest.raises(ValueError, match="max_characters"):
        run_discovery_page(
            store,
            request(
                "exa.contents",
                query=None,
                filters={"id": "selected", "max_characters": "many",
                         "selection_reason": "Inspect selected source"},
                window={"limit": 1},
            ),
            credential="synthetic-key",
            transport=FakeHTTP(),
        )
    with pytest.raises(ValueError, match="mode"):
        run_discovery_page(
            store,
            request("exa.search", filters={"mode": "deep"}, window={"limit": 1}),
            credential="synthetic-key",
            transport=FakeHTTP(),
        )
    assert store.records("discovery_page") == []


def test_exa_payment_required_reports_route_unavailable(tmp_path):
    result = run_discovery_page(
        StateStore(tmp_path),
        request("exa.search", window={"limit": 1}),
        credential="synthetic-key",
        transport=FakeHTTP(packet({}, status=402)),
    )
    assert result["outcome"] == "provider_failure"
    assert result["provider_payload_condition"] == "http_error"
    delivery = StateStore(tmp_path).get(**result["delivery_ref"])
    assert delivery["body"]["envelope"]["failure"]["code"] == "route_unavailable"


def test_interrupted_dispatch_is_uncertain_and_new_attempt_can_continue(tmp_path):
    store = StateStore(tmp_path)
    req = request()

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run_discovery_page(store, req, transport=interrupted)
    uncertain = run_discovery_page(store, req, transport=FakeHTTP())
    assert uncertain["outcome"] == "uncertain_dispatch"
    with pytest.raises(ValueError, match="already used"):
        run_discovery_page(
            store, {**req, "requested_at": "2026-10-09T11:00:00Z"}, transport=FakeHTTP()
        )
    retry = run_discovery_page(
        store,
        {**req, "attempt_id": "a2"},
        transport=FakeHTTP(packet({"message": {"items": [], "total-results": 0}})),
    )
    assert retry["outcome"] == "empty"
    assert len(store.records("discovery_page")) == 2


def test_oversized_payload_has_size_and_hash_distinct_from_invalid_json(tmp_path):
    store = StateStore(tmp_path)
    req = request()
    large = b"x" * (MAX_RESPONSE_BYTES + 1)
    oversized = record_delivery(
        store,
        req,
        large,
        delivery_id="large",
        adapter="count_offset",
        retrieved_at="2026-10-09T10:01:00Z",
    )
    invalid = record_delivery(
        store,
        {**req, "attempt_id": "a2"},
        b"{",
        delivery_id="invalid",
        adapter="count_offset",
        retrieved_at="2026-10-09T10:02:00Z",
    )
    assert oversized["outcome"] == "oversized_response"
    assert invalid["outcome"] == "malformed_payload"
    assert store.get(**oversized["delivery_ref"])["body"]["envelope"]["failure"]["category"] == "oversized_response"
    assert oversized["payload_condition"] == "oversized"
    assert oversized["original_bytes"] == len(large)
    assert (
        oversized["raw_payload_ref"]["original_sha256"]
        != invalid["raw_payload_ref"]["original_sha256"]
    )
    assert invalid["payload_condition"] == "invalid_json"
    assert store.get(**oversized["delivery_ref"])["body"]["payload_condition"] == "oversized"


def test_oversized_http_packet_keeps_provider_hash_and_separate_condition(tmp_path):
    store = StateStore(tmp_path)
    raw = b"x" * (MAX_RESPONSE_BYTES + 1)
    result = run_discovery_page(store, request(), transport=FakeHTTP(HTTPResult(200, raw, {})))
    assert result["outcome"] == "oversized_response"
    assert result["accepted_hit_refs"] == []
    assert result["window"]["more_available"] == "unknown"
    assert result["window"]["next_window"] is None
    assert result["provider_payload_condition"] == "oversized"
    assert result["http_legs"][0]["bytes"] == len(raw)
    assert result["http_legs"][0]["hash_scope"] == "observed_prefix"
    assert result["http_legs"][0]["complete_body_sha256"] is None
    assert store.get(**result["delivery_ref"])["body"]["upstream_payload_condition"] == "oversized"
    assert result["http_legs"][0]["sha256"] != result["raw_payload_ref"]["original_sha256"]
