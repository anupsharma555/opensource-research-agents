"""Source bytes must be classified before their passages can support a claim."""

import hashlib
import json
import subprocess
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler

import pytest

from ora.evidence import EvidenceLibrary, OversizedSourceError
from ora.evidence.intake import IntakeService, PublicSourceHTTPError, fetch_public_source
from ora.retrieval.discovery import available_routes, run_discovery_page
from ora.retrieval.providers import HTTPResult
from ora.retrieval.receipts import record_delivery
from ora.state import StateStore


def setup(tmp_path):
    store = StateStore(tmp_path / "state")
    lib = EvidenceLibrary(store)
    publication = lib.register_publication(
        "publication",
        identifiers={"doi": "10.1234/target"},
        title="Synthetic CBT trial",
        verification="Synthetic DOI fixture",
    )["publication_ref"]
    return store, lib, IntakeService(lib), publication


def inspect(
    service,
    publication,
    suffix,
    content,
    *,
    mime="text/plain",
    surface="abstract",
    rights="permitted",
    selected=None,
    doi="10.1234/target",
    truncated=False,
    identity_basis="Synthetic exact publication source",
):
    return service.inspect_response(
        f"intake-{suffix}",
        publication,
        content=content,
        content_type=mime,
        source_url=f"https://example.org/{suffix}",
        retrieved_at="2026-10-09T12:00:00Z",
        source_version=f"synthetic-{suffix}-v1",
        rights_status=rights,
        rights_basis="Synthetic project-owned fixture"
        if rights == "permitted"
        else "Synthetic restriction",
        declared_surface=surface,
        expected_doi=doi,
        selected_texts=selected,
        truncated=truncated,
        identity_basis=identity_basis,
    )


def test_published_abstract_passage_and_explicit_missing_detail(tmp_path):
    store, lib, service, publication = setup(tmp_path)
    abstract = b"DOI: 10.1234/target. Among 40 adults, symptoms decreased by 3 points at week 8."
    selected = "symptoms decreased by 3 points at week 8"
    saved = inspect(service, publication, "abstract", abstract, selected=[selected])
    assert saved["classification"] == "abstract"
    assert saved["copy_ref"]
    assert service.source_for_model(saved["passage_refs"][0])["text"] == selected
    assert store.read_blob(store.get(**saved["copy_ref"])["body"]["blob"]["sha256"]) == abstract
    quality = store.get(**saved["quality_ref"])["body"]
    assert quality["source_sha256"] == hashlib.sha256(abstract).hexdigest()
    assert quality["selected_locators"] == [f"text; chars=0:{len(abstract)}; chars=38:78"]
    need = service.open_information_need(
        "need-method",
        publication,
        field="randomization method",
        question="How was allocation concealed?",
        missing_from=saved["copy_ref"],
        same_study_refs=[],
        independent_context_refs=[],
    )
    assert store.get(**need)["body"]["status"] == "open"
    assert "randomization method" not in service.source_for_model(saved["passage_refs"][0])["text"]


@pytest.mark.parametrize(
    "suffix,content,surface,expected",
    [
        (
            "login",
            b"<html><p>Sign in to access this article</p></html>",
            "full_text",
            "access_page",
        ),
        ("captcha", b"<html><p>Verify you are human</p></html>", "full_text", "access_page"),
        ("wrong", b"DOI 10.1234/other. Abstract: another study.", "abstract", "wrong_article"),
        ("teaser", b"CBT may improve symptoms.", "teaser", "teaser"),
        (
            "summary",
            b"Provider synthesized description of a study.",
            "provider_summary",
            "provider_summary",
        ),
        ("partial", b"DOI 10.1234/target. The abstract is incomplete", "abstract", "partial"),
    ],
)
def test_unusable_surfaces_do_not_create_passages(tmp_path, suffix, content, surface, expected):
    store, _, service, publication = setup(tmp_path)
    saved = inspect(
        service, publication, suffix, content, surface=surface, truncated=suffix == "partial"
    )
    assert saved["classification"] == expected
    if suffix == "partial":
        assert store.get(**saved["copy_ref"])["body"]["access_surface"] == "metadata"
    else:
        assert saved["copy_ref"] is None
    assert store.get(**saved["quality_ref"])["body"]["classification"] == expected
    assert store.records("source_passage") == []


def test_html_and_xml_keep_sections_and_exact_selected_text(tmp_path):
    store, _, service, publication = setup(tmp_path)
    html = (
        b"<html><h1>Abstract</h1><p>DOI: 10.1234/target.</p>"
        b"<h2>Results</h2><p>The score was -2.5 points (n=40), not +2.5.</p>"
        b"<h2>Discussion</h2><p>Interpretation was limited.</p></html>"
    )
    saved = inspect(
        service,
        publication,
        "html",
        html,
        mime="text/html",
        surface="full_text",
        selected=["-2.5 points (n=40), not +2.5"],
    )
    assert saved["classification"] == "passage"
    passage = service.source_for_model(saved["passage_refs"][0])
    assert "section=Results" in passage["locator"]
    xml = (
        b"<article><front><article-meta><article-id pub-id-type='doi'>10.1234/target"
        b"</article-id></article-meta></front><body><sec><title>Results</title>"
        b"<p>There were 12/20 responders.</p><table-wrap><caption>Response table</caption>"
        b"<table><tr><th>Group</th><td>CBT</td></tr></table><table-wrap-foot>"
        b"Footnote: per-protocol.</table-wrap-foot></table-wrap></sec></body></article>"
    )
    saved_xml = inspect(
        service,
        publication,
        "xml",
        xml,
        mime="application/xml",
        surface="full_text",
        selected=["12/20 responders", "Footnote: per-protocol."],
    )
    assert saved_xml["classification"] == "passage"
    assert len(saved_xml["passage_refs"]) == 2
    assert store.get(**saved_xml["quality_ref"])["body"]["parser"] == "stdlib-elementtree"


def test_restricted_copy_keeps_only_locator_and_no_model_text(tmp_path):
    store, _, service, publication = setup(tmp_path)
    content = b"DOI 10.1234/target. An abstract with restricted source use."
    saved = inspect(
        service,
        publication,
        "restricted",
        content,
        rights="restricted",
        selected=["An abstract with restricted source use."],
    )
    copy = store.get(**saved["copy_ref"])["body"]
    assert copy["blob"] is None and copy["access_surface"] == "metadata"
    assert saved["passage_refs"] == []
    assert store.get(**saved["source_use_ref"])["body"]["allowed_uses"] == ["discovery"]


def test_public_api_abstract_policy_survives_intake_but_explicit_restriction_wins(tmp_path):
    store, _, service, publication = setup(tmp_path)
    abstract = "DOI 10.1234/target. Published abstract reports symptoms at week 8."
    route = "europe_pmc.search"
    capability = available_routes()[route]
    request = {
        "mission_id": "synthetic-mission",
        "task_id": "synthetic-task",
        "request_id": "abstract-policy",
        "attempt_id": "attempt-1",
        "route": route,
        "provider": capability["provider"],
        "index": capability["index"],
        "operation": "search",
        "query": "synthetic trial",
        "filters": {},
        "requested_at": "2026-10-09T12:00:00Z",
        "requested_window": {"cursor": "*", "limit": 1},
    }
    payload = {
        "hitCount": 1,
        "resultList": {
            "result": [
                {
                    "source": "MED",
                    "id": "123",
                    "doi": "10.1234/target",
                    "title": "Synthetic trial",
                    "abstractText": abstract,
                    "isOpenAccess": "N",
                }
            ]
        },
    }
    discovered = run_discovery_page(
        store,
        request,
        transport=lambda *_args, **_kwargs: HTTPResult(200, json.dumps(payload).encode(), {}),
    )
    hit_ref = discovered["hits"][0]["hit_ref"]
    source = store.get(**hit_ref)["body"]["source"]
    result = service.inspect_response(
        "private-abstract",
        publication,
        content=source["content"].encode(),
        content_type="text/plain",
        source_url=source["original_url"],
        retrieved_at="2026-10-09T12:00:01Z",
        source_version="synthetic-abstract-v1",
        rights_status="unknown",
        rights_basis="No separate publisher license verified",
        declared_surface="abstract",
        expected_doi="10.1234/target",
        selected_texts=["symptoms at week 8"],
        retrieval_hit_ref=hit_ref,
        identity_basis="Synthetic DOI match",
    )
    assert result["classification"] == "abstract"
    assert result["passage_refs"]
    quality = store.get(**result["quality_ref"])["body"]
    use = store.get(**result["source_use_ref"])["body"]
    assert quality["retrieval_policy_applies_to_bytes"] is True
    assert use["retrieval_source_use_policy"]["copyright_license_status"] == "unverified"
    assert use["allowed_uses"] == ["discovery", "storage", "model_input"]
    assert use["export_allowed"] is False
    restricted = service.inspect_response(
        "restricted-abstract",
        publication,
        content=source["content"].encode(),
        content_type="text/plain",
        source_url=source["original_url"],
        retrieved_at="2026-10-09T12:00:02Z",
        source_version="synthetic-abstract-v1",
        rights_status="restricted",
        rights_basis="Explicit source prohibition",
        declared_surface="abstract",
        expected_doi="10.1234/target",
        retrieval_hit_ref=hit_ref,
        identity_basis="Synthetic DOI match",
    )
    assert restricted["passage_refs"] == []
    assert store.get(**restricted["source_use_ref"])["body"]["allowed_uses"] == ["discovery"]
    alternate = service.inspect_response(
        "alternate-abstract",
        publication,
        content=source["content"].encode(),
        content_type="text/plain",
        source_url="https://example.org/different-copy",
        retrieved_at="2026-10-09T12:00:03Z",
        source_version="synthetic-alternate-v1",
        rights_status="unknown",
        rights_basis="Different source not qualified",
        declared_surface="abstract",
        expected_doi="10.1234/target",
        retrieval_hit_ref=hit_ref,
        identity_basis="Synthetic DOI match",
        separate_source=True,
    )
    assert (
        store.get(**alternate["quality_ref"])["body"]["retrieval_policy_applies_to_bytes"] is False
    )
    assert store.get(**alternate["source_use_ref"])["body"]["allowed_uses"] == ["discovery"]


def public_receipt(content, *, final_url="https://example.org/article", content_type="text/plain"):
    return {
        "status": 200,
        "content": content,
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "byte_length": len(content),
        "content_type": content_type,
        "requested_url": "https://doi.org/10.1234/target",
        "final_url": final_url,
        "redirect_chain": [
            {"from_url": "https://doi.org/10.1234/target", "to_url": final_url, "status": 302}
        ],
        "access_basis": "public_https_unauthenticated",
        "authenticated": False,
        "retrieved_at": "2026-10-09T12:00:00Z",
    }


def test_checked_public_redirected_abstract_gets_separate_private_direct_use(tmp_path):
    store, _, service, publication = setup(tmp_path)
    content = b"DOI 10.1234/target. Abstract: symptoms improved at week 8."
    receipt = public_receipt(content)
    result = service.inspect_response(
        "direct-public-abstract",
        publication,
        content=content,
        content_type="text/plain",
        source_url=receipt["final_url"],
        retrieved_at=receipt["retrieved_at"],
        source_version="synthetic-direct-v1",
        rights_status="unknown",
        rights_basis="No separate license verified",
        declared_surface="abstract",
        expected_doi="10.1234/target",
        selected_texts=["symptoms improved at week 8"],
        public_fetch_receipt=receipt,
    )
    assert result["classification"] == "abstract" and result["passage_refs"]
    quality = store.get(**result["quality_ref"])["body"]
    use = store.get(**result["source_use_ref"])["body"]
    policy = use["direct_source_use_policy"]
    assert policy["decision"] == "private_research_use"
    assert policy["requested_url"] != policy["final_url"]
    assert policy["redirect_chain"] == receipt["redirect_chain"]
    assert policy["source_sha256"] == hashlib.sha256(content).hexdigest()
    assert policy["copyright_license_status"] == "unverified"
    assert use["allowed_uses"] == ["discovery", "storage", "model_input", "factual_synthesis"]
    assert use["export_allowed"] is False
    assert quality["direct_source_use_policy"] == policy


@pytest.mark.parametrize("defect", ["hash", "bytes", "url", "status", "authentication"])
def test_mismatched_public_fetch_receipt_never_grants_model_use(tmp_path, defect):
    store, _, service, publication = setup(tmp_path)
    content = b"DOI 10.1234/target. Abstract: a result from a published study."
    receipt = public_receipt(content)
    if defect == "hash":
        receipt["content_sha256"] = "0" * 64
    elif defect == "bytes":
        receipt["content"] = b"Different bytes"
    elif defect == "url":
        receipt["final_url"] = "https://example.org/other"
    elif defect == "status":
        receipt["status"] = 403
    else:
        receipt["authenticated"] = True
    result = service.inspect_response(
        "direct-mismatch-" + defect,
        publication,
        content=content,
        content_type="text/plain",
        source_url="https://example.org/article",
        retrieved_at="2026-10-09T12:00:00Z",
        source_version="synthetic-direct-v1",
        rights_status="unknown",
        rights_basis="Unverified source use",
        declared_surface="abstract",
        expected_doi="10.1234/target",
        public_fetch_receipt=receipt,
    )
    assert result["classification"] == "abstract"
    use = store.get(**result["source_use_ref"])["body"]
    assert use["direct_source_use_policy"]["decision"] == "not_granted"
    assert use["allowed_uses"] == ["discovery"]
    assert store.get(**result["copy_ref"])["body"]["blob"] is None


@pytest.mark.parametrize(
    "content,surface,classification",
    [
        (b"<html><p>Log in to access this article</p></html>", "full_text", "access_page"),
        (b"DOI 10.1234/other. Abstract: wrong study.", "abstract", "wrong_article"),
        (b"DOI 10.1234/target. Search teaser.", "teaser", "teaser"),
        (b"DOI 10.1234/target. Provider-generated text.", "provider_summary", "provider_summary"),
    ],
)
def test_public_fetch_never_grants_unusable_surface(tmp_path, content, surface, classification):
    store, _, service, publication = setup(tmp_path)
    receipt = public_receipt(content)
    result = service.inspect_response(
        "direct-unusable",
        publication,
        content=content,
        content_type="text/plain",
        source_url=receipt["final_url"],
        retrieved_at=receipt["retrieved_at"],
        source_version="synthetic-direct-v1",
        rights_status="unknown",
        rights_basis="Unverified source use",
        declared_surface=surface,
        expected_doi="10.1234/target",
        public_fetch_receipt=receipt,
    )
    assert result["classification"] == classification
    assert result["copy_ref"] is None
    assert (
        store.get(**result["quality_ref"])["body"]["direct_source_use_policy"]["decision"]
        == "not_granted"
    )


def test_explicit_source_restriction_overrides_checked_public_direct_copy(tmp_path):
    store, _, service, publication = setup(tmp_path)
    content = b"DOI 10.1234/target. Abstract: a published result."
    receipt = public_receipt(content)
    result = service.inspect_response(
        "direct-restricted",
        publication,
        content=content,
        content_type="text/plain",
        source_url=receipt["final_url"],
        retrieved_at=receipt["retrieved_at"],
        source_version="synthetic-direct-v1",
        rights_status="restricted",
        rights_basis="Explicit source prohibition",
        declared_surface="abstract",
        expected_doi="10.1234/target",
        public_fetch_receipt=receipt,
    )
    assert store.get(**result["source_use_ref"])["body"]["allowed_uses"] == ["discovery"]


def test_generic_discovery_identity_assertion_cannot_grant_direct_copy(tmp_path):
    store, _, service, publication = setup(tmp_path)
    content = b"Abstract: The reported outcome improved at week 8."
    receipt = public_receipt(content)
    common = dict(
        content=content,
        content_type="text/plain",
        source_url=receipt["final_url"],
        retrieved_at=receipt["retrieved_at"],
        source_version="synthetic-direct-v1",
        rights_status="unknown",
        rights_basis="No separate license verified",
        declared_surface="abstract",
        expected_doi=None,
        public_fetch_receipt=receipt,
    )
    generic = service.inspect_response(
        "generic-identity",
        publication,
        identity_basis="Selected discovery hit asserted title",
        **common,
    )
    assert store.get(**generic["source_use_ref"])["body"]["allowed_uses"] == ["discovery"]
    verified = service.inspect_response(
        "checked-identity",
        publication,
        identity_basis="verified_source: publisher page title and publication metadata matched",
        **common,
    )
    assert (
        store.get(**verified["source_use_ref"])["body"]["direct_source_use_policy"][
            "identity_check"
        ]
        == "verified_source_basis"
    )
    assert "model_input" in store.get(**verified["source_use_ref"])["body"]["allowed_uses"]


def test_public_copy_with_explicit_embedded_use_prohibition_is_not_granted(tmp_path):
    store, _, service, publication = setup(tmp_path)
    content = (
        b"<html><h1>Abstract</h1><p>DOI 10.1234/target. Among 40 adults, symptoms improved at week 8.</p>"
        b"<h2>Copyright</h2><p>No text and data mining.</p></html>"
    )
    receipt = public_receipt(content, content_type="text/html")
    result = service.inspect_response(
        "embedded-prohibition",
        publication,
        content=content,
        content_type="text/html",
        source_url=receipt["final_url"],
        retrieved_at=receipt["retrieved_at"],
        source_version="synthetic-prohibition-v1",
        rights_status="unknown",
        rights_basis="No separate license verified",
        declared_surface=None,
        expected_doi="10.1234/target",
        public_fetch_receipt=receipt,
    )
    assert result["classification"] == "abstract"
    use = store.get(**result["source_use_ref"])["body"]
    assert use["direct_source_use_policy"]["source_prohibition_detected"] is True
    assert use["allowed_uses"] == ["discovery"]


def test_large_login_page_cannot_become_public_private_copy(tmp_path):
    store, _, service, publication = setup(tmp_path)
    content = (
        b"<html><h1>Sign in</h1><p>Log in to access this article.</p>"
        + b"Unrelated page content. " * 300
        + b"<h1>Abstract</h1><p>DOI 10.1234/target. A result.</p></html>"
    )
    receipt = public_receipt(content, content_type="text/html")
    result = service.inspect_response(
        "large-login",
        publication,
        content=content,
        content_type="text/html",
        source_url=receipt["final_url"],
        retrieved_at=receipt["retrieved_at"],
        source_version="synthetic-login-v1",
        rights_status="unknown",
        rights_basis="No separate license verified",
        declared_surface=None,
        expected_doi="10.1234/target",
        public_fetch_receipt=receipt,
    )
    assert result["classification"] == "access_page"
    assert result["copy_ref"] is None
    assert (
        store.get(**result["quality_ref"])["body"]["direct_source_use_policy"]["decision"]
        == "not_granted"
    )


def test_delimited_structured_html_abstract_is_not_full_text(tmp_path):
    store, _, service, publication = setup(tmp_path)
    long_abstract = "Substantive abstract detail. " * 155
    content = (
        "<html><h1>Abstract</h1><h2>Methods</h2><p>DOI 10.1234/target. "
        + long_abstract
        + "</p><h2>Results</h2><p>Symptoms improved at week 8.</p></html>"
    ).encode()
    result = service.inspect_response(
        "structured-abstract",
        publication,
        content=content,
        content_type="text/html",
        source_url="https://example.org/abstract",
        retrieved_at="2026-10-09T12:00:00Z",
        source_version="synthetic-abstract-v1",
        rights_status="permitted",
        rights_basis="Synthetic fixture",
        declared_surface=None,
        expected_doi="10.1234/target",
        selected_texts=["Symptoms improved at week 8."],
    )
    assert result["classification"] == "abstract"
    assert (
        store.get(**result["quality_ref"])["body"]["document_structure"] == "html_abstract_section"
    )
    assert store.get(**result["copy_ref"])["body"]["access_surface"] == "abstract"
    assert result["passage_refs"]


def test_jats_body_structure_qualifies_full_text_without_abstract_word(tmp_path):
    store, _, service, publication = setup(tmp_path)
    content = (
        b"<article><front><article-meta><article-id pub-id-type='doi'>10.1234/target"
        b"</article-id></article-meta></front><body><sec><title>Introduction</title>"
        b"<p>"
        + b"The article describes a real study. "
        * 50
        + b"</p></sec><sec><title>Results</title>"
        b"<p>Symptoms improved at week 8.</p></sec></body></article>"
    )
    receipt = public_receipt(content, content_type="application/xml")
    result = service.inspect_response(
        "jats-public-body",
        publication,
        content=content,
        content_type="application/xml",
        source_url=receipt["final_url"],
        retrieved_at=receipt["retrieved_at"],
        source_version="synthetic-jats-v1",
        rights_status="unknown",
        rights_basis="No separate license verified",
        declared_surface=None,
        expected_doi="10.1234/target",
        selected_texts=["Symptoms improved at week 8."],
        public_fetch_receipt=receipt,
    )
    assert result["classification"] == "full_text"
    assert store.get(**result["quality_ref"])["body"]["document_structure"] == "jats_article_body"
    assert store.get(**result["copy_ref"])["body"]["access_surface"] == "full_text"
    assert (
        store.get(**result["source_use_ref"])["body"]["direct_source_use_policy"]["decision"]
        == "private_research_use"
    )
    assert result["passage_refs"]


def test_html_article_body_remains_full_text_after_abstract_section(tmp_path):
    store, _, service, publication = setup(tmp_path)
    body = "The article reports methods and sample details. " * 15
    content = (
        "<html><h1>Abstract</h1><p>DOI 10.1234/target. Summary result.</p>"
        "<h1>Introduction</h1><p>" + body + "</p><h1>Methods</h1>"
        "<p>Participants and allocation were described.</p></html>"
    ).encode()
    result = service.inspect_response(
        "html-body",
        publication,
        content=content,
        content_type="text/html",
        source_url="https://example.org/full",
        retrieved_at="2026-10-09T12:00:00Z",
        source_version="synthetic-html-v1",
        rights_status="permitted",
        rights_basis="Synthetic fixture",
        declared_surface=None,
        expected_doi="10.1234/target",
        selected_texts=["Participants and allocation were described."],
    )
    assert result["classification"] == "full_text"
    assert store.get(**result["quality_ref"])["body"]["document_structure"] == "html_article_body"


def test_public_fetch_accepts_new_public_host_and_records_redirect(monkeypatch):
    class Response:
        status = 200
        url = "https://another-public.example/article"
        headers = {"Content-Type": "text/plain"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            assert size == 17
            return b"Synthetic source"

    class Opener:
        def __init__(self, handlers):
            self.handlers = handlers

        def open(self, request, **_kwargs):
            handler = next(item for item in self.handlers if isinstance(item, HTTPRedirectHandler))
            redirected = handler.redirect_request(request, None, 302, "Found", {}, Response.url)
            assert redirected.full_url == Response.url
            return Response()

    monkeypatch.setattr(
        "ora.evidence.intake.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.215.14", 443))],
    )
    monkeypatch.setattr("ora.evidence.intake.build_opener", lambda *handlers: Opener(handlers))
    result = fetch_public_source("https://new-public.example/abstract", max_bytes=16)
    assert result["content"] == b"Synthetic source"
    assert result["content_sha256"] == hashlib.sha256(b"Synthetic source").hexdigest()
    assert result["byte_length"] == len(b"Synthetic source")
    assert result["access_basis"] == "public_https_unauthenticated"
    assert result["authenticated"] is False
    assert result["requested_url"] == "https://new-public.example/abstract"
    assert result["final_url"] == Response.url
    assert result["redirect_chain"] == [
        {
            "from_url": "https://new-public.example/abstract",
            "to_url": Response.url,
            "status": 302,
        }
    ]
    with pytest.raises(Exception, match="permitted public HTTPS"):
        fetch_public_source(
            "https://new-public.example/abstract",
            allowed_hosts={"new-public.example"},
            max_bytes=16,
        )


@pytest.mark.parametrize(
    "url",
    ["http://example.org/a", "https://user:pass@example.org/a", "https://127.0.0.1/a"],
)
def test_public_fetch_rejects_nonpublic_or_credentialled_source_before_http(monkeypatch, url):
    def dns(host, *_args, **_kwargs):
        address = "127.0.0.1" if host == "127.0.0.1" else "93.184.215.14"
        return [(None, None, None, None, (address, 443))]

    monkeypatch.setattr("ora.evidence.intake.socket.getaddrinfo", dns)
    monkeypatch.setattr(
        "ora.evidence.intake.build_opener",
        lambda *_handlers: pytest.fail("Blocked source reached HTTP opener"),
    )
    with pytest.raises(Exception, match="public HTTPS|public addresses"):
        fetch_public_source(url)


def test_public_fetch_rejects_private_redirect_and_keeps_http_status(monkeypatch):
    class RedirectingOpener:
        def __init__(self, handlers):
            self.handlers = handlers

        def open(self, request, **_kwargs):
            handler = next(item for item in self.handlers if isinstance(item, HTTPRedirectHandler))
            handler.redirect_request(request, None, 302, "Found", {}, "https://10.0.0.1/private")
            pytest.fail("Private redirect was accepted")

    def dns(host, *_args, **_kwargs):
        address = "10.0.0.1" if host == "10.0.0.1" else "93.184.215.14"
        return [(None, None, None, None, (address, 443))]

    monkeypatch.setattr("ora.evidence.intake.socket.getaddrinfo", dns)
    monkeypatch.setattr(
        "ora.evidence.intake.build_opener", lambda *handlers: RedirectingOpener(handlers)
    )
    with pytest.raises(Exception, match="public addresses"):
        fetch_public_source("https://example.org/article")

    class ErrorOpener:
        def open(self, *_args, **_kwargs):
            raise HTTPError("https://example.org/missing", 404, "Not Found", {}, BytesIO())

    monkeypatch.setattr("ora.evidence.intake.build_opener", lambda *_handlers: ErrorOpener())
    with pytest.raises(PublicSourceHTTPError, match="HTTP 404") as failure:
        fetch_public_source("https://example.org/missing")
    assert failure.value.status_code == 404
    assert failure.value.requested_url == "https://example.org/missing"
    assert failure.value.final_url == "https://example.org/missing"
    assert failure.value.redirect_chain == []


def test_public_fetch_pins_checked_connection_ip_and_original_tls_hostname(monkeypatch):
    resolved = 0
    dialed = []
    tls_names = []

    def dns(*_args, **_kwargs):
        nonlocal resolved
        resolved += 1
        address = "93.184.215.14" if resolved == 1 else "93.184.215.15"
        return [(None, None, None, None, (address, 443))]

    class Context:
        def wrap_socket(self, sock, *, server_hostname):
            tls_names.append(server_hostname)
            return sock

    class Response:
        status = 200
        url = "https://new-public.example/article"
        headers = {"Content-Type": "text/plain"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return b"source"

    class Opener:
        def __init__(self, handlers):
            self.handlers = handlers

        def open(self, request, **_kwargs):
            handler = next(item for item in self.handlers if isinstance(item, HTTPSHandler))

            def exercise_connection(connection_type, _request, **_params):
                connection_type("new-public.example", timeout=1, context=Context()).connect()
                return Response()

            handler.do_open = exercise_connection
            return handler.https_open(request)

    monkeypatch.setattr("ora.evidence.intake.socket.getaddrinfo", dns)
    monkeypatch.setattr(
        "ora.evidence.intake.socket.create_connection",
        lambda destination, *_args: dialed.append(destination) or object(),
    )
    monkeypatch.setattr("ora.evidence.intake.build_opener", lambda *handlers: Opener(handlers))
    result = fetch_public_source("https://new-public.example/article")
    assert result["content"] == b"source"
    assert dialed == [("93.184.215.15", 443)]
    assert tls_names == ["new-public.example"]


def test_public_fetch_rejects_dns_rebind_to_private_at_connect(monkeypatch):
    resolved = 0

    def dns(*_args, **_kwargs):
        nonlocal resolved
        resolved += 1
        address = "93.184.215.14" if resolved == 1 else "127.0.0.1"
        return [(None, None, None, None, (address, 443))]

    class Opener:
        def __init__(self, handlers):
            self.handlers = handlers

        def open(self, request, **_kwargs):
            handler = next(item for item in self.handlers if isinstance(item, HTTPSHandler))

            def exercise_connection(connection_type, _request, **_params):
                connection_type("new-public.example", timeout=1).connect()
                pytest.fail("Private address was dialed")

            handler.do_open = exercise_connection
            return handler.https_open(request)

    monkeypatch.setattr("ora.evidence.intake.socket.getaddrinfo", dns)
    monkeypatch.setattr(
        "ora.evidence.intake.socket.create_connection",
        lambda *_args, **_kwargs: pytest.fail("Private address was dialed"),
    )
    monkeypatch.setattr("ora.evidence.intake.build_opener", lambda *handlers: Opener(handlers))
    with pytest.raises(Exception, match="public addresses"):
        fetch_public_source("https://new-public.example/article")


def test_corrupt_xml_is_scoped_parser_failure(tmp_path):
    store, _, service, publication = setup(tmp_path)
    saved = inspect(
        service,
        publication,
        "bad-xml",
        b"<article><broken",
        mime="application/xml",
        surface="full_text",
    )
    assert saved["classification"] == "parser_failure"
    assert saved["copy_ref"] is None
    assert store.records("source_passage") == []


def test_abstract_without_visible_identifier_or_other_basis_stays_unverified(tmp_path):
    _, _, service, publication = setup(tmp_path)
    saved = inspect(
        service, publication, "no-identity", b"Symptoms decreased by 3 points.", identity_basis=None
    )
    assert saved["classification"] == "identity_unverified"
    assert saved["copy_ref"] is None


def test_restricted_hit_cannot_launder_same_url_but_alternate_copy_can_proceed(tmp_path):
    store, _, service, publication = setup(tmp_path)
    delivery = record_delivery(
        store,
        {
            "mission_id": "synthetic-mission",
            "task_id": "synthetic-task",
            "request_id": "restricted-search",
            "attempt_id": "attempt-1",
            "route": "synthetic-index",
            "provider": "synthetic-count",
            "operation": "search",
            "requested_at": "2026-10-09T12:00:00Z",
            "requested_window": {"offset": 0, "limit": 1},
            "query": "synthetic CBT trial",
        },
        {
            "status": "ok",
            "count": 1,
            "offset": 0,
            "items": [
                {
                    "id": "teaser",
                    "url": "https://example.org/restricted",
                    "title": "Synthetic CBT trial",
                    "content": "Short teaser",
                    "surface": "teaser",
                    "origin": "original_source",
                    "rights_status": "restricted",
                    "access_status": "limited",
                    "doi": "10.1234/target",
                }
            ],
        },
        delivery_id="restricted-delivery",
        adapter="count_offset",
        retrieved_at="2026-10-09T12:00:00Z",
    )
    hit = delivery["accepted_hit_refs"][0]
    article = (
        b"<html><h1>Results</h1><p>DOI: 10.1234/target.</p>"
        b"<p>Among 40 adults, symptoms decreased by 3 points.</p></html>"
    )
    same_url = service.inspect_response(
        "same-url",
        publication,
        content=article,
        content_type="text/html",
        source_url="https://example.org/restricted",
        retrieved_at="2026-10-09T12:01:00Z",
        source_version="same-url-v1",
        rights_status="permitted",
        rights_basis="Synthetic asserted permission",
        declared_surface="full_text",
        expected_doi="10.1234/target",
        identity_basis="Synthetic exact DOI",
        selected_texts=["symptoms decreased by 3 points"],
        retrieval_hit_ref=hit,
    )
    assert same_url["classification"] == "passage"
    assert same_url["passage_refs"] == []
    assert store.get(**same_url["copy_ref"])["body"]["blob"] is None
    redirected_without_new_policy = service.inspect_response(
        "redirected-without-policy",
        publication,
        content=article,
        content_type="text/html",
        source_url="https://example.org/redirected",
        retrieved_at="2026-10-09T12:01:30Z",
        source_version="redirected-v1",
        rights_status="permitted",
        rights_basis="Synthetic asserted permission",
        declared_surface="full_text",
        expected_doi="10.1234/target",
        identity_basis="Synthetic exact DOI",
        selected_texts=["symptoms decreased by 3 points"],
        retrieval_hit_ref=hit,
    )
    assert redirected_without_new_policy["passage_refs"] == []
    alternate = service.inspect_response(
        "alternate-url",
        publication,
        content=article,
        content_type="text/html",
        source_url="https://example.org/permitted-alternate",
        retrieved_at="2026-10-09T12:02:00Z",
        source_version="alternate-v1",
        rights_status="permitted",
        rights_basis="Synthetic alternate copy permission",
        declared_surface="full_text",
        expected_doi="10.1234/target",
        identity_basis="Synthetic exact DOI",
        selected_texts=["symptoms decreased by 3 points"],
        retrieval_hit_ref=hit,
        separate_source=True,
    )
    assert len(alternate["passage_refs"]) == 1
    assert (
        service.source_for_model(alternate["passage_refs"][0])["text"]
        == "symptoms decreased by 3 points"
    )
    assert store.get(**alternate["quality_ref"])["body"]["retrieval_hit_ref"] == hit


def test_partial_original_keeps_usable_span_and_names_missing_field(tmp_path):
    store, _, service, publication = setup(tmp_path)
    fragment = (
        b"DOI: 10.1234/target. Among 40 adults, the score fell by -2.5 points. More text is missing"
    )
    saved = service.inspect_response(
        "partial-original",
        publication,
        content=fragment,
        content_type="text/plain",
        source_url="https://example.org/partial",
        retrieved_at="2026-10-09T12:00:00Z",
        source_version="synthetic-partial-v1",
        rights_status="permitted",
        rights_basis="Synthetic permitted source",
        declared_surface="full_text",
        expected_doi="10.1234/target",
        truncated=True,
        selected_texts=["the score fell by -2.5 points"],
        missing_fields=["allocation method"],
    )
    assert saved["classification"] == "partial"
    assert store.get(**saved["copy_ref"])["body"]["access_surface"] == "passage"
    assert (
        service.source_for_model(saved["passage_refs"][0])["text"]
        == "the score fell by -2.5 points"
    )
    need = store.get(**saved["information_need_refs"][0])["body"]
    assert need["field"] == "allocation method" and need["status"] == "open"


def test_bad_selection_is_omission_without_discarding_good_selection(tmp_path):
    store, _, service, publication = setup(tmp_path)
    content = b"DOI: 10.1234/target. The score fell by -2.5 points."
    saved = inspect(
        service,
        publication,
        "mixed-selection",
        content,
        selected=["unreported subgroup", "-2.5 points"],
    )
    assert saved["classification"] == "abstract"
    assert len(saved["passage_refs"]) == 1
    assert saved["selection_omissions"] == [
        {"index": 1, "reason": "selection_absent_or_ambiguous", "occurrences": 0}
    ]
    assert service.source_for_model(saved["passage_refs"][0])["text"] == "-2.5 points"
    assert (
        store.get(**saved["quality_ref"])["body"]["selection_omissions"]
        == saved["selection_omissions"]
    )


def test_mime_sniff_and_namespaced_xml_keep_actual_source_identity(tmp_path, monkeypatch):
    store, _, service, publication = setup(tmp_path)

    def fake_pdf_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0], 0, b"DOI: 10.1234/target.\nResults\nThe difference was -2.5 points.\f", b""
        )

    monkeypatch.setattr("ora.evidence.intake.subprocess.run", fake_pdf_run)
    saved_pdf = inspect(
        service,
        publication,
        "mislabeled-pdf",
        b"%PDF-1.4\nsynthetic-pdf",
        mime="text/html",
        surface="full_text",
        selected=["-2.5 points"],
    )
    assert saved_pdf["classification"] == "passage"
    quality = store.get(**saved_pdf["quality_ref"])["body"]
    assert quality["detected_content_type"] == "application/pdf" and quality["mime_mismatch"]
    assert service.source_for_model(saved_pdf["passage_refs"][0])["text"] == "-2.5 points"
    login = inspect(
        service,
        publication,
        "mislabeled-login",
        b"<html><p>Sign in to access this article</p></html>",
        mime="application/pdf",
        surface="full_text",
    )
    assert login["classification"] == "access_page"
    namespaced = (
        b"<?xml version='1.0'?><j:article xmlns:j='urn:jats'>"
        b"<j:article-id pub-id-type='doi'>10.1234/target</j:article-id>"
        b"<j:sec><j:title>Results</j:title><j:p>The difference was -3 points."
        b"</j:p></j:sec></j:article>"
    )
    saved_xml = inspect(
        service,
        publication,
        "namespaced-xml",
        namespaced,
        mime="text/plain",
        surface="full_text",
        selected=["-3 points"],
    )
    assert saved_xml["classification"] == "passage"
    assert "section=Results" in service.source_for_model(saved_xml["passage_refs"][0])["locator"]


def test_truncated_namespaced_xml_keeps_only_closed_selected_element(tmp_path):
    _, _, service, publication = setup(tmp_path)
    fragment = (
        b"<?xml version='1.0'?><j:article xmlns:j='urn:jats'>"
        b"<j:article-id pub-id-type='doi'>10.1234/target</j:article-id>"
        b"<j:sec><j:title>Results</j:title>"
        b"<j:p>Reported change was -4 points.</j:p><j:p>Unfinished"
    )
    saved = inspect(
        service,
        publication,
        "partial-jats",
        fragment,
        mime="text/plain",
        surface="full_text",
        truncated=True,
        selected=["-4 points"],
    )
    assert saved["classification"] == "partial"
    assert service.source_for_model(saved["passage_refs"][0])["text"] == "-4 points"


@pytest.mark.parametrize(
    "advertised,body,expected_observed",
    [
        ("99", b"not read", None),
        (None, b"123456789", 9),
    ],
)
def test_bounded_fetch_reports_oversize_without_claiming_full_body_hash(
    monkeypatch, advertised, body, expected_observed
):
    class Response:
        status = 200
        url = "https://example.org/article"
        headers = {"Content-Type": "application/pdf"}
        if advertised is not None:
            headers["Content-Length"] = advertised

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, size):
            assert size == 9
            return body[:size]

    class Opener:
        def open(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(
        "ora.evidence.intake.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.215.14", 443))],
    )
    monkeypatch.setattr("ora.evidence.intake.build_opener", lambda *_args: Opener())
    with pytest.raises(OversizedSourceError) as caught:
        fetch_public_source(
            "https://example.org/article", allowed_hosts={"example.org"}, max_bytes=8
        )
    receipt = caught.value.receipt()
    assert receipt["condition"] == "oversized_response"
    assert receipt["limit_bytes"] == 8 and receipt["advertised_bytes"] == (
        int(advertised) if advertised else None
    )
    assert receipt["observed_bytes"] == expected_observed
    assert receipt["complete_body_sha256"] is None
    assert "selected_passage_or_table" in receipt["next_routes"]
    if expected_observed is None:
        assert receipt["observed_prefix_sha256"] is None
    else:
        assert receipt["observed_prefix_sha256"] == hashlib.sha256(body).hexdigest()
