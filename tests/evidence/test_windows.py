"""Agents can request later exact text without replacing an inspected copy."""

import pytest

from ora.evidence import (
    EvidenceError,
    EvidenceLibrary,
    IntakeService,
    checked_model_text,
    read_source_window,
)
from ora.state import StateStore
from ora.state.store import StateConflict
from tests.evidence.test_source_format_walkthroughs import _pdf


def checked_copy(tmp_path, content, *, mime="application/xml", surface="full_text",
                 table_check=None):
    store = StateStore(tmp_path / "state")
    library = EvidenceLibrary(store)
    publication = library.register_publication(
        "publication", identifiers={"doi": "10.1234/window"},
        title="Synthetic source", verification="Synthetic exact DOI",
    )["publication_ref"]
    saved = IntakeService(library).inspect_response(
        "source-intake", publication, content=content, content_type=mime,
        source_url="https://example.org/source", retrieved_at="2026-10-09T12:00:00Z",
        source_version="synthetic-v1", rights_status="permitted",
        rights_basis="Project-owned synthetic source", declared_surface=surface,
        expected_doi="10.1234/window", identity_basis="Synthetic exact DOI",
        table_check=table_check,
    )
    assert saved["copy_ref"] is not None
    return library, saved


def xml_sections(count):
    sections = "".join(
        f"<sec><title>Methods {index}</title><p>Later method {index} uses dose {index} mg.</p></sec>"
        for index in range(count)
    )
    return ("<article><article-id pub-id-type='doi'>10.1234/window</article-id>"
            + sections + "</article>").encode()


def test_later_methods_are_readable_beyond_initial_three_blocks(tmp_path):
    library, saved = checked_copy(tmp_path, xml_sections(9))
    copy_ref = saved["copy_ref"]
    first = read_source_window(library, "window-first", copy_ref)
    assert first["total_blocks"] == 10  # DOI identity block plus nine method paragraphs.
    assert first["next_cursor"] == {"block_offset": 3, "char_offset": 0}
    assert library.store.get(**first["window_ref"])["body"]["passage_refs"] == first["passage_refs"]
    assert all("Later method 8" not in item["text_preview"] for item in first["passages"])
    later = read_source_window(library, "window-later", copy_ref, block_offset=9)
    assert later["source_id"] == copy_ref["id"]
    assert later["next_cursor"] is None
    assert later["passages"][0]["text_preview"] == "Later method 8 uses dose 8 mg."
    assert "section=Methods 8" in later["passages"][0]["locator"]
    packet = checked_model_text(library, copy_ref, later["passage_refs"])
    assert "Later method 8 uses dose 8 mg." in packet["text"]
    assert len(library.store.records("source_copy")) == 1


def test_long_block_cursor_continues_without_loss_and_replays_after_restart(tmp_path):
    text = "DOI: 10.1234/window. " + "A" * 10500 + " END"
    library, saved = checked_copy(tmp_path, text.encode(), mime="text/plain", surface="abstract")
    copy_ref = saved["copy_ref"]
    cursor = {"block_offset": 0, "char_offset": 0}
    slices = []
    refs = []
    index = 0
    while cursor is not None:
        page = read_source_window(library, f"window-{index}", copy_ref,
                                  block_limit=1, char_limit=1200, **cursor)
        slices.extend(item["text_preview"] for item in page["passages"])
        refs.extend(page["passage_refs"])
        cursor = page["next_cursor"]
        index += 1
        assert index < 20
    assert "".join(slices) == text
    assert len(refs) == index and index > 3
    reopened = EvidenceLibrary(StateStore(library.store.root))
    assert checked_model_text(reopened, copy_ref, refs)["content_sha256"] == (
        library.store.get(**copy_ref)["body"]["content_sha256"]
    )
    assert reopened.store.get(**saved["copy_ref"])["body"]["blob"] is not None


def test_repeated_words_keep_distinct_exact_locators_and_idempotent_replay(tmp_path):
    xml = (b"<article><article-id pub-id-type='doi'>10.1234/window</article-id>"
           b"<sec><title>Methods</title><p>Same repeated sentence.</p>"
           b"<p>Same repeated sentence.</p></sec></article>")
    library, saved = checked_copy(tmp_path, xml)
    copy_ref = saved["copy_ref"]
    page = read_source_window(library, "repeat-window", copy_ref, block_offset=1, block_limit=2)
    assert [item["text_preview"] for item in page["passages"]] == [
        "Same repeated sentence.", "Same repeated sentence."
    ]
    assert page["passage_refs"][0] != page["passage_refs"][1]
    assert page["passages"][0]["locator"] != page["passages"][1]["locator"]
    assert read_source_window(library, "repeat-window", copy_ref,
                              block_offset=1, block_limit=2) == page
    second = read_source_window(library, "repeat-window-second", copy_ref,
                                block_offset=1, block_limit=2)
    assert second["passage_refs"] == page["passage_refs"]
    assert len(library.store.records("source_copy")) == 1
    assert len(library.store.records("source_passage")) == 2
    with pytest.raises(StateConflict):
        read_source_window(library, "repeat-window", copy_ref, block_offset=2)


def test_revoked_source_use_rejects_further_read_and_old_passage_model_use(tmp_path):
    library, saved = checked_copy(tmp_path, xml_sections(2))
    copy_ref = saved["copy_ref"]
    page = read_source_window(library, "allowed-window", copy_ref)
    use_ref = saved["source_use_ref"]

    def revoke(tx):
        use = tx.get(**use_ref)
        tx.put("source_use", use_ref["id"],
               {**use["body"], "allowed_uses": ["discovery"], "policy_status": "revoked"},
               expected_version=use_ref["version"])
        return {"revoked": True}

    library.store.transact("revoke-window-use", {"use": use_ref}, revoke)
    with pytest.raises(EvidenceError, match="does not grant model input"):
        read_source_window(library, "new-window", copy_ref, block_offset=1)
    with pytest.raises(EvidenceError, match="does not grant model input"):
        read_source_window(library, "allowed-window", copy_ref)
    with pytest.raises(EvidenceError, match="does not grant model input"):
        checked_model_text(library, copy_ref, page["passage_refs"])
    assert len(library.store.records("source_read_window")) == 1


def test_policy_change_at_transaction_or_before_return_is_detected(tmp_path, monkeypatch):
    library, saved = checked_copy(tmp_path, xml_sections(2))
    copy_ref = saved["copy_ref"]
    store = library.store
    original = store.transact
    use_ref = saved["source_use_ref"]

    def change_policy(operation_id):
        def change(tx):
            use = tx.get("source_use", use_ref["id"])
            tx.put("source_use", use_ref["id"], {**use["body"], "policy_status": "granted"},
                   expected_version=use["version"])
            return {"changed": True}
        original(operation_id, {"test": operation_id}, change)

    def before_transaction(operation_id, request, action, **kwargs):
        if operation_id == "window-change-at-tx":
            change_policy("switch-before-tx")
        return original(operation_id, request, action, **kwargs)

    monkeypatch.setattr(store, "transact", before_transaction)
    with pytest.raises(EvidenceError, match="changed during window construction"):
        read_source_window(library, "window-change-at-tx", copy_ref)
    assert store.records("source_read_window") == []

    def after_transaction(operation_id, request, action, **kwargs):
        result = original(operation_id, request, action, **kwargs)
        if operation_id == "window-change-before-return":
            change_policy("switch-after-tx")
        return result

    monkeypatch.setattr(store, "transact", after_transaction)
    with pytest.raises(EvidenceError, match="changed before window return"):
        read_source_window(library, "window-change-before-return", copy_ref)
    assert len(store.records("source_read_window")) == 1


def test_pdf_window_reports_page_and_table_context_without_claiming_cell_validity(
    tmp_path,
):
    table_check = {"page": 1, "row": "Group", "header": "Group n=40", "units": "mg",
                   "footnote": "none present", "rendered_original_sha256": "a" * 64,
                   "reviewer_basis": "Synthetic rendered-page fixture"}
    pdf = _pdf([
        ["DOI: 10.1234/window.", "Results", "Table 1.", "Group n=40; value -3 mg."],
        ["Discussion", "Later interpretation."],
    ])
    library, saved = checked_copy(tmp_path, pdf, mime="application/pdf",
                                  table_check=table_check)
    page = read_source_window(library, "pdf-window", saved["copy_ref"], block_limit=1)
    assert "page=1" in page["passages"][0]["locator"]
    assert page["table_context"]["rendered_original_check"] == table_check
    assert "does not validate a table cell" in page["table_context"]["caution"]
    later = read_source_window(library, "pdf-later-window", saved["copy_ref"],
                               block_offset=1, block_limit=1)
    assert "page=2" in later["passages"][0]["locator"]
    assert later["table_context"]["rendered_original_check"] is None
    assert later["table_context"]["reviewed_page_elsewhere"] == 1


def test_pdf_progress_uses_physical_page_locators_across_blank_page(tmp_path):
    pdf = _pdf([
        ["DOI: 10.1234/window. First page text."], None,
        ["Third page text."],
    ])
    library, saved = checked_copy(tmp_path, pdf,
                                  mime="application/pdf", surface="abstract")
    first = read_source_window(library, "pdf-progress-first", saved["copy_ref"],
                               block_limit=1, char_limit=12)
    assert "PDF page 1" in first["reading_progress"]
    assert "block 1/2 chars 0:12" in first["reading_progress"]
    assert "More retained text" in first["reading_progress"]
    assert first["next_cursor"] == {"block_offset": 0, "char_offset": 12}
    later = read_source_window(library, "pdf-progress-third", saved["copy_ref"],
                               block_offset=1, block_limit=1)
    assert "PDF page 3" in later["reading_progress"]
    assert "block 2/2" in later["reading_progress"]
    assert "total pages unknown" in later["reading_progress"]
    assert "whole source reading unverified" in later["reading_progress"]
    assert later["next_cursor"] is None


def test_xml_progress_names_extracted_blocks_not_pdf_pages(tmp_path):
    library, saved = checked_copy(tmp_path, xml_sections(2))
    window = read_source_window(library, "xml-progress", saved["copy_ref"],
                                block_limit=1, char_limit=15)
    assert window["reading_progress"].startswith("Extracted block 1/")
    assert "see section locators" in window["reading_progress"]
    assert "PDF page" not in window["reading_progress"]
    assert window["next_cursor"] == {"block_offset": 0, "char_offset": 15}
