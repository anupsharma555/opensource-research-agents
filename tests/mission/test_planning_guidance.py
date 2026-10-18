"""Optional planning help arrives at use, once, through durable tool history."""

import json

import pytest

from ora.contracts.models import LiveStartRequest
from tests.mission.test_planning_integration import _drive_until, _scripted
from tests.mission.test_service import ROOT, answer, drain, service, tool


def _results(host):
    return [json.loads(host.store.read_blob(record['body']['blob']['sha256']))
            for record in host.store.records('context_tool_result')]


@pytest.mark.parametrize('interrupted', [False, True])
def test_first_selection_delivers_guide_once_and_replays_after_interruption(
    tmp_path, monkeypatch, interrupted,
):
    packets = []
    guide = (ROOT / 'runtime_skills/task_planning/CONCISE.md').read_text()

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        assert guide not in packet['messages'][0]['content']
        if step == 1:
            assert all('optional_planning_guidance' not in message.get('content', '')
                       for message in packet['messages'][1:])
            return tool('request_capability', {'tool_id': 'read_plan'}, step)
        result = json.loads(next(message['content'] for message in reversed(packet['messages'])
                                 if message['role'] == 'tool'))
        if step == 2:
            assert result['optional_planning_guidance']['text'] == guide
            assert result['optional_planning_guidance']['version'] == '2.0.0'
            return tool('read_plan', {}, step)
        assert 'optional_planning_guidance' not in result
        if step == 3:
            return tool('request_capability', {'tool_id': 'save_plan'}, step)
        return answer('The synthetic task needs no further work.', step)

    transport = _scripted(transport)
    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(request_id='once-planning-guide', question='Inspect task state',
                                     agent_ids=['agent-synthesis']))
    if interrupted:
        update = host._update_control

        def crash_after_result(control, **changes):
            if changes.get('planning_guidance_ref'):
                raise RuntimeError('Simulated interruption after durable guide result')
            return update(control, **changes)

        monkeypatch.setattr(host, '_update_control', crash_after_result)
    try:
        if interrupted:
            with pytest.raises(RuntimeError, match='Simulated interruption'):
                _drive_until(host, run.run_id, lambda: False)
            assert len(packets) == 1
            assert len(_results(host)) == 1
        else:
            _drive_until(host, run.run_id, lambda: bool(
                host._control(run.run_id, 'agent-synthesis')['body'].get('planning_guidance_ref')))
        first_result = host.store.records('context_tool_result')[0]
    finally:
        host.close()
    resumed = service(tmp_path, monkeypatch, transport)
    if interrupted:
        from ora.mission import service as service_module
        original_check = service_module.checked_registry

        def changed_package(root, role_id, pins, selected):
            if selected == ["task-planning"]:
                pytest.fail("A committed guidance result must replay without reloading the package")
            return original_check(root, role_id, pins, selected)

        monkeypatch.setattr(service_module, 'checked_registry', changed_package)
    try:
        drain(resumed, run.run_id)
        assert not transport.failures
        assert len(packets) == 4
        assert resumed._run(run.run_id)['body']['status'] == 'completed'
        assert resumed.store.get(first_result['kind'], first_result['id']) == first_result
        issued = [result for result in _results(resumed)
                  if 'optional_planning_guidance' in json.loads(result['content'])]
        assert len(issued) == 1
        control = resumed._control(run.run_id, 'agent-synthesis')['body']
        assert control['planning_guidance_ref']['id'] == first_result['id']
        assert not control.get('active_skill_ids')
    finally:
        resumed.close()


def test_first_tool_use_delivers_guide_when_capability_was_already_selected(tmp_path, monkeypatch):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            return tool('read_plan', {}, 1)
        result = json.loads(next(message['content'] for message in reversed(packet['messages'])
                                 if message['role'] == 'tool'))
        assert result['optional_planning_guidance']['skill_id'] == 'task-planning'
        return answer('No checkpoint was needed for this synthetic task.', 2)

    transport = _scripted(transport)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(request_id='guide-on-use', question='Inspect task state',
                                         agent_ids=['agent-synthesis']))
        host._update_control(host._control(run.run_id, 'agent-synthesis'),
                             requested_capabilities=['read_plan'])
        drain(host, run.run_id)
        assert not transport.failures
        assert len(packets) == 2
        assert host._run(run.run_id)['body']['status'] == 'completed'
    finally:
        host.close()
