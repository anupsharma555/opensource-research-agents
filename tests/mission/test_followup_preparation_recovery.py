"""Exercise durable follow-up acceptance and bounded local preparation recovery."""

from __future__ import annotations

import json
import time

import pytest

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.mission.context import ContextOverflow
from ora.retrieval.providers import HTTPResult
from ora.state import StateError
from tests.mission.test_service import answer, service, tool


def _parent_and_child(host):
    parent_run = host.start(LiveStartRequest(
        request_id="saved-parent", question="Review a synthetic abstract",
        evidence_text="Synthetic abstract with a checked opening and later results. " * 70,
        agent_ids=["agent-synthesis"],
    ))
    for _ in range(200):
        host._drive_run(host._run(parent_run.run_id))
        if host._run(parent_run.run_id)["body"]["status"] == "completed":
            break
        time.sleep(.005)
    parent = host.state(parent_run.run_id).snapshot
    assert parent.artifacts
    artifact = parent.artifacts[0]
    request = LiveFollowupRequest(
        request_id="saved-child", parent_run_id=parent_run.run_id,
        parent_snapshot_id=parent.snapshot_id, target_id=artifact.id,
        target_version=artifact.version,
        question="Check the exact parent result against related work",
        agent_ids=["agent-critique"],
    )
    return request


def _retrieved_parent_and_child(host):
    parent_run = host.start(LiveStartRequest(
        request_id="retrieved-parent", question="Review one retrieved synthetic abstract",
        agent_ids=["agent-synthesis"],
    ))
    for _ in range(400):
        host._drive_run(host._run(parent_run.run_id))
        if host._run(parent_run.run_id)["body"]["status"] == "completed":
            break
        time.sleep(.005)
    parent = host.state(parent_run.run_id).snapshot
    assert len(parent.sources) == 1 and parent.artifacts
    artifact = parent.artifacts[0]
    return LiveFollowupRequest(
        request_id="retrieved-child", parent_run_id=parent_run.run_id,
        parent_snapshot_id=parent.snapshot_id, target_id=artifact.id,
        target_version=artifact.version,
        question="Reassess the parent result against related work",
        agent_ids=["agent-critique"],
    )


def test_followup_receipt_survives_derived_view_failure_and_replays_exactly(
    tmp_path, monkeypatch
):
    requests = []

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        return answer("A qualified synthetic result.", len(requests))

    host = service(tmp_path, monkeypatch, transport)
    try:
        followup = _parent_and_child(host)
        original = host._refresh_snapshot

        def unavailable_view(run_id):
            raise StateError("Synthetic derived view failure")

        monkeypatch.setattr(host, "_refresh_snapshot", unavailable_view)
        child = host.followup(followup)
        assert child.reused is False
        assert host.store.get("mission_run", child.run_id) is not None
        assert len(host._controls(child.run_id)) == 1
        diagnostic = [item for item in host.store.records("mission_host_diagnostic")
                      if item["body"].get("category") == "accepted_run_view_unavailable"
                      and item["body"].get("run_id") == child.run_id]
        assert len(diagnostic) == 1
        assert len(requests) == 1  # View publication never dispatches a model call.
        monkeypatch.setattr(host, "_refresh_snapshot", original)
        replay = host.followup(followup)
        assert replay.reused is True and replay.run_id == child.run_id
        assert len(host._controls(child.run_id)) == 1
        assert host.state(child.run_id).snapshot is not None
        assert len(requests) == 1
    finally:
        host.close()


def test_start_receipt_survives_only_derived_view_failure(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        request = LiveStartRequest(
            request_id="saved-start-view-failure", question="Check saved acceptance",
            agent_ids=["agent-synthesis"],
        )
        original = host._refresh_snapshot

        def unavailable_view(_run_id):
            raise StateError("Synthetic derived view failure")

        monkeypatch.setattr(host, "_refresh_snapshot", unavailable_view)
        receipt = host.start(request)
        assert receipt.reused is False
        assert host.store.get("mission_run", receipt.run_id) is not None
        assert len(host._controls(receipt.run_id)) == 1
        assert host.store.get("mission_snapshot_head", receipt.run_id) is None
        monkeypatch.setattr(host, "_refresh_snapshot", original)
        assert host.start(request).reused is True
        assert host.state(receipt.run_id).snapshot is not None
        assert host.store.records("model_request_intent") == []
    finally:
        host.close()


@pytest.mark.parametrize("force_unfittable", [False, True])
def test_followup_search_result_survives_one_local_context_fallback(
    tmp_path, monkeypatch, force_unfittable
):
    requests = []
    host = None

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            return tool("search_scholarly", {
                "route": "pubmed.abstract", "selected_id": 12345, "limit": 1,
            }, 1)
        if len(requests) == 2:
            hit = host.store.records("retrieval_hit")[0]
            return tool("inspect_hit", {"hit_id": hit["id"]}, 2)
        if len(requests) == 3:
            copy = host.store.records("source_copy")[0]
            return tool("publish_result", {
                "title": "Synthetic parent observation",
                "text": "The checked opening describes a trial comparison.",
                "citations": [{"source_id": copy["id"], "span_id": "selected-1"}],
                "limitations": ["This is a synthetic offline abstract."],
            }, 3)
        return tool("search_scholarly", {
            "route": "crossref.search", "query": "synthetic abstract comparison", "limit": 2,
        }, 4)

    host = service(tmp_path, monkeypatch, transport)
    try:
        xml = (
            "<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>12345</PMID>"
            "<Article><ArticleTitle>Synthetic retrieved trial</ArticleTitle><Abstract>"
            "<AbstractText>Trial comparison in a project-owned abstract. "
            + "Later synthetic result detail. " * 145
            + "</AbstractText></Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
        ).encode()

        def discovery(_method, url, **_kwargs):
            if "crossref" in url:
                return HTTPResult(200, json.dumps({"message": {"items": [
                    {"DOI": "10.1234/synthetic.one", "title": ["Synthetic one"]},
                    {"DOI": "10.1234/synthetic.two", "title": ["Synthetic two"]},
                ], "total-results": 2}}).encode(), {})
            return HTTPResult(200, xml, {})

        host.discovery_transport = discovery
        followup = _retrieved_parent_and_child(host)
        child = host.followup(followup)
        for _ in range(200):
            host._drive_run(host._run(child.run_id))
            control = host._control(child.run_id, "agent-critique")
            if control["body"]["phase"] == "ready_model" and control["body"]["tool_turn_refs"]:
                break
            time.sleep(.005)
        else:
            pytest.fail("Child search result was not saved before next preparation")
        assert len(requests) == 4
        old_call = control["body"]["call_ref"]
        assert host.store.get("model_response_receipt", f"model-response:model-intent:{old_call['id']}:1")
        host._update_control(
            control, input_budget={"observed_limit_tokens": 10000, "target_fraction": .70}
        )
        control = host._control(child.run_id, "agent-critique")
        next_call_id, next_attempt_id = host._model_ids(control, control["body"]["sequence"] + 1)
        original_assemble = host.context.assemble
        if force_unfittable:
            monkeypatch.setattr(host, "_completed_search_has_navigation", lambda *_: False)

        def local_pressure(*args, **kwargs):
            if force_unfittable or kwargs.get("source_refs") and kwargs.get("tool_turn_refs"):
                raise ContextOverflow("Synthetic required context exceeds local allowance")
            return original_assemble(*args, **kwargs)

        monkeypatch.setattr(host.context, "assemble", local_pressure)
        if force_unfittable:
            with pytest.raises(ContextOverflow):
                host._schedule_model(control)
            assert len(requests) == 4
            assert host.store.get("model_request_intent", f"model-intent:{next_call_id}:1") is None
            assert host.store.get("agent_attempt", next_attempt_id)["body"]["status"] == "failed"
            budgets = [item["body"] for item in host.store.records("mission_call_budget")
                       if item["body"].get("run_id") == child.run_id]
            assert sum(b["source_turn_mode"] == "source_free_recovery" for b in budgets) == 1
            return
        assert host._schedule_model(control)
        current = host._control(child.run_id, "agent-critique")
        intent = host.store.get(
            "model_request_intent", f"model-intent:{current['body']['call_ref']['id']}:1"
        )
        assert intent is not None
        prepared = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        assert prepared["model"] == requests[3]["model"]
        assert len(requests) == 4  # Preparation did not repeat the saved search.
        assert host.store.get(
            "context_tool_result", f"tool-result:{control['body']['tool_turn_refs'][-1]['id']}"
        )
        assert current["body"]["call_ref"]["id"] == next_call_id
        assert intent["body"]["source_decisions"]
        budget = host.store.get("mission_call_budget", next_call_id)["body"]
        assert budget["preview_admission_status"] == "fits_pending_saved_intent"
        assert budget["exact_complete_request_estimate_tokens"] <= 7000
        assert budget["current_tool_turn_ref"] is None
        assert budget["compacted_tool_turn_refs"][-1] == control["body"]["tool_turn_refs"][-1]
        assert any("source_packets" in message.get("content", "") for message in prepared["messages"])
        index = json.loads(prepared["messages"][-1]["content"])
        navigation = index["latest_search_navigation"]
        assert navigation["route"] == "crossref.search"
        assert navigation["hit_count"] == 2
        assert len(navigation["first_hit_ids"]) == 2
        assert navigation["result_ref"]["kind"] == "context_tool_result"
        assert index["followup_target"]["target_ref"]["id"] == followup.target_id
        assert index["followup_target"]["fetch_exact_parent_target"]["tool"] == (
            "read_context_record"
        )
    finally:
        host.close()
