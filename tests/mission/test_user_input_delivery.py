"""Exercise user amendments at real prepared and in-flight mission boundaries."""
from __future__ import annotations

import json
import time

import pytest

from ora.contracts.models import LiveStartRequest, LiveUserInputRequest
from tests.mission.test_service import answer, service, tool


def _until(host, run_id, predicate, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Offline user-input boundary did not advance')


def _command(host, run_id, text, kind='direction'):
    control = host._control(run_id, 'agent-synthesis')
    task = host.store.get('board_proposal', control['body']['task_id'])
    return LiveUserInputRequest(
        request_id='synthetic-amendment', run_id=run_id, task_id=task['id'],
        target_agent_id=control['body']['actor_id'], expected_task_version=task['version'],
        kind=kind, text=text,
    )


@pytest.mark.parametrize('first_result', ['direct', 'publication'])
@pytest.mark.parametrize('kind', ['direction', 'unverified_pasted_evidence'])
def test_inflight_user_input_preserves_sent_bytes_and_requires_current_turn(
    tmp_path, monkeypatch, first_result, kind,
):
    received = []
    errors = []
    saved = {}
    amendment = 'Synthetic user update: use the label Cedar when describing this task.'
    host = None

    def transport(request, *, timeout):
        try:
            packet = json.loads(request.data)
            received.append(request.data)
            if len(received) == 1:
                assert amendment not in request.data.decode()
                # The adapter has already saved the dispatch marker at this point.
                assert len(host.store.records('model_dispatch_attempt')) == 1
                saved['receipt'] = host.submit_user_input(_command(host, saved['run_id'], amendment, kind))
                if first_result == 'direct':
                    return answer('Earlier answer without the amendment.', 1)
                return tool('publish_result', {
                    'title': 'Earlier draft', 'text': 'Earlier answer without the amendment.',
                    'citations': [], 'limitations': ['Synthetic test only'],
                }, 1)
            assert len(received) == 2
            index = json.loads(packet['messages'][-1]['content'])
            if kind == 'direction':
                entry = index['current_user_direction']
                assert entry['ref'] == saved['receipt'].input_ref.model_dump()
                assert entry['text'] == amendment
                assert entry['kind'] == kind
                assert entry['origin'] == 'authenticated_local_user'
            else:
                entry = next(item for item in index['details'] if item['type'] == 'user_input')
                assert entry['ref'] == saved['receipt'].input_ref.model_dump()
                assert entry['body']['text'] == amendment
                assert entry['body']['kind'] == kind
                assert entry['body']['origin'] == 'authenticated_local_user'
                assert index['user_input_guidance']['count'] == 1
            assert amendment not in packet['messages'][0]['content']
            assert all(amendment not in str(item) for message in packet['messages']
                       if message['role'] == 'user' for item in json.loads(message['content']).get('source_packets', []))
            return answer('Cedar is the updated synthetic task label.', 2)
        except BaseException as exc:
            errors.append(repr(exc))
            raise

    host = service(tmp_path, monkeypatch, transport)
    try:
        started = host.start(LiveStartRequest(
            request_id='user-input-flight', question='Describe the synthetic task label.',
            evidence_text='Initial synthetic label: Birch.', agent_ids=['agent-synthesis'],
        ))
        saved['run_id'] = started.run_id
        profile = host._run(started.run_id)['body']['profile']
        config = host._configuration(host._control(started.run_id, 'agent-synthesis'))['body']
        _until(host, started.run_id, lambda: host._run(started.run_id)['body']['status'] == 'completed')
        assert errors == []
        assert len(received) == 2
        assert len(host.store.records('mission_user_input')) == 1
        assert len(host.store.records('source_copy')) == 1
        assert host._run(started.run_id)['body']['profile'] == profile
        assert host._configuration(host._control(started.run_id, 'agent-synthesis'))['body'] == config
        final = host.store.get(**host._control(started.run_id, 'agent-synthesis')['body']['report_ref'])
        assert 'Cedar is the updated' in final['body']['body']
        intents = host.store.records('model_request_intent')
        assert {host.store.read_blob(item['body']['blob']['sha256']) for item in intents} == set(received)
        assert any(saved['receipt'].input_ref.model_dump() in item['body']['included_refs']
                   for item in host.store.records('context_inclusion'))
    finally:
        host.close()


def test_queued_user_input_survives_reopen_and_refreshes_only_its_target(tmp_path, monkeypatch):
    from tests.mission.test_peer_queued_freshness import _start_queued

    attempts = []
    host = service(tmp_path, monkeypatch, lambda *_a, **_k: attempts.append('unexpected'))
    reopened = None
    try:
        run, pending, intent, work, _spec, _cap = _start_queued(host, 'queued-user-input')
        original = host.store.read_blob(intent['body']['blob']['sha256'])
        receipt = host.submit_user_input(_command(host, run.run_id, 'Targeted synthetic correction.'))
        host.close()
        reopened = service(tmp_path, monkeypatch, lambda *_a, **_k: attempts.append('unexpected'))
        pending = reopened._control(run.run_id, 'agent-synthesis')
        assert reopened._supersede_queued_peer_input(pending)
        replaced = reopened.store.get('executor_work', work['id'])['body']
        assert replaced['phase'] == 'superseded'
        assert replaced['status_basis'] == 'new_user_input_before_dispatch'
        assert reopened.store.read_blob(intent['body']['blob']['sha256']) == original
        assert reopened._schedule_model(reopened._control(run.run_id, 'agent-synthesis'))
        assert reopened._schedule_model(reopened._control(run.run_id, 'agent-critique'))
        for alias in ('agent-synthesis', 'agent-critique'):
            control = reopened._control(run.run_id, alias)
            call = control['body']['call_ref']
            sent = reopened.store.get('model_request_intent', f"model-intent:{call['id']}:{call['version']}")
            packet = json.loads(reopened.store.read_blob(sent['body']['blob']['sha256']))
            assert ('Targeted synthetic correction.' in json.dumps(packet)) is (alias == 'agent-synthesis')
            if alias == 'agent-critique':
                assert 'user_input_guidance' not in json.dumps(packet)
            assert not reopened._supersede_queued_peer_input(control)
        assert reopened.store.get(**receipt.input_ref.model_dump())
        assert reopened.store.records('model_dispatch_attempt') == []
        assert attempts == []
    finally:
        (reopened or host).close()


def test_claimed_user_input_refresh_has_no_dispatch_and_recovers(tmp_path, monkeypatch):
    import asyncio

    from tests.mission.test_peer_queued_freshness import _start_queued

    attempts = []
    host = service(tmp_path, monkeypatch, lambda *_a, **_k: attempts.append('unexpected'))
    reopened = None
    try:
        run, pending, intent, work, _spec, cap_id = _start_queued(host, 'claimed-user-input')
        original = host.session.invoke_capture
        saved = []

        def update_after_claim(pointer, **kwargs):
            saved.append(host.submit_user_input(_command(host, run.run_id, 'Claim-boundary amendment.')))
            return original(pointer, **kwargs)

        monkeypatch.setattr(host.session, 'invoke_capture', update_after_claim)
        asyncio.run(host.executor.run_ready(pending['body']['mission_id']))
        capacity = host.store.get('capacity_operation', cap_id)['body']
        assert capacity['attempts'][-1]['status'] == 'terminal_no_effect'
        assert capacity['attempts'][-1]['charged_tokens'] == 0
        assert capacity['attempts'][-1]['usage_known'] is True
        assert host.store.get('model_dispatch_attempt', f"model-dispatch:{intent['id']}") is None
        host.close()
        reopened = service(tmp_path, monkeypatch, lambda *_a, **_k: attempts.append('unexpected'))
        assert reopened._supersede_claimed_peer_input(reopened._control(run.run_id, 'agent-synthesis'))
        replaced = reopened.store.get('executor_work', work['id'])['body']
        assert replaced['phase'] == 'superseded'
        assert replaced['status_basis'] == 'new_user_input_before_provider_dispatch'
        assert reopened._schedule_model(reopened._control(run.run_id, 'agent-synthesis'))
        assert attempts == [] and len(saved) == 1
    finally:
        (reopened or host).close()


def test_input_included_during_assembly_is_not_falsely_stale(tmp_path, monkeypatch):
    from ora.mission import context as context_module

    host = service(tmp_path, monkeypatch, lambda *_a, **_k: None)
    try:
        run = host.start(LiveStartRequest(request_id='input-watermark-race',
            question='Describe a synthetic label.', agent_ids=['agent-synthesis']))
        original_heads = context_module._heads
        saved = []

        def heads(store, kind):
            if kind == 'mission_user_input' and not saved:
                saved.append(host.submit_user_input(_command(host, run.run_id, 'Already-visible amendment.')))
            return original_heads(store, kind)

        monkeypatch.setattr(context_module, '_heads', heads)
        assert host._schedule_model(host._control(run.run_id, 'agent-synthesis'))
        control = host._control(run.run_id, 'agent-synthesis')
        call = control['body']['call_ref']
        intent = host.store.get('model_request_intent', f"model-intent:{call['id']}:{call['version']}")
        inclusion = host.store.get('context_inclusion', f"context:{intent['id']}")
        assert saved[0].input_ref.model_dump() in inclusion['body']['included_refs']
        with host.store._connect() as db:
            accepted_sequence = db.execute(
                "SELECT MAX(sequence) FROM events WHERE event_type='mission_user_input_accepted'"
            ).fetchone()[0]
        assert accepted_sequence > inclusion['body']['event_through']
        host.executor._prepare(control['body']['mission_id'])
        assert 'Already-visible amendment.' in host.store.read_blob(intent['body']['blob']['sha256']).decode()
        assert not host._supersede_queued_peer_input(control)
        assert host._new_task_message_after_input(control) is None
    finally:
        host.close()


def test_large_user_input_is_exactly_recoverable_and_foreign_reader_is_denied(tmp_path, monkeypatch):
    from ora.mission.context import ContextError

    host = service(tmp_path, monkeypatch, lambda *_a, **_k: None)
    try:
        run = host.start(LiveStartRequest(request_id='large-user-input',
            question='Assess the supplied synthetic note.', agent_ids=['agent-synthesis', 'agent-critique']))
        text = 'Synthetic unverified detail. ' * 1500
        receipt = host.submit_user_input(_command(host, run.run_id, text, 'unverified_pasted_evidence'))
        host._update_control(host._control(run.run_id, 'agent-synthesis'), input_budget={
            'observed_limit_tokens': 8000, 'target_fraction': .70,
            'basis': 'qualification_imposed_limit', 'actual_provider_observation': False,
        })
        assert host._schedule_model(host._control(run.run_id, 'agent-synthesis'))
        control = host._control(run.run_id, 'agent-synthesis')
        call = control['body']['call_ref']
        intent = host.store.get('model_request_intent', f"model-intent:{call['id']}:{call['version']}")
        packet = json.loads(host.store.read_blob(intent['body']['blob']['sha256']))
        inclusion = host.store.get('context_inclusion', f"context:{intent['id']}")
        pointer = receipt.input_ref.model_dump()
        assert any(item.get('fetch') == pointer for item in inclusion['body']['omitted'])
        assert text not in json.dumps(packet)
        assert 'read_context_page' in {item['function']['name'] for item in packet['tools']}
        scope = dict(mission_id=control['body']['mission_id'], actor_id=control['body']['actor_id'],
                     task_id=control['body']['task_id'],
                     inclusion_ref={key: inclusion[key] for key in ('kind', 'id', 'version')})
        position = next(i for i, item in enumerate(inclusion['body']['omitted']) if item.get('fetch') == pointer)
        page = host.context.read_omitted_page(**scope, cursor=position, limit=1)
        assert pointer['id'] in json.dumps(page)
        parts = []
        offset = 0
        while True:
            window = host.context.read_omitted_record(**scope, pointer=pointer, offset=offset, limit=2000)
            parts.append(window['text'])
            if window['next_offset'] is None:
                break
            assert window['next_request']['id'] == pointer['id']
            offset = window['next_offset']
        assert json.loads(''.join(parts))['text'] == text
        foreign = host._control(run.run_id, 'agent-critique')
        with pytest.raises(ContextError):
            host.context.read_omitted_record(**{**scope, 'actor_id': foreign['body']['actor_id'],
                                               'task_id': foreign['body']['task_id']},
                                            pointer=pointer, offset=0, limit=2000)
        assert host.store.records('source_copy') == []
        assert host.store.records('model_dispatch_attempt') == []
    finally:
        host.close()


def test_omitted_user_text_reader_result_reaches_next_model_request(tmp_path, monkeypatch):
    packets, errors, saved = [], [], {}
    text = 'Synthetic pasted detail. ' * 1600
    host = None

    def transport(request, *, timeout):
        try:
            packet = json.loads(request.data)
            packets.append(packet)
            index = json.loads(packet['messages'][-1]['content'])
            attached = {item['function']['name'] for item in packet['tools']}
            if len(packets) == 1:
                assert text not in json.dumps(packet)
                assert index['user_input_guidance']['count'] == 1
                assert 'read_context_page' in attached
                return tool('read_context_page', {
                    'inclusion_id': index['older_records']['inclusion_id'], 'cursor': 0, 'limit': 10,
                }, len(packets))
            if 'read' not in saved:
                page = next(json.loads(message['content']) for message in packet['messages']
                            if message.get('name') == 'read_context_page')
                entry = next(item for item in page['items'] if item['ref']['kind'] == 'mission_user_input')
                saved['read'] = entry['summary']['read_request']
            if not saved.get('read_sent'):
                if 'read_context_record' not in attached:
                    assert not saved.get('attachment_requested')
                    saved['attachment_requested'] = True
                    return tool('request_capability', {'tool_id': 'read_context_record'}, len(packets))
                saved['read_sent'] = True
                return tool('read_context_record', {
                    key: value for key, value in saved['read'].items() if key != 'tool'
                }, len(packets))
            result = next(json.loads(message['content']) for message in packet['messages']
                          if message.get('name') == 'read_context_record')
            assert result['ref'] == saved['receipt'].input_ref.model_dump()
            assert text[:500] in result['text']
            assert result['returned_chars'] <= 2000
            assert result['next_request']['offset'] == result['returned_chars']
            assert result['next_offset'] < result['total_chars']
            saved['received_exact_window'] = True
            return answer('The first saved window contains synthetic pasted detail; further text is unread.', len(packets))
        except BaseException as exc:
            errors.append(repr(exc))
            raise

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(request_id='user-reader-path',
            question='Identify the kind of text in the first window of the submitted note.',
            agent_ids=['agent-synthesis']))
        saved['receipt'] = host.submit_user_input(_command(host, run.run_id, text, 'unverified_pasted_evidence'))
        host._update_control(host._control(run.run_id, 'agent-synthesis'), input_budget={
            'observed_limit_tokens': 8000, 'target_fraction': .70,
            'basis': 'qualification_imposed_limit', 'actual_provider_observation': False,
        })
        _until(host, run.run_id, lambda: host._run(run.run_id)['body']['status'] == 'completed')
        assert not errors
        assert saved['received_exact_window']
        assert len(packets) in {3, 4}
        assert host.store.records('source_copy') == []
    finally:
        host.close()
