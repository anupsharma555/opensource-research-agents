"""Focused checks for structure that should survive HTML content extraction."""

import pytest

from ora.evidence.html_content import HTMLExtractionError, extract_html_blocks


def _texts(source: str) -> list[str]:
    return [block.text for block in extract_html_blocks(source)]


def test_explicit_row_header_retains_first_column_label():
    texts = _texts(
        "<table><caption>Recall score</caption><tr><th>Group</th><th>Score</th></tr>"
        "<tr><th>Intervention</th><td>24</td></tr></table>"
    )
    assert texts == ["Table: Recall score | Row: Group: Intervention | Score: 24"]


def test_independent_articles_and_main_regions_are_all_preserved():
    texts = _texts(
        "<main><article><h1>First</h1><p>One finding.</p></article></main>"
        "<main><article><h1>Second</h1><p>Another finding.</p></article></main>"
    )
    assert texts == ["First", "One finding.", "Second", "Another finding."]
    assert _texts(
        "<main><h1>Primary</h1><p>Primary result.</p></main>"
        "<article><h1>Appendix</h1><p>Additional result.</p></article>"
    ) == ["Primary", "Primary result.", "Appendix", "Additional result."]


def test_hidden_ancestor_cannot_supply_content_root():
    texts = _texts(
        "<body><div hidden><main><p>Fabricated result.</p></main></div>"
        "<article><p>Visible result.</p></article></body>"
    )
    assert texts == ["Visible result."]


def test_inline_markup_and_generic_div_preserve_order_and_joined_tokens():
    texts = _texts(
        "<main><div>Value: <span>24</span> units</div>"
        "<p>CO<sub>2</sub> decreased; <a href='#method'>method</a> stated.</p></main>"
    )
    assert texts == ["Value: 24 units", "CO2 decreased; method stated."]


def test_meaningful_article_regions_are_retained_but_empty_shell_is_empty():
    assert _texts(
        "<main><header><h1>Methods</h1></header><aside>Scope note</aside>"
        "<footer>Author note</footer></main>"
    ) == ["Methods", "Scope note", "Author note"]
    assert not _texts(
        "<body><main><script>Fake result</script></main><footer>Subscribe</footer></body>"
    )


def test_hidden_table_cell_does_not_become_a_value():
    texts = _texts(
        "<main><table><tr><th>Group</th><th>Score</th></tr>"
        "<tr><th>Visible</th><td>24</td><td hidden>Fake 99</td></tr>"
        "</table></main>"
    )
    assert texts == ["Table | Row: Group: Visible | Score: 24"]


def test_blank_spanned_corner_does_not_displace_tiered_column_headers():
    texts = _texts(
        "<table><tr><td rowspan='2'></td><th colspan='2' scope='colgroup'>Mars</th>"
        "<th colspan='2' scope='colgroup'>Venus</th></tr>"
        "<tr><th scope='col'>Produced</th><th scope='col'>Sold</th>"
        "<th scope='col'>Produced</th><th scope='col'>Sold</th></tr>"
        "<tr><th scope='row'>Teddy Bears</th><td>50,000</td><td>30,000</td>"
        "<td>100,000</td><td>80,000</td></tr></table>"
    )
    assert "Table | Row: Teddy Bears | Mars / Produced: 50,000" in texts
    assert "Table | Row: Teddy Bears | Mars / Sold: 30,000" in texts
    assert "Table | Row: Teddy Bears | Venus / Produced: 100,000" in texts
    assert "Table | Row: Teddy Bears | Venus / Sold: 80,000" in texts


def test_explicit_headers_override_positional_column_guess():
    texts = _texts(
        "<table><tr><th id='control'>Control</th><th id='treatment'>Treatment</th></tr>"
        "<tr><td headers='treatment'>14</td><td headers='control'>9</td></tr></table>"
    )
    assert texts == ["Table | Treatment: 14", "Table | Control: 9"]


def test_standalone_section_link_is_content_but_top_jump_is_navigation():
    texts = _texts(
        "<main><p><a href='#limitations'>Study limitations</a></p>"
        "<p><a href='#top'>Back to top</a></p></main>"
    )
    assert texts == ["Study limitations"]


def test_excessive_depth_and_element_count_fail_explicitly():
    with pytest.raises(HTMLExtractionError, match="nesting"):
        extract_html_blocks("<div>" * 1_000 + "finding" + "</div>" * 1_000)
    with pytest.raises(HTMLExtractionError, match="element count"):
        extract_html_blocks("<main>" + "<br>" * 100_001 + "</main>")


def test_hidden_inline_list_content_never_leaks_into_visible_item():
    assert _texts(
        "<main><ul><li>Public <span hidden>Secret</span> value</li></ul></main>"
    ) == ["Public value"]


def test_hidden_table_section_cannot_supply_rows_or_headers():
    texts = _texts(
        "<table><thead hidden><tr><th>Secret header</th><th>Secret N</th></tr></thead>"
        "<tr><th>Arm</th><th>N</th></tr>"
        "<tbody hidden><tr><td>Secret arm</td><td>999</td></tr></tbody>"
        "<tbody><tr><td>Visible arm</td><td>12</td></tr></tbody></table>"
    )
    assert texts == ["Table | Row: Arm: Visible arm | N: 12"]


def test_nested_table_under_hidden_wrapper_is_not_swept_into_output():
    texts = _texts(
        "<main><table><tr><th>Container</th><th>Contents</th></tr>"
        "<tr><th>Visible box</th><td>One sleeve"
        "<div hidden><table><tr><th>Secret</th></tr><tr><td>999</td></tr></table></div>"
        "</td></tr></table></main>"
    )
    assert texts == ["Table | Row: Container: Visible box | Contents: One sleeve"]


def test_repeated_column_header_band_replaces_old_column_label():
    texts = _texts(
        "<table><tr><th>Arm</th><th>Count</th></tr>"
        "<tr><td>A</td><td>12</td></tr>"
        "<tr><th>Arm</th><th>Rate</th></tr>"
        "<tr><td>B</td><td>8%</td></tr></table>"
    )
    assert texts == [
        "Table | Row: Arm: A | Count: 12",
        "Table | Row: Arm: B | Rate: 8%",
    ]


def test_ambiguous_header_band_does_not_carry_old_label_forward():
    texts = _texts(
        "<table><tr><th>Arm</th><th>Count</th></tr><tr><td>A</td><td>12</td></tr>"
        "<tr><th colspan='2'>Revised measure</th></tr>"
        "<tr><td>B</td><td>8%</td></tr></table>"
    )
    assert "Table | Row: Arm: A | Count: 12" in texts
    assert any("Column 2: 8%" in text for text in texts)
    assert not any("Count: 8%" in text for text in texts)
