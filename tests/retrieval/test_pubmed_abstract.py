"""Synthetic EFetch records prove the exact-PMID abstract boundary offline."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from ora.retrieval.discovery import available_routes, run_discovery_page
from ora.retrieval.providers import HTTPResult
from ora.state import StateStore


def _request(*, selected_id="12345", attempt="a1", query=""):
    capability = available_routes()["pubmed.abstract"]
    return {
        "mission_id": "synthetic-mission",
        "task_id": "synthetic-task",
        "request_id": "synthetic-pubmed-abstract",
        "attempt_id": attempt,
        "agent_instance_id": "synthetic-agent",
        "route": "pubmed.abstract",
        "provider": capability["provider"],
        "index": capability["index"],
        "operation": capability["operation"],
        "query": query,
        "filters": {"selected_id": selected_id},
        "requested_at": "2026-10-10T10:00:00Z",
        "requested_window": {"offset": 0, "limit": 1},
    }


def _xml(*, pmid="12345", abstract="Results show 17 of 20 participants improved.",
         copyright_text="", extra_article=""):
    abstract_node = (
        f"<Abstract><AbstractText Label=\"RESULTS\">{abstract}</AbstractText>"
        f"<CopyrightInformation>{copyright_text}</CopyrightInformation></Abstract>"
        if abstract is not None else ""
    )
    return (
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
        "<!DOCTYPE PubmedArticleSet PUBLIC \"-//NLM//DTD PubMedArticle//EN\" "
        "\"https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_250101.dtd\">"
        "<PubmedArticleSet><PubmedArticle><MedlineCitation>"
        f"<PMID>{pmid}</PMID><Article><ArticleTitle>Synthetic trial title</ArticleTitle>"
        "<Journal><JournalIssue><PubDate><Year>2024</Year><Month>Oct</Month>"
        "<Day>10</Day></PubDate></JournalIssue><Title>Synthetic Journal</Title></Journal>"
        f"{abstract_node}</Article></MedlineCitation><PubmedData><ArticleIdList>"
        "<ArticleId IdType=\"doi\">10.1234/synthetic</ArticleId>"
        "<ArticleId IdType=\"pmc\">PMC12345</ArticleId>"
        "</ArticleIdList></PubmedData></PubmedArticle>"
        f"{extra_article}</PubmedArticleSet>"
    ).encode()


class _HTTP:
    def __init__(self, *packets):
        self.packets = list(packets)
        self.calls = []

    def __call__(self, method, url, *, body=None, headers=None):
        self.calls.append((method, url, body, headers))
        return self.packets.pop(0)


def _packet(body, status=200, headers=None):
    return HTTPResult(status, body, headers or {})


def test_exact_efetch_abstract_retains_section_text_policy_and_http_lineage(tmp_path):
    store = StateStore(tmp_path)
    http = _HTTP(_packet(_xml()))
    request = _request()
    result = run_discovery_page(store, request, transport=http)
    assert result["outcome"] == "success"
    assert len(result["hits"]) == 1
    hit = result["hits"][0]
    assert hit["access_surface"] == "abstract"
    assert hit["scientific_metadata"] == {
        "pmid": "12345", "doi": "10.1234/synthetic", "pmcid": "PMC12345",
        "journal": "Synthetic Journal", "publication_date": "2024 Oct 10",
        "publication_date_basis": "pubmed.efetch.Article.Journal.JournalIssue.PubDate",
    }
    assert hit["source_use_policy"]["allowed_uses"] == [
        "discovery", "private_research_storage", "model_input", "factual_synthesis",
    ]
    assert hit["source_use_policy"]["copyright_license_status"] == "unverified"
    assert hit["source_use_policy"]["full_text_access"] == "not_inferred"
    assert store.get(**hit["hit_ref"])["body"]["source"]["content"] == (
        "RESULTS: Results show 17 of 20 participants improved."
    )
    assert result["http_legs"][0]["name"] == "pubmed.abstract"
    assert result["http_legs"][0]["complete_body_sha256"]
    assert result["provider_meta"]["abstract_availability"] == "present"
    params = parse_qs(urlsplit(http.calls[0][1]).query)
    assert urlsplit(http.calls[0][1]).path.endswith("/efetch.fcgi")
    assert params == {"db": ["pubmed"], "id": ["12345"], "retmode": ["xml"]}
    assert run_discovery_page(StateStore(tmp_path), request, transport=_HTTP()) == result


def test_exact_efetch_with_omitted_offset_is_successful_and_replays(tmp_path):
    store = StateStore(tmp_path)
    request = _request()
    request["requested_window"] = {"limit": 2}
    result = run_discovery_page(store, request, transport=_HTTP(_packet(_xml())))
    assert result["outcome"] == "success"
    assert result["window"]["window_errors"] == []
    assert result["window"]["returned_offset"] == 0
    assert len(result["hits"]) == 1
    assert run_discovery_page(StateStore(tmp_path), request, transport=_HTTP()) == result


def test_efetch_retains_reported_electronic_date_without_inventing_a_day(tmp_path):
    xml = _xml().replace(
        b"</Article>",
        b'<ArticleDate DateType="Electronic"><Year>2023</Year><Month>12</Month>'
        b"</ArticleDate></Article>",
    )
    result = run_discovery_page(
        StateStore(tmp_path), _request(), transport=_HTTP(_packet(xml)),
    )
    assert result["outcome"] == "success"
    assert result["hits"][0]["scientific_metadata"]["electronic_publication_date"] == "2023 12"


def test_absent_abstract_is_metadata_only_and_not_a_text_grant(tmp_path):
    store = StateStore(tmp_path)
    result = run_discovery_page(store, _request(), transport=_HTTP(_packet(_xml(abstract=None))))
    assert result["outcome"] == "success"
    hit = result["hits"][0]
    assert hit["access_surface"] == "metadata"
    assert hit["source_use_policy"] is None
    assert store.get(**hit["hit_ref"])["body"]["source"]["content"] is None
    assert result["provider_meta"]["abstract_availability"] == "absent"


@pytest.mark.parametrize("body", [
    _xml(pmid="67890"),
    _xml(extra_article="<PubmedArticle><MedlineCitation><PMID>99999</PMID></MedlineCitation></PubmedArticle>"),
    b"<PubmedArticleSet><PubmedArticle>",
])
def test_mismatch_multiple_or_malformed_xml_cannot_create_a_hit(tmp_path, body):
    result = run_discovery_page(StateStore(tmp_path), _request(), transport=_HTTP(_packet(body)))
    assert result["outcome"] == "malformed_payload"
    assert result["hits"] == []
    assert result["provider_payload_condition"] == "malformed"


def test_explicit_model_use_prohibition_retains_only_locator(tmp_path):
    store = StateStore(tmp_path)
    result = run_discovery_page(
        store, _request(), transport=_HTTP(_packet(_xml(copyright_text="AI model input prohibited"))),
    )
    hit = result["hits"][0]
    assert hit["rights_status"] == "restricted"
    assert hit["source_use_policy"]["allowed_uses"] == ["discovery"]
    saved = store.get(**hit["hit_ref"])["body"]
    assert saved["source"]["content"] is None
    assert saved["source_use_policy"]["source_notice"]["explicit_prohibition_detected"]


def test_conflicting_identifiers_are_not_joined_and_secondary_pmid_must_match(tmp_path):
    conflicting_doi = _xml().replace(
        b"</ArticleIdList>",
        b'<ArticleId IdType="doi">10.1234/conflict</ArticleId></ArticleIdList>',
    )
    result = run_discovery_page(
        StateStore(tmp_path / "doi"), _request(), transport=_HTTP(_packet(conflicting_doi)),
    )
    assert result["outcome"] == "success"
    assert "doi" not in result["hits"][0]["scientific_metadata"]
    assert result["provider_meta"]["identifier_conflicts"] == ["doi"]

    mismatched_pmid = _xml().replace(
        b"</ArticleIdList>",
        b'<ArticleId IdType="pubmed">67890</ArticleId></ArticleIdList>',
    )
    rejected = run_discovery_page(
        StateStore(tmp_path / "pmid"), _request(), transport=_HTTP(_packet(mismatched_pmid)),
    )
    assert rejected["outcome"] == "malformed_payload"
    assert rejected["hits"] == []


def test_reported_truncation_is_visible_and_not_a_complete_abstract_claim(tmp_path):
    result = run_discovery_page(
        StateStore(tmp_path), _request(),
        transport=_HTTP(_packet(_xml(abstract="Early findings. ABSTRACT TRUNCATED AT 250 WORDS"))),
    )
    hit = result["hits"][0]
    assert hit["access_surface"] == "abstract"
    assert hit["access_status"] == "limited"
    assert hit["source_use_policy"]["abstract_completeness"] == "reported_truncated"
    assert result["provider_meta"]["abstract_truncated"] is True


def test_rate_wait_and_oversize_keep_independent_diagnostics(tmp_path):
    store = StateStore(tmp_path / "wait")
    limited = run_discovery_page(
        store, _request(),
        transport=_HTTP(_packet(b"", status=429, headers={"retry-after": "60"})),
    )
    assert limited["outcome"] == "provider_failure" and limited["retry_at"]
    waiting = run_discovery_page(
        StateStore(tmp_path / "wait"), _request(attempt="a2"), transport=_HTTP(),
    )
    assert waiting["outcome"] == "quota_wait"
    assert waiting["wait_ref"] == limited["wait_ref"]

    oversized = run_discovery_page(
        StateStore(tmp_path / "oversized"), _request(),
        transport=_HTTP(_packet(b"x" * (8 * 1024 * 1024 + 1))),
    )
    assert oversized["outcome"] == "oversized_response"
    assert oversized["http_legs"][0]["hash_scope"] == "observed_prefix"
    assert oversized["http_legs"][0]["complete_body_sha256"] is None


def test_conflicting_exact_ids_reject_before_dispatch(tmp_path):
    request = _request(query="67890")
    transport = _HTTP()
    with pytest.raises(ValueError, match="Conflicting pmid"):
        run_discovery_page(StateStore(tmp_path), request, transport=transport)
    assert transport.calls == []


def test_interrupted_efetch_requires_an_explicit_new_attempt(tmp_path):
    store = StateStore(tmp_path)
    selected = _request()

    def interrupted(method, url, *, body=None, headers=None):
        raise RuntimeError("synthetic interrupted dispatch")

    with pytest.raises(RuntimeError, match="synthetic interrupted dispatch"):
        run_discovery_page(store, selected, transport=interrupted)
    uncertain = run_discovery_page(StateStore(tmp_path), selected, transport=_HTTP())
    assert uncertain["outcome"] == "uncertain_dispatch"
    assert uncertain["page_ref"]
    resumed_http = _HTTP(_packet(_xml()))
    resumed = run_discovery_page(
        StateStore(tmp_path), _request(attempt="a2"), transport=resumed_http,
    )
    assert resumed["outcome"] == "success"
    assert len(resumed_http.calls) == 1
