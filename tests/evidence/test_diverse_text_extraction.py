"""Offline source-reader checks across unlike, synthetic document structures."""

from ora.evidence.intake import extract_blocks


def test_technical_code_and_parameter_definition_survive_together():
    html = ("<main><h1>Array transform</h1><pre><code>const values = [2, 3];\n"
            "const doubled = values.map((x) => x * 2);\n"
            "// [4, 6]</code></pre><dl><dt>callbackFn</dt>"
            "<dd>Called only for assigned indexes.</dd></dl></main>").encode()
    blocks, parser = extract_blocks(html, "text/html")
    assert parser == "stdlib-htmlparser-structured-v5"
    assert any("const doubled = values.map((x) => x * 2);\n// [4, 6]" in b.text
               and "section=Array transform" in b.locator for b in blocks)
    assert any("callbackFn" in b.text and "Called only for assigned indexes." in b.text
               for b in blocks)


def test_institution_caption_retains_number_and_object_in_one_block():
    html = ("<article><h1>Planet</h1><figure><img alt='Planet illustration'>"
            "<figcaption>Planet Q has a mass 0.28 times its star's reference mass "
            "and orbits at 0.0486 distance units.</figcaption></figure></article>").encode()
    blocks, _ = extract_blocks(html, "text/html")
    assert any("Planet Q" in b.text and "0.28 times its star's reference mass" in b.text
               and "0.0486 distance units" in b.text for b in blocks)


def test_calendar_row_values_keep_year_date_and_holiday_association():
    html = ("<main><h1>Holidays</h1><table><caption>Region A 2027</caption>"
            "<tr><th>Date</th><th>Day</th><th>Holiday</th></tr>"
            "<tr><th scope='row'>28 December</th><td>Tuesday</td>"
            "<td>Founders Day (substitute day)</td></tr></table></main>").encode()
    blocks, _ = extract_blocks(html, "text/html")
    same_row = [b.text for b in blocks if "Region A 2027" in b.text
                and "Row: Date: 28 December" in b.text]
    assert len(same_row) == 2
    assert any("Day: Tuesday" in text for text in same_row)
    assert any("Holiday: Founders Day (substitute day)" in text for text in same_row)
    assert all(not ("Day: Tuesday" in text and "Founders Day" in text)
               for text in same_row)


def test_reference_negation_and_following_clause_remain_together():
    html = ("<article><h1>Urban heat</h1><p>Climate change is <em>not</em> "
            "the cause of urban heat islands, but it amplifies heat waves.</p></article>").encode()
    blocks, _ = extract_blocks(html, "text/html")
    assert any("is not the cause" in b.text and "but it amplifies heat waves" in b.text
               for b in blocks)


def test_jats_table_formula_and_plain_utf8_control():
    xml = (b"<article><body><sec><title>Methods</title><table-wrap>"
           b"<caption><p>Cooling formula</p></caption><table>"
           b"<tr><th>Name</th><th>Formula</th></tr><tr><th>T<sub>x</sub></th>"
           b"<td>T<sub>Test</sub>&#x2013;T<sub>Ref</sub></td></tr>"
           b"</table></table-wrap></sec></body></article>")
    blocks, parser = extract_blocks(xml, "application/xml")
    assert parser == "stdlib-elementtree-structured-v2"
    assert any("Cooling formula" in b.text and "T_x" in b.text
               and "T_{Test}–T_{Ref}" in b.text for b in blocks)
    plain = "Climate change is not the cause.\nResponse: 1.9°C over ~3 km."
    text_blocks, text_parser = extract_blocks(plain.encode("utf-8-sig"), "text/plain")
    assert text_parser == "utf8-text"
    assert text_blocks[0].text == plain
    assert text_blocks[0].locator == f"text; chars=0:{len(plain)}"
