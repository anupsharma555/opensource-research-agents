"""Eight synthetic, offline source-to-selected-passage format walkthroughs.

These check real local extraction and exact saved-source mapping. They do not
establish that an autonomous model would choose or understand the passages.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ora.evidence.document_ocr import (
    ocr_public_document_page,
    read_public_document_ocr,
    render_public_document_page,
)
from ora.evidence.intake import IntakeService, checked_model_text, extract_blocks
from ora.evidence.library import EvidenceLibrary
from ora.evidence.windows import read_source_window
from ora.state import StateStore
from tests.evidence.test_document_ocr import SCOPE, saved_public_pdf

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "source_formats"


def _pdf(parts: list[list[str | tuple[int, int, str]] | bytes | None]) -> bytes:
    """Small synthetic PDF; bytes pages contain an actual JPEG image XObject."""
    objects: list[bytes] = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    page_refs = []
    for part in parts:
        if isinstance(part, bytes):
            width, height = _jpeg_size(part)
            image_id = len(objects) + 1
            objects.append(
                f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
                f"/Length {len(part)} >>\nstream\n".encode() + part + b"\nendstream"
            )
            stream = b"q 560 0 0 720 26 36 cm /Im1 Do Q\n"
            resources = f"<< /XObject << /Im1 {image_id} 0 R >> >>".encode()
        else:
            lines = part or []
            commands = []
            for index, line in enumerate(lines):
                x, y, label = line if isinstance(line, tuple) else (38, 745 - 23 * index, line)
                escaped = label.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                commands.append(f"BT /F1 13 Tf {x} {y} Td ({escaped}) Tj ET\n")
            stream = "".join(commands).encode()
            resources = b"<< /Font << /F1 3 0 R >> >>"
        content_id = len(objects) + 1
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream")
        page_id = len(objects) + 1
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            + resources + f" /Contents {content_id} 0 R >>".encode()
        )
        page_refs.append(page_id)
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = ("<< /Type /Pages /Kids [" + " ".join(f"{number} 0 R" for number in page_refs)
                  + f"] /Count {len(page_refs)} >>").encode()
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend((f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
                   f"startxref\n{xref}\n%%EOF\n").encode())
    return bytes(output)


def _jpeg_size(data: bytes) -> tuple[int, int]:
    index = 2
    while index < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker in {0xD8, 0xD9}:
            continue
        length = int.from_bytes(data[index:index + 2], "big")
        if marker in {0xC0, 0xC1, 0xC2}:
            return (int.from_bytes(data[index + 5:index + 7], "big"),
                    int.from_bytes(data[index + 3:index + 5], "big"))
        index += length
    raise ValueError("JPEG fixture lacks dimensions")


def _raster_page(tmp_path: Path, lines: list[str]) -> bytes:
    """Render actual PDF text to JPEG, then embed pixels in a new scan PDF."""
    source = tmp_path / "raster-source.pdf"
    source.write_bytes(_pdf([lines]))
    prefix = tmp_path / "raster-source"
    subprocess.run(["pdftoppm", "-f", "1", "-l", "1", "-r", "180", "-jpeg",
                    "-singlefile", str(source), str(prefix)], check=True, capture_output=True)
    return (tmp_path / "raster-source.jpg").read_bytes()


def _inspected(tmp_path: Path, content: bytes, mime: str):
    store = StateStore(tmp_path)
    library = EvidenceLibrary(store)
    document = saved_public_pdf(
        store, content, content_type=mime, url="https://example.org/synthetic-format",
    )
    result = IntakeService(library).inspect_public_document("inspect-format", document, **SCOPE)
    assert result["copy_ref"] and result["source_use_ref"]
    copy = store.get(**result["copy_ref"])
    assert copy["body"]["content_sha256"] == hashlib.sha256(content).hexdigest()
    assert store.read_blob(copy["body"]["blob"]["sha256"]) == content
    return store, library, document, result


@pytest.mark.parametrize("case,mime,filename,needle,qualifier,locator", [
    ("H1", "text/html", "h1_navigation.html", "reduced annual electricity use by 18 percent",
     "winter weekends", "section=Project result"),
    ("H2", "text/html", "h2_table.html", "Before (kWh): 1,200",
     "excludes winter weekends", "section=Community library retrofit"),
    ("X1", "application/jats+xml", "x1_article.xml", "from 1,200 to 984 kWh",
     "excludes winter weekends", "section=Results"),
    ("T1", "text/plain", "t1_notes.txt", "later checked result",
     "excludes winter weekends", "text; chars=0:"),
])
def test_structured_text_walkthrough(
    tmp_path, case, mime, filename, needle, qualifier, locator,
):
    content = (FIXTURES / filename).read_bytes()
    store, library, _document, inspected = _inspected(tmp_path, content, mime)
    blocks, _parser = extract_blocks(content, mime)
    target = next(index for index, block in enumerate(blocks) if needle in block.text)
    assert locator in blocks[target].locator, case
    if case == "H1":
        assert all(block.text not in {"Catalog", "Help"} for block in blocks)
        assert target > 0  # A navigation-heavy source needs targeted continuation.
    if case == "H2":
        assert "Site: Library" in " ".join(block.text for block in blocks)
        assert "After (kWh): 984" in " ".join(block.text for block in blocks)
        assert any("Annual electricity use, kWh" in block.text for block in blocks)
    if case == "X1":
        assert any("Annual electricity use fell by 18 percent" in block.text
                   and "section=Abstract;" in block.locator for block in blocks)
        # Fresh copies separate abstract/body scope; retained legacy copies keep
        # their historical mappings in test_xml_mapping_compatibility.
        assert any("section=Methods" in block.locator and qualifier in block.text for block in blocks)
    if case == "T1":
        text = content.decode("utf-8")
        assert blocks[0].text == text
        assert text.count("18 percent") == 2
    char_offset = (content.decode("utf-8").index("The later checked result")
                   if case == "T1" else 0)
    if case == "H1":
        first = read_source_window(
            library, "read-first", inspected["copy_ref"], block_limit=1,
        )
        assert first["next_cursor"] == {"block_offset": 1, "char_offset": 0}
        assert needle not in first["passages"][0]["text_preview"]
    window = read_source_window(
        library, "read-target", inspected["copy_ref"], block_offset=target,
        block_limit=1, char_offset=char_offset, char_limit=4000,
    )
    assert window["passages"][0]["locator"].startswith(blocks[target].locator)
    assert needle in window["passages"][0]["text_preview"]
    if case == "T1":
        assert window["passages"][0]["locator"].endswith(
            f"chars={char_offset}:{len(content.decode('utf-8'))}"
        )
        assert "first estimate" not in window["passages"][0]["text_preview"]
    selected = [window["passage_refs"][0]]
    if qualifier not in window["passages"][0]["text_preview"]:
        qualifying = next(index for index, block in enumerate(blocks) if qualifier in block.text)
        other = read_source_window(
            library, "read-qualifier", inspected["copy_ref"], block_offset=qualifying,
            block_limit=1, char_limit=4000,
        )
        selected.append(other["passage_refs"][0])
    if case == "H2":
        # Each cell keeps its caption and column label; select both values.
        selected.append(read_source_window(
            library, "read-table-after", inspected["copy_ref"],
            block_offset=target + 1, block_limit=1, char_limit=4000,
        )["passage_refs"][0])
    packet = checked_model_text(library, inspected["copy_ref"], selected)
    assert needle in packet["text"] and qualifier in packet["text"]
    assert packet["copy_ref"] == inspected["copy_ref"]
    assert packet["content_sha256"] == hashlib.sha256(content).hexdigest()
    assert [item["passage_ref"] for item in packet["passages"]] == selected
    assert all(store.get(**pointer)["body"]["copy_ref"] == inspected["copy_ref"]
               for pointer in selected)


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="Poppler pdftotext unavailable")
@pytest.mark.parametrize("case,pages,target_page,needle,qualifier", [
    ("P1", [
        ["Community library retrofit", "Annual electricity use fell by 18 percent."],
        ["Qualification: winter weekends are excluded because the reading room is closed."],
    ], 1, "18 percent", "winter weekends are excluded"),
    ("P2", [
        [(38, 745, "LEFT COLUMN: demand fell."),
         (320, 745, "RIGHT COLUMN: winter exception."),
         (38, 722, "Savings lasted through autumn."),
         (320, 722, "Weekends were not measured."),
         (38, 700, "Annual electricity use (kWh)"),
         (38, 677, "Site"), (170, 677, "Before (kWh)"), (350, 677, "After (kWh)"),
         (38, 654, "Library"), (170, 654, "1,200"), (350, 654, "984"),
         (38, 631, "Footnote: winter weekends excluded.")],
    ], 1, "Library", "winter weekends excluded"),
])
def test_text_pdf_walkthrough(tmp_path, case, pages, target_page, needle, qualifier):
    content = _pdf(pages)
    store, library, _document, inspected = _inspected(tmp_path, content, "application/pdf")
    blocks, parser = extract_blocks(content, "application/pdf")
    from ora.evidence.pdf_content import PDF_PARSER_VERSION

    assert parser == PDF_PARSER_VERSION
    assert blocks[target_page - 1].locator == f"page={target_page}; layout-text"
    assert needle in blocks[target_page - 1].text
    if case == "P2":
        assert "Before (kWh)" in blocks[0].text and "After (kWh)" in blocks[0].text
        assert "Annual electricity use (kWh)" in blocks[0].text
        assert "Library" in blocks[0].text and "1,200" in blocks[0].text and "984" in blocks[0].text
        assert "LEFT COLUMN" in blocks[0].text and "RIGHT COLUMN" in blocks[0].text
        assert "Savings lasted through autumn." in blocks[0].text
        assert "Weekends were not measured." in blocks[0].text
        assert re.search(r"(?m)^Site\s+Before \(kWh\)\s+After \(kWh\)$", blocks[0].text)
        assert re.search(r"(?m)^Library\s+1,200\s+984$", blocks[0].text)
    windows = [read_source_window(
        library, f"read-page-{page}", inspected["copy_ref"], block_offset=page - 1,
        block_limit=1, char_limit=4000,
    ) for page in range(1, len(pages) + 1)]
    selected = [window["passage_refs"][0] for window in windows]
    packet = checked_model_text(library, inspected["copy_ref"], selected)
    assert needle in packet["text"] and qualifier in packet["text"]
    assert [item["locator"].split(";", 1)[0] for item in packet["passages"]] == [
        f"page={page}" for page in range(1, len(pages) + 1)
    ]
    assert packet["content_sha256"] == hashlib.sha256(content).hexdigest()
    assert all(store.get(**pointer)["body"]["copy_ref"] == inspected["copy_ref"]
               for pointer in selected)


@pytest.mark.skipif(
    not all(shutil.which(name) for name in ("pdftotext", "pdftoppm", "pdfinfo", "tesseract")),
    reason="Native Poppler/Tesseract unavailable",
)
@pytest.mark.parametrize("case", ["O1", "O2"])
def test_scanned_pdf_walkthrough(tmp_path, case):
    raster = _raster_page(tmp_path, [
        "OCR result: 12.5 kWh saved per day.",
        "Not measured on winter weekends.",
    ])
    pages = [raster] if case == "O1" else [
        ["Selectable page: the retrofit began in spring."], None, raster,
    ]
    content = _pdf(pages)
    store, _library, document, inspected = _inspected(tmp_path / "state", content, "application/pdf")
    blocks, _parser = extract_blocks(content, "application/pdf")
    if case == "O1":
        assert blocks == []
        assert inspected["classification"] == "image_document"
        page_number = 1
    else:
        assert len(blocks) == 1 and blocks[0].locator == "page=1; layout-text"
        assert "12.5" not in blocks[0].text
        page_number = 3
    rendered = render_public_document_page(
        store, document, **SCOPE, page_number=page_number, operation_id="render-target",
    )
    ocr = ocr_public_document_page(
        store, rendered["page_image_ref"], **SCOPE, operation_id="ocr-target",
    )
    assert ocr["status"] == "ocr_unverified"
    assert ocr["model_input_status"] == "permitted"
    assert "12.5 kWh" in ocr["text"]
    assert "Not measured on winter weekends" in ocr["text"]
    assert "page=" + str(page_number) in store.get(**ocr["ocr_ref"])["body"]["locator"]
    assert store.records("source_passage") == []
    assert read_public_document_ocr(store, ocr["ocr_ref"], **SCOPE)["text"] == ocr["text"]
    repeated = ocr_public_document_page(
        store, rendered["page_image_ref"], **SCOPE, operation_id="ocr-reuse",
    )
    assert repeated["ocr_ref"] == ocr["ocr_ref"]
