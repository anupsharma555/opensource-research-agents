"""Selected OCR region and layout survive paging and durable acceptance offline."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ora.contracts.models import ReadDocumentOCRInput
from ora.coordination.board import ref
from ora.coordination.executor import WorkResult
from ora.evidence.intake import IntakeService
from ora.evidence.library import EvidenceLibrary
from ora.evidence.source_assets import fetch_source_asset, list_source_assets
from ora.mission.document_context import ocr_model_view, source_copy_url
from ora.mission.document_tools import (
    document_tool_result,
    read_document_page,
    validate_document_result_tx,
)
from ora.state import StateError, StateStore
from tests.evidence.test_document_ocr import SCOPE, saved_public_pdf, synthetic_pdf
from tests.evidence.test_source_assets import _png, _receipt

REGION = {"x": 0.01, "y": 0.01, "width": 0.95, "height": 0.25}


@pytest.mark.parametrize("region", [
    {"x": 0.8, "y": 0, "width": 0.3, "height": 1},
    {"x": 0, "y": 0, "width": 0, "height": 1},
    {"x": float("nan"), "y": 0, "width": 0.3, "height": 1},
])
def test_canonical_ocr_region_rejects_invalid_rectangle(region):
    with pytest.raises(ValidationError):
        ReadDocumentOCRInput(document_ref={"kind": "public_document_fetch", "id": "doc", "version": 1},
                             page_number=1, region=region)


def test_ocr_paging_preserves_region_layout_and_exact_saved_variant(tmp_path):
    store = StateStore(tmp_path)
    document = saved_public_pdf(store, synthetic_pdf("Selected sample text for continued reading"))
    IntakeService(EvidenceLibrary(store)).inspect_public_document("inspect", document, **SCOPE)
    result = read_document_page(store, document, page_number=1, operation_id="first",
                                region=REGION, layout="block", **SCOPE)
    visible_scope = {key: SCOPE[key] for key in ("mission_id", "actor_id", "task_id")}
    first = document_tool_result(store, "read_document_ocr", {**result, "limit": 10},
                                 **visible_scope)
    next_args = first["next_request"]
    assert next_args["region"] == REGION and next_args["layout"] == "block"
    second_raw = read_document_page(store, next_args["document_ref"],
        page_number=next_args["page_number"], operation_id="second", region=next_args["region"],
        layout=next_args["layout"], **SCOPE)
    second = document_tool_result(store, "read_document_ocr", {
        **second_raw, "offset": next_args["offset"], "limit": next_args["limit"],
    }, **visible_scope)
    assert second["ocr_ref"] == first["ocr_ref"]
    assert first["text"] + second["text"] == result["text"][:20]
    assert len(store.records("public_document_page_ocr")) == 1
    assert first["evidence_status"] == "ocr_unverified_not_a_checked_passage"
    compact = ocr_model_view(store, first)
    assert compact["text"] == first["text"]
    assert compact["region"] == REGION and compact["next_request"] == first["next_request"]
    assert compact["source_url"] == "https://example.org/synthetic-document.pdf"
    assert "engine_identity" not in compact and "ocr_preparation" not in compact
    assert store.get(**first["ocr_ref"])["body"]["engine_identity"]

    requested = {"id": "ocr-call", "name": "read_document_ocr", "arguments": {
        "document_ref": document, "page_number": 1, "region": REGION, "layout": "block", "limit": 10,
    }}
    spec = SimpleNamespace(mission_id=SCOPE["mission_id"], actor_id=SCOPE["actor_id"],
                           proposal_ref={"id": SCOPE["task_id"]})
    outcome = WorkResult(disposition="continue_task", payload={
        "tool_call_id": "ocr-call", "name": "read_document_ocr", "result": first,
    }, output_refs=(first["ocr_ref"],))
    store.transact("accept-selected-ocr", {}, lambda tx: validate_document_result_tx(
        tx, spec, outcome, requested) or {"accepted": True}, mission_id=SCOPE["mission_id"])
    requested["arguments"]["offset"] = 10
    with pytest.raises(StateError, match="text window"):
        store.transact("reject-different-window", {}, lambda tx: validate_document_result_tx(
            tx, spec, outcome, requested), mission_id=SCOPE["mission_id"])
    requested["arguments"]["offset"] = 0
    requested["arguments"]["region"] = None
    with pytest.raises(StateError, match="selected region or layout"):
        store.transact("reject-different-ocr", {}, lambda tx: validate_document_result_tx(
            tx, spec, outcome, requested), mission_id=SCOPE["mission_id"])


def test_ocr_caption_uses_original_parent_url_after_redirect(tmp_path):
    store = StateStore(tmp_path / "state")
    original_url = "https://example.org/synthetic-parent"
    redirected = "https://cdn.example.org/temporary?signature=synthetic-private"
    html = (b'<main><figure><img src="https://example.org/figure.png">'
            b'<figcaption>Synthetic panel A: units are mg/L.</figcaption></figure></main>')
    document = saved_public_pdf(store, html, content_type="text/html", url=original_url)
    record = store.get(**document)
    document = store.transact("redirected-parent", {}, lambda tx: ref(tx.put(
        record["kind"], record["id"], {**record["body"], "final_url": redirected,
            "redirect_chain": [{"from_url": original_url, "to_url": redirected, "status": 302}]},
        expected_version=record["version"])), mission_id=SCOPE["mission_id"])
    inspected = IntakeService(EvidenceLibrary(store)).inspect_public_document(
        "inspect-parent", document, **SCOPE)
    store.transact("select-parent", {}, lambda tx: tx.put("mission_task_control", "control", {
        **SCOPE, "source_refs": [inspected["copy_ref"]],
    }), mission_id=SCOPE["mission_id"])
    copy = store.get(**inspected["copy_ref"])
    assert copy["body"]["reference"] == redirected
    assert source_copy_url(store, copy) == original_url
    asset = list_source_assets(store, inspected["copy_ref"], scope=SCOPE)["assets"][0]
    png = _png(tmp_path / "pixels")
    image = fetch_source_asset(store, asset["asset_ref"], scope=SCOPE, operation_id="fetch",
        fetcher=lambda url: _receipt(png, url))
    IntakeService(EvidenceLibrary(store)).inspect_public_document("inspect-child", image, **SCOPE)
    raw = read_document_page(store, image, page_number=1, operation_id="ocr", **SCOPE)
    checked = document_tool_result(store, "read_document_ocr", raw,
        **{key: SCOPE[key] for key in ("mission_id", "actor_id", "task_id")})
    visible = ocr_model_view(store, checked)
    assert visible["parent_source_url"] == original_url
    assert visible["source_url"] == "https://example.org/figure.png"
    assert visible["source_caption"] == asset["caption"]
    assert "signature=" not in str(visible)
