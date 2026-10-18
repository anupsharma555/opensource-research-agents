"""Exact public PDF, XML, text and image inspection use the same source gate."""

from __future__ import annotations

import hashlib

import pytest

from ora.evidence.document_ocr import (
    current_document_page_use,
    ocr_public_document_page,
    read_public_document_ocr,
    render_public_document_page,
)
from ora.evidence.intake import IntakeService, extract_blocks
from ora.evidence.library import EvidenceError, EvidenceLibrary
from ora.evidence.windows import read_source_window
from ora.state import StateStore
from tests.evidence.test_document_ocr import SCOPE, saved_public_pdf, synthetic_pdf


@pytest.mark.parametrize("mime,content,expected_locator", [
    ("application/pdf", synthetic_pdf("Synthetic results are on page one."), "page=1"),
    ("application/jats+xml", (
        b"<?xml version='1.0'?><article><body><sec><title>Results</title>"
        b"<p>Synthetic XML results are available.</p></sec></body></article>"
    ), "section=Results"),
    ("text/plain", b"Synthetic plain text result.\n", "text; chars=0:"),
])
def test_public_nonhtml_text_keeps_original_bytes_and_locators(
    tmp_path, mime, content, expected_locator,
):
    store = StateStore(tmp_path)
    document_ref = saved_public_pdf(store, content, content_type=mime)
    intake = IntakeService(EvidenceLibrary(store))
    result = intake.inspect_public_document(
        "inspect-format", document_ref, **SCOPE,
    )
    assert result["classification"] == "web_document"
    assert result["model_input_status"] == "permitted"
    assert result["copy_ref"] and result["source_use_ref"]
    copy = store.get(**result["copy_ref"])
    assert store.read_blob(copy["body"]["blob"]["sha256"]) == content
    assert copy["body"]["content_sha256"] == hashlib.sha256(content).hexdigest()
    quality = store.get(**result["quality_ref"])["body"]
    assert quality["source_sha256"] == hashlib.sha256(content).hexdigest()
    assert quality["text_inspection_incomplete"] is False
    blocks, _ = extract_blocks(content, quality["detected_content_type"])
    assert any(expected_locator in block.locator for block in blocks)


def test_scanned_pdf_gets_bounded_image_grant_and_ocr_not_checked_passage(tmp_path):
    store = StateStore(tmp_path)
    document_ref = saved_public_pdf(store, synthetic_pdf(""))
    intake = IntakeService(EvidenceLibrary(store))
    result = intake.inspect_public_document("inspect-scanned", document_ref, **SCOPE)
    assert result["classification"] == "image_document"
    assert result["copy_ref"] and result["source_use_ref"]
    quality = store.get(**result["quality_ref"])["body"]
    assert quality["text_inspection_incomplete"] is True
    assert quality["inspected_text_length"] == 0
    assert store.records("source_passage") == []
    with pytest.raises(EvidenceError, match="read_document_ocr for a selected page"):
        read_source_window(EvidenceLibrary(store), "image-window", result["copy_ref"])
    rendered = render_public_document_page(
        store, document_ref, **SCOPE, page_number=1, operation_id="render-scanned",
    )
    assert rendered["model_input_status"] == "permitted"
    ocr = ocr_public_document_page(
        store, rendered["page_image_ref"], **SCOPE, operation_id="ocr-scanned",
    )
    assert ocr["status"] == "ocr_unverified"
    assert ocr["model_input_status"] == "permitted"
    assert read_public_document_ocr(store, ocr["ocr_ref"], **SCOPE)["status"] == "ocr_unverified"
    assert store.records("source_passage") == []


def test_raster_only_public_page_delivers_unverified_ocr_without_checked_passage(tmp_path):
    pdf_store = StateStore(tmp_path / "raster-fixture")
    pdf_ref = saved_public_pdf(pdf_store, synthetic_pdf("Synthetic raster words"))
    rendered = render_public_document_page(
        pdf_store, pdf_ref, **SCOPE, page_number=1, operation_id="render-raster-fixture",
    )
    image_record = pdf_store.get(**rendered["page_image_ref"])
    png = pdf_store.read_blob(image_record["body"]["blob"]["sha256"])

    store = StateStore(tmp_path / "image-document")
    document_ref = saved_public_pdf(
        store, png, content_type="image/png", url="https://example.org/synthetic-page.png",
    )
    inspected = IntakeService(EvidenceLibrary(store)).inspect_public_document(
        "inspect-raster-only", document_ref, **SCOPE,
    )
    assert inspected["classification"] == "image_document"
    assert store.get(**inspected["quality_ref"])["body"]["inspected_text_length"] == 0
    assert inspected["model_input_status"] == "permitted"
    page = render_public_document_page(
        store, document_ref, **SCOPE, page_number=1, operation_id="render-raster-only",
    )
    ocr = ocr_public_document_page(
        store, page["page_image_ref"], **SCOPE, operation_id="ocr-raster-only",
    )
    assert ocr["status"] == "ocr_unverified"
    assert "Synthetic raster words" in ocr["text"]
    assert store.records("source_passage") == []


def test_parser_failure_and_unsupported_binary_never_gain_source_use(tmp_path):
    store = StateStore(tmp_path / "malformed")
    document_ref = saved_public_pdf(store, b"%PDF-1.4\nnot a complete PDF")
    result = IntakeService(EvidenceLibrary(store)).inspect_public_document(
        "inspect-malformed", document_ref, **SCOPE,
    )
    assert result["classification"] == "parser_failure"
    assert result["copy_ref"] is None and result["source_use_ref"] is None
    assert store.get(**result["quality_ref"])["body"]["classification"] == "parser_failure"

    other = StateStore(tmp_path / "binary")
    binary_ref = saved_public_pdf(other, b"\x00\x01synthetic-binary", content_type="application/octet-stream")
    with pytest.raises(EvidenceError, match="Unsupported public document"):
        IntakeService(EvidenceLibrary(other)).inspect_public_document(
            "inspect-binary", binary_ref, **SCOPE,
        )
    assert other.records("source_copy") == []


def test_restricted_lead_revoked_use_and_ocr_prohibition_block_delivery(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    document_ref = saved_public_pdf(store, synthetic_pdf(""))
    document = store.get(**document_ref)
    hit = store.get(**document["body"]["hit_ref"])
    store.transact("restrict-synthetic-hit", {}, lambda tx: tx.put(
        "retrieval_hit", hit["id"], {
            **hit["body"], "source": {**hit["body"]["source"],
                                       "rights_status": "restricted"},
        }, expected_version=hit["version"]), mission_id=SCOPE["mission_id"])
    with pytest.raises(EvidenceError, match="exact browser lead"):
        IntakeService(EvidenceLibrary(store)).inspect_public_document(
            "inspect-restricted", document_ref, **SCOPE,
        )
    with pytest.raises(EvidenceError, match="permitted original lead"):
        render_public_document_page(
            store, document_ref, **SCOPE, page_number=1, operation_id="restricted-hit",
        )

    permitted_store = StateStore(tmp_path / "permitted")
    permitted_ref = saved_public_pdf(permitted_store, synthetic_pdf(""))
    intake = IntakeService(EvidenceLibrary(permitted_store))
    inspected = intake.inspect_public_document("inspect-for-revocation", permitted_ref, **SCOPE)
    page = render_public_document_page(
        permitted_store, permitted_ref, **SCOPE, page_number=1,
        operation_id="render-permitted",
    )
    from subprocess import CompletedProcess

    from ora.evidence import document_ocr

    original_run = document_ocr._run
    def prohibited_ocr(argv, **kwargs):
        if argv[0] == "tesseract":
            return CompletedProcess(argv, 0, b"No AI model input is prohibited on this page.", b"")
        return original_run(argv, **kwargs)
    monkeypatch.setattr(document_ocr, "_run", prohibited_ocr)
    withheld = ocr_public_document_page(
        permitted_store, page["page_image_ref"], **SCOPE,
        operation_id="ocr-prohibited",
    )
    assert withheld["status"] == "ocr_restricted"
    assert withheld["model_input_status"] == "restricted"
    assert "text" not in withheld
    assert "text" not in read_public_document_ocr(
        permitted_store, withheld["ocr_ref"], **SCOPE,
    )
    use = permitted_store.get(**inspected["source_use_ref"])
    permitted_store.transact("revoke-public-use", {}, lambda tx: tx.put(
        "source_use", use["id"], {**use["body"], "allowed_uses": [],
                                     "policy_status": "revoked"},
        expected_version=use["version"]), mission_id=SCOPE["mission_id"])
    with pytest.raises(EvidenceError, match="permitted original lead|not permitted"):
        current_document_page_use(permitted_store, permitted_ref, **SCOPE)
    with pytest.raises(EvidenceError, match="permitted original lead|not permitted"):
        read_public_document_ocr(permitted_store, withheld["ocr_ref"], **SCOPE)


def test_hit_revoked_between_inspection_and_source_use_commit(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    document_ref = saved_public_pdf(store, synthetic_pdf(""))
    document = store.get(**document_ref)
    hit = store.get(**document["body"]["hit_ref"])
    original_transact = store.transact

    def revoke_before_use(operation_id, request, callback, **kwargs):
        if operation_id == "inspect-race-use":
            original_transact("revoke-before-use", {}, lambda tx: tx.put(
                "retrieval_hit", hit["id"], {
                    **hit["body"], "source": {
                        **hit["body"]["source"], "rights_status": "restricted",
                    },
                }, expected_version=hit["version"]), mission_id=SCOPE["mission_id"])
        return original_transact(operation_id, request, callback, **kwargs)

    monkeypatch.setattr(store, "transact", revoke_before_use)
    with pytest.raises(EvidenceError, match="origin or copy changed"):
        IntakeService(EvidenceLibrary(store)).inspect_public_document(
            "inspect-race", document_ref, **SCOPE,
        )
    assert store.records("source_use") == []
