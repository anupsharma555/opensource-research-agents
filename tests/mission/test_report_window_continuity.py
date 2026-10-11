"""Already selected report text survives intervening actions, not authorization loss."""

import hashlib
import json

import pytest

from ora.coordination.board import ref
from ora.mission.context import ContextError, ContextOverflow
from ora.mission.service import TOOL_SCHEMAS
from ora.models import GroqSelectedAdapter, SourceUse
from ora.state.store import canonical
from tests.mission.test_report_window_source_fit import _report_turn
from tests.models.test_groq import config


def continuation(tmp_path, *, source_tool_id='later-source-read'):
    saved = _report_turn(tmp_path, offset=1200, source_linked=True)
    store, context, task, copy_ref, refs, parent, text, report_response, window, result_ref = saved
    source_response = store.get(**refs['response'])
    source_result = store.get('context_tool_result', f"tool-result:{source_response['id']}")
    source_message = json.loads(store.read_blob(source_result['body']['blob']['sha256']))
    private = json.loads(store.read_blob(source_response['body']['blob']['sha256']))
    private['tool_calls'][0]['id'] = source_tool_id
    blob = store.put_blob(canonical(private).encode())

    def save(tx):
        response = tx.put('model_response_receipt', 'later-source-response', {
            **source_response['body'], 'call_ref': refs['next_call'], 'blob': blob,
        })
        call = tx.put('agent_call', 'continuation-call', tx.get(**refs['next_call'])['body'])
        return {'response': ref(response), 'call': ref(call)}

    later = store.transact('source-after-report', {}, save, mission_id='m')
    context.save_tool_result(
        'later-source-result', response_ref=later['response'], tool_call_id=source_tool_id,
        name='read_source_window', content=source_message['content'],
    )
    return saved, later


def packed(saved, later, allowance=20000, *, with_source=True):
    _, context, task, copy_ref, refs, _, _, report_response, _, _ = saved
    return context.assemble(
        later['call'] if with_source else refs['source_free_call'], task,
        source_refs=[copy_ref] if with_source else [], control_ref=refs['control'],
        tool_turn_refs=[later['response']] if with_source else [],
        archived_tool_turn_refs=[report_response], allowance=allowance, estimate_tokens=len,
    )


def continuity_receipt(pack):
    return next(item for item in pack.tool_result_compactions
                if item.get('kind') == 'retained_report_window')


def test_selected_report_and_source_windows_reach_next_prepared_request(tmp_path):
    saved, later = continuation(tmp_path)
    store, context, _, copy_ref, refs, parent, text, response, window, result_ref = saved
    original_blob = store.read_blob(store.get(**result_ref)['body']['blob']['sha256'])
    pack = packed(saved, later)
    report_message = next(item for item in pack.messages if item.get('tool_call_id') == 'report-read')
    assert json.loads(report_message['content']) == window
    assert text[1300:1450] in window['text']  # Well beyond the 700-character preview.
    assert context.context_source_dependency_refs(pack) == (copy_ref,)
    assert continuity_receipt(pack)['status'] == 'included_exact_window'
    assert result_ref in pack.included_refs and response in pack.included_refs
    assert not any(item['ref'] == response for item in pack.omitted)
    assert any(item['ref'] == parent['report'] for item in pack.omitted)  # Other report text.

    selected = store.get(**refs['control'])['body']['selected_passage_refs'][copy_ref['id']]
    source_text = '\n'.join(store.get(**pointer)['body']['text'] for pointer in selected)
    adapter = GroqSelectedAdapter(tmp_path, transport=lambda *_args, **_kwargs: pytest.fail('No live call'))
    request = adapter.prepare(
        config(), call_id='prepared-continuation', attempt_id='continuation-attempt',
        task_category='collect', phase='collect', messages=pack.messages,
        tools=tuple({'type': 'function', 'function': {'name': name, 'parameters': TOOL_SCHEMAS[name]}}
                    for name in ('read_context_record', 'read_source_window')),
        sources=(SourceUse(copy_ref['id'], copy_ref['version'],
                           hashlib.sha256(source_text.encode()).hexdigest(),
                           'permitted', True, text=source_text, route='direct'),),
    )
    serialized = canonical(request.body)
    retained = next(item for item in request.body['messages'] if item.get('tool_call_id') == 'report-read')
    assert json.loads(retained['content'])['text'] == window['text']
    assert json.loads(request.body['messages'][1]['content'])['source_packets'][0]['text'] == source_text
    assert 'PRIVATE_SYNTHETIC_REASONING' not in serialized
    assert [item['tool_call_id'] for item in request.body['messages'] if item['role'] == 'tool'] == [
        'report-read', 'later-source-read',
    ]
    assert store.read_blob(store.get(**result_ref)['body']['blob']['sha256']) == original_blob


def test_report_window_is_withheld_when_source_use_is_revoked(tmp_path):
    saved, later = continuation(tmp_path)
    store, context, _, copy_ref, _, _, _, response, window, _ = saved
    prior_policy = store.resolve('source_use.copy', f"{copy_ref['id']}:{copy_ref['version']}")

    def revoke(tx):
        return tx.put('source_use', prior_policy['id'], {
            **prior_policy['body'], 'allowed_uses': [], 'policy_status': 'restricted',
        }, expected_version=prior_policy['version'])

    store.transact('revoke-report-dependency', {}, revoke, mission_id='m')
    pack = packed(saved, later, with_source=False)
    assert canonical(window['text'])[1:-1] not in canonical(pack.messages)
    assert not any(item.get('tool_call_id') == 'report-read' for item in pack.messages)
    assert continuity_receipt(pack)['status'] == 'omitted_due_source_use'
    assert any(item['ref'] == response and item['reason'] == 'report_window_source_use_unavailable'
               for item in pack.omitted)
    assert context.context_source_dependency_refs(pack) == ()


def test_insufficient_space_keeps_explicit_exact_window_omission(tmp_path):
    saved, later = continuation(tmp_path)
    _, _, _, _, _, parent, _, response, window, _ = saved
    for allowance in range(2200, 12000, 50):
        try:
            pack = packed(saved, later, allowance)
        except ContextOverflow:
            continue
        if continuity_receipt(pack)['status'] == 'omitted_for_input_allowance':
            break
    else:
        pytest.fail('Expected a fitting current source turn without room for the report window')
    assert pack.estimated_tokens <= allowance
    assert not any(item.get('tool_call_id') == 'report-read' for item in pack.messages)
    receipt = continuity_receipt(pack)
    assert (receipt['report_ref'], receipt['projection_sha256'], receipt['offset'], receipt['returned_chars']) == (
        parent['report'], window['projection_sha256'], window['offset'], window['returned_chars'],
    )
    assert any(item['ref'] == response and item['reason'] == 'report_window_omitted_for_input_allowance'
               for item in pack.omitted)
    assert any(item['fetch'] == parent['report'] for item in pack.omitted)


def test_earlier_tool_id_reuse_does_not_block_current_source_read(tmp_path):
    saved, later = continuation(tmp_path, source_tool_id='report-read')
    pack = packed(saved, later)
    assert continuity_receipt(pack)['status'] == 'omitted_due_tool_call_id_conflict'
    messages = [item for item in pack.messages if item.get('role') == 'tool']
    assert len(messages) == 1 and messages[0]['name'] == 'read_source_window'


def test_identical_current_report_window_is_not_duplicated(tmp_path):
    saved, later = continuation(tmp_path)
    store, context, _, _, _, _, _, response_ref, window, _ = saved
    response = store.get(**response_ref)
    private = json.loads(store.read_blob(response['body']['blob']['sha256']))
    private['tool_calls'][0]['id'] = 'current-report-read'
    blob = store.put_blob(canonical(private).encode())
    newer = store.transact('current-report-read', {}, lambda tx: ref(tx.put(
        'model_response_receipt', 'current-report-response', {**response['body'], 'blob': blob},
    )), mission_id='m')
    context.save_tool_result(
        'current-report-result', response_ref=newer, tool_call_id='current-report-read',
        name='read_context_record', content=canonical(window),
    )
    pack = packed(saved, {**later, 'response': newer})
    assert continuity_receipt(pack)['status'] == 'same_exact_window_already_in_input'
    assert [item['tool_call_id'] for item in pack.messages if item.get('role') == 'tool'] == [
        'current-report-read',
    ]


@pytest.mark.parametrize('field,value', [('projection_sha256', '0' * 64), ('text', 'Forged report text')])
def test_saved_report_window_cannot_change_during_continuation(tmp_path, monkeypatch, field, value):
    saved, later = continuation(tmp_path)
    store, _, _, _, _, _, _, _, _, result_ref = saved
    digest = store.get(**result_ref)['body']['blob']['sha256']
    original = store.read_blob

    def corrupt(pointer):
        raw = original(pointer)
        if pointer != digest:
            return raw
        payload = json.loads(raw)
        window = json.loads(payload['content'])
        window[field] = value
        if field == 'text':
            window['returned_chars'] = len(value)
        payload['content'] = canonical(window)
        return canonical(payload).encode()

    monkeypatch.setattr(store, 'read_blob', corrupt)
    with pytest.raises(ContextError, match='Saved report window changed'):
        packed(saved, later)
