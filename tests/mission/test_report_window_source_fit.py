"""Report-window inputs retain checked sources without repeated parent display metadata."""

import json

import pytest

from ora.coordination.board import ref
from ora.mission.context import ContextOverflow
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack
from tests.mission.test_context_window_compaction import _window_turn


def _report_turn(tmp_path, offset=0, target_variant=None, *, source_linked=False):
    store, context, task, copy_ref, window, refs, _ = _window_turn(tmp_path)
    parent_text = " ".join(f"Finding {i}: distinct retained report statement." for i in range(80))

    def save_parent(tx):
        parent_run = tx.put("mission_run", "parent-run", {
            "mission_id": "parent-m", "question": "Earlier question",
            "scope": "Earlier scope", "source_refs": [],
        })
        report = tx.put("report", "parent-report", {
            "mission_id": "parent-m", "title": "Earlier answer", "body": parent_text,
            "citations": [], "limitations": [],
        })
        snapshot = tx.put("research_snapshot", "parent-snapshot", {
            "mission_id": "parent-m", "inspection": {
                "acceptedOutputReferences": [ref(report)],
                "sources": [{"copyReference": copy_ref}] if source_linked else [],
            },
        })
        run = tx.get("mission_run", "window-run")
        tx.put("mission_run", run["id"], {
            **run["body"], "scope": "Child exact scientific scope",
            "parent_run_id": parent_run["id"], "parent_snapshot_id": snapshot["id"],
            "target_ref": ref(report),
        }, expected_version=run["version"])
        return {"parent": ref(parent_run), "report": ref(report)}

    parent = store.transact("add-report-parent", {}, save_parent, mission_id="m")
    first = context.assemble(
        refs['source_free_call'], task, control_ref=refs['control'],
        allowance=30000, estimate_tokens=len,
    )
    inclusion = bind_no_source_pack(store, context, first, 'report-window-inclusion')
    result = context.read_omitted_record(
        mission_id='m', actor_id='peer', task_id=task['id'], inclusion_ref=inclusion,
        pointer=parent['report'], offset=offset, limit=900,
    )
    arguments = {'inclusion_id': inclusion['id'], **parent['report'],
                 'offset': offset, 'limit': 900}
    blob = store.put_blob(canonical({'content': None, 'reasoning': 'PRIVATE_SYNTHETIC_REASONING',
        'tool_calls': [{
        'id': 'report-read', 'type': 'function', 'function': {
            'name': 'read_context_record', 'arguments': canonical(arguments),
        },
    }]}).encode())

    def save(tx):
        return ref(tx.put('model_response_receipt', 'report-window-response', {
            'mission_id': 'm', 'task_id': task['id'], 'call_ref': refs['source_free_call'],
            'status': 'tool_requested', 'blob': blob,
        }))

    response = store.transact('report-window-response', {}, save, mission_id='m')
    saved = context.save_tool_result(
        'report-window-result', response_ref=response, tool_call_id='report-read',
        name='read_context_record', content=canonical(result),
    )
    if target_variant:
        # Change the current target descriptor after the earlier exact read,
        # keeping the read result itself authoritative and unmodified.
        original_projection = context._scope_projection

        def different_target(*args, **kwargs):
            projection, *rest = original_projection(*args, **kwargs)
            target = projection['followup_target']
            projection['followup_target'] = {
                **target, 'target_ref': {**target['target_ref'], **target_variant},
            }
            return (projection, *rest)

        context._scope_projection = different_target
    return store, context, task, copy_ref, refs, parent, parent_text, response, result, saved


@pytest.mark.parametrize('offset', [0, 1200])
def test_report_window_compaction_preserves_source_scope_and_unread_preview(tmp_path, offset):
    store, context, task, copy_ref, refs, parent, text, response, result, saved = (
        _report_turn(tmp_path, offset)
    )
    raw_before = store.read_blob(store.get(**saved)['body']['blob']['sha256'])
    matches = []
    for allowance in range(2200,9000,25):
        try:
            packed = context.assemble(
                refs['next_call'], task, source_refs=[copy_ref], control_ref=refs['control'],
                tool_turn_refs=[response], archived_tool_turn_refs=[refs['response']],
                allowance=allowance, estimate_tokens=len,
            )
        except ContextOverflow:
            continue
        index = json.loads(packed.messages[-1]['content'])
        if index['followup_target'].get('target_window_in_current_tool_result'):
            matches.append((packed,index))
            break
    assert matches
    packed,index = matches[0]
    target = index['followup_target']
    assert packed.source_refs == (copy_ref,)
    assert index['run_scope']['scope_preview'] == 'Child exact scientific scope'
    assert target['target_ref'] == parent['report']
    assert target['parent_run_ref'] == parent['parent']
    assert target['parent_details_page'] == 'older_records'
    assert json.loads(packed.messages[-2]['content'])['text'] == result['text']
    preview = text[:160]
    if canonical(preview)[1:-1] in result['text']:
        assert 'target_body_preview' not in target
    else:
        assert target['target_body_preview'] == preview
    assert any(o['fetch']==parent['report'] for o in packed.omitted)
    assert store.read_blob(store.get(**saved)['body']['blob']['sha256']) == raw_before


@pytest.mark.parametrize('variant', [{'id': 'another-report'}, {'version': 2}])
def test_other_report_or_version_does_not_displace_target_preview(tmp_path, variant):
    _, context, task, copy_ref, refs, _, text, response, _, _ = _report_turn(
        tmp_path, target_variant=variant,
    )
    successful = []
    for allowance in range(2200, 9000, 100):
        try:
            pack = context.assemble(
                refs['next_call'], task, source_refs=[copy_ref], control_ref=refs['control'],
                tool_turn_refs=[response], allowance=allowance, estimate_tokens=len,
            )
        except ContextOverflow:
            continue
        target = json.loads(pack.messages[-1]['content'])['followup_target']
        assert not target.get('target_window_in_current_tool_result')
        assert text.startswith(target['target_body_preview'])
        successful.append(pack)
        break
    assert successful


def test_peer_record_after_report_read_keeps_source_and_exact_report_recovery(tmp_path):
    from ora.coordination.mailbox import PeerMailbox

    store, context, task, copy_ref, refs, parent, _, response, _, _ = _report_turn(tmp_path)
    request = PeerMailbox(store).send_request(
        'ask-reader', mission_id='m', request_id='review-request', message_id='review-message',
        requester_id='sender', recipient_id='peer', question='Check the exact comparison.',
        expected_artifact='Source-linked critique', cause_ref=task, payload={}, task_ref=task,
    )
    def next_call(tx):
        prior = tx.get(**refs['source_free_call'])
        return ref(tx.put('agent_call', 'peer-after-report-call', prior['body']))

    call_ref = store.transact('next-peer-read-call', {}, next_call, mission_id='m')
    pointer = request['request_ref']
    for allowance in range(2500, 9000, 100):
        try:
            packed = context.assemble(
                call_ref, task, control_ref=refs['control'],
                archived_tool_turn_refs=[refs['response'], response],
                allowance=allowance, estimate_tokens=len,
            )
        except ContextOverflow:
            continue
        if any(item['ref'] == pointer for item in packed.omitted):
            break
    else:
        raise AssertionError('Expected a bounded pack with a recoverable peer request')
    inclusion = bind_no_source_pack(store, context, packed, 'peer-after-report')
    result = context.read_omitted_record(
        mission_id='m', actor_id='peer', task_id=task['id'], inclusion_ref=inclusion,
        pointer=pointer, offset=0, limit=2000,
    )
    blob = store.put_blob(canonical({'content': None, 'tool_calls': [{
        'id': 'peer-read', 'type': 'function', 'function': {
            'name': 'read_context_record', 'arguments': canonical({
                'inclusion_id': inclusion['id'], **pointer, 'offset': 0, 'limit': 2000,
            }),
        },
    }]}).encode())

    def save(tx):
        return ref(tx.put('model_response_receipt', 'peer-read-response', {
            'mission_id': 'm', 'task_id': task['id'], 'call_ref': refs['source_free_call'],
            'status': 'tool_requested', 'blob': blob,
        }))

    peer_response = store.transact('peer-read-response', {}, save, mission_id='m')
    context.save_tool_result(
        'peer-read-result', response_ref=peer_response, tool_call_id='peer-read',
        name='read_context_record', content=canonical(result),
    )
    final = context.assemble(
        refs['next_call'], task, source_refs=[copy_ref], control_ref=refs['control'],
        tool_turn_refs=[peer_response], archived_tool_turn_refs=[refs['response'], response],
        allowance=10000, estimate_tokens=len,
    )
    assert final.source_refs == (copy_ref,)
    assert json.loads(final.messages[-2]['content'])['text'] == result['text']
    assert 'Check the exact comparison.' in result['text']
    # The model-selected report window survives the intervening peer read.
    report_read = next(message for message in final.messages
                       if message.get('tool_call_id') == 'report-read')
    assert json.loads(report_read['content'])['ref'] == parent['report']
    assert not any(o['fetch'] == response for o in final.omitted)
    assert any(o['fetch'] == parent['report'] for o in final.omitted)
