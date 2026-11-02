"""Expose hit inspection only when the current saved hit scope can use it."""

from __future__ import annotations

import json

from ora.contracts.models import LiveStartRequest
from ora.models import ModelSession
from ora.retrieval.providers import HTTPResult
from tests.mission.test_service import ROOT, _search_spec, answer, drain, service, tool


def names(request: dict) -> set[str]:
    return {item["function"]["name"] for item in request["tools"]}


def test_note_only_turn_can_publish_provisional_report_without_source_citations(
    tmp_path, monkeypatch,
):
    requests = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        assert "Synthetic provisional comparison" in json.dumps(body["messages"])
        return tool("publish_result", {
            "title": "Provisional shortlist",
            "text": "The saved working note favors closer methods review; primary records remain unverified.",
            "citations": [],
            "limitations": ["Only a synthetic working note was reviewed."],
        }, 1)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="note-only-hit-exposure", question="Review the saved working note provisionally",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")["body"]
        host.notes.write(
            "synthetic-note", mission_id=control["mission_id"],
            actor_id=control["actor_id"], task_ref=control["proposal_ref"],
            note_id="synthetic-note", category="hypothesis",
            text="Synthetic provisional comparison; not a verified publication.",
        )
        drain(host, run.run_id)
        assert len(requests) == 1
        assert all("inspect_hit" not in names(request) for request in requests)
        assert "request_capability" in names(requests[0])
        assert "citations: []" in requests[0]["messages"][0]["content"]
        publish = next(item["function"] for item in requests[0]["tools"]
                       if item["function"]["name"] == "publish_result")
        assert "inspected source/span citations" in publish["description"]
        citation_guidance = publish["parameters"]["properties"]["citations"]["description"]
        assert "note IDs are not source IDs" in citation_guidance
        assert "Use [] for a summary based only on task input or already read peer records" in citation_guidance
        report = host.store.records("report")[0]
        assert report["body"]["citations"] == []
        assert host.state(run.run_id).snapshot.mission.status == "completed"
    finally:
        host.close()


def test_requesting_hit_inspection_before_a_saved_hit_returns_prerequisite(
    tmp_path, monkeypatch,
):
    requests = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            return tool("discover_capabilities", {}, 1)
        if len(requests) == 2:
            return tool("request_capability", {"tool_id": "inspect_hit"}, 2)
        return tool("publish_result", {
            "title": "Bounded result", "text": "No retrieval hit was supplied.",
            "citations": [], "limitations": ["No source was inspected."],
        }, 3)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="hit-request-before-hit", question="Check available saved evidence",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        assert len(requests) == 3
        assert all("inspect_hit" not in names(request) for request in requests)
        results = [json.loads(json.loads(host.store.read_blob(
            item["body"]["blob"]["sha256"]
        ))["content"]) for item in host.store.records("context_tool_result")]
        discovered = next(item for item in results if "granted" in item)
        rejected = next(item for item in results if item.get("field") == "tool_id")
        assert "retained saved-hit text" in discovered["granted"]["inspect_hit"]
        assert discovered["not_ready"]["inspect_hit"] == "Get an eligible saved search result first."
        assert "request_capability" in discovered["next_action"]
        assert rejected["status"] == "rejected"
        assert "search_scholarly" in rejected["next_action"]
        assert host.store.records("mission_capability_selection") == []
        assert host._control(run.run_id, "agent-synthesis")["body"][
            "requested_capabilities"
        ] == []
    finally:
        host.close()


def test_saved_pinned_call_keeps_its_original_hit_tool_manifest(tmp_path, monkeypatch):
    def no_transport(*_args, **_kwargs):
        raise AssertionError("No provider dispatch is needed")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="pinned-hit-manifest", question="Preserve this prepared call",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        monkeypatch.setattr(host, "_has_inspectable_hit", lambda _control: True)
        assert host._schedule_model(control)
        prepared = host._control(run.run_id, "agent-synthesis")
        call = host.store.get(**prepared["body"]["call_ref"])
        intent_ref = {"kind": "model_request_intent",
                      "id": f"model-intent:{call['id']}:{call['version']}", "version": 1}
        intent = host.store.get(**intent_ref)
        assert "inspect_hit" in call["body"]["exposed_tools"]
    finally:
        host.close()

    resumed = service(tmp_path, monkeypatch, no_transport)
    try:
        assert not resumed._has_inspectable_hit(
            resumed._control(run.run_id, "agent-synthesis")
        )
        loaded = ModelSession(resumed.store, ROOT).load(intent_ref, for_dispatch=True)
        assert loaded.request.request_sha256 == intent["body"]["request_sha256"]
        assert "inspect_hit" in resumed.store.get(**prepared["body"]["call_ref"])[
            "body"]["exposed_tools"]
    finally:
        resumed.close()


def test_saved_scholarly_hit_remains_available_within_its_mission(tmp_path, monkeypatch):
    def no_transport(*_args, **_kwargs):
        raise AssertionError("No provider dispatch is needed")

    host = service(tmp_path, monkeypatch, no_transport)
    host.discovery_transport = lambda *_args, **_kwargs: HTTPResult(200, json.dumps({
        "hitCount": 1, "nextCursorMark": "*",
        "resultList": {"result": [{
            "source": "MED", "id": "123", "pmid": "123",
            "title": "Synthetic saved hit", "abstractText": "A saved synthetic abstract.",
        }]},
    }).encode(), {})
    try:
        run = host.start(LiveStartRequest(
            request_id="same-mission-hit-scope", question="Review a saved hit",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        other_run = host.start(LiveStartRequest(
            request_id="other-mission-hit-scope", question="Separate task",
            agent_ids=["agent-synthesis"],
        ))
        synthesis = host._control(run.run_id, "agent-synthesis")
        critique = host._control(run.run_id, "agent-critique")
        other = host._control(other_run.run_id, "agent-synthesis")
        assert not host._has_inspectable_hit(synthesis)
        found, _ = host._search(_search_spec(host, run.run_id, suffix="scope"), {
            "route": "europe_pmc.search", "query": "synthetic", "limit": 1,
        })
        assert found["hits"]
        assert host._has_inspectable_hit(synthesis)
        assert host._has_inspectable_hit(critique)
        assert not host._has_inspectable_hit(other)
        exposed, _ = host._exposed_tools(critique, host._configuration(critique))
        assert "inspect_hit" in exposed
    finally:
        host.close()


def test_requesting_eligible_attached_hit_inspection_reports_direct_call(
    tmp_path, monkeypatch,
):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        assert "inspect_hit" in names(packet)
        if len(packets) == 1:
            return tool("request_capability", {"tool_id": "inspect_hit"}, 1)
        result = next(json.loads(item["content"]) for item in packet["messages"]
                      if item.get("role") == "tool"
                      and item.get("name") == "request_capability")
        assert result["status"] == "selected_for_next_turn"
        assert result["tool_id"] == "inspect_hit"
        assert result["already_attached"] is True
        assert "Call it directly" in result["next_action"]
        return answer("This synthetic hit still needs inspection.", 2)

    host = service(tmp_path, monkeypatch, transport)
    host.discovery_transport = lambda *_args, **_kwargs: HTTPResult(200, json.dumps({
        "hitCount": 1, "nextCursorMark": "*", "resultList": {"result": [{
            "source": "MED", "id": "123", "pmid": "123",
            "title": "Synthetic saved hit", "abstractText": "A saved synthetic abstract.",
        }]},
    }).encode(), {})
    try:
        run = host.start(LiveStartRequest(
            request_id="attached-hit-selection", question="Review a synthetic saved hit",
            agent_ids=["agent-synthesis"],
        ))
        found, _ = host._search(_search_spec(host, run.run_id, suffix="attached"), {
            "route": "europe_pmc.search", "query": "synthetic", "limit": 1,
        })
        assert found["hits"]
        drain(host, run.run_id)
        assert len(packets) == 2
        selection = host.store.records("mission_capability_selection")
        assert len(selection) == 1
        assert selection[0]["body"]["tool_id"] == "inspect_hit"
    finally:
        host.close()
