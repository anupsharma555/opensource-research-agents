"""Older saved role catalogs do not acquire the new planning grants."""

import pytest

from ora.contracts.models import LiveStartRequest
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
