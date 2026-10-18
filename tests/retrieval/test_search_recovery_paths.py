"""Known negative search and source-fetch paths preserve useful next actions."""

import asyncio
import json

import pytest

from ora.evidence.intake import PublicSourceHTTPError
from ora.mission.web_recovery import record_negative_fetch, saved_negative_fetch
from ora.retrieval.providers import HTTPResult
from tests.retrieval.test_web_search import response, setup_search


def test_quick_two_known_empty_routes_complete_with_gap_choices(tmp_path):
    calls = []
    def transport(method, url, **kwargs):
        calls.append(url)
        return response(empty=True)
    store, executor, engine, spec = setup_search(tmp_path, transport)
    operation = engine.start(spec, {'mode': 'quick', 'information_need': 'Find source evidence',
                                    'queries': ['synthetic question']})
    asyncio.run(executor.run_ready('mission'))
    result = engine.read(spec, {'operation_id': operation['operation_ref']['id']})
    assert result['status'] == 'completed'
    assert len(calls) == 2
    assert [entry['provider'] for entry in result['available_results']] == ['exa', 'tavily']
    assert all(entry['result']['hits'] == [] for entry in result['available_results'])
    recoveries = [entry['result']['recovery'] for entry in result['available_results']]
    assert all(item['scientific_absence_established'] is False for item in recoveries)
    assert 'await_saved_fallback' in recoveries[0]['alternatives']
    assert 'report_gap' in recoveries[1]['alternatives']
    assert all('identifier' not in entry['result'] for entry in result['available_results'])


def test_negative_fetch_is_exact_and_another_saved_hit_remains_available(tmp_path):
    def transport(method, url, **kwargs):
        return HTTPResult(200, json.dumps({'results': [
            {'id': 'one', 'url': 'https://example.org/missing', 'title': 'Missing page',
             'highlights': ['Uninspected candidate']},
            {'id': 'two', 'url': 'https://example.org/available', 'title': 'Available page',
             'highlights': ['Another candidate']},
        ]}).encode(), {})
    store, executor, engine, spec = setup_search(tmp_path, transport)
    operation = engine.start(spec, {'mode': 'research', 'information_need': 'Find an available source',
                                    'queries': ['synthetic question']})
    asyncio.run(executor.run_ready('mission'))
    entries = engine.read(spec, {'operation_id': operation['operation_ref']['id']})['available_results']
    missing, alternative = (entry['result']['hits'][0] for entry in entries)
    error = PublicSourceHTTPError(404, requested_url=missing['original_url'],
                                  final_url=missing['original_url'], redirect_chain=[])
    scope = dict(work_id='fetch-one', mission_id=spec.mission_id, run_id=spec.run_id,
                 actor_id=spec.actor_id, task_id=spec.proposal_ref['id'],
                 hit_ref=missing['hit_ref'], response_ref=None)
    reply, refs = record_negative_fetch(store, **scope, error=error)
    assert refs == ()
    assert reply['http_status'] == 404
    assert reply['classification'] == 'url_not_found'
    assert reply['negative_fetch_ref']['kind'] == 'public_source_negative_fetch'
    assert reply['evidence_scope'] == 'URL retrieval outcome only; no source text was inspected'
    assert 'another saved hit' in reply['next_action'].lower()
    assert saved_negative_fetch(store, **scope) == (reply, ())
    assert saved_negative_fetch(store, **{**scope, 'work_id': 'deliberate-fresh-attempt'}) is None
    assert saved_negative_fetch(store, **{**scope, 'work_id': 'fetch-two',
                                        'hit_ref': alternative['hit_ref']}) is None
    assert alternative['original_url'] == 'https://example.org/available'
    with pytest.raises(ValueError):
        saved_negative_fetch(store, **{**scope, 'hit_ref': alternative['hit_ref']})
