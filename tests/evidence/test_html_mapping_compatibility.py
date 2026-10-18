"""Retained HTML citations replay with the parser that created their coordinates."""

from __future__ import annotations

import pytest

from ora.evidence import EvidenceLibrary, IntakeService, intake, read_source_window
from ora.evidence.coverage import extracted_text_coverage
from ora.evidence.library import EvidenceError
from ora.mission import passage_search
from ora.mission.context import WorkingContext
from ora.mission.projection import _source
from ora.state import StateStore
from tests.evidence.test_document_ocr import SCOPE, saved_public_pdf

HTML = (
    b"<html><body><div role='navigation'><p>Menu: home and help.</p></div>"
    b"<main><h1>Synthetic outcome</h1><p>The intervention improved recall.</p>"
    b"<table><caption>Recall score</caption><tr><th>Group</th><th>Score</th></tr>"
    b"<tr><td>Intervention</td><td>24</td></tr></table></main></body></html>"
)


def _inspected(tmp_path, operation_id: str):
    store = StateStore(tmp_path)
    library = EvidenceLibrary(store)
    document_ref = saved_public_pdf(
        store, HTML, content_type="text/html", url="https://example.org/synthetic-html"
    )
    result = IntakeService(library).inspect_public_document(operation_id, document_ref, **SCOPE)
    assert result["model_input_status"] == "permitted"
    return store, library, result


def test_legacy_html_window_model_projection_and_coverage_keep_original_mapping(
    tmp_path, monkeypatch,
):
    # Simulate an inspection saved before the structured HTML parser existed.
    original_extract = intake.extract_blocks

    def legacy_extract(content, content_type, *, allow_partial_xml=False):
        return original_extract(
            content, content_type, allow_partial_xml=allow_partial_xml,
            html_parser_version="stdlib-htmlparser",
        )

    with monkeypatch.context() as patch:
        patch.setattr(intake, "extract_blocks", legacy_extract)
        store, library, inspected = _inspected(tmp_path / "old", "inspect-legacy-html")

    copy_ref = inspected["copy_ref"]
    copy_body = store.get(**copy_ref)["body"]
    assert ":stdlib-htmlparser" in copy_body["mapping_version"]
    legacy_blocks, legacy_parser = intake.extract_copy_blocks(HTML, copy_body)
    assert legacy_parser == "stdlib-htmlparser"
    assert legacy_blocks[0].text == "Menu: home and help."
    assert intake.extract_copy_blocks(HTML, {**copy_body, "mapping_version": None}) == (
        legacy_blocks, legacy_parser,
    )

    window = read_source_window(
        library, "read-legacy-html", copy_ref, block_limit=10, char_limit=4000,
    )
    assert window["passages"][0]["text_preview"] == legacy_blocks[0].text
    assert window["passages"][0]["locator"].startswith(legacy_blocks[0].locator)
    selected = window["passage_refs"]
    assert IntakeService(library).source_for_model(selected[0])["text"] == legacy_blocks[0].text
    assert extracted_text_coverage(
        store, copy_ref, selected, [window["window_ref"]],
    )["status"] == "complete_extracted_text"
    projected, _provenance = _source(store, copy_ref, [window["window_ref"]])
    assert projected["passages"][0]["text"] == legacy_blocks[0].text

    def save_control(tx):
        record = tx.put("mission_task_control", "legacy-html-search-control", {
            **SCOPE, "phase": "ready_model", "source_refs": [copy_ref],
            "selected_passage_refs": {copy_ref["id"]: selected},
            "read_window_refs": [window["window_ref"]],
        })
        return {key: record[key] for key in ("kind", "id", "version")}

    control_ref = store.transact(
        "legacy-html-search-control", {}, save_control, mission_id=SCOPE["mission_id"],
    )
    search = passage_search.search_saved_passages(
        WorkingContext(store), store.get(**control_ref), "Menu",
    )
    assert search["results"][0]["block_locator"] == legacy_blocks[0].locator

    use = store.get(**inspected["source_use_ref"])
    store.transact("revoke-legacy-html", {}, lambda tx: tx.put(
        "source_use", use["id"], {
            **use["body"], "allowed_uses": [], "policy_status": "revoked",
        }, expected_version=use["version"]), mission_id=SCOPE["mission_id"])
    with pytest.raises(EvidenceError, match="does not grant model input"):
        read_source_window(library, "read-revoked-html", copy_ref)
    with pytest.raises(EvidenceError, match="does not grant model input"):
        IntakeService(library).source_for_model(selected[0])


def test_new_html_copy_omits_menu_and_keeps_table_labels(tmp_path):
    store, library, inspected = _inspected(tmp_path / "new", "inspect-structured-html")
    copy_ref = inspected["copy_ref"]
    body = store.get(**copy_ref)["body"]
    assert intake.HTML_PARSER_VERSION in body["mapping_version"]
    blocks, parser = intake.extract_copy_blocks(HTML, body)
    assert parser == intake.HTML_PARSER_VERSION
    joined = " ".join(block.text for block in blocks)
    assert "Menu: home and help" not in joined
    assert "Recall score" in joined
    assert "Group" in joined and "Score" in joined
    assert "Intervention" in joined and "24" in joined
    assert any("The intervention improved recall." in block.text for block in blocks)
    window = read_source_window(
        library, "read-structured-html", copy_ref, block_limit=10, char_limit=4000,
    )
    assert all("Menu: home and help" not in item["text_preview"] for item in window["passages"])
    assert extracted_text_coverage(
        store, copy_ref, window["passage_refs"], [window["window_ref"]],
    )["status"] == "complete_extracted_text"


def test_v2_copy_keeps_coordinates_after_v3_preserves_preformatted_text(tmp_path, monkeypatch):
    content = b"<main><h1>Code</h1><pre>// Example\nconst value = 24;\n</pre></main>"
    original_extract = intake.extract_blocks

    def v2_extract(raw, mime, **kwargs):
        return original_extract(raw, mime, **{
            **kwargs, "html_parser_version": intake.HTML_V2_PARSER_VERSION,
        })

    store = StateStore(tmp_path / "v2")
    library = EvidenceLibrary(store)
    document = saved_public_pdf(store, content, content_type="text/html")
    with monkeypatch.context() as patch:
        patch.setattr(intake, "extract_blocks", v2_extract)
        old = IntakeService(library).inspect_public_document("inspect-v2", document, **SCOPE)
    old_body = store.get(**old["copy_ref"])["body"]
    assert old_body["mapping_version"].endswith(intake.HTML_V2_PARSER_VERSION)
    blocks, version = intake.extract_copy_blocks(content, old_body)
    assert version == intake.HTML_V2_PARSER_VERSION
    old_window = read_source_window(
        library, "read-v2", old["copy_ref"], block_offset=1, block_limit=1,
    )
    old_text = IntakeService(library).source_for_model(old_window["passage_refs"][0])["text"]
    assert old_text == blocks[1].text and "\n" not in old_text

    new_store = StateStore(tmp_path / "v3")
    new_library = EvidenceLibrary(new_store)
    new_document = saved_public_pdf(new_store, content, content_type="text/html")
    new = IntakeService(new_library).inspect_public_document("inspect-v3", new_document, **SCOPE)
    new_window = read_source_window(
        new_library, "read-v3", new["copy_ref"], block_offset=1, block_limit=1,
    )
    new_text = IntakeService(new_library).source_for_model(new_window["passage_refs"][0])["text"]
    assert new_text == "// Example\nconst value = 24;\n"
    assert new_store.get(**new["copy_ref"])["body"]["mapping_version"].endswith(
        intake.HTML_PARSER_VERSION,
    )


def test_v3_description_list_copy_replays_separate_legacy_blocks():
    content = (
        b"<main><dl><dt>Adjusted risk</dt>"
        b"<dd>Risk after age adjustment.</dd></dl></main>"
    )
    v3_blocks, v3_version = intake.extract_blocks(
        content, "text/html", html_parser_version=intake.HTML_V3_PARSER_VERSION,
    )
    v4_blocks, v4_version = intake.extract_blocks(content, "text/html")
    assert v3_version == "stdlib-htmlparser-structured-v3"
    assert [block.text for block in v3_blocks] == [
        "Adjusted risk", "Risk after age adjustment.",
    ]
    assert v4_version == intake.HTML_PARSER_VERSION
    assert len(v4_blocks) == 1 and "Adjusted risk" in v4_blocks[0].text
    retained = {
        "mime_type": "text/html",
        "mapping_version": f"{intake.PARSER_VERSION}:{v3_version}",
    }
    assert intake.extract_copy_blocks(content, retained) == (v3_blocks, v3_version)
