"""Synthetic HTML extraction checks for source meaning, not exact rendering.

These are local parser checks. They do not establish source credibility or that a
model selected and interpreted the right passage in a live research run.
"""

from pathlib import Path

import pytest

from ora.evidence.intake import extract_blocks

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "html_fidelity"


def _blocks(name: str):
    blocks, _parser = extract_blocks((FIXTURES / name).read_bytes(), "text/html")
    return blocks


def _inline(source: str):
    blocks, _parser = extract_blocks(source.encode(), "text/html")
    return blocks


def _text(blocks) -> str:
    return " ".join(block.text for block in blocks)


def _matching(blocks, value: str):
    return [block for block in blocks if value in block.text]


def test_semantic_article_retains_research_prose_and_removes_site_chrome():
    blocks = _blocks("semantic_article.html")
    text = _text(blocks)
    for retained in (
        "Finding records in a local archive",
        "search index to identify 24 records",
        "navigation method was described",
        "18 contained complete dates",
    ):
        assert retained in text
    for chrome in (
        "Site-wide catalog", "Search this site", "Browse catalog",
        "Help center", "Related pages", "Subscribe for site updates",
    ):
        assert chrome not in text


def test_legacy_ids_keep_main_body_and_drop_navigation_even_without_nav_tags():
    blocks = _blocks("legacy_article.html")
    text = _text(blocks)
    for retained in (
        "Preservation observations", "16 of 20 sample sheets",
        "Primary outcome", "Eight of those sheets", "Secondary outcome",
        "search navigation was evaluated separately",
    ):
        assert retained in text
    for chrome in (
        "Catalog topics", "Digital collections", "Visitor help",
        "Browse by category", "Search all collections", "On this page",
        "Previous page", "Connect and subscribe",
    ):
        assert chrome not in text


def test_nested_list_retains_parent_child_relationship_without_merging_siblings():
    blocks = _blocks("legacy_article.html")
    parent = _matching(blocks, "Primary outcome")
    children = _matching(blocks, "Eight of those sheets")
    assert parent and children
    assert "16 sheets" in _text(parent)
    assert "no visible discoloration" in _text(children)
    assert "four sheets required repair" in _text(blocks)
    assert not any(
        "Primary outcome" in block.text and "Secondary outcome" in block.text
        for block in blocks
    )


@pytest.mark.parametrize("value,header,row,other_header", [
    ("Flat archival sleeve", "Preferred", "Paper", "Acceptable"),
    ("Buffered folder", "Acceptable", "Paper", "Preferred"),
    ("Ventilated container", "Preferred", "Film", "Acceptable"),
    ("Cool dry cabinet", "Acceptable", "Film", "Preferred"),
])
def test_table_values_carry_applicable_header_and_row_label(
    value, header, row, other_header,
):
    blocks = _blocks("tables.html")
    matching = _matching(blocks, value)
    assert matching
    assert any(header in block.text and row in block.text for block in matching)
    assert all(other_header not in block.text for block in matching)
    assert any("Suggested handling by material" in block.text for block in matching)
    assert "recommendations assume a dry storage room" in _text(blocks)


@pytest.mark.parametrize("value,season,site,other", [
    ("12", "Spring", "North room", "Autumn"),
    ("14", "Autumn", "North room", "Spring"),
    ("9", "Spring", "South room", "Autumn"),
    ("11", "Autumn", "South room", "Spring"),
])
def test_spanned_table_preserves_values_with_correct_context(value, season, site, other):
    blocks = _blocks("tables.html")
    matching = [block for block in blocks if value in block.text.split()]
    assert matching
    assert any(season in block.text and site in block.text for block in matching)
    assert not any(
        season in block.text and site in block.text and other in block.text
        for block in matching
    )


def test_nested_table_does_not_inherit_outer_headers():
    blocks = _blocks("tables.html")
    inner = _matching(blocks, "Fragile item")
    assert inner
    assert any("X1" in block.text and "Meaning" in block.text for block in inner)
    assert all("Container" not in block.text and "Contents" not in block.text for block in inner)
    assert "One sleeve" in _text(blocks)


def test_optional_closing_tags_do_not_merge_rows_or_list_siblings():
    blocks = _inline("""<main><h1>Assessment</h1>
      <ul><li>First stage had 12 records<li>Second stage had 9 records</ul>
      <table><tr><th>Group<th>Count<tr><td>A<td>12<tr><td>B<td>9</table>
      </main>""")
    assert "First stage had 12 records" in _text(blocks)
    assert "Second stage had 9 records" in _text(blocks)
    assert not any("First stage" in b.text and "Second stage" in b.text for b in blocks)
    assert any("Group" in b.text and "A" in b.text and "12" in b.text for b in blocks)
    assert any("Group" in b.text and "B" in b.text and "9" in b.text for b in blocks)


def test_entities_and_inline_markup_keep_word_spacing_and_symbols():
    blocks = _inline("""<main><p>Five&nbsp;<strong>samples</strong> had a 6&nbsp;%
      change &amp; <em>remained</em> stable.</p></main>""")
    text = _text(blocks)
    assert "Five samples" in text
    assert "6 %" in text
    assert "change & remained stable" in text
    assert "&amp;" not in text


def test_hidden_or_script_only_shell_does_not_create_source_passages():
    blocks = _inline("""<html><head><script>var article = 'Fabricated finding';</script>
      <style>.result { color: red }</style></head><body>
      <nav><p>Browse collections</p></nav>
      <div hidden><p>Unrendered draft result</p></div>
      <div aria-hidden="true"><p>Offscreen helper result</p></div>
      <form role="search"><label>Search collections</label></form>
      <main id="app"></main></body></html>""")
    assert not blocks
