"""PDF order checks keep physical pages and tabular relationships intact."""

import re
import shutil

import pytest

import ora.evidence.pdf_content as pdf_content
from ora.evidence.pdf_content import (
    PDF_PARSER_VERSION,
    PDFExtractionError,
    _bbox_pages,
    _same_visible_characters,
    extract_pdf_blocks,
)
from tests.evidence.test_source_format_walkthroughs import _pdf, _raster_page

pytestmark = pytest.mark.skipif(
    not shutil.which("pdftotext"), reason="Poppler pdftotext unavailable",
)


def test_version_and_simple_single_column_stay_in_layout_mode():
    assert PDF_PARSER_VERSION == "poppler-bbox-layout-reading-order-v2"
    blocks = extract_pdf_blocks(_pdf([[
        "Study result: -2.5 points, not +2.5.",
        "Footnote: winter observations were excluded.",
    ]]))
    assert len(blocks) == 1
    assert blocks[0].locator == "page=1; layout-text"
    assert "-2.5 points, not +2.5" in blocks[0].text
    assert "Footnote: winter observations were excluded." in blocks[0].text


def test_two_prose_columns_read_contiguously_after_pagewide_heading():
    pdf = _pdf([[
        (38, 745, "Annual assessment of two independent study groups"),
        (38, 690, "LEFT FIRST: demand fell by 18 percent."),
        (320, 690, "RIGHT FIRST: winter was an exception."),
        (38, 665, "LEFT SECOND: the gain lasted all year."),
        (320, 665, "RIGHT SECOND: weekends were excluded."),
    ]])
    blocks = extract_pdf_blocks(pdf)
    assert len(blocks) == 1
    text = blocks[0].text
    assert blocks[0].locator == "page=1; reading-order-text"
    assert text.index("Annual assessment") < text.index("LEFT FIRST")
    assert text.index("LEFT FIRST") < text.index("LEFT SECOND")
    assert text.index("LEFT SECOND") < text.index("RIGHT FIRST")
    assert text.index("RIGHT FIRST") < text.index("RIGHT SECOND")


def test_table_between_prose_keeps_layout_row_alignment():
    pdf = _pdf([[
        (38, 745, "LEFT COLUMN: demand fell."),
        (320, 745, "RIGHT COLUMN: winter exception."),
        (38, 722, "Savings lasted through autumn."),
        (320, 722, "Weekends were not measured."),
        (38, 700, "Annual electricity use (kWh)"),
        (38, 677, "Site"), (170, 677, "Before (kWh)"), (350, 677, "After (kWh)"),
        (38, 654, "Library"), (170, 654, "1,200"), (350, 654, "984"),
        (38, 631, "Footnote: winter weekends excluded."),
    ]])
    blocks = extract_pdf_blocks(pdf)
    assert len(blocks) == 1 and blocks[0].locator == "page=1; layout-text"
    text = blocks[0].text
    assert re.search(r"(?m)^Site\s+Before \(kWh\)\s+After \(kWh\)$", text)
    assert re.search(r"(?m)^Library\s+1,200\s+984$", text)
    assert "winter weekends excluded" in text


def test_narrow_sidebar_does_not_interrupt_wider_article_abstract():
    pdf = _pdf([[
        (38, 745, "Publication metadata"), (250, 745, "Abstract"),
        (38, 710, "Citation: journal and editor."),
        (250, 710, "Urban forests cooled across 17 study groups."),
        (38, 685, "Funding: public grant only."),
        (250, 685, "Temperature fell 1.9 C across 3 km in Seoul."),
        (38, 660, "License: reuse permitted."),
        (250, 660, "The reduction did not hold for the lawn area."),
    ]])
    block = extract_pdf_blocks(pdf)[0]
    assert block.locator == "page=1; reading-order-text"
    assert block.text.index("Abstract") < block.text.index("Urban forests")
    assert block.text.index("Urban forests") < block.text.index("Temperature fell")
    assert block.text.index("Temperature fell") < block.text.index("Citation:")
    assert "did not hold for the lawn" in block.text


def test_scientific_signs_units_and_decimals_are_part_of_integrity_gate():
    original = "−1.9°C ± 0.3°C; p≥0.05; change was not +1.9°C"
    assert _same_visible_characters(original, "change was not +1.9°C; p≥0.05; −1.9°C ± 0.3°C")
    assert not _same_visible_characters(original, original.replace("−", ""))
    assert not _same_visible_characters(original, original.replace("±", ""))
    assert not _same_visible_characters(original, original.replace("°", ""))
    assert not _same_visible_characters(original, original.replace("≥", ""))
    assert not _same_visible_characters(original, original.replace("1.9", "19"))


def test_numbered_footnote_after_two_columns_remains_after_both():
    pdf = _pdf([[
        (38, 745, "LEFT: group size was 120 participants."),
        (320, 745, "RIGHT: winter was excluded from analysis."),
        (38, 720, "LEFT: benefit was -2.5 points overall."),
        (320, 720, "RIGHT: no estimate for rainy days."),
        (38, 600, "1. Footnote: instrument drift was corrected."),
    ]])
    block = extract_pdf_blocks(pdf)[0]
    assert block.locator == "page=1; reading-order-text"
    assert "-2.5 points" in block.text
    assert block.text.index("RIGHT: no estimate") < block.text.index("1. Footnote:")


def test_invalid_bbox_geometry_is_an_explicit_diagnostic():
    malformed = (
        b"<html xmlns='http://www.w3.org/1999/xhtml'><page width='612' height='792'>"
        b"<block xMin='NaN' yMin='1' xMax='10' yMax='20'><line><word>12</word>"
        b"</line></block></page></html>"
    )
    with pytest.raises(PDFExtractionError, match="geometry"):
        _bbox_pages(malformed)


def test_poppler_output_limit_is_enforced_during_capture(monkeypatch):
    monkeypatch.setattr(pdf_content, "_MAX_OUTPUT_BYTES", 64)
    pdf = _pdf([["A sufficiently long selectable line for the output limit check."]])
    with pytest.raises(PDFExtractionError, match="output exceeds parser limit"):
        pdf_content._run(pdf, "-bbox-layout")


def test_dense_geometry_preserves_layout_without_reorder_analysis(monkeypatch):
    boxes = b"".join(
        (f"<block xMin='10' yMin='{i}' xMax='20' yMax='{i + 1}'>"
         "<line><word>x</word></line></block>").encode()
        for i in range(501)
    )
    bbox = (
        b"<html xmlns='http://www.w3.org/1999/xhtml'><page width='612' height='792'>"
        + boxes + b"</page></html>"
    )
    monkeypatch.setattr(
        pdf_content, "_run", lambda _content, *options: bbox if "-bbox-layout" in options else b"Visible text\f"
    )
    monkeypatch.setattr(
        pdf_content, "_table_like", lambda *_args: pytest.fail("dense geometry analyzed")
    )
    assert extract_pdf_blocks(b"synthetic") == [
        pdf_content.PDFBlock("Visible text", "page=1; layout-text")
    ]


@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="Poppler rasterizer unavailable")
def test_blank_and_scan_pages_do_not_shift_physical_page_numbers(tmp_path):
    image = _raster_page(tmp_path, ["Printed only in pixels: 273 K."])
    pdf = _pdf([
        ["Selectable page one."], None, image, ["Selectable page four."],
    ])
    blocks = extract_pdf_blocks(pdf)
    assert [block.locator for block in blocks] == [
        "page=1; layout-text", "page=4; layout-text",
    ]
    assert "273 K" not in " ".join(block.text for block in blocks)
