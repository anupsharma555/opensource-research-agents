"""Original pasted-input citations remain exact when checked windows are appended."""

import hashlib

import pytest

from ora.coordination.board import ref
from ora.evidence import EvidenceError, checked_model_text, read_source_window
from ora.evidence.coverage import extracted_text_coverage
from tests.evidence.test_coverage import supplied_without_quality_mapping


def test_original_submission_and_appended_window_replay_without_rewriting(tmp_path):
    library, saved, text = supplied_without_quality_mapping(tmp_path)
    original = library.store.get(**saved['passage_refs'][0])
    window = read_source_window(library, 'append-window', saved['copy_ref'],
                                block_offset=0, char_offset=1600, char_limit=500)
    selected = saved['passage_refs'] + window['passage_refs']
    packet = checked_model_text(library, saved['copy_ref'], selected)
    assert packet['passage_refs'] == selected
    assert packet['passages'][0]['text'] == text
    assert packet['passages'][0]['locator'] == original['body']['locator']
    assert packet['passages'][1]['text'] == text[1600:2100]
    assert library.store.get(**saved['passage_refs'][0]) == original


def test_exact_whole_submission_and_window_share_text_with_both_citations(tmp_path):
    library, saved, text = supplied_without_quality_mapping(tmp_path)
    window = read_source_window(library, 'whole-window', saved['copy_ref'],
                                block_offset=0, char_offset=0, char_limit=5000)
    selected = [*saved['passage_refs'], *window['passage_refs']]
    packet = checked_model_text(library, saved['copy_ref'], selected)

    assert packet['passage_refs'] == selected
    assert len(packet['passages']) == 2
    assert packet['text'].count(text) == 1
    assert all(packet['text'].count(pointer['id']) == 1 for pointer in selected)
    assert all(entry['locator'] in packet['text'] for entry in packet['passages'])
    assert packet['text_sha256'] == hashlib.sha256(packet['text'].encode()).hexdigest()


@pytest.mark.parametrize('change', ['text', 'locator', 'start', 'end', 'copy_ref', 'inspection_state'])
def test_changed_original_submission_is_not_treated_as_a_checked_full_input(tmp_path, change):
    library, saved, text = supplied_without_quality_mapping(tmp_path)
    original = library.store.get(**saved['passage_refs'][0])
    changes = {'text': text + 'fabricated', 'locator': 'submitted text; chars=0:1',
               'start': 1, 'end': len(text) + 1,
               'copy_ref': {**saved['copy_ref'], 'version': 2},
               'inspection_state': 'unchecked'}
    def tamper(tx):
        return ref(tx.put('source_passage', original['id'],
                          {**original['body'], change: changes[change]},
                          expected_version=original['version']))
    altered = library.store.transact('alter-original', {}, tamper)
    with pytest.raises(EvidenceError):
        checked_model_text(library, saved['copy_ref'], [altered])
    assert extracted_text_coverage(library.store, saved['copy_ref'], [altered], [])['status'] == 'unknown'


def test_other_retrieval_route_cannot_use_submission_locator(tmp_path):
    library, saved, _ = supplied_without_quality_mapping(tmp_path, route='other_route')
    with pytest.raises(EvidenceError, match='does not replay'):
        checked_model_text(library, saved['copy_ref'], saved['passage_refs'])


def test_revoked_submission_is_not_allowed_by_locator_compatibility(tmp_path):
    library, saved, _ = supplied_without_quality_mapping(tmp_path)
    copy_ref = saved['copy_ref']
    use = library.store.resolve('source_use.copy', f"{copy_ref['id']}:{copy_ref['version']}")
    def revoke(tx):
        record = tx.put('source_use', use['id'],
                        {**use['body'], 'policy_status': 'revoked', 'allowed_uses': []},
                        expected_version=use['version'])
        return ref(record)
    library.store.transact('revoke-use', {}, revoke)
    with pytest.raises(EvidenceError):
        checked_model_text(library, copy_ref, saved['passage_refs'])


@pytest.mark.parametrize('options', [
    {'mime': 'text/html'}, {'validation': 'different-inspection'},
])
def test_submission_locator_requires_original_plain_text_inspection(tmp_path, options):
    library, saved, _ = supplied_without_quality_mapping(tmp_path, **options)
    with pytest.raises(EvidenceError, match='does not replay'):
        checked_model_text(library, saved['copy_ref'], saved['passage_refs'])


@pytest.mark.parametrize('text', ['Café — outcome\n\n  exact spacing\r\n', '\ufeffCafé — outcome\n'])
def test_raw_submission_coordinates_preserve_unicode_and_initial_bom(tmp_path, text):
    library, saved, _ = supplied_without_quality_mapping(tmp_path, text=text)
    window = read_source_window(library, 'unicode-window', saved['copy_ref'],
                                block_offset=0, char_offset=0, char_limit=8)
    selected = saved['passage_refs'] + window['passage_refs']
    packet = checked_model_text(library, saved['copy_ref'], selected)
    assert packet['passages'][0]['text'] == text
    assert packet['passages'][0]['locator'] == f'submitted text; chars=0:{len(text)}'
    assert packet['passages'][1]['text'] == text.removeprefix('\ufeff')[:8]
    coverage = extracted_text_coverage(library.store, saved['copy_ref'], selected, [])
    assert coverage['status'] == 'complete_extracted_text'
    assert coverage['covered_chars'] == len(text.removeprefix('\ufeff'))
    assert coverage['original_source_partial'] is True
