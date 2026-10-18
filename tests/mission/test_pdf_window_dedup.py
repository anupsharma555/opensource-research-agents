"""Legacy public-document length bookkeeping must not duplicate selected text."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from ora.coordination.board import ref
from ora.evidence import EvidenceLibrary, IntakeService, read_source_window
from ora.evidence.coverage import extracted_text_coverage
from ora.evidence.intake import checked_model_text, extract_blocks
from ora.mission.context import WorkingContext
from ora.state import StateStore
from ora.state.store import canonical
from tests.evidence.test_document_ocr import SCOPE, saved_public_pdf


def two_page_pdf() -> bytes:
    """Small local fixture with one extractable text block on each page."""
    streams = [
        b"BT /F1 26 Tf 60 710 Td (Synthetic first-page detail) Tj ET\n",
        b"BT /F1 26 Tf 60 710 Td (Synthetic second-page detail) Tj ET\n",
    ]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        *[b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
          + stream + b"endstream" for stream in streams],
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    start = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
                  f"startxref\n{start}\n%%EOF\n".encode())
    return bytes(output)


def public_window(tmp_path, content: bytes, mime: str, *, legacy: bool):
    store = StateStore(tmp_path)
    library = EvidenceLibrary(store)
    document_ref = saved_public_pdf(store, content, content_type=mime)
    inspected = IntakeService(library).inspect_public_document(
        "inspect-public", document_ref, **SCOPE,
    )
    assert inspected["model_input_status"] == "permitted"
    quality = store.get(**inspected["quality_ref"])
    blocks, parser = extract_blocks(content, mime)
    assert len(blocks) == 2 and quality["body"]["parser"] == parser
    assert quality["body"]["inspected_text_length"] == sum(len(b.text) for b in blocks)
    if legacy:
        use = store.get(**inspected["source_use_ref"])

        def old_joined_length(tx):
            old_body = {**quality["body"],
                        "inspected_text_length": len("\n".join(b.text for b in blocks))}
            old_body.pop("inspected_text_length_basis")
            old_quality = tx.put("intake_quality", "legacy-public-quality", old_body)
            tx.put("source_use", use["id"], {
                **use["body"], "quality_ref": ref(old_quality),
            }, expected_version=use["version"])
            return ref(old_quality)

        store.transact("legacy-public-quality", {}, old_joined_length,
                       mission_id=SCOPE["mission_id"])
    window = read_source_window(
        library, "read-first-public-block", inspected["copy_ref"],
        block_limit=1, char_limit=4000,
    )
    return store, library, inspected, window, blocks


@pytest.mark.parametrize("mime,content", [
    ("application/pdf", two_page_pdf()),
    ("text/html", b"<html><body><p>Synthetic first block.</p>"
                  b"<p>Synthetic second block.</p></body></html>"),
])
def test_exact_legacy_joined_length_preserves_public_window_coverage(
    tmp_path, mime, content,
):
    store, _library, inspected, window, blocks = public_window(
        tmp_path, content, mime, legacy=True,
    )
    coverage = extracted_text_coverage(
        store, inspected["copy_ref"], window["passage_refs"], [window["window_ref"]],
    )
    assert coverage["status"] == "incomplete_extracted_text"
    assert coverage["total_blocks"] == len(blocks) == 2
    assert coverage["covered_chars"] == len(blocks[0].text)
    assert coverage["next_unread_cursor"] is not None


def test_new_public_pdf_quality_uses_exact_block_sum(tmp_path):
    store, _library, inspected, window, blocks = public_window(
        tmp_path, two_page_pdf(), "application/pdf", legacy=False,
    )
    quality = store.get(**inspected["quality_ref"])["body"]
    assert quality["inspected_text_length_basis"] == "sum_of_extracted_blocks_v1"
    assert quality["inspected_text_length"] == sum(len(b.text) for b in blocks)
    assert extracted_text_coverage(
        store, inspected["copy_ref"], window["passage_refs"], [window["window_ref"]],
    )["status"] == "incomplete_extracted_text"


@pytest.mark.parametrize("change,expected_reason", [
    ("length", "extraction_mapping_unavailable"),
    ("hash", "source_quality_mismatch"),
    ("basis", "extraction_mapping_unavailable"),
])
def test_legacy_mapping_rejects_corrupt_quality(tmp_path, change, expected_reason):
    store, _library, inspected, window, _blocks = public_window(
        tmp_path, two_page_pdf(), "application/pdf", legacy=True,
    )
    copy_ref = inspected["copy_ref"]
    use = store.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
    quality = store.get(**use["body"]["quality_ref"])

    def corrupt(tx):
        changed = dict(quality["body"])
        if change == "length":
            changed["inspected_text_length"] += 1
        elif change == "hash":
            changed["source_sha256"] = "0" * 64
        else:
            changed["inspected_text_length_basis"] = "unsupported_basis"
        bad = tx.put("intake_quality", "corrupt-public-quality", changed)
        tx.put("source_use", use["id"], {
            **use["body"], "quality_ref": ref(bad),
        }, expected_version=use["version"])
        return ref(bad)

    store.transact("corrupt-public-quality", {}, corrupt,
                   mission_id=SCOPE["mission_id"])
    coverage = extracted_text_coverage(
        store, copy_ref, window["passage_refs"], [window["window_ref"]],
    )
    assert coverage["status"] == "unknown"
    assert coverage["reason"] == expected_reason


def test_legacy_pdf_preview_compacts_only_under_exact_selection_and_current_use(tmp_path):
    store, library, inspected, window, _blocks = public_window(
        tmp_path, two_page_pdf(), "application/pdf", legacy=True,
    )
    copy_ref = inspected["copy_ref"]
    passage_ref = window["passage_refs"][0]
    passage = store.get(**passage_ref)
    source_packet = checked_model_text(library, copy_ref, [passage_ref])["text"]
    original_text = window["passages"][0]["text_preview"]
    assert original_text in source_packet
    result = {**window, "passages": [{
        **window["passages"][0], "span_id": passage["body"]["span_id"],
    }], "model_selection": {"mode": "replace", "passage_refs": [passage_ref]},
        "next_request": {"source_id": copy_ref["id"],
                         "source_version": copy_ref["version"],
                         **(window["next_cursor"] or {}), "selection_mode": "replace"}}
    response_blob = store.put_blob(canonical({
        "content": None, "tool_calls": [{"id": "pdf-window-call", "type": "function",
        "function": {"name": "read_source_window", "arguments": "{}"}}],
    }).encode())

    def save(tx):
        call = tx.put("agent_call", "pdf-next-call", {
            "mission_id": SCOPE["mission_id"], "instance_id": SCOPE["actor_id"],
            "task_id": SCOPE["task_id"], "input_refs": [copy_ref],
        })
        control = tx.put("mission_task_control", "pdf-control", {
            **SCOPE, "selected_passage_refs": {copy_ref["id"]: [passage_ref]},
            "source_refs": [copy_ref], "read_window_refs": [window["window_ref"]],
        })
        response = tx.put("model_response_receipt", "pdf-window-response", {
            **SCOPE, "call_ref": ref(call), "status": "tool_requested",
            "blob": response_blob,
        })
        return {"call_ref": ref(call), "control_ref": ref(control),
                "response_ref": ref(response)}

    saved = store.transact(
        "save-pdf-window-turn", {}, save, mission_id=SCOPE["mission_id"],
    )
    call_ref, control_ref, response_ref = (
        saved["call_ref"], saved["control_ref"], saved["response_ref"]
    )
    context = WorkingContext(store)
    tool_ref = context.save_tool_result(
        "save-pdf-window-result", response_ref=response_ref,
        tool_call_id="pdf-window-call", name="read_source_window", content=canonical(result),
    )
    exchange, _ = context._tool_exchange(response_ref)
    compacted = context._compact_selected_window_preview(
        response_ref, exchange[-1], call=store.get(**call_ref),
        control=store.get(**control_ref), selected_source_refs=(copy_ref,),
    )
    assert compacted is not None
    projected = json.loads(compacted[0]["content"])
    assert projected["saved_tool_result_ref"] == tool_ref
    assert projected["passages"][0]["passage_ref"] == passage_ref
    assert projected["next_request"] == result["next_request"]
    assert projected["table_context"] == result["table_context"]
    assert projected["reading_progress"] == result["reading_progress"]
    assert "PDF page 1" in projected["reading_progress"]
    assert "More retained text" in projected["reading_progress"]
    assert original_text not in compacted[0]["content"]
    assert source_packet.count(original_text) + compacted[0]["content"].count(
        original_text,
    ) == 1

    control = store.get(**control_ref)
    revoked = store.transact("pdf-unselect", {}, lambda tx: ref(tx.put(
        "mission_task_control", control["id"], {
            **control["body"], "selected_passage_refs": {copy_ref["id"]: []},
        }, expected_version=control["version"])), mission_id=SCOPE["mission_id"])
    assert context._compact_selected_window_preview(
        response_ref, exchange[-1], call=store.get(**call_ref),
        control=store.get(**revoked), selected_source_refs=(copy_ref,),
    ) is None
    use = store.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
    store.transact("pdf-revoke-use", {}, lambda tx: tx.put(
        "source_use", use["id"], {**use["body"], "allowed_uses": [],
                                     "policy_status": "revoked"},
        expected_version=use["version"]), mission_id=SCOPE["mission_id"])
    assert context._compact_selected_window_preview(
        response_ref, exchange[-1], call=store.get(**call_ref),
        control=store.get(**control_ref), selected_source_refs=(copy_ref,),
    ) is None


def test_selected_source_recap_pages_only_recoverable_web_lead_cards(tmp_path):
    store = StateStore(tmp_path)

    def save(tx):
        response = tx.put("model_response_receipt", "saved-search-response", {
            **SCOPE, "status": "tool_requested",
        })
        result = tx.put("context_tool_result", "saved-search-result", {
            **SCOPE, "response_ref": ref(response), "name": "read_search_results",
        })
        return {"response_ref": ref(response), "result_ref": ref(result)}

    refs = store.transact("saved-search-recap", {}, save,
                          mission_id=SCOPE["mission_id"])
    navigation = {
        **refs, "operation_id": "synthetic-operation", "status": "completed",
        "continuation_cursor": 1, "candidate_count": 2,
        "first_candidates": [{"hit_id": "first", "title_preview": "Synthetic lead one"},
                             {"hit_id": "second", "title_preview": "Synthetic lead two"}],
        "detail_request": {"tool": "read_tool_result",
                           "result_id": refs["result_ref"]["id"],
                           "offset": 0, "limit": 1000, "projection_sha256": ""},
        "next_request": {"tool": "read_search_results",
                         "operation_id": "synthetic-operation", "cursor": 1},
        "result_status": "saved_discovery_leads_not_checked_source_evidence",
    }
    index = {"latest_web_search_navigation": deepcopy(navigation)}
    assert WorkingContext(store)._defer_selected_source_web_leads(index)
    compact = index["latest_web_search_navigation"]
    assert "first_candidates" not in compact
    assert compact["first_candidates_deferred_for_selected_source"] == 2
    for key in ("operation_id", "response_ref", "result_ref", "detail_request",
                "next_request", "status", "result_status", "continuation_cursor"):
        assert compact[key] == navigation[key]
    assert store.get(**refs["result_ref"])["body"]["response_ref"] == refs["response_ref"]

    bad = {"latest_web_search_navigation": deepcopy(navigation)}
    bad["latest_web_search_navigation"]["detail_request"]["result_id"] = "foreign-result"
    assert not WorkingContext(store)._defer_selected_source_web_leads(bad)
    assert bad["latest_web_search_navigation"]["first_candidates"] == navigation[
        "first_candidates"
    ]
