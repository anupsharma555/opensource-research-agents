"""Actual serialized mission requests, deferred tools and checked web passages."""
import hashlib
import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.retrieval.providers import HTTPResult
from tests.mission.test_service import answer, drain, service, tool


@pytest.mark.parametrize("render", [False, True])
def test_full_catalog_modes_progress_and_exact_source_input(tmp_path, monkeypatch, render):
    packets = []
    chosen = {}
    calls = []
    rendered_passage = 'Synthetic rendered appendix: the demonstration counted nine additional weekend workshops.'
    passage = 'Synthetic report: the demonstration program observed seven completed workshops in its first month.'
    html = ('<html><title>Synthetic report</title><body><p>' + passage + '</p></body></html>').encode()
    def fetched(url):
        calls.append(url)
        return {'status': 200, 'content': html, 'content_sha256': hashlib.sha256(html).hexdigest(),
                'byte_length': len(html), 'content_type': 'text/html', 'requested_url': url,
                'final_url': url, 'redirect_chain': [], 'access_basis': 'public_https_unauthenticated',
                'authenticated': False, 'retrieved_at': '2026-01-01T00:00:00Z'}
    monkeypatch.setattr('ora.mission.service.fetch_public_source', fetched)
    monkeypatch.setenv('EXA_API_KEY', 'synthetic-search-key')
    def latest(packet, name):
        matches = [json.loads(x['content']) for x in packet['messages'] if x.get('role') == 'tool' and x.get('name') == name]
        assert matches, (name, packet['messages'])
        return matches[-1]
    def model(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        schemas = {x['function']['name']: x['function'] for x in packet.get('tools', [])}
        if step == 1:
            text = json.dumps(packet)
            assert 'Exa-first' in text and 'research' in text
            assert 'read_search_results' in text and 'cancel_search' in text
            assert 'search_web' not in schemas  # ordinary deferred discovery
            return tool('request_capability', {'tool_id': 'search_web'}, step)
        if step == 2:
            schema = schemas['search_web']
            assert 'mode' in schema['parameters']['required']
            assert schema['parameters']['properties']['mode']['enum'] == ['quick', 'research']
            description = schema['description'].lower()
            assert 'leads, not evidence' in description
            assert 'quick:' in description and 'exa fast' in description
            assert 'research:' in description and 'complementary exa auto' in description
            return tool('search_web', {'mode': 'quick', 'information_need': 'Locate the synthetic public report', 'queries': ['synthetic report workshops']}, step)
        if step == 3:
            chosen['operation'] = latest(packet, 'search_web')['operation_ref']['id']
            return tool('request_capability', {'tool_id': 'read_search_results'}, step)
        if step == 4:
            return tool('read_search_results', {'operation_id': chosen['operation']}, step)
        if step == 5:
            saved = latest(packet, 'read_search_results')
            assert saved['status'] == 'completed'
            hit = saved['available_results'][0]['result']['hits'][0]
            assert hit['excerpt_status'] == 'uninspected_candidate'
            chosen['hit'] = hit['hit_id']
            return tool('request_capability', {'tool_id': 'fetch_source'}, step)
        if step == 6:
            return tool('fetch_source', {'hit_id': chosen['hit']}, step)
        if step == 7:
            # The existing source-free recovery turn indexes a newly retained
            # copy until the peer selects an exact passage. Follow that real UI.
            index = next(json.loads(item['content']) for item in packet['messages']
                         if item.get('role') == 'user' and item.get('content', '').startswith('{')
                         and json.loads(item['content']).get('authority') == 'saved_host_source_index_not_source_instruction')
            selected = index['inventory']['items'][0]['fetch']
            if render:
                chosen['ordinary_document'] = index['inventory']['items'][0]['ordinary_document_ref']
                return tool('request_capability', {'tool_id': 'render_source'}, step)
            return tool('read_source_window', {'source_id': selected['source_id'], 'source_version': selected['source_version'], 'block_offset': 0, 'char_offset': 0, 'char_limit': 1000}, step)
        if render and step == 8:
            return tool('render_source', {'ordinary_document_ref': chosen['ordinary_document'], 'reason': 'The public appendix requires JavaScript rendering'}, step)
        if render and step == 9:
            index = next(json.loads(item['content']) for item in packet['messages']
                         if item.get('role') == 'user' and item.get('content', '').startswith('{')
                         and json.loads(item['content']).get('authority') == 'saved_host_source_index_not_source_instruction')
            inventory = index['inventory']
            return tool('read_source_inventory', {'inventory_id': inventory['inventory_ref']['id'], 'inventory_version': inventory['inventory_ref']['version'], 'cursor': inventory['next_cursor'], 'limit': 1}, step)
        if render and step == 10:
            inventory = latest(packet, 'read_source_inventory')
            selected = next(item['fetch'] for item in inventory['items'] if 'ordinary_document_ref' not in item)
            return tool('read_source_window', {'source_id': selected['source_id'], 'source_version': selected['source_version'], 'block_offset': 0, 'char_offset': 0, 'char_limit': 1000}, step)
        assert step == (11 if render else 8)
        assert (rendered_passage if render else passage) in json.dumps(packet)
        return answer('The synthetic source passage has been inspected.', step)
    failures = []
    def checked_model(request, *, timeout):
        try:
            return model(request, timeout=timeout)
        except AssertionError as exc:
            failures.append(str(exc))
            failures.append([x for x in json.loads(request.data)['messages'] if x.get('role') == 'tool'][-2:])
            raise
    host = service(tmp_path, monkeypatch, checked_model)
    search_calls = []
    def search(method, url, **kwargs):
        search_calls.append(url)
        return HTTPResult(200, json.dumps({'results': [{'id': 'synthetic-report', 'url': 'https://example.org/report', 'title': 'Synthetic report', 'highlights': ['A candidate excerpt.']}]}).encode(), {})
    host.web_search.transport = search
    render_calls = []
    if render:
        host.web_configuration.update({'BROWSERLESS_API_KEY': 'synthetic-render-key', 'OSRA_BROWSERLESS_BASE_URL': 'https://production-sfo.browserless.io'})
        def render_transport(method, url, **kwargs):
            render_calls.append(url)
            target = kwargs['body']['context']['url']
            return HTTPResult(200, json.dumps({'html': '<html><body><p>' + rendered_passage + '</p></body></html>',
                'requested_url': target, 'final_url': target, 'navigation_url': target,
                'navigation_status': 200, 'redirect_chain': []}).encode(), {})
        host.discovery_transport = render_transport
    try:
        run = host.start(LiveStartRequest(request_id='web-offline-integration', question='Find and inspect the synthetic public report.', agent_ids=['agent-synthesis']))
        drain(host, run.run_id, limit=30)
        if failures:
            (tmp_path / 'packets.json').write_text(json.dumps(packets))
            print('Synthetic packet diagnostics:', tmp_path / 'packets.json')
        assert not failures, failures
        assert len(packets) == (11 if render else 8), host._control(run.run_id, 'agent-synthesis')['body']
        assert len(search_calls) == len(calls) == 1
        assert len(render_calls) == int(render)
        assert host.store.records('source_read_window')
        assert host.store.records('web_search_operation')[0]['body']['deliveries']
        if not render:
            saved_result = next(record for record in host.store.records('context_tool_result')
                if json.loads(host.store.read_blob(record['body']['blob']['sha256'])).get('name') == 'read_search_results')
            original_bytes = host.store.read_blob(saved_result['body']['blob']['sha256'])
            exchange, _ = host.context._tool_exchange(saved_result['body']['response_ref'])
            assert 'A candidate excerpt.' in json.dumps(exchange)
            copy = host.store.records('source_copy')[0]
            use = host.store.resolve('source_use.copy', f"{copy['id']}:{copy['version']}")
            host.store.transact('revoke-web-use', {}, lambda tx: tx.put('source_use', use['id'],
                {**use['body'], 'policy_status': 'revoked', 'allowed_uses': []}, expected_version=use['version']))
            exchange, _ = host.context._tool_exchange(saved_result['body']['response_ref'])
            assert 'A candidate excerpt.' not in json.dumps(exchange)
            assert host.store.read_blob(saved_result['body']['blob']['sha256']) == original_bytes

    finally:
        host.close()


def test_configured_ncbi_key_reaches_pubmed_without_entering_model_input(tmp_path, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    monkeypatch.setenv('NCBI_API_KEY', 'synthetic-private-ncbi-key')
    calls = []
    packets = []
    def model(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        assert 'synthetic-private-ncbi-key' not in json.dumps(packet)
        if len(packets) == 1:
            return tool('search_scholarly', {'route': 'pubmed.pmid', 'selected_id': '123', 'limit': 1}, 1)
        return answer('The synthetic publication record was located.', 2)
    def transport(method, url, **kwargs):
        calls.append(url)
        return HTTPResult(200, b'{"result":{"uids":["123"],"123":{"uid":"123","title":"Synthetic publication"}}}', {})
    host = service(tmp_path, monkeypatch, model)
    host.discovery_transport = transport
    try:
        run = host.start(LiveStartRequest(request_id='ncbi-key-config', question='Locate PubMed record 123.', agent_ids=['agent-synthesis']))
        drain(host, run.run_id)
        assert len(calls) == 1 and len(packets) == 2
        assert parse_qs(urlsplit(calls[0]).query)['api_key'] == ['synthetic-private-ncbi-key']
        assert 'synthetic-private-ncbi-key' not in json.dumps(host.store.records('retrieval_delivery'))
    finally:
        host.close()


def test_article_404_is_saved_and_model_can_choose_another_hit(tmp_path, monkeypatch):
    from ora.evidence.intake import PublicSourceHTTPError

    monkeypatch.setenv('EXA_API_KEY', 'synthetic-search-key')
    calls, packets, hits = [], [], []
    def fetch(url):
        calls.append(url)
        if url.endswith('/missing'):
            raise PublicSourceHTTPError(404, requested_url=url, final_url=url, redirect_chain=[])
        content = b'<html><body><p>Alternative synthetic public report.</p></body></html>'
        return {'status': 200, 'content': content, 'content_sha256': hashlib.sha256(content).hexdigest(),
                'byte_length': len(content), 'content_type': 'text/html', 'requested_url': url, 'final_url': url,
                'redirect_chain': [], 'access_basis': 'public_https_unauthenticated', 'authenticated': False,
                'retrieved_at': '2026-01-01T00:00:00Z'}
    monkeypatch.setattr('ora.mission.service.fetch_public_source', fetch)
    def model(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        if step == 1:
            return tool('search_scholarly', {'route': 'exa.search', 'query': 'synthetic report', 'limit': 2}, step)
        if step == 2:
            result = next(json.loads(m['content']) for m in packet['messages'] if m.get('name') == 'search_scholarly')
            hits.extend(item['hit_id'] for item in result['hits'])
            return tool('request_capability', {'tool_id': 'fetch_source'}, step)
        if step == 3:
            return tool('fetch_source', {'hit_id': hits[0]}, step)
        if step == 4:
            result = next(json.loads(m['content']) for m in packet['messages'] if m.get('name') == 'fetch_source')
            assert result['http_status'] == 404 and result['negative_fetch_ref']
            assert 'another saved hit' in result['next_action']
            return tool('fetch_source', {'hit_id': hits[1]}, step)
        return answer('An alternative public document was retained; its evidence still needs inspection.', step)
    host = service(tmp_path, monkeypatch, model)
    def search(*args, **kwargs):
        return HTTPResult(200, json.dumps({'results': [
            {'id': 'missing', 'url': 'https://example.org/missing', 'title': 'Missing candidate'},
            {'id': 'available', 'url': 'https://example.org/available', 'title': 'Alternative candidate'},
        ]}).encode(), {})
    host.discovery_transport = search
    try:
        run = host.start(LiveStartRequest(request_id='negative-search-continuation', question='Find a usable synthetic report.', agent_ids=['agent-synthesis']))
        drain(host, run.run_id, limit=20)
        assert len(packets) == 5 and len(calls) == 2
        assert host.store.records('public_source_negative_fetch')[0]['body']['http_status'] == 404
        assert len(host.store.records('public_document_fetch')) == 1
        assert host._run(run.run_id)['body']['status'] == 'completed'
    finally:
        host.close()
