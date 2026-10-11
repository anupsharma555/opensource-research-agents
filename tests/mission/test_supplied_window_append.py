"""Appending a pasted-source window preserves exact evidence in the next model input."""

import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.evidence.coverage import extracted_text_coverage
from tests.mission.test_service import answer, drain, service, tool


@pytest.mark.parametrize('mode', ['append', 'replace'])
def test_pasted_window_selection_reaches_exact_next_input(tmp_path, monkeypatch, mode):
    text = 'Original beginning. ' + 'A' * 120 + ' Window passage. ' + 'B' * 120 + ' Original end.'
    requests = []
    copy_ref = None

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            return tool('read_source_window', {
                'source_id': copy_ref['id'], 'source_version': copy_ref['version'],
                'block_offset': 0, 'char_offset': 130, 'char_limit': 50,
                'selection_mode': mode,
            }, 1)
        return answer('The provided passage is retained as supplied evidence.', len(requests))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id=f'append-submission-{mode}', question='Inspect the supplied passage.',
            evidence_text=text, agent_ids=['agent-synthesis'],
        ))
        copy = host.store.records('source_copy')[0]
        copy_ref = {key: copy[key] for key in ('kind', 'id', 'version')}
        original = host.store.get('source_passage', copy['body']['passage_ids'][0])
        original_ref = {key: original[key] for key in ('kind', 'id', 'version')}
        drain(host, run.run_id)
        assert len(requests) == 2
        window = host.store.records('source_read_window')[0]
        selection = host._control(run.run_id, 'agent-synthesis')['body']['selected_passage_refs'][copy['id']]
        expected = ([original_ref] if mode == 'append' else []) + window['body']['passage_refs']
        assert selection == expected
        prepared = requests[1]
        packets = json.loads(prepared['messages'][1]['content'])['source_packets']
        assert len(packets) == 1
        assert packets[0]['passage_refs'] == expected
        assert text[130:180] in packets[0]['text']
        assert (text in packets[0]['text']) is (mode == 'append')
        if mode == 'append':
            assert original['body']['locator'] in packets[0]['text']
        else:
            assert 'Original beginning.' not in packets[0]['text']
            assert 'Original end.' not in packets[0]['text']
        assert host.store.get(**original_ref) == original
        intent = next(item for item in host.store.records('model_request_intent')
                      if json.loads(host.store.read_blob(item['body']['blob']['sha256'])) == prepared)
        assert intent is not None
        coverage = extracted_text_coverage(host.store, copy_ref, selection, [])
        assert coverage['status'] == ('complete_extracted_text' if mode == 'append'
                                      else 'incomplete_extracted_text')
        assert coverage['original_source_partial'] is True
    finally:
        host.close()
