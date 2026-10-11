"""Scholarly abstract cleanup keeps scientific comparisons and paragraph structure."""

import pytest

from ora.retrieval.providers import _crossref_item, _epmc_item


def mapped_abstract(route, text):
    if route == 'europe_pmc':
        return _epmc_item({'source': 'MED', 'id': '123', 'abstractText': text})['content']
    return _crossref_item({'DOI': '10.1234/markup', 'title': ['Example trial'],
                          'abstract': text})['content']


@pytest.mark.parametrize('route', ['europe_pmc', 'crossref'])
@pytest.mark.parametrize('threshold', ['< 8', '&lt; 8'])
def test_comparison_and_following_results_survive_markup(route, threshold):
    text = (f'<h4>Results</h4>Remission (score {threshold}): OR 1.75 '
            '(95% CI 1.10 to 2.80), p < 0.01. '
            '<p>At 9 months OR 1.20; interval includes no difference.</p>')
    output = mapped_abstract(route, text)
    assert output == ('Results\nRemission (score < 8): OR 1.75 '
                      '(95% CI 1.10 to 2.80), p < 0.01. \n'
                      'At 9 months OR 1.20; interval includes no difference.')


@pytest.mark.parametrize('route', ['europe_pmc', 'crossref'])
def test_normal_jats_entities_and_inline_words_keep_text_boundaries(route):
    text = ('<jats:abstract><jats:title>Methods &amp; results</jats:title>'
            '<jats:p>Adults aged &ge; 21 received inter<italic>net</italic> CBT.</jats:p>'
            '<jats:p>Symptoms decreased by 2&nbsp;points.<br/>Uncertainty remains.</jats:p>'
            '</jats:abstract>')
    assert mapped_abstract(route, text) == (
        'Methods & results\nAdults aged ≥ 21 received internet CBT.\n'
        'Symptoms decreased by 2\u00a0points.\nUncertainty remains.'
    )


@pytest.mark.parametrize('route', ['europe_pmc', 'crossref'])
def test_plain_text_inequalities_and_spacing_are_not_markup(route):
    text = 'Score < 8 and p < 0.01; score > 3.\n\nExact  internal spacing.'
    # Blank paragraph separation may compact; scientific text and inline space do not.
    assert mapped_abstract(route, text) == text.replace('\n\n', '\n')


@pytest.mark.parametrize('route', ['europe_pmc', 'crossref'])
def test_encoded_angle_text_is_not_parsed_a_second_time(route):
    assert mapped_abstract(route, '<p>Label &lt;group&gt;; p &lt; .05.</p>') == 'Label <group>; p < .05.'


@pytest.mark.parametrize('route', ['europe_pmc', 'crossref'])
@pytest.mark.parametrize('text', [
    'A <B and C> D',
    'score <control> comparison',
    'Unknown <study:p>comparison</study:p> remains.',
    'A <B and C> D </B>',
    'Unknown <Control>value</Control> remains.',
    'Unfamiliar <research:threshold value="8"/> stays inspectable.',
])
def test_unrecognized_or_ambiguous_angle_text_is_preserved(route, text):
    assert mapped_abstract(route, f'<p>{text}</p>') == text


@pytest.mark.parametrize('route', ['europe_pmc', 'crossref'])
def test_known_formatting_with_valued_attributes_still_extracts(route):
    text = '<p id="result">A <B class="emphasis">larger</B> score than <control>.</p>'
    assert mapped_abstract(route, text) == 'A larger score than <control>.'
