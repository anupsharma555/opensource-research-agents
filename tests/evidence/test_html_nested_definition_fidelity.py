"""New HTML copies preserve authored structure inside description definitions."""

from ora.evidence.intake import extract_blocks, extract_copy_blocks


def _read(source: str):
    return extract_blocks(source.encode(), "text/html")


def test_nested_pre_keeps_lines_indentation_and_owning_term():
    blocks, parser = _read(
        "<main><h2>Examples</h2><dl><dt>Writer</dt><dd>"
        "<p>Use the writer for records.</p><div class='highlight'><pre><code>"
        "import csv\nwith open('records.csv') as stream:\n"
        "    writer = csv.writer(stream)\n</code></pre></div>"
        "<p>Close the stream after writing.</p></dd>"
        "<dt>Reader</dt><dd>Reads records only.</dd></dl></main>"
    )
    assert parser == "stdlib-htmlparser-structured-v5"
    code = [block for block in blocks if "writer = csv.writer(stream)" in block.text]
    assert len(code) == 1
    assert "Term: Writer" in code[0].text
    assert "import csv\nwith open('records.csv') as stream:\n    writer" in code[0].text
    assert any("Term: Writer" in block.text and "Close the stream" in block.text
               for block in blocks)
    assert not any("Term: Reader" in block.text and "writer = csv.writer" in block.text
                   for block in blocks)


def test_nested_list_and_table_retain_local_relationships():
    blocks, _ = _read(
        "<main><dl><dt>Outcome</dt><dd><p>Two study arms.</p>"
        "<ul><li>Follow-up<ul><li>Winter only</li></ul></li></ul>"
        "<table><caption>Change, mg/L</caption><tr><th>Arm</th><th>Change</th></tr>"
        "<tr><th scope='row'>A</th><td>-0.30</td></tr></table>"
        "</dd><dt>Other</dt><dd>Separate result.</dd></dl></main>"
    )
    assert any("Term: Outcome" in block.text and "Follow-up > Winter only" in block.text
               for block in blocks)
    assert any("Term: Outcome" in block.text and "Table: Change, mg/L" in block.text
               and "Row: Arm: A" in block.text and "Change: -0.30" in block.text
               for block in blocks)
    assert not any("Term: Other" in block.text and "-0.30" in block.text
                   for block in blocks)


def test_plain_definition_groups_keep_term_scope_without_extra_blocks():
    blocks, _ = _read(
        "<main><dl><div><dt>ET</dt><dt>Evapotranspiration</dt>"
        "<dd>Evaporation plus transpiration.</dd><dd>Measured in millimeters.</dd></div>"
        "<div><dt>Runoff</dt><dd>Water flowing over land.</dd></div></dl></main>"
    )
    assert len(blocks) == 2
    assert "Evapotranspiration" in blocks[0].text
    assert "Measured in millimeters" in blocks[0].text
    assert "Runoff" in blocks[1].text and "Runoff" not in blocks[0].text


def test_code_inside_definition_list_item_stays_preformatted():
    blocks, _ = _read(
        "<main><dl><dt>Procedure</dt><dd><ul><li>Run this example:"
        "<pre>step_one()\n    step_two()</pre>"
        "<ul><li>Check the result.</li></ul></li></ul></dd>"
        "<dt>Other</dt><dd>Different procedure.</dd></dl></main>"
    )
    code = [block for block in blocks if "step_two()" in block.text]
    assert len(code) == 1
    assert "Term: Procedure" in code[0].text
    assert "step_one()\n    step_two()" in code[0].text
    assert any("Run this example: > Check the result." in block.text for block in blocks)
    assert not any("Term: Other" in block.text and "step_one()" in block.text
                   for block in blocks)


def test_code_inside_other_definition_wrapper_is_not_flattened():
    blocks, _ = _read(
        "<main><dl><dt>Analysis</dt><dd><section class='example'>"
        "<pre>estimate = -0.30\n    units = 'mg/L'</pre>"
        "</section></dd></dl></main>"
    )
    assert any("Term: Analysis" in block.text
               and "estimate = -0.30\n    units = 'mg/L'" in block.text
               for block in blocks)


def test_retained_v4_mapping_replays_old_flattened_definition():
    source = ("<main><dl><dt>Writer</dt><dd><p>Example:</p>"
              "<pre>import csv\n    use(csv)</pre></dd></dl></main>").encode()
    legacy, old_version = extract_blocks(
        source, "text/html", html_parser_version="stdlib-htmlparser-structured-v4",
    )
    current, new_version = extract_blocks(source, "text/html")
    assert "import csv use(csv)" in legacy[0].text
    assert any("import csv\n    use(csv)" in block.text for block in current)
    assert old_version == "stdlib-htmlparser-structured-v4"
    assert new_version == "stdlib-htmlparser-structured-v5"
    copy_body = {"mime_type": "text/html",
                 "mapping_version": f"ora-intake-v1:{old_version}"}
    assert extract_copy_blocks(source, copy_body) == (legacy, old_version)
