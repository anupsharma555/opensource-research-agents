"""Navigation hints identify retained positions without widening checked evidence."""

import pytest

from ora.evidence import EvidenceError, EvidenceLibrary, IntakeService, read_source_window
from ora.state import StateStore
from tests.evidence.test_document_ocr import SCOPE, saved_public_pdf


def checked_html(tmp_path, content):
    store = StateStore(tmp_path)
    library = EvidenceLibrary(store)
    document_ref = saved_public_pdf(
        store, content, content_type="text/html", url="https://example.org/synthetic.html",
    )
    saved = IntakeService(library).inspect_public_document(
        "inspect-navigation-html", document_ref, **SCOPE,
    )
    assert saved["model_input_status"] == "permitted"
    return library, saved


def test_short_html_block_exposes_exact_section_offsets_without_reading_ahead(tmp_path):
    html = (b"<html><body><h1>Overview</h1><p>Navigation introduction.</p>"
            b"<h2>Measurements</h2><p>The synthetic value is 17 units.</p>"
            b"<h2>Limitations</h2><p>The value excludes later observations.</p>"
            b"</body></html>")
    library, saved = checked_html(tmp_path, html)
    copy_ref = saved["copy_ref"]
    first = read_source_window(library, "nav-first", copy_ref, block_limit=1, char_limit=2000)
    assert [item["text_preview"] for item in first["passages"]] == ["Overview"]
    assert first["next_cursor"] == {"block_offset": 1, "char_offset": 0}
    navigation = first["navigation"]
    assert navigation["scope"] == "navigation_only_not_checked_claim_evidence"
    assert navigation["total_blocks"] == 6
    assert [(entry["label"], entry["block_offset"]) for entry in navigation["sections"]] == [
        ("Overview", 0), ("Measurements", 2), ("Limitations", 4),
    ]
    assert len(first["passage_refs"]) == 1
    assert len(library.store.records("source_passage")) == 1
    later = read_source_window(
        library, "nav-later", copy_ref, block_offset=3, block_limit=1, char_limit=2000,
    )
    assert later["passages"][0]["text_preview"] == "The synthetic value is 17 units."
    assert later["passages"][0]["locator"].startswith("section=Measurements;")


def test_navigation_is_bounded_and_revoked_replay_returns_nothing(tmp_path):
    headings = "".join(f"<h2>Part {index}</h2><p>Value {index}.</p>" for index in range(20))
    library, saved = checked_html(tmp_path, f"<html><body>{headings}</body></html>".encode())
    copy_ref = saved["copy_ref"]
    window = read_source_window(library, "nav-bounded", copy_ref, block_limit=1)
    assert window["navigation"]["section_count"] == 20
    assert len(window["navigation"]["sections"]) == 8
    assert window["navigation"]["omitted_section_count"] == 12
    use_ref = saved["source_use_ref"]

    def revoke(tx):
        use = tx.get(**use_ref)
        tx.put("source_use", use_ref["id"],
               {**use["body"], "policy_status": "revoked", "allowed_uses": []},
               expected_version=use_ref["version"])
        return {"revoked": True}

    library.store.transact("revoke-nav", {"use_ref": use_ref}, revoke)
    with pytest.raises(EvidenceError, match="does not grant model input"):
        read_source_window(library, "nav-bounded", copy_ref, block_limit=1)
