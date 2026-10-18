"""V3 HTML reader checks for source meaning and control separation."""

from ora.evidence.html_content_v3 import HTML_PARSER_VERSION, extract_html_blocks


def _texts(source: str) -> list[str]:
    return [block.text for block in extract_html_blocks(source)]


def test_v3_version_and_preformatted_code_line_breaks():
    assert HTML_PARSER_VERSION == "stdlib-htmlparser-structured-v3"
    texts = _texts(
        "<main><h1>Example</h1><pre><code>const n = 1;\n"
        "// Explain next line\nconst mapped = n + 1;\n</code></pre></main>"
    )
    assert texts == ["Example", "const n = 1;\n// Explain next line\nconst mapped = n + 1;\n"]


def test_pre_preserves_edge_newlines_except_the_single_html_leading_lf():
    assert _texts("<pre>\n\n  first\n  second\n\n</pre>") == [
        "\n  first\n  second\n\n",
    ]
    assert _texts("<pre><code>\n  first\n</code></pre>") == ["\n  first\n"]
    assert _texts("<pre>\r\n\r\n  first\r\n</pre>") == ["\n  first\n"]


def test_inline_boundaries_keep_authored_space_without_splitting_subscript():
    texts = _texts(
        "<main><p><em>Evapo- </em>for evaporation; "
        "<em>-transpiration </em>for transpiration; CO<sub>2</sub>.</p></main>"
    )
    assert texts == ["Evapo- for evaporation; -transpiration for transpiration; CO2."]


def test_structural_link_controls_are_skipped_but_prose_and_aside_survive():
    texts = _texts(
        "<main><header><h1>Study</h1></header>"
        "<div class='article-scroll-wrapper'><h2>Contents</h2>"
        "<ul><li><a href='#methods'>Methods</a></li>"
        "<li><a href='#results'>Results</a></li></ul></div>"
        "<p>The navigation method was measured.</p>"
        "<aside><p>Important limitation: only one site.</p></aside>"
        "<h2 id='methods'>Methods</h2><p>Preserved methods.</p></main>"
    )
    assert texts == [
        "Study", "The navigation method was measured.",
        "Important limitation: only one site.", "Methods", "Preserved methods.",
    ]


def test_menu_and_feedback_clusters_are_excluded_by_structure():
    texts = _texts(
        "<main><h1>Finding</h1><div class='vector-dropdown'>"
        "<input type='checkbox'><ul><li><a href='/af'>Afrikaans</a></li>"
        "<li><a href='/es'>Español</a></li><li><a href='/fr'>Français</a></li></ul></div>"
        "<p>Result: 24 records.</p><aside><ul>"
        "<li>Relevant?<button>Yes</button><button>No</button></li>"
        "<li>Helpful?<button>Yes</button><button>No</button></li>"
        "</ul></aside></main>"
    )
    assert texts == ["Finding", "Result: 24 records."]


def test_repeated_research_sentence_is_not_globally_deduplicated():
    texts = _texts(
        "<main><section><h2>Experiment 1</h2><p>Mean value was 12.</p></section>"
        "<section><h2>Experiment 2</h2><p>Mean value was 12.</p></section></main>"
    )
    assert texts == ["Experiment 1", "Mean value was 12.", "Experiment 2", "Mean value was 12."]


def test_dynamic_loading_placeholder_is_not_source_content():
    assert _texts(
        "<main><ul><li id='loadingMetrics'><p>Loading metrics</p></li></ul>"
        "<h1>Published finding</h1><p>The result was 12.</p></main>"
    ) == ["Published finding", "The result was 12."]


def test_reference_section_with_citation_prose_is_not_treated_as_toc():
    texts = _texts(
        "<main><div class='toc-section'><h2>References</h2><ol>"
        "<li>Smith et al. (2025). Study of urban trees. Journal 12:24. "
        "<a href='https://example.org/a'>DOI</a> "
        "<a href='https://example.org/b'>Abstract</a> "
        "<a href='https://example.org/c'>Full text</a></li>"
        "</ol></div></main>"
    )
    assert "References" in texts
    assert any("Study of urban trees" in value for value in texts)


def test_fragment_footnotes_do_not_hide_substantive_list_items():
    texts = _texts(
        "<main><ul><li>Yield fell 5% (<a href='#one'>1</a>)</li>"
        "<li>Quality improved (<a href='#two'>2</a>)</li></ul></main>"
    )
    assert texts == ["Yield fell 5% (1)", "Quality improved (2)"]


def test_feedback_list_does_not_hide_independent_aside_caveat():
    texts = _texts(
        "<main><aside><p>Limitation: measurements came from one site.</p>"
        "<ul><li>Useful?<button>Yes</button><button>No</button></li>"
        "<li>Correct?<button>Yes</button><button>No</button></li></ul>"
        "</aside></main>"
    )
    assert texts == ["Limitation: measurements came from one site."]
