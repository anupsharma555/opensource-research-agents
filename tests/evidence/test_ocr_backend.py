"""Focused synthetic checks for bounded OCR preparation, identity and replay."""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess

import pytest
from PIL import Image

from ora.evidence.document_ocr import (
    ocr_public_document_page,
    read_public_document_ocr,
    render_public_document_ocr_page,
    render_public_document_page,
)
from ora.evidence.intake import IntakeService
from ora.evidence.library import EvidenceError, EvidenceLibrary
from ora.evidence.ocr_images import normalized_region, prepare_ocr_raster
from ora.state import StateStore
from tests.evidence.test_document_ocr import SCOPE, saved_public_pdf, synthetic_pdf
from tests.evidence.test_source_format_walkthroughs import _pdf, _raster_page

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("pdfinfo", "pdftoppm", "tesseract")),
    reason="local OCR executables unavailable",
)


def _pdf_document(tmp_path, text: str = "Synthetic measurement 42 units"):
    store = StateStore(tmp_path)
    document = saved_public_pdf(store, synthetic_pdf(text))
    return store, document


def _image_document(tmp_path):
    pdf = synthetic_pdf("Synthetic image note 17 units")
    source = tmp_path / "image-source.pdf"
    prefix = tmp_path / "image-source"
    source.write_bytes(pdf)
    subprocess.run(
        ["pdftoppm", "-f", "1", "-l", "1", "-singlefile", "-scale-to", "700",
         "-png", str(source), str(prefix)],
        check=True, capture_output=True, timeout=30,
    )
    image = prefix.with_suffix(".png").read_bytes()
    store = StateStore(tmp_path / "state")
    document = saved_public_pdf(
        store, image, content_type="image/png", url="https://example.org/synthetic-image.png",
    )
    return store, document


@pytest.mark.parametrize("region", [
    {}, {"x": 0, "y": 0, "width": 0, "height": 1},
    {"x": -0.1, "y": 0, "width": 0.5, "height": 0.5},
    {"x": 0.8, "y": 0, "width": 0.3, "height": 0.5},
    {"x": True, "y": 0, "width": 0.5, "height": 0.5},
    {"x": float("nan"), "y": 0, "width": 0.5, "height": 0.5},
])
def test_region_rejects_invalid_coordinates(region):
    with pytest.raises(EvidenceError, match="OCR region"):
        normalized_region(region)


def test_pdf_ocr_render_is_distinct_from_vision_and_replays(tmp_path, monkeypatch):
    store, document = _pdf_document(tmp_path)
    vision = render_public_document_page(
        store, document, **SCOPE, page_number=1, operation_id="vision",
    )
    ocr = render_public_document_ocr_page(
        store, document, **SCOPE, page_number=1, operation_id="ocr",
    )
    assert ocr["page_image_ref"] != vision["page_image_ref"]
    assert ocr["ocr_preparation"]["prepared_dimensions"][1] == 3200
    assert ocr["ocr_preparation"]["pdf_scale_to"] == 3200
    from ora.evidence import document_ocr

    monkeypatch.setattr(document_ocr, "_run", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("exact prepared image should replay")))
    again = render_public_document_ocr_page(
        store, document, **SCOPE, page_number=1, operation_id="ocr-again",
    )
    assert again["page_image_ref"] == ocr["page_image_ref"]


def test_pdf_region_has_exact_pixel_box_and_block_layout(tmp_path):
    store, document = _pdf_document(tmp_path)
    region = {"x": 0.05, "y": 0.05, "width": 0.8, "height": 0.3}
    page = render_public_document_ocr_page(
        store, document, **SCOPE, page_number=1, operation_id="crop", region=region,
    )
    preparation = page["ocr_preparation"]
    assert preparation["region"] == region
    assert preparation["crop_pixels"] == [123, 160, 2103, 1120]
    assert preparation["prepared_dimensions"] == [2004, 984]
    assert preparation["white_border_pixels"] == 12
    result = ocr_public_document_page(
        store, page["page_image_ref"], **SCOPE, operation_id="ocr-crop",
    )
    assert result["psm"] == 6 and result["layout"] == "auto"
    assert result["region"] == region
    assert result["restriction_screen_scope"] == "selected_region"
    assert result["engine_identity"]["traineddata_sha256"]
    assert result["engine_identity"]["binary_sha256"]
    assert result["tsv_sha256"] and result["ocr_quality"]["word_boxes"] >= 1
    assert result["ocr_quality"]["confidence_is_accuracy"] is False
    observed = store.transact(
        "read-selected-ocr-in-transaction", {},
        lambda tx: read_public_document_ocr(tx, result["ocr_ref"], **SCOPE),
        mission_id=SCOPE["mission_id"],
    )
    assert observed["ocr_ref"] == result["ocr_ref"]


def test_small_standalone_image_is_enlarged_and_keeps_source_hash(tmp_path):
    store, document = _image_document(tmp_path)
    page = render_public_document_ocr_page(
        store, document, **SCOPE, page_number=1, operation_id="image-ocr",
    )
    preparation = page["ocr_preparation"]
    assert preparation["scale"] > 1 and preparation["scale"] <= 2
    assert max(preparation["prepared_dimensions"]) == 1400
    assert preparation["source_image_sha256"] == store.get(**document)["body"]["content_sha256"]
    result = ocr_public_document_page(
        store, page["page_image_ref"], **SCOPE, operation_id="image-read",
    )
    assert result["status"] == "ocr_unverified" and result["psm"] == 3


def test_standalone_image_region_uses_block_layout_and_rejects_page_two(tmp_path):
    store, document = _image_document(tmp_path)
    region = {"x": 0.05, "y": 0.05, "width": 0.9, "height": 0.4}
    with pytest.raises(EvidenceError, match="only page one"):
        render_public_document_ocr_page(
            store, document, **SCOPE, page_number=2, operation_id="invalid-page",
        )
    prepared = render_public_document_ocr_page(
        store, document, **SCOPE, page_number=1, operation_id="image-region",
        region=region,
    )
    assert prepared["ocr_preparation"]["white_border_pixels"] == 12
    result = ocr_public_document_page(
        store, prepared["page_image_ref"], **SCOPE, operation_id="image-region-ocr",
    )
    assert result["psm"] == 6 and result["region"] == region
    assert "Synthetic image note" in store.get(**result["ocr_ref"])["body"]["text"]


def test_explicit_layouts_get_distinct_ocr_records(tmp_path):
    store, document = _pdf_document(tmp_path)
    page = render_public_document_ocr_page(
        store, document, **SCOPE, page_number=1, operation_id="render",
    )
    outputs = {
        layout: ocr_public_document_page(
            store, page["page_image_ref"], **SCOPE, operation_id=f"ocr-{layout}",
            layout=layout,
        ) for layout in ("auto", "block", "sparse")
    }
    assert {layout: result["psm"] for layout, result in outputs.items()} == {
        "auto": 3, "block": 6, "sparse": 11,
    }
    assert len({str(result["ocr_ref"]) for result in outputs.values()}) == 3
    for result in outputs.values():
        assert result["engine_identity"]["traineddata_sha256"]


def test_ocr_cache_replays_across_store_reopen(tmp_path, monkeypatch):
    store, document = _pdf_document(tmp_path)
    page = render_public_document_ocr_page(
        store, document, **SCOPE, page_number=1, operation_id="render",
    )
    first = ocr_public_document_page(
        store, page["page_image_ref"], **SCOPE, operation_id="first",
    )
    from ora.evidence import document_ocr

    original = document_ocr._run
    ocr_calls = 0
    def no_ocr(argv, **kwargs):
        nonlocal ocr_calls
        if argv[0] == "tesseract" and "--psm" in argv:
            ocr_calls += 1
            raise AssertionError("cached OCR should not rerun")
        return original(argv, **kwargs)
    monkeypatch.setattr(document_ocr, "_run", no_ocr)
    reopened = StateStore(tmp_path)
    second = ocr_public_document_page(
        reopened, page["page_image_ref"], **SCOPE, operation_id="second",
    )
    assert second["ocr_ref"] == first["ocr_ref"]
    assert ocr_calls == 0


def test_different_regions_have_distinct_image_and_ocr_cache_entries(tmp_path):
    store, document = _pdf_document(tmp_path)
    regions = [
        {"x": 0.05, "y": 0.05, "width": 0.8, "height": 0.3},
        {"x": 0.05, "y": 0.55, "width": 0.8, "height": 0.3},
    ]
    pages = [render_public_document_ocr_page(
        store, document, **SCOPE, page_number=1,
        operation_id=f"region-{index}", region=region,
    ) for index, region in enumerate(regions)]
    assert pages[0]["page_image_ref"] != pages[1]["page_image_ref"]
    results = [ocr_public_document_page(
        store, page["page_image_ref"], **SCOPE, operation_id=f"ocr-{index}",
    ) for index, page in enumerate(pages)]
    assert results[0]["ocr_ref"] != results[1]["ocr_ref"]
    assert [result["region"] for result in results] == regions


def test_legacy_page_ocr_record_is_read_without_rewriting(tmp_path):
    store, document = _pdf_document(tmp_path)
    page = render_public_document_page(
        store, document, **SCOPE, page_number=1, operation_id="legacy-render",
    )
    first = ocr_public_document_page(
        store, page["page_image_ref"], **SCOPE, operation_id="legacy-ocr",
    )
    body = store.get(**first["ocr_ref"])["body"]
    assert body["schema"] == "ora.evidence.public_document_page_ocr.v1"
    assert body["engine"] == "tesseract-eng-psm3-v1"
    assert "tsv_blob" not in body
    reopened = StateStore(tmp_path)
    read = read_public_document_ocr(reopened, first["ocr_ref"], **SCOPE)
    assert read["ocr_ref"] == first["ocr_ref"]
    assert "ocr_quality" not in read and "region" not in read
    assert reopened.get(**first["ocr_ref"])["body"] == body


def test_revocation_blocks_cached_selected_region_text(tmp_path):
    store, document = _pdf_document(tmp_path)
    inspected = IntakeService(EvidenceLibrary(store)).inspect_public_document(
        "inspect", document, **SCOPE,
    )
    page = render_public_document_ocr_page(
        store, document, **SCOPE, page_number=1, operation_id="render",
        region={"x": 0.05, "y": 0.05, "width": 0.8, "height": 0.3},
    )
    result = ocr_public_document_page(
        store, page["page_image_ref"], **SCOPE, operation_id="read",
    )
    assert result["model_input_status"] == "permitted"
    use = store.get(**inspected["source_use_ref"])
    store.transact("revoke", {}, lambda tx: tx.put(
        "source_use", use["id"], {**use["body"], "allowed_uses": [],
                                     "policy_status": "revoked"},
        expected_version=use["version"]), mission_id=SCOPE["mission_id"])
    with pytest.raises(EvidenceError, match="permitted original lead|not permitted"):
        read_public_document_ocr(store, result["ocr_ref"], **SCOPE)


def test_blank_crop_and_tiny_region_are_explicit(tmp_path):
    store, document = _pdf_document(tmp_path)
    with pytest.raises(EvidenceError, match="too small"):
        render_public_document_ocr_page(
            store, document, **SCOPE, page_number=1, operation_id="tiny",
            region={"x": 0, "y": 0, "width": 0.001, "height": 0.001},
        )
    blank = render_public_document_ocr_page(
        store, document, **SCOPE, page_number=1, operation_id="blank",
        region={"x": 0.2, "y": 0.7, "width": 0.3, "height": 0.2},
    )
    result = ocr_public_document_page(
        store, blank["page_image_ref"], **SCOPE, operation_id="blank-read",
    )
    assert result["status"] == "ocr_unverified"
    record = store.get(**result["ocr_ref"])
    assert record["body"]["text"] == ""


def test_exif_orientation_sets_crop_coordinate_space():
    image = Image.new("RGB", (60, 100), "white")
    exif = Image.Exif()
    exif[274] = 6
    output = io.BytesIO()
    image.save(output, format="JPEG", exif=exif)
    prepared, metadata = prepare_ocr_raster(
        output.getvalue(), mime_type="image/jpeg",
        region={"x": 0, "y": 0, "width": 0.5, "height": 0.5},
        enlarge_small=False,
    )
    assert metadata["exif_orientation_applied"] is True
    assert metadata["original_dimensions"] == [100, 60]
    assert metadata["crop_pixels"] == [0, 0, 50, 30]
    assert hashlib.sha256(prepared).hexdigest() == metadata["image_sha256"]


def test_scanned_page_inside_mixed_pdf_retains_page_three(tmp_path):
    scan = _raster_page(tmp_path, ["Scanned result: 42 units."])
    mixed = _pdf([
        ["Selectable introduction"], None, scan,
        ["Later qualification applies to this study."],
    ])
    store = StateStore(tmp_path / "mixed-state")
    document = saved_public_pdf(store, mixed)
    page = render_public_document_ocr_page(
        store, document, **SCOPE, page_number=3, operation_id="render-scan",
    )
    result = ocr_public_document_page(
        store, page["page_image_ref"], **SCOPE, operation_id="read-scan",
    )
    assert result["page_number"] == 3
    assert "Scanned result" in store.get(**result["ocr_ref"])["body"]["text"]
    assert result["restriction_screen_scope"] == "selected_page"


def test_sparse_raster_labels_are_read_as_unverified_text(tmp_path):
    scan = _raster_page(tmp_path, [
        "Site       Before kWh       After kWh",
        "Library       1200       984",
        "Footnote: estimates only",
    ])
    store = StateStore(tmp_path / "table-state")
    document = saved_public_pdf(store, _pdf([scan]))
    page = render_public_document_ocr_page(
        store, document, **SCOPE, page_number=1, operation_id="render-table",
    )
    result = ocr_public_document_page(
        store, page["page_image_ref"], **SCOPE,
        operation_id="read-sparse-labels", layout="sparse",
    )
    text = store.get(**result["ocr_ref"])["body"]["text"]
    assert result["psm"] == 11
    assert "Library" in text and "Footnote" in text
    assert result["ocr_quality"]["confidence_is_accuracy"] is False


def test_unsupported_tiff_is_rejected_before_ocr():
    image = Image.new("RGB", (100, 100), "white")
    output = io.BytesIO()
    image.save(output, format="TIFF")
    with pytest.raises(EvidenceError, match="format"):
        prepare_ocr_raster(output.getvalue(), mime_type="image/jpeg",
                           region=None, enlarge_small=False)
