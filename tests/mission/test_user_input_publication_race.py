"""A targeted amendment cannot be lost between saving and accepting a report."""
from ora.contracts.models import LiveStartRequest
from tests.mission.test_service import answer, service, tool
from tests.mission.test_user_input_delivery import _command, _until


def test_user_input_after_report_save_preserves_history_and_refreshes_acceptance(tmp_path, monkeypatch):
    packets, errors, saved = [], [], {}
    text = 'Synthetic user correction: reconsider the archive label.'

    def transport(request, *, timeout):
        try:
            packets.append(request.data)
            if len(packets) == 1:
                return tool('publish_result', {
                    'title': 'Earlier label', 'text': 'Earlier synthetic archive label.',
                    'citations': [], 'limitations': [],
                }, 1)
            assert len(packets) == 2 and text in request.data.decode()
            return answer('Revised synthetic archive label after the user amendment.', 2)
        except BaseException as exc:
            errors.append(repr(exc))
            raise

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(request_id='user-input-publication-race',
            question='Describe the synthetic archive label.', agent_ids=['agent-synthesis']))
        original_accept = host.executor._accept

        def accept(spec, cap, fence, result, **kwargs):
            if spec.handler_id == 'mission-tool' and not saved:
                reports = [p for p in result.output_refs if p['kind'] == 'report']
                if reports:
                    saved['report'] = reports[0]
                    assert host.store.get(**reports[0])['body']['body'] == 'Earlier synthetic archive label.'
                    saved['input'] = host.submit_user_input(_command(host, run.run_id, text))
            return original_accept(spec, cap, fence, result, **kwargs)

        monkeypatch.setattr(host.executor, '_accept', accept)
        _until(host, run.run_id, lambda: host._run(run.run_id)['body']['status'] == 'completed')
        assert not errors and len(packets) == 2 and saved
        final_ref = host._control(run.run_id, 'agent-synthesis')['body']['report_ref']
        assert final_ref != saved['report']
        assert host.store.get(**saved['report'])['body']['body'] == 'Earlier synthetic archive label.'
        assert 'Revised synthetic' in host.store.get(**final_ref)['body']['body']
    finally:
        host.close()
