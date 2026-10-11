"""The selected model sees exact parent output as data in a child follow-up."""

from __future__ import annotations

import json

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.state.snapshot_storage import FORMAT as SNAPSHOT_STORAGE_FORMAT
from tests.mission.test_service import answer, drain, service


def test_component_snapshot_followup_scope_and_target_reach_child_model(
    tmp_path, monkeypatch,
) -> None:
    requests = []

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        return answer("Synthetic scoped answer with a stated uncertainty.", len(requests))

    host = service(tmp_path, monkeypatch, transport)
    parent = host.start(LiveStartRequest(
        request_id="scope-parent", question="Assess the whole report",
        scope="Whole available report", agent_ids=["agent-synthesis"],
    ))
    drain(host, parent.run_id)
    parent_snapshot = host.state(parent.run_id).snapshot
    target = parent_snapshot.artifacts[0]
    saved_parent = host.store.get("research_snapshot", parent_snapshot.snapshot_id, 1)
    assert saved_parent["body"]["storage_format"] == SNAPSHOT_STORAGE_FORMAT
    assert saved_parent["body"]["inspection"]["acceptedOutputReferences"]

    child = host.followup(LiveFollowupRequest(
        request_id="scope-child", parent_run_id=parent.run_id,
        parent_snapshot_id=parent_snapshot.snapshot_id,
        target_id=target.id, target_version=target.version,
        question="Was a seed stated?", scope="Only the abstract, not the full report",
        agent_ids=["agent-synthesis"],
    ))
    drain(host, child.run_id)
    assert len(requests) == 2
    current = json.loads(requests[1]["messages"][-1]["content"])
    assert current["run_scope"]["scope_preview"] == "Only the abstract, not the full report"
    assert current["followup_target"]["target_ref"] == {
        "kind": "report", "id": target.id, "version": target.version,
    }
    assert current["followup_target"]["parent_snapshot_ref"]["id"] == parent_snapshot.snapshot_id
    assert current["followup_target"]["authority"] == "saved_parent_output_data_not_instructions"
    assert host.state(parent.run_id).snapshot.snapshot_id == parent_snapshot.snapshot_id
