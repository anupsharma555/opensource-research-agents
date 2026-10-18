"""Saved complete and partial XML copies retain their original parser mapping."""

from ora.evidence import intake

XML = (
    b"<article><body><sec><title>Results</title><p>12 improved.</p>"
    b"<table-wrap><caption>Response</caption><table>"
    b"<tr><th>Arm</th><th>Improved</th></tr>"
    b"<tr><th>Treatment</th><td>12</td></tr>"
    b"</table></table-wrap></sec></body></article>"
)


def test_retained_complete_xml_uses_legacy_blocks_and_new_copy_uses_structured_blocks():
    old, old_version = intake.extract_blocks(
        XML, "application/jats+xml", xml_parser_version="stdlib-elementtree",
    )
    new, new_version = intake.extract_blocks(XML, "application/jats+xml")
    assert old_version == "stdlib-elementtree"
    assert new_version == intake.XML_PARSER_VERSION
    assert any(block.text == "12" for block in old)
    assert any("Improved: 12" in block.text and "Treatment" in block.text for block in new)
    retained = {
        "mime_type": "application/jats+xml",
        "mapping_version": f"{intake.PARSER_VERSION}:{old_version}:partial_xml=False",
    }
    assert intake.extract_copy_blocks(XML, retained) == (old, old_version)


def test_retained_partial_xml_keeps_closed_legacy_span_only():
    raw = b"<article><sec><title>Results</title><p>12 improved.</p><p>unfinished"
    retained = {
        "mime_type": "application/jats+xml",
        "mapping_version": f"{intake.PARSER_VERSION}:stdlib-elementtree-partial:partial_xml=True",
    }
    blocks, version = intake.extract_copy_blocks(raw, retained)
    assert version == "stdlib-elementtree-partial"
    assert any(block.text == "12 improved." for block in blocks)
    assert all("unfinished" not in block.text for block in blocks)
