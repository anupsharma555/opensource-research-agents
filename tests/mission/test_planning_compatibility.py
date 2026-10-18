"""Older saved role catalogs do not acquire the new planning grants."""

import pytest

from ora.contracts.models import LiveStartRequest, RuntimePlan
from ora.coordination.board import ref
from tests.mission.test_service import service


@pytest.mark.parametrize("legacy", [False, True])
def test_saved_catalog_without_planning_does_not_gain_tools_on_resume(tmp_path, monkeypatch, legacy):
    def unused(*args, **kwargs):
        pytest.fail("Configuration inspection must not dispatch a model")

    host = service(tmp_path, monkeypatch, unused)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"pre-planning-catalog-{legacy}", question="An unfinished synthetic task",
            agent_ids=["agent-synthesis"],
        ))

        def old_catalog(tx):
            current = tx.get("mission_run", run.run_id)
            body = current["body"]
            for catalog in body["skill_catalog"].values():
                catalog.pop("task-planning", None)
            if legacy:
                body.pop("skill_lifecycle_version", None)
            return ref(tx.put("mission_run", run.run_id, body,
                              expected_version=current["version"]))

        host.store.transact("simulate-prior-catalog", {}, old_catalog)
    finally:
        host.close()

    resumed = service(tmp_path, monkeypatch, unused)
    try:
        control = resumed._control(run.run_id, "agent-synthesis")
        configuration = resumed._configuration(control)["body"]
        assert {"save_plan", "read_plan"}.isdisjoint(configuration["permitted_tools"])
        assert "task-planning" not in configuration["skills"]
        assert not resumed.store.records("model_response_receipt")
    finally:
        resumed.close()


@pytest.mark.parametrize("activate", [False, True])
def test_v1_saved_calls_keep_full_save_revisions_without_new_modify_grant(tmp_path, monkeypatch, activate):
    import json

    from ora.mission.skill_activation import _pin, legacy_planning_skill
    from tests.mission.test_planning import document
    from tests.mission.test_planning_integration import _drive_until, _scripted
    from tests.mission.test_service import ROOT, answer, drain, tool

    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        assert '"modify_plan"' not in packet['messages'][0]['content']
        assert 'optional_planning_guidance' not in json.dumps(packet)
        if step > 1 and activate:
            assert '# Task planning' in packet['messages'][0]['content']
        if step == 1:
            if activate:
                return tool('activate_skill', {'skill_id': 'task-planning'}, step)
            return tool('request_capability', {'tool_id': 'save_plan'}, step)
        if step in {2, 3}:
            schema = next(t['function']['parameters'] for t in packet['tools']
                          if t['function']['name'] == 'save_plan')
            assert schema['properties']['expected_version']['minimum'] == 0
            return tool('save_plan', {'plan': document(step), 'expected_version': step - 2}, step)
        return answer('A synthetic legacy task completed.', step)

    transport = _scripted(transport)
    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(request_id='saved-planning-v1', question='Keep the old contract',
                                     agent_ids=['agent-synthesis']))

    def old_run(tx):
        r = tx.get('mission_run', run.run_id)
        body = r['body']
        body.pop('planning_tool_version')
        for catalog in body['skill_catalog'].values():
            catalog['task-planning'] = _pin(legacy_planning_skill(ROOT), True)
        return ref(tx.put('mission_run', run.run_id, body, expected_version=r['version']))

    host.store.transact('saved-v1-contract', {}, old_run)
    try:
        _drive_until(host, run.run_id, lambda: len(packets) == 2
                     and bool(host.store.records('working_note'))
                     and host._control(run.run_id, 'agent-synthesis')['body']['phase'] == 'ready_model')
        first = host.store.records('working_note')[0]
    finally:
        host.close()
    resumed = service(tmp_path, monkeypatch, transport)
    try:
        drain(resumed, run.run_id)
        assert not transport.failures
        current = resumed.store.get('working_note', first['id'])
        assert current['version'] == 2 and json.loads(current['body']['text']) == RuntimePlan.model_validate(document(3)).model_dump(mode='json')
        assert resumed.store.get(**ref(first)) == first
        assert resumed._run(run.run_id)['body']['status'] == 'completed'
    finally:
        resumed.close()
