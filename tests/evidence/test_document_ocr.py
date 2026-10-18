"""Local selected-page image and OCR records retain exact public-source lineage."""

from __future__ import annotations

import hashlib

import pytest

from ora.coordination.board import ref
from ora.evidence.document_ocr import (
    current_document_page_use,
    ocr_public_document_page,
    read_public_document_ocr,
    render_public_document_page,
)
from ora.evidence.intake import IntakeService, detected_content_type
from ora.evidence.library import EvidenceError, EvidenceLibrary
from ora.state import StateStore

SCOPE = {"mission_id": "synthetic-mission", "run_id": "synthetic-run",
         "actor_id": "synthetic-peer", "task_id": "synthetic-task"}
URL = "https://example.org/synthetic-document.pdf"


def synthetic_pdf(text: str) -> bytes:
    """One-page, local-only text PDF without a document-generation dependency."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 26 Tf 60 710 Td ({escaped}) Tj ET\n".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
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


def saved_public_pdf(store: StateStore, content: bytes, *,
                     content_type: str = "application/pdf", url: str = URL) -> dict:
    digest = hashlib.sha256(content).hexdigest()
    blob = store.put_blob(content)
    def save(tx):
        delivery = tx.put("retrieval_delivery", "synthetic-delivery", {})
        hit = tx.put("retrieval_hit", "synthetic-hit", {
            **SCOPE, "agent_instance_id": SCOPE["actor_id"],
            "route": "exa.search", "delivery_ref": ref(delivery),
            "source": {"origin": "original_source", "original_url": url,
                       "rights_status": "unknown", "content_sha256": None},
        })
        tx.link(hit, delivery, "derived_from")
        document = tx.put("public_document_fetch", "synthetic-document", {
            **SCOPE, "hit_ref": ref(hit), "response_ref": ref(delivery),
            "status": 200, "requested_url": url, "final_url": url,
            "redirect_chain": [], "checked_at": "2026-10-15T00:00:00Z",
            "content_type": content_type,
            "detected_content_type": detected_content_type(content, content_type),
            "content_sha256": digest, "byte_length": len(content),
            "access_basis": "public_https_unauthenticated", "authenticated": False,
            "hit_rights_status": "unknown", "blob": blob,
        })
        tx.link(document, hit, "fetched_from_browser_hit")
        tx.link(document, delivery, "derived_from_browser_response")
        return ref(document)
    return store.transact("save-synthetic-pdf", {}, save, mission_id=SCOPE["mission_id"])


def test_selected_pdf_page_is_saved_and_ocr_stays_unverified(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    content = synthetic_pdf("Synthetic OCR page")
    document_ref = saved_public_pdf(store, content)
    rendered = render_public_document_page(
        store, document_ref, **SCOPE, page_number=1, operation_id="render-one",
    )
    assert rendered["model_input_status"] == "not_granted"
    assert rendered["source_sha256"] == hashlib.sha256(content).hexdigest()
    image = store.get(**rendered["page_image_ref"])
    pixels = store.read_blob(image["body"]["blob"]["sha256"])
    assert pixels.startswith(b"\x89PNG\r\n\x1a\n")
    assert hashlib.sha256(pixels).hexdigest() == rendered["image_sha256"]
    assert image["body"]["page_number"] == 1
    from ora.evidence import document_ocr

    def no_second_local_process(*_args, **_kwargs):
        raise AssertionError("An exact saved page and OCR should be reused")

    monkeypatch.setattr(document_ocr, "_run", no_second_local_process)
    assert render_public_document_page(
        store, document_ref, **SCOPE, page_number=1, operation_id="render-again",
    )["page_image_ref"] == rendered["page_image_ref"]
    monkeypatch.undo()
    ocr = ocr_public_document_page(
        store, rendered["page_image_ref"], **SCOPE, operation_id="ocr-one",
    )
    assert ocr["status"] == "ocr_unverified"
    assert "text" not in ocr
    assert ocr["model_input_status"] == "not_granted"
    saved = store.get(**ocr["ocr_ref"])
    assert "Synthetic OCR page" in saved["body"]["text"]
    assert saved["body"]["document_ref"] == document_ref
    assert saved["body"]["locator"] == "page=1; local-ocr-text"
    assert saved["body"]["text_sha256"] == hashlib.sha256(saved["body"]["text"].encode()).hexdigest()
    assert "text" not in read_public_document_ocr(store, ocr["ocr_ref"], **SCOPE)
    monkeypatch.setattr(document_ocr, "_run", no_second_local_process)
    assert ocr_public_document_page(
        store, rendered["page_image_ref"], **SCOPE, operation_id="ocr-again",
    )["ocr_ref"] == ocr["ocr_ref"]


def test_page_permission_check_accepts_transaction_and_reuses_exact_quality(tmp_path, monkeypatch):
    from ora.evidence import document_ocr

    store = StateStore(tmp_path)
    document_ref = saved_public_pdf(store, synthetic_pdf("Selected page text"))
    inspected = IntakeService(EvidenceLibrary(store)).inspect_public_document(
        "inspect-page", document_ref, **SCOPE,
    )
    assert inspected["model_input_status"] == "permitted"

    def no_reparse(*_args, **_kwargs):
        raise AssertionError("Exact quality should avoid a second full PDF parse")

    monkeypatch.setattr(document_ocr, "extract_blocks", no_reparse)
    checked = store.transact("check-page-transaction", {}, lambda tx: {
        key: value for key, value in current_document_page_use(tx, document_ref, **SCOPE).items()
        if key != "content"
    },
        mission_id=SCOPE["mission_id"])
    assert checked["model_input_status"] == "permitted"
    assert checked["source_use_ref"] == inspected["source_use_ref"]

    use = store.get(**inspected["source_use_ref"])
    def revoke_in_transaction(tx):
        tx.put("source_use", use["id"], {
            **use["body"], "allowed_uses": [], "policy_status": "revoked",
        }, expected_version=use["version"])
        with pytest.raises(EvidenceError, match="permitted original lead|not permitted"):
            current_document_page_use(tx, document_ref, **SCOPE)
        return {"revoked": True}

    store.transact("check-revocation-in-transaction", {}, revoke_in_transaction,
                   mission_id=SCOPE["mission_id"])


def test_page_scope_and_page_bound_are_enforced(tmp_path):
    store = StateStore(tmp_path)
    document_ref = saved_public_pdf(store, synthetic_pdf("Synthetic page"))
    with pytest.raises(EvidenceError, match="another peer task"):
        render_public_document_page(
            store, document_ref, **{**SCOPE, "actor_id": "other-peer"},
            page_number=1, operation_id="wrong-peer",
        )
    with pytest.raises(EvidenceError, match="outside the saved document"):
        render_public_document_page(
            store, document_ref, **SCOPE, page_number=2, operation_id="wrong-page",
        )
    with pytest.raises(EvidenceError, match="outside the local bound"):
        render_public_document_page(
            store, document_ref, **SCOPE, page_number=0, operation_id="page-zero",
        )
    assert current_document_page_use(store, document_ref, **SCOPE)["source_sha256"]


def test_revocation_during_local_ocr_prevents_text_return(tmp_path, monkeypatch):
    from subprocess import CompletedProcess

    from ora.evidence import document_ocr

    store = StateStore(tmp_path)
    document_ref = saved_public_pdf(store, synthetic_pdf(""))
    inspected = IntakeService(EvidenceLibrary(store)).inspect_public_document(
        "inspect-before-ocr", document_ref, **SCOPE,
    )
    page = render_public_document_page(
        store, document_ref, **SCOPE, page_number=1, operation_id="render-before-revoke",
    )
    use = store.get(**inspected["source_use_ref"])
    original_run = document_ocr._run
    def revoke_while_ocr_runs(argv, **kwargs):
        if argv[0] == "tesseract":
            store.transact("revoke-during-ocr", {}, lambda tx: tx.put(
                "source_use", use["id"], {
                    **use["body"], "allowed_uses": [], "policy_status": "revoked",
                }, expected_version=use["version"]), mission_id=SCOPE["mission_id"])
            return CompletedProcess(argv, 0, b"Synthetic OCR words", b"")
        return original_run(argv, **kwargs)
    monkeypatch.setattr(document_ocr, "_run", revoke_while_ocr_runs)
    with pytest.raises(EvidenceError, match="permitted original lead|not permitted"):
        ocr_public_document_page(
            store, page["page_image_ref"], **SCOPE, operation_id="ocr-race",
        )
    assert len(store.records("public_document_page_ocr")) == 1
