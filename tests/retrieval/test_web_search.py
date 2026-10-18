"""Offline progressive retrieval: real executor/ledger with synthetic transports."""
import asyncio
import json
import threading
from dataclasses import replace

import pytest
from pydantic import ValidationError

from ora.contracts.models import SearchWebInput
from ora.coordination.executor import WorkSpec
from ora.retrieval.providers import HTTPResult
from ora.retrieval.web_config import provider_policy
from ora.retrieval.web_search import WebSearch
from tests.coordination.test_executor import foundation, propose


def setup_search(tmp_path, transport, *, values=None, clock=None):
    from ora.coordination.capacity import CapacityPolicy
    # Configure the actual shared scheduler before creating the executor.
    quotas = provider_policy(CapacityPolicy({'model': 2, 'retrieval': 6, 'compute': 2}), {}).quotas
    store, board, capacity, executor, actors, configs = foundation(
        tmp_path, retrieval=6, quotas=quotas, clock=clock)
    engine = WebSearch(store, executor, values or {'EXA_API_KEY': 'synthetic-exa', 'TAVILY_API_KEY': 'synthetic-tavily'}, transport=transport)
    parent = propose(board, actors[0], 'parent')
    spec = WorkSpec(work_id='search-call', mission_id='mission', run_id='run',
                    proposal_ref=parent, actor_id=actors[0], configuration_ref=configs[0],
                    input_refs=(), handler_id='mission-tool', handler_version='1',
                    kind='compute', lane='search', attempt_id='parent-attempt')
    return store, executor, engine, spec


def response(query='query', *, empty=False):
    return HTTPResult(200, json.dumps({'results': [] if empty else [
        {'id': query, 'url': 'https://example.org/source', 'title': 'Synthetic source',
         'highlights': ['Candidate text, not an inspected source passage.']}
    ]}).encode(), {})


def test_required_mode_and_distinct_round_queries():
    for args in ({'queries': ['x'], 'information_need': 'find'},
                 {'mode': 'quick', 'queries': ['x', 'y'], 'information_need': 'find'},
                 {'mode': 'research', 'queries': ['x', ' X '], 'information_need': 'find'}):
        with pytest.raises(ValidationError):
            SearchWebInput.model_validate(args)


def test_quick_receipt_local_read_and_task_boundary(tmp_path):
    calls = []
    def transport(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return response()
    store, executor, engine, spec = setup_search(tmp_path, transport)
    started = engine.start(spec, {'mode': 'quick', 'information_need': 'Locate a source', 'queries': ['one topic']})
    assert started['status'] == 'pending'
    asyncio.run(executor.run_ready('mission'))
    result = engine.read(spec, {'operation_id': started['operation_ref']['id']})
    assert result['status'] == 'completed', result
    assert len(calls) == 1
    assert calls[0][2]['body']['type'] == 'fast'
    assert calls[0][2]['body']['contents'] == {'highlights': True}
    assert result['available_results'][0]['result']['hits']
    assert engine.read(spec, {'operation_id': started['operation_ref']['id']}) == result
    assert len(calls) == 1
    with pytest.raises(ValueError):
        engine.read(replace(spec, actor_id='other'), {'operation_id': started['operation_ref']['id']})


def test_five_in_flight_sixth_queued_and_progressive_delivery(tmp_path):
    async def scenario():
        release = threading.Event()
        started = []
        lock = threading.Lock()
        def transport(method, url, **kwargs):
            with lock:
                started.append(kwargs['body']['query'])
            if kwargs['body']['query'] != 'query-0':
                assert release.wait(8)
            return response(kwargs['body']['query'])
        store, executor, engine, spec = setup_search(tmp_path, transport)
        result = engine.start(spec, {'mode': 'research', 'information_need': 'Compare several dimensions', 'queries': [f'query-{i}' for i in range(6)]})
        running = asyncio.create_task(executor.run_ready('mission'))
        try:
            for _ in range(100):
                result = engine.read(spec, {'operation_id': result['operation_ref']['id']})
                if result['available_results']:
                    break
                await asyncio.sleep(.02)
            assert result['available_results']
            assert result['status'] == 'pending'
            assert len(started) <= 5  # shared one-second launch ceiling also applies
            active = [x for x in store.records('capacity_operation') if x['body']['reservation']]
            assert len(active) <= 5
        finally:
            release.set()
            await asyncio.wait_for(running, 10)
        # The remaining launch may require a later host drain after its quota wake.
        await asyncio.sleep(1.05)
        await executor.run_ready('mission')
        assert engine.read(spec, {'operation_id': result['operation_ref']['id']})['status'] == 'completed'
    asyncio.run(scenario())


def test_five_actual_simultaneous_exa_requests_hold_sixth(tmp_path):
    async def scenario():
        release = threading.Event()
        started = []
        lock = threading.Lock()
        def transport(method, url, **kwargs):
            with lock:
                started.append(kwargs['body']['query'])
            assert release.wait(8)
            return response(kwargs['body']['query'])
        store, executor, engine, spec = setup_search(tmp_path, transport)
        operation = engine.start(spec, {'mode': 'research', 'information_need': 'Six directions',
                                        'queries': [f'direction-{i}' for i in range(6)]})
        running = asyncio.create_task(executor.run_ready('mission'))
        try:
            for _ in range(200):
                with lock:
                    count = len(started)
                if count == 5:
                    break
                await asyncio.sleep(.01)
            assert count == 5
            active = [x for x in store.records('capacity_operation') if x['body']['reservation']]
            assert len(active) == 5
            assert engine.read(spec, {'operation_id': operation['operation_ref']['id']})['pending'] == 6
        finally:
            release.set()
            await asyncio.wait_for(running, 10)
    asyncio.run(scenario())


def test_empty_exa_falls_back_once_and_missing_provider_is_local(tmp_path):
    calls = []
    def transport(method, url, **kwargs):
        calls.append(url)
        return response(empty='exa.ai' in url)
    store, executor, engine, spec = setup_search(tmp_path, transport)
    started = engine.start(spec, {'mode': 'quick', 'information_need': 'Find missing source', 'queries': ['topic']})
    asyncio.run(executor.run_ready('mission'))
    result = engine.read(spec, {'operation_id': started['operation_ref']['id']})
    assert len(calls) == 2
    assert result['status'] == 'completed'
    assert [x['provider'] for x in result['available_results']] == ['exa', 'tavily']
    assert result['available_results'][1]['fallback_of']
    other = engine.start(replace(spec, work_id='second'), {'mode': 'research', 'provider': 'searxng', 'information_need': 'Alternative coverage', 'queries': ['topic']})
    asyncio.run(executor.run_ready('mission'))
    assert engine.read(spec, {'operation_id': other['operation_ref']['id']})['status'] == 'failed'
    assert len(calls) == 2


def test_restart_reuses_completed_round_and_cursor_preserves_all_routes(tmp_path):
    calls = []
    def transport(method, url, **kwargs):
        calls.append(kwargs['body']['query'])
        return response(kwargs['body']['query'])
    store, executor, engine, spec = setup_search(tmp_path, transport)
    args = {'mode': 'research', 'information_need': 'Compare two dimensions', 'queries': ['dimension one', 'dimension two']}
    initial = engine.start(spec, args)
    asyncio.run(executor.run_ready('mission'))
    restarted = WebSearch(store, executor, engine.values, transport=transport)
    result = restarted.start(spec, args)
    assert len(calls) == 2
    assert len(result['available_results']) == 1  # one URL, every route retained
    routes = result['available_results'][0]['result']['hits'][0]['discovery_routes']
    assert {x['query'] for x in routes} == set(args['queries'])
    assert len({x['hit_ref']['id'] for x in routes}) == 2
    assert result['continuation_cursor'] == 2
    first_page = restarted.read(spec, {'operation_id': initial['operation_ref']['id'], 'limit': 1})
    assert {x['query'] for x in first_page['available_results'][0]['result']['hits'][0]['discovery_routes']} == set(args['queries'])
    assert restarted.read(spec, {'operation_id': initial['operation_ref']['id'], 'cursor': 2})['available_results'] == []


def test_cancel_before_dispatch_and_uncertain_transport_do_not_repeat(tmp_path):
    calls = []
    def transport(*args, **kwargs):
        import urllib.error
        calls.append(True)
        raise urllib.error.URLError('synthetic uncertain response')
    store, executor, engine, spec = setup_search(tmp_path, transport)
    first = engine.start(spec, {'mode': 'quick', 'information_need': 'Cancelled inquiry', 'queries': ['unused']})
    cancelled = engine.cancel(spec, {'operation_id': first['operation_ref']['id']})
    asyncio.run(executor.run_ready('mission'))
    assert cancelled['status'] == 'cancelled'
    assert calls == []
    second = engine.start(replace(spec, work_id='uncertain'), {'mode': 'quick', 'information_need': 'Uncertain inquiry', 'queries': ['uncertain']})
    asyncio.run(executor.run_ready('mission'))
    result = engine.read(spec, {'operation_id': second['operation_ref']['id']})
    assert result['status'] == 'uncertain'
    asyncio.run(executor.run_ready('mission'))
    assert len(calls) == 1  # neither a blind retry nor a Tavily fallback


@pytest.mark.parametrize('status_code', [429, 503])
def test_rate_limit_wait_uses_ledger_and_fresh_attempt(tmp_path, status_code):
    import time
    calls = []
    def transport(*args, **kwargs):
        calls.append(True)
        return HTTPResult(status_code, b'{}', {'retry-after': '1'}) if len(calls) == 1 else response()
    store, executor, engine, spec = setup_search(tmp_path, transport)
    started = engine.start(spec, {'mode': 'quick', 'information_need': 'Paced lookup', 'queries': ['topic']})
    asyncio.run(executor.run_ready('mission'))
    assert engine.read(spec, {'operation_id': started['operation_ref']['id']})['status'] == 'pending'
    assert len(calls) == 1
    assert any(x['body']['phase'] == 'waiting' for x in store.records('capacity_operation'))
    time.sleep(1.05)
    asyncio.run(executor.run_ready('mission'))
    result = engine.read(spec, {'operation_id': started['operation_ref']['id']})
    assert result['status'] == 'completed', result
    assert len(calls) == 2


def test_saved_page_recovery_after_delivery_interruption_never_refetches(tmp_path, monkeypatch):
    calls = []
    def transport(*args, **kwargs):
        calls.append(True)
        return response()
    store, executor, engine, spec = setup_search(tmp_path, transport)
    started = engine.start(spec, {'mode': 'quick', 'information_need': 'Retain a saved result', 'queries': ['topic']})
    save = engine._save_delivery
    def interrupted(*args):
        raise RuntimeError('synthetic callback interruption')
    monkeypatch.setattr(engine, '_save_delivery', interrupted)
    asyncio.run(executor.run_ready('mission'))
    assert len(calls) == 1
    monkeypatch.setattr(engine, '_save_delivery', save)
    child = next(x for x in executor._work('mission') if x['body']['spec']['handler_id'] == 'web-query')
    child_spec = executor._spec_from(child)
    cap_id = executor._capacity_id(child_spec)
    cap = executor.capacity.get(cap_id)
    if cap['phase'] == 'dispatched':
        executor.capacity.timeout(cap_id, executor.owner, cap['generation'])
    assert executor.capacity.get(cap_id)['phase'] == 'uncertain'
    restarted = WebSearch(store, executor, engine.values, transport=transport)
    recovered = restarted.read(spec, {'operation_id': started['operation_ref']['id']})
    assert len(calls) == 1
    assert recovered['status'] == 'completed', recovered
    assert recovered['available_results']
    assert executor.capacity.get(cap_id)['phase'] == 'completed'


def test_cancel_after_dispatch_retains_late_result_without_acceptance(tmp_path):
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        calls = []
        def transport(*args, **kwargs):
            calls.append(True)
            entered.set()
            assert release.wait(5)
            return response()
        store, executor, engine, spec = setup_search(tmp_path, transport)
        started = engine.start(spec, {'mode': 'quick', 'information_need': 'Stop late work', 'queries': ['topic']})
        running = asyncio.create_task(executor.run_ready('mission'))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.01)
            assert entered.is_set()
            assert engine.cancel(spec, {'operation_id': started['operation_ref']['id']})['status'] == 'cancelled'
        finally:
            release.set()
            await asyncio.wait_for(running, 10)
        result = engine.read(spec, {'operation_id': started['operation_ref']['id']})
        assert result['status'] == 'cancelled'
        assert result['available_results']
        assert len(calls) == 1
        child = next(x for x in executor._work('mission') if x['body']['spec']['handler_id'] == 'web-query')
        cap = executor.capacity.get(executor._capacity_id(executor._spec_from(child)))
        assert cap['phase'] == 'known_unaccepted'
    asyncio.run(scenario())


def test_searxng_uses_only_dedicated_configuration(tmp_path):
    from ora.retrieval.web_config import searx_endpoint
    for value in ('http://127.0.0.1:18080', 'http://localhost:8080', 'https://user:password@example.org'):
        with pytest.raises(ValueError):
            searx_endpoint({'OSRA_SEARXNG_BASE_URL': value})
    calls = []
    def transport(method, url, **kwargs):
        calls.append(url)
        return HTTPResult(200, b'{"results":[{"url":"https://example.org/other","title":"Other source","content":"Discovery only"}]}', {})
    store, executor, engine, spec = setup_search(tmp_path, transport, values={'OSRA_SEARXNG_BASE_URL': 'http://127.0.0.1:18081'})
    start = engine.start(spec, {'mode': 'research', 'provider': 'searxng', 'information_need': 'A complementary index', 'queries': ['topic']})
    asyncio.run(executor.run_ready('mission'))
    assert calls[0].startswith('http://127.0.0.1:18081/search?')
    assert engine.read(spec, {'operation_id': start['operation_ref']['id']})['status'] == 'completed'


def test_saved_capacity_limits_can_only_tighten_web_and_retrieval(tmp_path):
    from ora.coordination.capacity import CapacityScheduler
    store, executor, engine, spec = setup_search(tmp_path, lambda *a, **k: response())
    existing = executor.capacity.policy
    tighter = provider_policy(existing, {'OSRA_EXA_CONCURRENCY': '3',
                                         'OSRA_EXA_REQUESTS_PER_SECOND': '2',
                                         'OSRA_RETRIEVAL_SLOTS': '4'})
    CapacityScheduler.add_resource_scopes(store, tighter)
    assert store.get('capacity_config', 'default')['body'] == tighter.snapshot()
    assert tighter.permits['retrieval'] == 4
    assert tighter.quotas['web:exa'].requests == 2
    assert tighter.quotas['web:exa'].concurrency == 3
    with pytest.raises(Exception):
        CapacityScheduler.add_resource_scopes(store, existing)


def test_reuse_checks_producing_source_permissions(tmp_path, monkeypatch):
    from ora.coordination.board import ref
    from ora.mission.context import WorkingContext

    calls = []
    store, executor, engine, spec = setup_search(tmp_path, lambda *a, **k: (calls.append(True), response())[1])
    source_ref = {'kind': 'source_copy', 'id': 'synthetic-copy', 'version': 1}
    def save(tx):
        call = tx.put('agent_call', 'synthetic-call', {'mission_id': 'mission'})
        tx.put('model_request_intent', 'model-intent:synthetic-call:1', {
            'call_ref': ref(call), 'source_refs': [source_ref],
            'context_source_dependency_refs': [],
        })
        return {}
    store.transact('synthetic-search-lineage', {}, save)
    spec = replace(spec, call_id='synthetic-call', call_version=1)
    permission = {'allowed': True}
    def current_use(self, refs):
        assert refs == [source_ref]
        return [{'model_input_status': 'permitted' if permission['allowed'] else 'restricted'}]
    monkeypatch.setattr(WorkingContext, 'require_current_source_use', current_use)
    started = engine.start(spec, {'mode': 'quick', 'information_need': 'A source-bound search', 'queries': ['topic']})
    permission['allowed'] = False
    with pytest.raises(ValueError, match='source use'):
        engine.read(spec, {'operation_id': started['operation_ref']['id']})
    asyncio.run(executor.run_ready('mission'))
    assert calls == []
    child = next(x for x in executor._work('mission') if x['body']['spec']['handler_id'] == 'web-query')
    cap = executor.capacity.get(executor._capacity_id(executor._spec_from(child)))
    assert cap['phase'] == 'failed'
    assert not cap['reservation']


def test_fast_provider_results_remain_readable_while_other_provider_is_slow(tmp_path):
    async def scenario():
        slow_started, release = threading.Event(), threading.Event()
        def transport(method, url, **kwargs):
            if '18081' in url:
                slow_started.set()
                assert release.wait(6)
            return response()
        store, executor, engine, spec = setup_search(tmp_path, transport, values={
            'EXA_API_KEY': 'synthetic-key', 'OSRA_SEARXNG_BASE_URL': 'http://127.0.0.1:18081'})
        fast = engine.start(spec, {'mode': 'quick', 'information_need': 'One missing fact', 'queries': ['topic']})
        slow = engine.start(replace(spec, work_id='complement'), {'mode': 'research', 'provider': 'searxng',
            'information_need': 'Independent complementary coverage', 'queries': ['alternative topic']})
        running = asyncio.create_task(executor.run_ready('mission'))
        try:
            for _ in range(150):
                first = engine.read(spec, {'operation_id': fast['operation_ref']['id']})
                if slow_started.is_set() and first['status'] == 'completed':
                    break
                await asyncio.sleep(.01)
            assert first['available_results'] and first['status'] == 'completed'
            assert engine.read(spec, {'operation_id': slow['operation_ref']['id']})['status'] == 'pending'
        finally:
            release.set()
            await asyncio.wait_for(running, 8)
    asyncio.run(scenario())
