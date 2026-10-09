"""Accepted retrieval hits become encounters, not automatic study identities."""

import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from ora.evidence import EvidenceError, EvidenceLibrary
from ora.retrieval.receipts import record_delivery
from ora.state import StateStore


def request(suffix):
    return {
        "mission_id": "simulated-mission", "task_id": "simulated-task",
        "request_id": f"source-search-{suffix}", "attempt_id": f"attempt-{suffix}",
        "route": "simulated-index", "provider": "simulated-count",
        "operation": "search", "requested_at": "2026-01-02T00:00:00Z",
        "requested_window": {"offset": 0, "limit": 2},
        "query": "simulated depression comparison",
    }


def one_hit(store, suffix, **source_changes):
    source = {
        "id": f"provider-item-{suffix}", "url": f"https://example.org/simulated/{suffix}",
        "title": "Simulated CBT report", "content": "Simulated depression response at six weeks.",
        "surface": "abstract", "origin": "original_source",
        "rights_status": "permitted", "access_status": "available",
    }
    source.update(source_changes)
    saved = record_delivery(
        store, request(suffix), {"status": "ok", "count": 1, "offset": 0, "items": [source]},
        delivery_id=f"delivery-{suffix}", adapter="count_offset",
        retrieved_at="2026-01-02T00:01:00Z",
    )
    return saved["accepted_hit_refs"][0]


def work(lib, suffix):
    return lib.register_publication(
        f"publication-{suffix}", identifiers={"doi": f"10.1234/simulated-{suffix}"},
        title="Simulated CBT report", verification="Synthetic exact DOI fixture",
    )["publication_ref"]


def test_retained_retrieval_text_keeps_exact_hit_lineage(tmp_path):
    store = StateStore(tmp_path / "state")
    lib = EvidenceLibrary(store)
    publication = work(lib, "allowed")
    hit = one_hit(
        store, "allowed", authors=[{"name": "Ada Example"}],
        tables=[{"rows": [["intervention", 20, 0.25], ["control", 19, None]]}],
    )
    encounter = lib.record_retrieval_hit("encounter-allowed", publication, hit)["encounter_ref"]
    saved = lib.retain_retrieval_hit_copy(
        "copy-allowed", publication, encounter, source_version="retrieval-snapshot-v1",
        passages=[{
            "span_id": "reported-result", "locator": "Abstract, sentence 1",
            "text": "Simulated depression response at six weeks.", "start": 0, "end": 43,
        }],
    )
    body = store.get(**saved["copy_ref"])["body"]
    assert store.get(**encounter)["body"]["retrieval_hit"] == hit
    assert body["access_surface"] == "abstract"
    assert body["inspection_state"] == "provider_asserted"
    assert body["scientific_metadata"]["authors"] == [{"name": "Ada Example"}]
    assert body["structured_content"]["tables"][0]["rows"][1] == ["control", 19, None]
    assert body["mapping_version"] == "reference-count-offset.scientific-v1"
    assert saved["promoted"] is False
    assert store.get("publication", publication["id"])["body"]["accepted_copy"] is None
    candidate = lib.search_passages("depression response")["results"][0]
    assert candidate["inspection_state"] == "provider_asserted"
    assert lib.search_passages("depression response", checked_only=True)["results"] == []
    assert store.read_blob(body["blob"]["sha256"]) == b"Simulated depression response at six weeks."
    assert lib.get_passage(saved["passage_refs"][0]["id"])["passage"]["body"]["locator"] == "Abstract, sentence 1"


def test_same_exact_hit_has_one_encounter_across_distinct_parallel_handoffs(tmp_path):
    store = StateStore(tmp_path / "state")
    lib = EvidenceLibrary(store)
    publication = work(lib, "shared")
    hit = one_hit(store, "shared")

    def handoff(index):
        return lib.record_retrieval_hit(f"handoff-{index}", publication, hit)["encounter_ref"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(handoff, range(2))
    assert first == second
    assert len(store.records("source_encounter")) == 1
    assert handoff(0) == first  # Same operation replay.

    other_hit = one_hit(store, "other-route")
    other = lib.record_retrieval_hit("handoff-other-route", publication, other_hit)["encounter_ref"]
    assert other != first
    assert len(store.records("source_encounter")) == 2


def test_two_routes_one_work_partial_then_abstract_keep_late_failure_and_peer_request(tmp_path):
    store = StateStore(tmp_path / "state")
    lib = EvidenceLibrary(store)
    doi = "10.1234/two-route-work"

    def deliver(suffix, provider, surface, access, text):
        asked = request(suffix)
        asked["provider"] = provider
        asked["route"] = f"route-{suffix}"
        return record_delivery(
            store, asked,
            {"status": "ok", "count": 1, "offset": 0, "items": [{
                "id": f"{provider}-item", "url": f"https://example.org/{suffix}",
                "title": "One synthetic report", "content": text,
                "surface": surface, "origin": "original_source",
                "rights_status": "permitted", "access_status": access,
                "authors": [{"name": "Ada Example"}], "doi": doi,
            }]},
            delivery_id=f"delivery-{suffix}", adapter="count_offset",
            retrieved_at="2026-01-02T00:01:00Z",
        )

    partial = deliver("alpha", "simulated-alpha", "teaser", "limited", "A short teaser.")
    full = deliver("beta", "simulated-beta", "abstract", "available", "A verified synthetic abstract.")
    first_hit, second_hit = partial["accepted_hit_refs"][0], full["accepted_hit_refs"][0]
    first_doi = store.get(**first_hit)["body"]["source"]["scientific_metadata"]["doi"]
    second_doi = store.get(**second_hit)["body"]["source"]["scientific_metadata"]["doi"]
    assert first_doi == second_doi == doi
    first_work = lib.register_publication(
        "work-alpha", identifiers={"doi": first_doi}, title="One synthetic report",
        verification="Synthetic source explicitly supplies DOI",
    )["publication_ref"]
    second_work = lib.register_publication(
        "work-beta", identifiers={"doi": second_doi}, title="One synthetic report",
        verification="Independent synthetic source supplies the same DOI",
    )["publication_ref"]
    assert first_work["id"] == second_work["id"]
    first_encounter = lib.record_retrieval_hit("encounter-alpha", first_work, first_hit)["encounter_ref"]
    second_encounter = lib.record_retrieval_hit("encounter-beta", second_work, second_hit)["encounter_ref"]
    assert first_encounter != second_encounter
    lib.retain_retrieval_hit_copy(
        "teaser-copy", first_work, first_encounter, source_version="synthetic-teaser-v1",
    )
    checked = lib.add_copy(
        "checked-abstract", second_work, second_encounter,
        content=b"A verified synthetic abstract.", content_sha256=None,
        mime_type="text/plain", source_version="synthetic-abstract-v1",
        access_surface="abstract", retrieved_at="2026-01-02T00:01:00Z",
        rights_basis="Synthetic permitted source", retain_bytes=True,
        reference="https://example.org/beta", passages=[{
            "span_id": "abstract", "locator": "Abstract, sentence 1",
            "text": "A verified synthetic abstract.", "start": 0, "end": 30,
        }],
        inspection_state="checked", validation_record="synthetic-source-check-v1",
    )
    assert checked["promoted"] is True

    failed_request = request("alpha")
    failed_request.update(provider="simulated-alpha", route="route-alpha", attempt_id="late-failure")
    failure = record_delivery(
        store, failed_request, {"status": "error", "error_code": "upstream_error"},
        delivery_id="delivery-alpha-late-failure", adapter="count_offset",
        retrieved_at="2026-01-02T00:02:00Z",
    )
    assert failure["outcome"] == "provider_failure"

    def ask_peer(tx):
        message = tx.put("message", "peer-review-request", {
            "mission_id": "simulated-mission", "status": "requested",
            "summary": "Check the two routes and the abstract's provenance.",
        })
        return {"message_ref": {key: message[key] for key in ("kind", "id", "version")}}

    peer = store.transact("ask-peer", {"subject": "source-review"}, ask_peer,
                          mission_id="simulated-mission")
    accepted = store.get("publication", first_work["id"])["body"]["accepted_copy"]
    assert accepted["id"] == checked["copy_ref"]["id"]
    assert len(store.records("source_encounter")) == 2
    assert len(store.records("retrieval_attempt")) == 3
    assert store.get(**peer["message_ref"])["body"]["status"] == "requested"


def test_rights_limited_hit_keeps_locator_but_does_not_promote_full_text(tmp_path):
    store = StateStore(tmp_path / "state")
    lib = EvidenceLibrary(store)
    publication = work(lib, "limited")
    hit = one_hit(store, "limited", surface="full_text", rights_status="restricted")
    encounter = lib.record_retrieval_hit("encounter-limited", publication, hit)["encounter_ref"]
    saved = lib.retain_retrieval_hit_copy(
        "copy-limited", publication, encounter, source_version="retrieval-snapshot-v1"
    )
    body = store.get(**saved["copy_ref"])["body"]
    assert body["blob"] is None
    assert body["content_sha256"] == store.get(**hit)["body"]["source"]["content_sha256"]
    assert body["access_surface"] == "metadata"
    assert body["reference"] == "https://example.org/simulated/limited"
    disallowed = b"Restricted source bytes must not be installed."
    with pytest.raises(EvidenceError, match="did not permit"):
        lib.add_copy(
            "forbidden-copy", publication, encounter, content=disallowed,
            content_sha256=None, mime_type="text/plain", source_version="retrieval-snapshot-v1",
            access_surface="full_text", retrieved_at="2026-01-02T00:01:00Z",
            rights_basis="Provider receipt says restricted", retain_bytes=True,
            reference="https://example.org/simulated/limited", passages=[],
        )
    assert not store.blobs.path(hashlib.sha256(disallowed).hexdigest()).exists()


def test_provider_summary_is_not_promoted_to_original_source(tmp_path):
    store = StateStore(tmp_path / "state")
    lib = EvidenceLibrary(store)
    publication = work(lib, "summary")
    hit = one_hit(store, "summary", surface="provider_summary", origin="provider_generated")
    encounter = lib.record_retrieval_hit("encounter-summary", publication, hit)["encounter_ref"]
    with pytest.raises(EvidenceError, match="Provider-generated summary"):
        lib.retain_retrieval_hit_copy(
            "copy-summary", publication, encounter, source_version="retrieval-snapshot-v1"
        )
    assert store.records("source_copy") == []
