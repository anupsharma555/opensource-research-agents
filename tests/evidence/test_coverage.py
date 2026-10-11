"""Exact saved reading spans do not overstate retained-text coverage."""

from __future__ import annotations

import hashlib

from ora.coordination.board import ref
from ora.evidence import EvidenceLibrary, IntakeService, read_source_window
from ora.evidence.coverage import extracted_text_coverage
from ora.state import StateStore


def checked_copy(tmp_path, content: bytes, *, mime="text/plain", surface="abstract",
                 selected: list[str] | None = None, truncated=False):
    store = StateStore(tmp_path / "state")
    library = EvidenceLibrary(store)
    publication = library.register_publication(
        "synthetic-publication", identifiers={"doi": "10.1234/coverage"},
        title="Synthetic source", verification="Synthetic exact DOI",
    )["publication_ref"]
    saved = IntakeService(library).inspect_response(
        "synthetic-inspection", publication, content=content, content_type=mime,
        source_url="https://example.org/synthetic", retrieved_at="2026-10-09T12:00:00Z",
        source_version="synthetic-v1", rights_status="permitted",
        rights_basis="Project-owned synthetic source", declared_surface=surface,
        expected_doi="10.1234/coverage", identity_basis="Synthetic exact DOI",
        selected_texts=selected or [], truncated=truncated,
    )
    assert saved["copy_ref"] is not None
    return library, saved


def supplied_without_quality_mapping(tmp_path, *, route="user_supplied", text=None,
                                     mime="text/plain", validation="exact-user-submission"):
    """Match the private mission's pinned submitted-text quality shape."""
    store = StateStore(tmp_path / "state")
    library = EvidenceLibrary(store)
    if text is None:
        text = "DOI: 10.1234/coverage. " + "A" * 1600 + "B" * 1600 + "C" * 1000
    content = text.encode()
    publication = library.register_publication(
        "submitted-publication", identifiers={}, title="Synthetic submitted text",
        verification="Synthetic private input",
    )["publication_ref"]
    encounter = library.record_encounter(
        "submitted-encounter", publication_ref=publication, retrieval_hit_ref=None,
        status="accepted", access_surface="passage", retrieved_at="2026-10-09T12:00:00Z",
        route=route,
    )["encounter_ref"]
    saved = library.add_copy(
        "submitted-copy", publication, encounter, content=content, content_sha256=None,
        mime_type=mime, source_version="submitted-v1", access_surface="passage",
        retrieved_at="2026-10-09T12:00:00Z", rights_basis="Synthetic private input",
        retain_bytes=True, reference="supplied:synthetic", passages=[{
            "span_id": "supplied-1", "locator": f"submitted text; chars=0:{len(text)}",
            "text": text, "start": 0, "end": len(text),
    }], inspection_state="checked", validation_record=validation,
    )
    copy_ref = saved["copy_ref"]
    digest = hashlib.sha256(content).hexdigest()

    def save_use(tx):
        quality = tx.put("intake_quality", "submitted-quality", {
            "source_sha256": digest, "classification": "passage", "truncated": False,
        })
        use = tx.put("source_use", "submitted-use", {
            "copy_ref": copy_ref, "source_sha256": digest, "quality_ref": ref(quality),
            "allowed_uses": ["model_input"], "policy_status": "granted",
        })
        tx.bind("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}",
                "source_use", use["id"])
        return {"quality_ref": ref(quality), "use_ref": ref(use)}

    store.transact("submitted-use", {}, save_use)
    return library, saved, text


def test_user_supplied_window_coverage_verifies_saved_bytes_without_quality_map(tmp_path):
    library, saved, text = supplied_without_quality_mapping(tmp_path)
    copy_ref = saved["copy_ref"]
    first = read_source_window(
        library, "supplied-first", copy_ref,
        block_offset=0, char_offset=0, block_limit=1, char_limit=1600,
    )
    later = read_source_window(
        library, "supplied-later", copy_ref,
        block_offset=0, char_offset=1600, block_limit=1, char_limit=1600,
    )
    windows = [first["window_ref"], later["window_ref"]]
    coverage = extracted_text_coverage(library.store, copy_ref, [], windows)
    assert coverage["status"] == "incomplete_extracted_text"
    assert coverage["scope"] == "retained_extracted_text_only"
    assert coverage["original_source_partial"] is True
    assert (coverage["covered_chars"], coverage["total_chars"]) == (3200, len(text))
    assert coverage["next_unread_cursor"] == {"block_offset": 0, "char_offset": 3200}
    selected_coverage = extracted_text_coverage(
        library.store, copy_ref, saved["passage_refs"], windows,
    )
    assert selected_coverage["status"] == "complete_extracted_text"
    assert selected_coverage["covered_chars"] == len(text)
    assert selected_coverage["original_source_partial"] is True

    def corrupt_window(tx):
        forged = tx.put("source_read_window", "forged-supplied-window", {
            **tx.get(**later["window_ref"])["body"], "content_sha256": "0" * 64,
        })
        return ref(forged)

    forged_ref = library.store.transact("forge-supplied-window", {}, corrupt_window)
    assert extracted_text_coverage(
        library.store, copy_ref, [], [forged_ref],
    )["status"] == "unknown"


def test_missing_quality_mapping_on_non_supplied_route_stays_unknown(tmp_path):
    library, saved, _text = supplied_without_quality_mapping(tmp_path, route="other_route")
    copy_ref = saved["copy_ref"]
    window = read_source_window(
        library, "other-route-window", copy_ref,
        block_offset=0, char_offset=0, block_limit=1, char_limit=1600,
    )
    coverage = extracted_text_coverage(
        library.store, copy_ref, [], [window["window_ref"]],
    )
    assert coverage["status"] == "unknown"
    assert coverage["reason"] == "extraction_mapping_unavailable"


def test_split_abstract_stays_incomplete_until_exact_remainder_is_read(tmp_path):
    text = "DOI: 10.1234/coverage. " + "A" * 2638
    assert len(text) == 2661
    library, saved = checked_copy(tmp_path, text.encode(), selected=[text[:1600]])
    copy_ref = saved["copy_ref"]
    before = extracted_text_coverage(library.store, copy_ref, saved["passage_refs"], [])
    assert before["status"] == "incomplete_extracted_text"
    assert before["scope"] == "retained_extracted_text_only"
    assert before["original_source_partial"] is False
    assert (before["total_chars"], before["covered_chars"], before["uncovered_chars"]) == (2661, 1600, 1061)
    assert before["next_unread_cursor"] == {"block_offset": 0, "char_offset": 1600}
    assert "A" * 100 not in str(before)

    remainder = read_source_window(
        library, "synthetic-remainder", copy_ref,
        block_offset=0, char_offset=1600, block_limit=1, char_limit=2000,
    )
    assert remainder["next_cursor"] is None
    complete = extracted_text_coverage(
        library.store, copy_ref, saved["passage_refs"], [remainder["window_ref"]],
    )
    assert complete["status"] == "complete_extracted_text"
    assert (complete["covered_chars"], complete["uncovered_chars"]) == (2661, 0)
    assert complete["next_unread_cursor"] is None
    assert complete["window_count"] == 1


def test_latest_window_without_cursor_does_not_hide_earlier_gaps(tmp_path):
    sections = "".join(
        f"<sec><title>Methods {index}</title><p>Later method {index} remains provisional.</p></sec>"
        for index in range(9)
    )
    xml = ("<article><article-id pub-id-type='doi'>10.1234/coverage</article-id>"
           + sections + "</article>").encode()
    library, saved = checked_copy(tmp_path, xml, mime="application/xml", surface="full_text")
    copy_ref = saved["copy_ref"]
    last = read_source_window(library, "synthetic-last-block", copy_ref, block_offset=9, block_limit=1)
    assert last["next_cursor"] is None
    progress = extracted_text_coverage(library.store, copy_ref, [], [last["window_ref"]])
    assert progress["status"] == "incomplete_extracted_text"
    assert progress["total_blocks"] == 10
    assert progress["next_unread_cursor"] == {"block_offset": 0, "char_offset": 0}
    assert progress["covered_chars"] < progress["total_chars"]

    first = read_source_window(library, "synthetic-first-blocks", copy_ref, block_offset=0, block_limit=3)
    middle = read_source_window(library, "synthetic-middle-blocks", copy_ref, block_offset=3, block_limit=6)
    complete = extracted_text_coverage(
        library.store, copy_ref, [],
        [last["window_ref"], first["window_ref"], middle["window_ref"]],
    )
    assert complete["status"] == "complete_extracted_text"
    assert complete["uncovered_chars"] == 0


def test_complete_retained_text_keeps_original_partial_status(tmp_path):
    text = "DOI: 10.1234/coverage. Only a partial article response was retained."
    library, saved = checked_copy(
        tmp_path, text.encode(), surface="full_text", selected=[text], truncated=True,
    )
    progress = extracted_text_coverage(
        library.store, saved["copy_ref"], saved["passage_refs"], [],
    )
    assert progress["status"] == "complete_extracted_text"
    assert progress["original_source_partial"] is True
    assert progress["source_surface"] == "partial"


def test_exact_saved_window_survives_later_source_use_version(tmp_path):
    text = "DOI: 10.1234/coverage. This retained abstract is a synthetic example."
    library, saved = checked_copy(tmp_path, text.encode())
    copy_ref = saved["copy_ref"]
    window = read_source_window(library, "synthetic-complete-window", copy_ref, block_limit=1)
    use_ref = saved["source_use_ref"]
    use = library.store.get(**use_ref)
    library.store.transact("synthetic-use-version", {}, lambda tx: {
        "new_version": tx.put("source_use", use["id"], use["body"],
                              expected_version=use["version"])["version"]
    })
    def foreign_window(tx):
        other = tx.put("source_copy", "synthetic-other-copy", {"inspection_state": "checked"})
        saved_window = tx.put("source_read_window", "synthetic-other-window", {
            "copy_ref": {"kind": other["kind"], "id": other["id"], "version": other["version"]},
        })
        return {"kind": saved_window["kind"], "id": saved_window["id"], "version": saved_window["version"]}

    other_ref = library.store.transact("synthetic-other-window", {}, foreign_window)
    progress = extracted_text_coverage(
        library.store, copy_ref, [], [other_ref, window["window_ref"]],
    )
    assert progress["status"] == "complete_extracted_text"
    assert progress["window_count"] == 1


def test_wrong_copy_and_forged_passage_are_unknown_not_complete(tmp_path):
    text = "DOI: 10.1234/coverage. The saved result is bounded."
    library, saved = checked_copy(tmp_path, text.encode(), selected=["bounded"])
    copy_ref = saved["copy_ref"]
    passage = library.store.get(**saved["passage_refs"][0])
    forged = library.store.transact("synthetic-forged-passage", {}, lambda tx: {
        "kind": "source_passage", "id": record["id"], "version": record["version"]
    } if (record := tx.put("source_passage", "synthetic-forged", {
        **passage["body"], "text": "changed",
    })) else {}, mission_id="")
    invalid = extracted_text_coverage(library.store, copy_ref, [forged], [])
    assert invalid["status"] == "unknown"
    assert invalid["covered_chars"] is None
    assert invalid["reason"] == "passage_locator_not_unique_or_exact"

    other_library, other = checked_copy(tmp_path / "other", text.encode(), selected=["bounded"])
    foreign = extracted_text_coverage(
        library.store, copy_ref, other["passage_refs"], [],
    )
    assert foreign["status"] == "unknown"
    assert foreign["reason"] == "missing_exact_record"
    assert other_library.store.get(**other["copy_ref"]) is not None


def test_unsupported_copy_and_bounded_display_do_not_write_history(tmp_path):
    text = "DOI: 10.1234/coverage. " + "B" * 9500
    library, saved = checked_copy(tmp_path, text.encode())
    copy_ref = saved["copy_ref"]
    refs = []
    for index in range(10):
        page = read_source_window(
            library, f"synthetic-alternate-{index}", copy_ref,
            block_offset=0, char_offset=index * 950, block_limit=1, char_limit=475,
        )
        refs.append(page["window_ref"])
    store = library.store
    before = (len(store.records("source_read_window")), len(store.events("synthetic-mission")))
    progress = extracted_text_coverage(store, copy_ref, [], refs)
    assert progress["status"] == "incomplete_extracted_text"
    assert progress["covered_interval_count"] == 10
    assert len(progress["covered_intervals"]) == 6
    assert progress["uncovered_interval_count"] > 6
    assert len(progress["uncovered_intervals"]) == 6
    assert (len(store.records("source_read_window")), len(store.events("synthetic-mission"))) == before

    unavailable = extracted_text_coverage(
        store, {"kind": "source_copy", "id": "missing-copy", "version": 1}, [], [],
    )
    assert unavailable["status"] == "unknown"
    assert unavailable["next_unread_cursor"] is None
