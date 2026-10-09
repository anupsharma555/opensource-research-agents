"""Exercise source identity, retained copies, and scoped review on local state."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ora.evidence import EvidenceError, EvidenceLibrary
from ora.state import StateStore


def library(tmp_path):
    return EvidenceLibrary(StateStore(tmp_path / "research-state"))


def publication(lib, operation, doi, title="Simulated CBT report"):
    return lib.register_publication(
        operation,
        identifiers={"doi": doi},
        title=title,
        verification="Synthetic fixture: DOI printed in this report metadata",
    )["publication_ref"]


def encounter(lib, operation, publication_ref, status="accepted", surface="abstract"):
    return lib.record_encounter(
        operation,
        publication_ref=publication_ref,
        retrieval_hit_ref=None,
        status=status,
        access_surface=surface,
        retrieved_at="2026-01-02T00:00:00Z",
        route=f"synthetic-route-{operation}",
    )["encounter_ref"]


def copy(lib, operation, publication_ref, encounter_ref, body, surface="abstract"):
    quote = body.split(". ")[0]
    return lib.add_copy(
        operation,
        publication_ref,
        encounter_ref,
        content=body.encode(),
        content_sha256=None,
        mime_type="text/plain",
        source_version="simulated-v1",
        access_surface=surface,
        retrieved_at="2026-01-02T00:00:00Z",
        rights_basis="Synthetic project-owned fixture",
        retain_bytes=True,
        reference=f"simulated://{operation}",
        passages=[{"span_id": "result", "locator": "Abstract, sentence 1", "text": quote, "start": 0, "end": len(quote)}],
        inspection_state="checked", validation_record="synthetic-offset-check-v1",
    )


def test_verified_aliases_join_one_publication_but_titles_do_not(tmp_path):
    lib = library(tmp_path)
    first = lib.register_publication(
        "first",
        identifiers={"doi": "https://doi.org/10.1234/CBT.A", "pmid": "12345678"},
        title="A simulated randomized comparison",
        verification="Both identifiers appear on the same synthetic source record",
    )["publication_ref"]
    matched = lib.register_publication(
        "second",
        identifiers={"doi": "doi:10.1234/cbt.a"},
        title="A simulated randomized comparison",
        verification="Exact DOI resolver record",
    )
    assert matched["status"] == "matched"
    assert matched["publication_ref"]["id"] == first["id"]
    assert lib.resolve_publication("pmid", "12345678")["id"] == first["id"]
    distinct = publication(lib, "third", "10.1234/cbt.b", "A simulated randomized comparison")
    assert distinct["id"] != first["id"]
    assert {ref["id"] for ref in lib.suggest_title_matches("A simulated randomized comparison") } == {first["id"], distinct["id"]}
    assert {ref["id"] for ref in lib.suggest_title_matches("A simulated randomised comparison")} == {first["id"], distinct["id"]}
    provisional = lib.register_publication(
        "fourth", identifiers={}, title="A simulated randomized comparison",
        verification="Title-only encounter retained without verified identity",
    )
    assert provisional["status"] == "provisional"
    assert provisional["publication_ref"]["id"] not in {first["id"], distinct["id"]}


def test_preprint_and_correction_are_typed_reports_not_new_trial_families(tmp_path):
    lib = library(tmp_path)
    preprint = lib.register_publication(
        "preprint", identifiers={"arxiv": "arXiv:2401.12345v2"},
        title="Simulated depression preprint", kind="preprint",
        verification="Synthetic preprint metadata",
    )["publication_ref"]
    journal = publication(lib, "journal", "10.1234/journal-final", "Simulated depression final report")
    correction = lib.register_publication(
        "correction", identifiers={"doi": "10.1234/journal-correction"},
        title="Correction for simulated report", kind="correction",
        verification="Synthetic correction metadata",
    )["publication_ref"]
    family = lib.register_study_family(
        "family", registry_ids=["ISRCTN00000000"], verification="Synthetic participant-group identity"
    )["family_ref"]
    lib.link_publication_to_family(
        "preprint-family", preprint, family, relation="preprint_of_study",
        basis="Synthetic fixture explicitly links participant groups",
    )
    lib.link_publication_to_family(
        "journal-family", journal, family, relation="reports_study",
        basis="Synthetic fixture explicitly links participant groups",
    )
    lib.link_publications(
        "preprint-journal", preprint, journal, relation="preprint_of",
        basis="Synthetic fixture explicitly links the versions",
    )
    lib.link_publications(
        "correction-journal", correction, journal, relation="correction_of",
        basis="Synthetic fixture explicitly names the corrected report",
    )
    assert {edge["relation"] for edge in lib.store.links({**preprint, "version": 1})} == {"preprint_of_study", "preprint_of"}
    assert lib.study_families_for_publication(preprint)[0]["count_key"] == lib.study_families_for_publication(journal)[0]["count_key"]
    assert lib.resolve_publication("arxiv", "2401.12345v3")["id"] == preprint["id"]


def test_conflicting_identifiers_are_recorded_for_review_not_merged(tmp_path):
    lib = library(tmp_path)
    one = publication(lib, "one", "10.1234/report-one")
    two = lib.register_publication(
        "two", identifiers={"pmid": "12345679"}, title="Different simulated report",
        verification="Separate synthetic record",
    )["publication_ref"]
    conflict = lib.register_publication(
        "collision", identifiers={"doi": "10.1234/report-one", "pmid": "12345679"},
        title="A metadata provider conflated two simulated reports",
        verification="Conflicting candidate metadata requiring review",
    )
    assert conflict["status"] == "ambiguous"
    assert {ref["id"] for ref in conflict["candidate_refs"]} == {one["id"], two["id"]}
    assert lib.store.get(**conflict["review_ref"])["body"]["assertion"]["title"].startswith("A metadata")
    assert lib.resolve_publication("doi", "10.1234/report-one")["id"] == one["id"]


def test_family_links_count_verified_registry_pair_but_not_title_candidate(tmp_path):
    lib = library(tmp_path)
    with pytest.raises(EvidenceError, match="separately"):
        lib.register_study_family(
            "unsafe-two-id-bind", registry_ids=["ISRCTN00000000", "NCT00000000"],
            verification="Unreviewed combined identifier assertion",
        )
    first = lib.register_study_family(
        "family-a", registry_ids=["ISRCTN00000000"], verification="Synthetic main registry record"
    )["family_ref"]
    second = lib.register_study_family(
        "family-b", registry_ids=["NCT00000000"], verification="Synthetic secondary registry record"
    )["family_ref"]
    third = lib.register_study_family(
        "family-c", registry_ids=["NCT00000001"], verification="Similar title only"
    )["family_ref"]
    lib.link_registry_families("candidate", first, third, verified=False, basis="Similar synthetic trial title")
    assert lib.study_cluster(first) == [first["id"]]
    registry_report = publication(lib, "registry-report", "10.1234/registry-link")
    evidence_ref = copy(
        lib, "registry-copy", registry_report, encounter(lib, "registry-hit", registry_report),
        "Main ISRCTN00000000 and secondary NCT00000000 identify one simulated trial.",
    )["passage_refs"][0]
    lib.link_registry_families(
        "verified", first, second, verified=True,
        basis="Synthetic fixture explicitly asserts main and secondary registry IDs name one trial",
        evidence_ref=evidence_ref,
    )
    assert lib.study_cluster(first) == sorted([first["id"], second["id"]])
    assert lib.study_cluster(third) == [third["id"]]
    report = publication(lib, "family-report", "10.1234/family-report")
    lib.link_publication_to_family(
        "report-link", report, first, relation="reports_study",
        basis="Synthetic fixture explicitly identifies participant group",
    )
    assert len(lib.store.links({**report, "version": 1})) == 1
    relationship = lib.study_families_for_publication(report)[0]
    assert relationship["verified_cluster_ids"] == sorted([first["id"], second["id"]])
    assert relationship["count_key"] == min(first["id"], second["id"])
    companion = publication(lib, "companion-report", "10.1234/companion-report")
    independent = publication(lib, "independent-report", "10.1234/independent-report")
    lib.link_publication_to_family(
        "companion-family", companion, second, relation="reports_study",
        basis="Synthetic companion report identifies the same participants",
    )
    lib.link_publication_to_family(
        "independent-family", independent, third, relation="reports_study",
        basis="Synthetic independent report identifies different participants",
    )
    count_keys = {
        lib.study_families_for_publication(ref)[0]["count_key"]
        for ref in (report, companion, independent)
    }
    assert len(count_keys) == 2  # Three reports, two verified study families.

    lib.store.transact("stale-linkage", {"fixture": "source corrected"},
                       lambda tx: {"review_refs": tx.invalidate(tx.get(**evidence_ref), "Synthetic registry link corrected")})
    assert lib.study_cluster(first) == [first["id"]]
    assert (
        lib.study_families_for_publication(report)[0]["count_key"]
        != lib.study_families_for_publication(companion)[0]["count_key"]
    )


def test_parallel_encounters_and_late_partial_do_not_replace_full_copy(tmp_path):
    lib = library(tmp_path)

    def register(index):
        return lib.register_publication(
            f"parallel-identity-{index}", identifiers={"doi": "10.1234/shared-report"},
            title="Simulated shared report", verification="Same verified synthetic DOI",
        )["publication_ref"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        refs = list(pool.map(register, range(2)))
    assert refs[0]["id"] == refs[1]["id"]
    first_hit = encounter(lib, "full-hit", refs[0], surface="full_text")
    second_hit = encounter(lib, "late-partial", refs[1], status="partial", surface="abstract")
    full = copy(lib, "full-copy", refs[0], first_hit, "Full text reports a simulated outcome. Methods remain fictional.", "full_text")
    partial = copy(lib, "partial-copy", refs[1], second_hit, "Abstract reports a simulated outcome.")
    assert full["promoted"] is True
    assert partial["promoted"] is False
    assert lib.store.get("publication", refs[0]["id"])["body"]["accepted_copy"]["id"] == full["copy_ref"]["id"]
    assert len(lib.store.records("source_encounter")) == 2
    assert lib.store.get(**partial["copy_ref"])["body"]["access_surface"] == "abstract"
    failed = encounter(lib, "failed-hit", refs[1], status="failed", surface="metadata")
    assert lib.store.get(**failed)["body"]["status"] == "failed"
    assert lib.store.get("publication", refs[0]["id"])["body"]["accepted_copy"]["id"] == full["copy_ref"]["id"]


def test_publication_identity_replays_after_failed_commit_without_extra_work(tmp_path):
    def fail_before_commit(point):
        if point == "before_metadata_commit":
            raise OSError("synthetic interruption before identity commit")

    failed = EvidenceLibrary(StateStore(tmp_path, fault=fail_before_commit))
    kwargs = {
        "identifiers": {"doi": "10.1234/crash-replay"},
        "title": "Synthetic replay work",
        "verification": "Exact synthetic identifier",
    }
    with pytest.raises(OSError, match="synthetic interruption"):
        failed.register_publication("register-after-crash", **kwargs)
    restarted = EvidenceLibrary(StateStore(tmp_path))
    first = restarted.register_publication("register-after-crash", **kwargs)
    assert restarted.register_publication("register-after-crash", **kwargs) == first
    assert len(restarted.store.records("publication")) == 1


def test_correction_only_reopens_claims_using_affected_copy_and_replay_is_stable(tmp_path):
    lib = library(tmp_path)
    first = publication(lib, "a-publication", "10.1234/a")
    other = publication(lib, "b-publication", "10.1234/b")
    notice = lib.register_publication(
        "notice-publication", identifiers={"doi": "10.1234/a-correction"},
        title="Correction for simulated report A", kind="correction",
        verification="Synthetic correction notice identifies its DOI",
    )["publication_ref"]
    a = copy(lib, "a-copy", first, encounter(lib, "a-hit", first), "Simulated report A says 18 of 40 responded.")
    b = copy(lib, "b-copy", other, encounter(lib, "b-hit", other), "Simulated report B says 20 of 50 responded.")
    c = copy(lib, "notice-copy", notice, encounter(lib, "notice-hit", notice), "Correction changes a reported count.")

    def add_claim(tx):
        a_claim = tx.put("claim", "claim-a", {"text": "A reported count"})
        b_claim = tx.put("claim", "claim-b", {"text": "B reported count"})
        report = tx.put("report", "as-of-report", {"text": "As-of count from report A"})
        tx.link(report, a_claim, "depends_on")
        return {
            "a": {key: a_claim[key] for key in ("kind", "id", "version")},
            "b": {key: b_claim[key] for key in ("kind", "id", "version")},
            "report": {key: report[key] for key in ("kind", "id", "version")},
        }

    claims = lib.store.transact("claims", {"fixture": "two independent synthetic claims"}, add_claim)
    lib.link_claim("a-support", claims["a"], a["passage_refs"][0])
    lib.link_claim("b-support", claims["b"], b["passage_refs"][0])
    result = lib.record_correction(
        "correct-a", a["copy_ref"], c["copy_ref"], kind="correction",
        reason="Synthetic source A count was amended",
        mission_id="simulated-mission",
    )
    assert claims["a"] in result["review_refs"]
    assert len(lib.store.events("simulated-mission")) == 1
    assert lib.store.review_status(claims["a"])
    assert lib.store.review_status(claims["report"])
    assert not lib.store.review_status(claims["b"])
    assert not lib.store.review_status(c["copy_ref"])
    assert lib.store.read_blob(lib.store.get(**a["copy_ref"])["body"]["blob"]["sha256"])
    reopened = EvidenceLibrary(StateStore(lib.store.root))
    assert reopened.store.get(**claims["report"])["body"]["text"] == "As-of count from report A"
    assert reopened.record_correction(
        "correct-a", a["copy_ref"], c["copy_ref"], kind="correction",
        reason="Synthetic source A count was amended",
        mission_id="simulated-mission",
    ) == result
    assert len(reopened.store.review_status(claims["a"])) == 1
    assert len(reopened.store.events("simulated-mission")) == 1


def test_passage_scoped_correction_keeps_other_claim_from_same_copy_current(tmp_path):
    lib = library(tmp_path)
    report = publication(lib, "multi-report", "10.1234/multi-report")
    notice = lib.register_publication(
        "multi-notice", identifiers={"doi": "10.1234/multi-notice"},
        title="Correction for one simulated table", kind="correction",
        verification="Synthetic correction metadata",
    )["publication_ref"]
    text = "Simulated response count was 18. Safety table reports no evaluated adverse events."
    response_quote = "Simulated response count was 18."
    safety_quote = "Safety table reports no evaluated adverse events."
    saved = lib.add_copy(
        "multi-copy", report, encounter(lib, "multi-hit", report),
        content=text.encode(), content_sha256=None, mime_type="text/plain",
        source_version="simulated-v1", access_surface="full_text",
        retrieved_at="2026-01-02T00:00:00Z", rights_basis="Synthetic project-owned fixture",
        retain_bytes=True, reference="simulated://multi", passages=[
            {"span_id": "response", "locator": "Results, sentence 1", "text": response_quote,
             "start": 0, "end": len(response_quote)},
            {"span_id": "safety", "locator": "Results, sentence 2", "text": safety_quote,
             "start": len(response_quote) + 1, "end": len(text)},
        ],
        inspection_state="checked", validation_record="synthetic-offset-check-v1",
    )
    correction = copy(
        lib, "multi-notice-copy", notice, encounter(lib, "multi-notice-hit", notice),
        "Correction changes only the simulated response count."
    )

    def claims(tx):
        response = tx.put("claim", "response-claim", {"text": "Reported response count"})
        safety = tx.put("claim", "safety-claim", {"text": "Adverse-event reporting status"})
        return {"response": {key: response[key] for key in ("kind", "id", "version")},
                "safety": {key: safety[key] for key in ("kind", "id", "version")}}

    refs = lib.store.transact("multi-claims", {"fixture": "two passages in one copy"}, claims)
    lib.link_claim("response-support", refs["response"], saved["passage_refs"][0])
    lib.link_claim("safety-support", refs["safety"], saved["passage_refs"][1])
    lib.record_correction(
        "response-correction", saved["copy_ref"], correction["copy_ref"],
        kind="correction", reason="Synthetic response count changed",
        affected_passage_refs=[saved["passage_refs"][0]],
    )
    assert lib.store.review_status(refs["response"])
    assert lib.store.review_status(refs["safety"]) == []


def test_exact_passage_and_lexical_baseline_report_recall_and_cost(tmp_path):
    lib = library(tmp_path)
    a = publication(lib, "search-a", "10.1234/search-a")
    b = publication(lib, "search-b", "10.1234/search-b")
    first = copy(lib, "search-copy-a", a, encounter(lib, "search-hit-a", a), "Simulated depression response occurred at six weeks.")
    second = copy(lib, "search-copy-b", b, encounter(lib, "search-hit-b", b), "Simulated anxiety retention was reported at twelve weeks.")
    passage_id = first["passage_refs"][0]["id"]
    exact = lib.get_passage(passage_id)
    assert exact["passage"]["body"]["text"] == "Simulated depression response occurred at six weeks."
    assert exact["publication"]["id"] == a["id"]
    diagnostic = lib.lookup_exact("source_passage", passage_id)
    assert diagnostic["record"]["id"] == passage_id
    assert diagnostic["metadata_lookup_ms"] >= 0
    assert diagnostic["queue_ms"] is None and diagnostic["artifact_read_ms"] is None
    assert diagnostic["index_ms"] >= 0
    assert diagnostic["connection_ms"] >= 0
    assert diagnostic["decode_ms"] >= 0
    assert diagnostic["artifact_read_status"] == "not_requested"
    measured = lib.lookup_exact(
        "source_copy", first["copy_ref"]["id"],
        queued_at=time.perf_counter() - 0.01, include_content=True,
    )
    assert measured["queue_ms"] >= 10
    assert measured["index_ms"] >= 0
    assert measured["artifact_read_ms"] >= 0
    assert measured["artifact_read_status"] == "read_and_verified"
    assert measured["content_bytes"] == b"Simulated depression response occurred at six weeks."
    with pytest.raises(EvidenceError, match="monotonic"):
        lib.lookup_exact("source_copy", first["copy_ref"]["id"],
                         queued_at=time.perf_counter() + 60)
    result = lib.search_passages("depression response six weeks", limit=1)
    assert result["results"][0]["passage_ref"]["id"] == passage_id
    assert result["examined"] == 2
    assert result["elapsed_ms"] >= 0
    assert second["passage_refs"][0]["id"] != passage_id


def test_rights_limited_copy_keeps_locator_and_hash_without_bytes(tmp_path):
    lib = library(tmp_path)
    work = publication(lib, "rights-work", "10.1234/rights-work")
    hit = encounter(lib, "rights-hit", work, surface="metadata")
    result = lib.add_copy(
        "rights-copy", work, hit, content=b"Publisher content inspected locally.",
        content_sha256=None, mime_type="text/plain", source_version="publisher-v1",
        access_surface="metadata", retrieved_at="2026-01-02T00:00:00Z",
        rights_basis="Synthetic fixture marks external redistribution unverified",
        retain_bytes=False, reference="https://example.org/simulated-locator",
        passages=[],
        inspection_state="checked", validation_record="synthetic-metadata-check-v1",
    )
    body = lib.store.get(**result["copy_ref"])["body"]
    assert body["blob"] is None
    assert body["content_sha256"] is not None
    assert body["reference"].startswith("https://")
    lookup = lib.lookup_exact("source_copy", result["copy_ref"]["id"], include_content=True)
    assert lookup["artifact_read_status"] == "not_retained"
    assert lookup["artifact_read_ms"] is None
    assert "content_bytes" not in lookup
