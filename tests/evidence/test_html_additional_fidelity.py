"""Additional HTML source structures tested against their authored relationships."""

from ora.evidence.html_content_v4 import extract_html_blocks


def _texts(source: str) -> list[str]:
    return [block.text for block in extract_html_blocks(source)]


def test_definition_terms_stay_attached_to_their_own_definitions():
    texts = _texts(
        "<main><h2>Measures</h2><dl>"
        "<dt>Adjusted risk</dt><dd>Risk after age adjustment.</dd>"
        "<dt>Crude risk</dt><dd>Unadjusted estimate.</dd>"
        "</dl></main>"
    )
    assert any("Adjusted risk" in value and "Risk after age adjustment" in value for value in texts)
    assert any("Crude risk" in value and "Unadjusted estimate" in value for value in texts)
    assert not any("Adjusted risk" in value and "Unadjusted estimate" in value for value in texts)


def test_table_cell_keeps_inline_numeric_qualifier_with_its_headers():
    texts = _texts(
        "<main><table><caption>Response</caption><tr><th>Arm</th><th>Result</th></tr>"
        "<tr><th>Treatment</th><td><em>12 </em>of <strong>20</strong> improved</td></tr>"
        "</table></main>"
    )
    assert any(
        "Response" in value and "Treatment" in value and "Result: 12 of 20 improved" in value
        for value in texts
    )


def test_figure_caption_survives_between_surrounding_paragraphs():
    texts = _texts(
        "<main><p>Before the intervention.</p><figure><img alt='Graph'>"
        "<figcaption>Figure 1. Mean score was 12.</figcaption></figure>"
        "<p>After the intervention.</p></main>"
    )
    assert texts == [
        "Before the intervention.",
        "Figure 1. Mean score was 12.",
        "After the intervention.",
    ]


def test_details_summary_and_explanation_keep_a_readable_boundary():
    texts = _texts(
        "<main><details open><summary>Methods caveat</summary>"
        "<p>Missing values were excluded.</p></details></main>"
    )
    assert texts == ["Methods caveat", "Missing values were excluded."]


def test_css_preformatted_content_keeps_authored_line_breaks():
    texts = _texts(
        "<main><div style='white-space: pre-wrap'>"
        "alpha = 1\nbeta = 2\n# note"
        "</div></main>"
    )
    assert texts == ["alpha = 1\nbeta = 2\n# note"]


def test_responsive_variants_keep_unique_qualifiers_without_global_deduplication():
    texts = _texts(
        "<main><section class='desktop'><h2>Overview</h2>"
        "<p>12 improved.</p><p>Adults only.</p></section>"
        "<section class='mobile'><h2>Overview</h2>"
        "<p>12 improved.</p><p>Adolescents included.</p></section></main>"
    )
    assert "Adults only." in texts
    assert "Adolescents included." in texts
    assert texts.count("12 improved.") == 2


def test_inline_line_break_separates_prose_without_splitting_subscript():
    texts = _texts("<main><p>First line<br>CO<sub>2</sub> on second line.</p></main>")
    assert texts == ["First line CO2 on second line."]


def test_multiple_terms_and_definitions_share_only_their_own_group():
    texts = _texts(
        "<main><dl><div><dt>ET</dt><dt>Evapotranspiration</dt>"
        "<dd>Evaporation plus transpiration.</dd><dd>Measured in millimeters.</dd></div>"
        "<div><dt>Runoff</dt><dd>Water flowing over land.</dd></div></dl></main>"
    )
    assert len(texts) == 2
    assert all(term in texts[0] for term in (
        "ET", "Evapotranspiration", "Evaporation plus transpiration.",
        "Measured in millimeters.",
    ))
    assert "Runoff" in texts[1] and "Water flowing over land." in texts[1]
    assert "Runoff" not in texts[0]


def test_closed_details_retains_source_body_but_keeps_summary_separate():
    texts = _texts(
        "<main><details><summary>Analysis note</summary>"
        "<p>Unblinded reviewers assessed outcomes.</p></details></main>"
    )
    assert texts == ["Analysis note", "Unblinded reviewers assessed outcomes."]


def test_inline_preformatted_values_and_explicit_normal_override():
    texts = _texts(
        "<main><div style='white-space: pre'>a  b\nc</div>"
        "<div style='white-space: break-spaces !important'>x  y\nz</div>"
        "<div style='white-space: pre-wrap'>outer\n"
        "<p style='white-space: normal'>normal   prose\ncontinues</p></div>"
        "<div class='pre-wrap'>class only\nno inline rule</div></main>"
    )
    assert texts == [
        "a  b\nc", "x  y\nz", "outer\n",
        "normal prose continues", "class only no inline rule",
    ]


def test_definition_with_paragraphs_and_nested_list_keeps_word_boundaries():
    texts = _texts(
        "<main><dl><dt>Adjusted risk</dt><dd>"
        "<p>Age adjusted.</p><p>Population weighted.</p>"
        "<ul><li>Adults</li><li>Children</li></ul>"
        "</dd></dl></main>"
    )
    assert texts == [
        "Term: Adjusted risk | Definition: Age adjusted. Population weighted. Adults Children"
    ]


def test_inline_important_wins_and_invalid_later_value_does_not_override():
    texts = _texts(
        "<main><div style='white-space: pre !important; white-space: normal'>"
        "a  b\nc</div>"
        "<div style='white-space: pre-wrap; white-space: invalid-value'>"
        "x  y\nz</div>"
        "<div style='white-space: pre; white-space: normal !important'>"
        "normal  spacing\ncontinues</div></main>"
    )
    assert texts == ["a  b\nc", "x  y\nz", "normal spacing continues"]
