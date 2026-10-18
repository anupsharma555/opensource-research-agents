"""Portable PDF and OCR checks using only synthetic source material."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from ora.evidence.document_ocr import RENDER_SCALE_TO
from ora.evidence.pdf_content import extract_pdf_blocks
from tests.evidence.test_source_format_walkthroughs import _pdf, _raster_page


def _require(*executables: str) -> None:
    for executable in executables:
        if shutil.which(executable) is None:
            pytest.skip(f"{executable} unavailable")


def _render(pdf: bytes, tmp_path, page_number: int) -> bytes:
    source = tmp_path / "synthetic.pdf"
    prefix = tmp_path / "selected"
    source.write_bytes(pdf)
    subprocess.run(
        ["pdftoppm", "-f", str(page_number), "-l", str(page_number),
         "-singlefile", "-scale-to", str(RENDER_SCALE_TO), "-png",
         str(source), str(prefix)],
        check=True, capture_output=True, timeout=30,
    )
    return prefix.with_suffix(".png").read_bytes()


def _ocr(image: bytes) -> str:
    return subprocess.run(
        ["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "3"],
        input=image, check=True, capture_output=True, timeout=30,
    ).stdout.decode("utf-8").strip()


def test_mixed_blank_scan_and_selectable_pages_keep_physical_identity(tmp_path):
    _require("pdftotext", "pdftoppm", "tesseract")
    scan = _raster_page(tmp_path, ["Scan note: avoid a positive effect claim."])
    pdf = _pdf([
        ["Text page one: measured change -2.5 units, not +2.5 units."],
        None,
        scan,
        ["Text page four: 0.05 units is a separate estimate."],
    ])
    blocks = extract_pdf_blocks(pdf)
    assert [block.locator.split(";")[0] for block in blocks] == ["page=1", "page=4"]
    assert "-2.5 units, not +2.5 units" in blocks[0].text
    assert "0.05 units is a separate estimate" in blocks[1].text
    assert "Scan note" in _ocr(_render(pdf, tmp_path, 3))


def test_two_prose_columns_preserve_order_and_later_qualification():
    _require("pdftotext")
    pdf = _pdf([[
        (38, 745, "Annual assessment of independent sites"),
        (38, 690, "LEFT FIRST: change was -2.5 units."),
        (320, 690, "RIGHT FIRST: effect was uncertain."),
        (38, 665, "LEFT SECOND: winter was excluded."),
        (320, 665, "RIGHT SECOND: more study is needed."),
    ]])
    blocks = extract_pdf_blocks(pdf)
    assert len(blocks) == 1
    assert blocks[0].locator == "page=1; reading-order-text"
    text = blocks[0].text
    assert text.index("Annual assessment") < text.index("LEFT FIRST")
    assert text.index("LEFT FIRST") < text.index("LEFT SECOND")
    assert text.index("LEFT SECOND") < text.index("RIGHT FIRST")
    assert text.index("RIGHT FIRST") < text.index("RIGHT SECOND")
    assert "effect was uncertain" in text and "more study is needed" in text


def test_table_rows_keep_units_values_and_footnote_adjacent():
    _require("pdftotext")
    pdf = _pdf([[
        (38, 745, "Annual energy use by site"),
        (38, 700, "Site"), (170, 700, "Before (kWh)"), (350, 700, "After (kWh)"),
        (38, 677, "Library"), (170, 677, "1,200"), (350, 677, "984"),
        (38, 654, "Clinic"), (170, 654, "-2.5"), (350, 654, "0.05"),
        (38, 620, "Footnote: winter weekends excluded; clinic values are estimates."),
    ]])
    blocks = extract_pdf_blocks(pdf)
    assert len(blocks) == 1 and blocks[0].locator == "page=1; layout-text"
    text = blocks[0].text
    assert re.search(r"(?m)^Site\s+Before \(kWh\)\s+After \(kWh\)$", text)
    assert re.search(r"(?m)^Library\s+1,200\s+984$", text)
    assert re.search(r"(?m)^Clinic\s+-2\.5\s+0\.05$", text)
    assert text.index("Clinic") < text.index("Footnote: winter weekends excluded")
