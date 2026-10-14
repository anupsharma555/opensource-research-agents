"""Wrong tool IDs receive useful recovery only within the exact task's source scope."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.coordination.executor import WorkSpec
from ora.mission.service import LocalToolRejection
from ora.retrieval.providers import HTTPResult
from tests.mission.test_service import _search_spec, answer, drain, revoke_source, service, tool


def task_spec(host, run_id: str, alias: str = "agent-synthesis") -> WorkSpec:
    control = host._control(run_id, alias)
    host._update_control(control, work_id=f"test-inspect-{run_id}")
    body = host._control(run_id, alias)["body"]
    return WorkSpec(
        work_id=body["work_id"], mission_id=body["mission_id"], run_id=run_id,
        proposal_ref=body["proposal_ref"], actor_id=body["actor_id"],
        configuration_ref={}, input_refs=(), handler_id="mission-tool",
        handler_version="1", kind="tool", lane="local",
    )


def rejected(host, spec, supplied_id):
    with pytest.raises(LocalToolRejection) as error:
        host._inspect_hit(spec, {"hit_id": supplied_id})
    return error.value


def signature(error):
    return error.field, error.condition, error.valid_form, error.next_action, error.details


@pytest.mark.parametrize("inherit", [False, True])
def test_saved_source_id_points_to_exact_reader_without_fetch(tmp_path, monkeypatch, inherit):
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        return answer("A scoped fixture answer.", 1)

    host = service(tmp_path, monkeypatch, transport)
    try:
        parent = host.start(LiveStartRequest(
            request_id="source-id-feedback", question="Review the supplied source",
            evidence_text="Project-owned fixture: the study compared two support conditions.",
            agent_ids=["agent-synthesis"],
        ))
        source_ref = host._control(parent.run_id, "agent-synthesis")["body"]["source_refs"][0]
        selected = parent
        if inherit:
            drain(host, parent.run_id)
            snapshot = host.state(parent.run_id).snapshot
            report = snapshot.artifacts[0]
            selected = host.followup(LiveFollowupRequest(
                request_id="inherited-source-feedback", parent_run_id=parent.run_id,
                parent_snapshot_id=snapshot.snapshot_id, target_id=report.id,
                target_version=report.version, question="Check the saved source",
                agent_ids=["agent-synthesis"],
            ))
        spec = task_spec(host, selected.run_id)
        assert source_ref in host._control(selected.run_id, "agent-synthesis")["body"]["source_refs"]
        before = len(calls)
        copies = host.store.records("source_copy")
        error = rejected(host, spec, source_ref["id"])
        assert error.field == "hit_id"
        assert "source copy, not a retrieval hit" in error.condition
        assert "another search is not needed" in error.next_action
        example = error.details["source_read_example"]
        assert example["tool"] == "read_source_window"
        assert example["arguments"] == {
            "source_id": source_ref["id"], "source_version": source_ref["version"],
            "block_offset": 0, "char_offset": 0, "char_limit": 1900,
            "selection_mode": "replace",
        }
        assert "Project-owned fixture" not in str(error.details)
        assert len(calls) == before
        assert host.store.records("source_copy") == copies
        assert host.store.records("source_read_window") == []
        assert host.store.records("retrieval_attempt") == []
    finally:
        host.close()


def test_outside_source_unknown_and_wrong_task_authority_are_indistinguishable(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: None)
    try:
        runs = [host.start(LiveStartRequest(
            request_id=f"scope-{n}", question="Read the selected fixture",
            evidence_text=f"Project-owned text {n}", agent_ids=["agent-synthesis"],
        )) for n in range(2)]
        spec = task_spec(host, runs[0].run_id)
        local = host._control(runs[0].run_id, "agent-synthesis")["body"]["source_refs"][0]
        foreign = host._control(runs[1].run_id, "agent-synthesis")["body"]["source_refs"][0]
        generic = signature(rejected(host, spec, "not-a-saved-id"))
        assert signature(rejected(host, spec, foreign["id"])) == generic
        assert signature(rejected(host, replace(spec, mission_id="outside-mission"), local["id"])) == generic
        assert signature(rejected(host, replace(spec, actor_id="outside-actor"), local["id"])) == generic
        assert signature(rejected(host, replace(spec, work_id="old-work"), local["id"])) == generic
        assert "Search within" not in generic[3]
        assert "read_context_page" in generic[3] and "read_context_record" in generic[3]
        assert generic[4] == {}
    finally:
        host.close()


def test_saved_source_revocation_does_not_recommend_reading_or_newer_version(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: None)
    try:
        run = host.start(LiveStartRequest(
            request_id="revoked-source-hint", question="Read permitted text only",
            evidence_text="Project-owned text", agent_ids=["agent-synthesis"],
        ))
        spec = task_spec(host, run.run_id)
        source_ref = host._control(run.run_id, "agent-synthesis")["body"]["source_refs"][0]
        revoked_ref = revoke_source(host, source_ref, spec.mission_id)
        assert revoked_ref["version"] > source_ref["version"]
        error = rejected(host, spec, source_ref["id"])
        assert "source_read_example" not in error.details
        assert error.details["permitted_source_refs"] == []
        assert error.details["unavailable_source_refs"][0]["copy_ref"] == source_ref
        assert error.details["unavailable_source_refs"][0]["model_input_status"] == "restricted"
        assert "cannot change source-use permission" in error.next_action
    finally:
        host.close()


def test_wrong_tool_feedback_reaches_model_and_recovers_with_local_source_reader(tmp_path, monkeypatch):
    calls = []
    source_ref = None
    received_rejection = None

    def transport(request, *, timeout):
        nonlocal received_rejection
        body = json.loads(request.data)
        calls.append(body)
        if len(calls) == 1:
            assert "inspect_hit" in {item["function"]["name"] for item in body["tools"]}
            return tool("inspect_hit", {"hit_id": source_ref["id"]}, 1)
        if len(calls) == 2:
            prior = json.loads(next(m["content"] for m in reversed(body["messages"])
                                    if m["role"] == "tool"))
            received_rejection = prior
            example = prior["source_read_example"]
            return tool(example["tool"], example["arguments"], 2)
        return answer("The saved local source was read.", 3)

    host = service(tmp_path, monkeypatch, transport)
    try:
        host.discovery_transport = lambda *_a, **_kw: HTTPResult(200, json.dumps({
            "hitCount": 1, "nextCursorMark": "*", "resultList": {"result": [{
                "source": "MED", "id": "123", "pmid": "123",
                "title": "Synthetic eligible hit",
                "abstractText": "An unrelated synthetic abstract used only for hit eligibility.",
            }]},
        }).encode(), {})
        run = host.start(LiveStartRequest(
            request_id="wrong-tool-recovery", question="Inspect the already retained source",
            evidence_text="A project-owned source comparing two support conditions.",
            agent_ids=["agent-synthesis"],
        ))
        source_ref = host._control(run.run_id, "agent-synthesis")["body"]["source_refs"][0]
        found, _ = host._search(_search_spec(host, run.run_id, suffix="eligible"), {
            "route": "europe_pmc.search", "query": "synthetic", "limit": 1,
        })
        assert found["hits"]
        saved_searches = host.store.records("retrieval_attempt")
        drain(host, run.run_id)
        assert len(calls) == 3
        assert source_ref["id"] in json.dumps(calls[2]["messages"])
        assert "A project-owned source comparing two support conditions." in json.dumps(calls[2]["messages"])
        assert received_rejection["status"] == "rejected"
        assert received_rejection["external_action"] == "none"
        assert received_rejection["source_ref"] == source_ref
        assert host.store.records("retrieval_attempt") == saved_searches
        assert len(host.store.records("source_read_window")) == 1
        assert host._run(run.run_id)["body"]["status"] == "completed"
    finally:
        host.close()
