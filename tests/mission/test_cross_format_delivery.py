"""Direct XML/PDF passages and derived OCR keep distinct model-use meanings."""

from __future__ import annotations

import hashlib
import json
import shutil

import pytest

from ora.evidence import EvidenceLibrary, IntakeService, checked_model_text, read_source_window
from ora.evidence.document_ocr import (
    ocr_public_document_page,
    read_public_document_ocr,
    render_public_document_page,
)
from ora.evidence.intake import extract_copy_blocks
from ora.evidence.library import EvidenceError
from ora.models import SourceSelection
from ora.state import StateStore
from tests.evidence.test_document_ocr import SCOPE, saved_public_pdf
from tests.evidence.test_source_format_walkthroughs import _pdf, _raster_page
from tests.models.test_session import setup

DOI = "10.1234/cross-format"
XML = (
    b"<?xml version='1.0'?><article><front><article-meta>"
    b"<article-id pub-id-type='doi'>10.1234/cross-format</article-id>"
    b"</article-meta></front><body><sec><title>Results</title>"
    b"<p>At week 6, the measured score was 14 units.</p>"
    b"<p>The estimate excludes missing visits.</p>"
    b"</sec></body></article>"
)


def _direct_copy(tmp_path, content: bytes, mime: str):
    store = StateStore(tmp_path)
    library = EvidenceLibrary(store)
    publication = library.register_publication(
        "cross-format-publication", identifiers={"doi": DOI},
        title="Synthetic source", verification="DOI visible in synthetic source bytes",
    )["publication_ref"]
    url = ("https://example.org/cross-format.xml" if mime == "application/jats+xml"
           else "https://example.org/cross-format.pdf")
    retrieved_at = "2026-10-17T12:00:00Z"
    receipt = {
        "status": 200, "content": content,
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "byte_length": len(content), "content_type": mime,
        "requested_url": url, "final_url": url, "redirect_chain": [],
        "access_basis": "public_https_unauthenticated", "authenticated": False,
        "retrieved_at": retrieved_at,
    }
    inspected = IntakeService(library).inspect_response(
        "inspect-cross-format", publication, content=content, content_type=mime,
        source_url=url, retrieved_at=retrieved_at,
        source_version="sha256:" + receipt["content_sha256"],
        rights_status="unknown", rights_basis="Synthetic public fetch; license unverified",
        declared_surface="full_text", expected_doi=DOI,
        identity_basis="DOI visible in the exact synthetic original",
        public_fetch_receipt=receipt,
    )
    assert inspected["classification"] == "passage"
    assert inspected["copy_ref"] is not None
    use = store.get(**inspected["source_use_ref"])["body"]
    assert use["direct_source_use_policy"]["public_fetch_verified"] is True
    assert use["direct_source_use_policy"]["identity_check"] == "visible_doi_match"
    assert "model_input" in use["allowed_uses"]
    return store, library, inspected


@pytest.mark.parametrize("mime,content,needles", [
    ("application/jats+xml", XML, (
        "At week 6, the measured score was 14 units.",
        "The estimate excludes missing visits.",
    )),
    ("application/pdf", _pdf([[
        "Results. DOI: 10.1234/cross-format.",
        "At week 6, the score was 14 units.",
        "Missing visits excluded.",
    ]]), ("the score was 14 units", "Missing visits excluded")),
], ids=["xml", "pdf"])
def test_direct_public_xml_or_pdf_reaches_exact_saved_model_request(
    tmp_path, mime, content, needles,
):
    if mime == "application/pdf" and not shutil.which("pdftotext"):
        pytest.skip("Poppler pdftotext unavailable")
    store, library, inspected = _direct_copy(tmp_path, content, mime)
    copy_ref = inspected["copy_ref"]
    copy_body = store.get(**copy_ref)["body"]
    assert store.read_blob(copy_body["blob"]["sha256"]) == content
    blocks, parser = extract_copy_blocks(content, copy_body)
    assert parser in copy_body["mapping_version"]
    selected_indices = list(dict.fromkeys(
        next(index for index, block in enumerate(blocks) if needle in block.text)
        for needle in needles
    ))
    windows = [read_source_window(
        library, f"cross-format-window-{index}", copy_ref,
        block_offset=index, block_limit=1, char_limit=4000,
    ) for index in selected_indices]
    refs = [window["passage_refs"][0] for window in windows]
    for index, window, pointer in zip(selected_indices, windows, refs, strict=True):
        assert window["passages"][0]["locator"].startswith(blocks[index].locator)
        assert window["passages"][0]["text_preview"] == blocks[index].text
        assert IntakeService(library).source_for_model(pointer)["text"] == blocks[index].text
    checked = checked_model_text(library, copy_ref, refs)
    assert all(checked["text"].count(needle) == 1 for needle in needles)

    reopened, _registry, bridge, call, _config, _copy_ref, captured = setup(
        tmp_path, source_ref=copy_ref,
    )
    prepared = bridge.prepare(
        call_id=call["id"], call_version=call["version"], phase="collect",
        messages=[{"role": "user", "content": "Read the selected result and limitation."}],
        sources=(SourceSelection(copy_ref, "permitted", True, "direct", tuple(refs)),),
    )
    assert captured == []
    intent = reopened.get(**prepared.intent_ref)["body"]
    request_bytes = reopened.read_blob(intent["blob"]["sha256"])
    assert hashlib.sha256(request_bytes).hexdigest() == intent["request_sha256"]
    request = json.loads(request_bytes)
    packets = [json.loads(message["content"])["source_packets"]
               for message in request["messages"] if message.get("role") == "user"
               and '"source_packets"' in message.get("content", "")]
    assert len(packets) == 1 and len(packets[0]) == 1
    packet = packets[0][0]
    assert packet["copy_sha256"] == hashlib.sha256(content).hexdigest()
    assert packet["passage_refs"] == refs
    assert packet["text"] == checked["text"]
    assert all(packet["text"].count(needle) == 1 for needle in needles)
    assert bridge.load(prepared.intent_ref, for_dispatch=True).request.body == request


@pytest.mark.skipif(
    not all(shutil.which(name) for name in ("pdftotext", "pdftoppm", "pdfinfo", "tesseract")),
    reason="Local Poppler/Tesseract unavailable",
)
def test_scanned_pdf_ocr_is_derived_unverified_text_not_checked_passage(tmp_path):
    raster = _raster_page(tmp_path, ["Synthetic scan value 17 units."])
    content = _pdf([raster])
    store = StateStore(tmp_path / "scan")
    document_ref = saved_public_pdf(store, content)
    library = EvidenceLibrary(store)
    inspected = IntakeService(library).inspect_public_document(
        "inspect-scanned-cross-format", document_ref, **SCOPE,
    )
    assert inspected["classification"] == "image_document"
    with pytest.raises(EvidenceError, match="read_document_ocr"):
        read_source_window(library, "scan-window", inspected["copy_ref"])
    rendered = render_public_document_page(
        store, document_ref, **SCOPE, page_number=1, operation_id="render-cross-format-scan",
    )
    ocr = ocr_public_document_page(
        store, rendered["page_image_ref"], **SCOPE, operation_id="ocr-cross-format-scan",
    )
    checked = read_public_document_ocr(store, ocr["ocr_ref"], **SCOPE)
    assert checked["status"] == "ocr_unverified"
    assert "17" in checked["text"] and "units" in checked["text"]
    assert checked["source_sha256"] == hashlib.sha256(content).hexdigest()
    assert store.records("source_passage") == []
