"""JATS extraction keeps authored scope, inline notation, and table relations."""

import pytest

from ora.evidence.intake import extract_blocks
from ora.evidence.library import EvidenceError
from ora.evidence.xml_content import XMLExtractionError, extract_xml_blocks


def _blocks(xml: bytes, *, partial: bool = False):
    blocks, parser = extract_blocks(
        xml, "application/jats+xml", allow_partial_xml=partial,
    )
    return blocks, parser


def _matching(blocks, phrase: str):
    return [block for block in blocks if phrase in block.text]


def test_abstract_and_body_sections_keep_distinct_context_and_restore_parent():
    xml = (
        b"<article><front><article-meta><abstract><sec><title>Methods</title>"
        b"<p>Abstract sample was 20.</p></sec></abstract></article-meta></front>"
        b"<body><sec><title>Results</title><p>Body sample was 40.</p>"
        b"<sec><title>Subgroup</title><p>Subgroup sample was 12.</p></sec>"
        b"<p>Parent result remained 40.</p></sec></body></article>"
    )
    blocks, _ = _blocks(xml)
    assert "Abstract" in _matching(blocks, "Abstract sample")[0].locator
    assert "Results" in _matching(blocks, "Body sample")[0].locator
    assert "Subgroup" in _matching(blocks, "Subgroup sample")[0].locator
    parent = _matching(blocks, "Parent result")
    assert len(parent) == 1 and "Results" in parent[0].locator
    assert "Subgroup" not in parent[0].locator


def test_inline_units_superscript_subscript_and_negation_keep_relation():
    xml = (
        b"<article><body><sec><title>Findings</title><p>"
        b"Dose was 10<sup>3</sup> cells/mL, not 10<sup>2</sup>; "
        b"CO<sub>2</sub> did not increase.</p></sec></body></article>"
    )
    blocks, _ = _blocks(xml)
    text = _matching(blocks, "Dose was")[0].text
    assert "10^3 cells/mL" in text
    assert "not 10^2" in text
    assert "CO_2 did not increase" in text


def test_namespaced_table_values_carry_caption_headers_row_and_note_once():
    xml = (
        b"<j:article xmlns:j='urn:jats'><j:body><j:sec><j:title>Results</j:title>"
        b"<j:table-wrap><j:label>Table 1</j:label>"
        b"<j:caption><j:p>Response by arm</j:p></j:caption>"
        b"<j:table><j:thead><j:tr><j:th>Arm</j:th><j:th>Improved</j:th>"
        b"<j:th>Unchanged</j:th></j:tr></j:thead><j:tbody>"
        b"<j:tr><j:th scope='row'>Treatment</j:th><j:td>12</j:td>"
        b"<j:td>8</j:td></j:tr></j:tbody></j:table>"
        b"<j:table-wrap-foot><j:p>Per-protocol analysis.</j:p></j:table-wrap-foot>"
        b"</j:table-wrap></j:sec></j:body></j:article>"
    )
    blocks, _ = _blocks(xml)
    improved = _matching(blocks, "Improved: 12")
    assert len(improved) == 1
    assert "Response by arm" in improved[0].text and "Treatment" in improved[0].text
    assert "Unchanged: 8" not in improved[0].text
    assert sum("Per-protocol analysis." in block.text for block in blocks) == 1
    assert any("Table 1" in block.text for block in blocks)


def test_partial_xml_exposes_only_closed_paragraph_and_retains_legacy_version():
    xml = (
        b"<article><body><sec><title>Results</title>"
        b"<p>Verified value was 12.</p><p>Unfinished"
    )
    blocks, parser = _blocks(xml, partial=True)
    assert parser == "stdlib-elementtree-partial"
    assert any(block.text == "Verified value was 12." for block in blocks)
    assert all("Unfinished" not in block.text for block in blocks)


def test_table_alternative_uses_structured_formula_and_retains_graphic_reference():
    xml = (
        b"<article><body><sec><title>Analysis</title><table-wrap>"
        b"<label>Table 2</label><caption><p>Cooling formulas</p></caption>"
        b"<alternatives><graphic xmlns:xlink='http://www.w3.org/1999/xlink' "
        b"xlink:href='info:doi/10.0000/synthetic.t2'/>"
        b"<table><tr><th>Name</th><th>Formula</th></tr>"
        b"<tr><td>T<sub>c</sub></td>"
        b"<td>T<sub>Con</sub>&#x2013;T<sub>AWS</sub></td></tr></table></alternatives>"
        b"<table-wrap-foot><p>AWS: reference station.</p></table-wrap-foot>"
        b"</table-wrap></sec></body></article>"
    )
    blocks, _ = _blocks(xml)
    formula = _matching(blocks, "Formula: T_{Con}–T_{AWS}")
    assert len(formula) == 1
    assert "Row: Name: T_c" in formula[0].text
    assert "Table 2. Cooling formulas" in formula[0].text
    assert sum("AWS: reference station." in block.text for block in blocks) == 1
    assert any("info:doi/10.0000/synthetic.t2" in block.text for block in blocks)


def test_break_inside_table_cell_separates_two_authored_items():
    xml = (
        b"<article><body><table-wrap><table><tr><th>Species</th></tr>"
        b"<tr><td><italic>Oak</italic><break/><italic>Pine</italic>"
        b"</td></tr></table></table-wrap></body></article>"
    )
    blocks, _ = _blocks(xml)
    assert any("Oak; Pine" in block.text for block in blocks)


def test_unstructured_abstract_and_citation_survive_without_body_duplication():
    xml = (
        b"<article><front><article-meta><abstract>Response was <bold>lower</bold>."
        b"</abstract></article-meta></front><body><p>Body response was 12.</p></body>"
        b"<back><ref-list><ref><mixed-citation>Author. Study. 2025."
        b"</mixed-citation></ref></ref-list></back></article>"
    )
    blocks, _ = _blocks(xml)
    assert sum("Response was lower." in block.text for block in blocks) == 1
    assert any("section=Abstract" in block.locator for block in _matching(blocks, "lower"))
    assert any("Body response was 12." in block.text for block in blocks)
    assert any("Author. Study. 2025." in block.text for block in blocks)


def test_complete_xml_depth_limit_fails_explicitly():
    xml = b"<root>" + b"<sec>" * 250 + b"x" + b"</sec>" * 250 + b"</root>"
    with pytest.raises(XMLExtractionError, match="parser limit"):
        extract_xml_blocks(xml)
    with pytest.raises(EvidenceError, match="parser limit"):
        extract_blocks(xml, "application/jats+xml", allow_partial_xml=True)


def test_colspan_value_names_every_covered_column_without_false_single_arm():
    xml = (
        b"<article><body><table-wrap><caption>Area</caption><table>"
        b"<tr><th>Measure</th><th>Ti</th><th>Park</th><th>Forest</th></tr>"
        b"<tr><th scope='row'>Forestland area</th><td colspan='3'>100</td></tr>"
        b"</table></table-wrap></body></article>"
    )
    blocks, _ = _blocks(xml)
    value = _matching(blocks, ": 100")
    assert len(value) == 1
    assert "Row: Measure: Forestland area" in value[0].text
    assert "Ti / Park / Forest: 100" in value[0].text


def test_rowspan_rowgroup_header_applies_to_both_distinct_rows():
    xml = (
        b"<article><body><table><thead><tr><th>Site</th><th>Season</th>"
        b"<th>Count</th></tr></thead><tbody>"
        b"<tr><th rowspan='2' scope='rowgroup'>North</th>"
        b"<th scope='row'>Spring</th><td>12</td></tr>"
        b"<tr><th scope='row'>Autumn</th><td>14</td></tr>"
        b"</tbody></table></body></article>"
    )
    blocks, _ = _blocks(xml)
    spring = _matching(blocks, "Count: 12")
    autumn = _matching(blocks, "Count: 14")
    assert len(spring) == len(autumn) == 1
    assert "North" in spring[0].text and "Spring" in spring[0].text
    assert "North" in autumn[0].text and "Autumn" in autumn[0].text
    assert "Spring" not in autumn[0].text and "Autumn" not in spring[0].text
