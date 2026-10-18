"""Portable cross-layout cases for literal source extraction, without model calls."""

import shutil

import pytest

from ora.evidence.intake import extract_blocks
from tests.evidence.test_source_format_walkthroughs import _pdf


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="Poppler unavailable")
def test_pdf_repeated_number_on_separate_physical_pages_keeps_qualifier_scope():
    source = _pdf([
        ["Trial A: change was -2.5 points at six months."],
        ["Trial B: change was +2.5 points at six months.",
         "Trial B qualification: no estimate for winter."],
    ])
    blocks, parser = extract_blocks(source, "application/pdf")
    assert parser == "poppler-bbox-layout-reading-order-v2"
    assert [block.locator.split(";")[0] for block in blocks] == ["page=1", "page=2"]
    assert "-2.5 points" in blocks[0].text and "+2.5 points" not in blocks[0].text
    assert "+2.5 points" in blocks[1].text and "no estimate for winter" in blocks[1].text


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="Poppler unavailable")
def test_pdf_separate_table_rows_keep_comparison_and_units_on_same_lines():
    source = _pdf([[
        (38, 745, "Observed concentration (mg/L)"),
        (38, 720, "Group"), (180, 720, "Before (mg/L)"), (360, 720, "After (mg/L)"),
        (38, 696, "Exposed"), (180, 696, "1.20"), (360, 696, "0.90"),
        (38, 672, "Control"), (180, 672, "0.80"), (360, 672, "0.85"),
        (38, 640, "Note: missing winter samples were excluded."),
    ]])
    blocks, _ = extract_blocks(source, "application/pdf")
    assert len(blocks) == 1 and blocks[0].locator == "page=1; layout-text"
    lines = blocks[0].text.splitlines()
    assert any("Exposed" in line and "1.20" in line and "0.90" in line for line in lines)
    assert any("Control" in line and "0.80" in line and "0.85" in line for line in lines)
    assert "missing winter samples were excluded" in blocks[0].text


def test_html_distinct_cells_retain_shared_table_row_identity():
    source = ("<main><h1>Schedule</h1><table><caption>Region A 2027</caption>"
              "<thead><tr><th>Date</th><th>Day of the week</th><th>Event</th></tr></thead>"
              "<tbody><tr><th scope='row'>26 March</th><td>Friday</td>"
              "<td>Good Friday</td></tr></tbody></table></main>").encode()
    blocks, parser = extract_blocks(source, "text/html")
    assert parser == "stdlib-htmlparser-structured-v5"
    weekday = next(block for block in blocks if "Day of the week: Friday" in block.text)
    holiday = next(block for block in blocks if "Event: Good Friday" in block.text)
    for block in (weekday, holiday):
        assert "Table: Region A 2027" in block.text
        assert "Row: Date: 26 March" in block.text
    assert weekday.locator != holiday.locator


def test_xml_table_note_is_retained_without_inventing_a_second_result():
    source = (b"<article><body><sec><title>Results</title><table-wrap>"
              b"<label>Table 1</label><caption><title>Change by group.</title></caption>"
              b"<table><thead><tr><th>Group</th><th>Change (mg/L)</th></tr></thead>"
              b"<tbody><tr><td>A</td><td>-0.30</td></tr></tbody></table>"
              b"<table-wrap-foot><p>Winter samples were excluded.</p></table-wrap-foot>"
              b"</table-wrap></sec></body></article>")
    blocks, parser = extract_blocks(source, "application/xml")
    assert parser == "stdlib-elementtree-structured-v2"
    rows = [block for block in blocks if "-0.30" in block.text]
    assert len(rows) == 1
    assert "Change by group" in rows[0].text and "Change (mg/L)" in rows[0].text
    assert any("Winter samples were excluded" in block.text for block in blocks)
