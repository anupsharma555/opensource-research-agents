"""Exercise saved mission projection semantics with offline model responses."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.mission import projection
from ora.mission.latency import save_expectation
from ora.mission.projection import build_snapshot, persist_snapshot
from ora.retrieval.providers import HTTPResult
from ora.state import StateError
from ora.state.inspection import load_snapshot, save_snapshot
from tests.mission.test_service import answer, drain, service, tool


def test_source_free_preparation_diagnostic_requires_exact_failed_call_lineage():
    mission_id, run_id, task_id = "synthetic-mission", "synthetic-run", "synthetic-task"
    call_ref = {"kind": "agent_call", "id": "synthetic-call", "version": 1}
    attempt_ref = {"kind": "agent_attempt", "id": "synthetic-attempt", "version": 2}
    budget_ref = {"kind": "mission_call_budget", "id": "synthetic-call", "version": 1}
    diagnostic = {"body": {
        "category": "local_context_source_free_recovery", "error_class": "ContextOverflow",
        "mission_id": mission_id, "run_id": run_id, "task_id": task_id,
        "call_ref": call_ref, "attempt_ref": attempt_ref, "budget_ref": budget_ref,
    }}
    records = {
        ("agent_call", "synthetic-call", 1): {"body": {
            "attempt_id": "synthetic-attempt", "mission_id": mission_id,
            "task_id": task_id, "instance_id": "synthetic-agent",
        }},
        ("agent_attempt", "synthetic-attempt", 2): {"body": {
            "status": "failed", "call_ref": call_ref, "mission_id": mission_id,
            "run_id": run_id, "task_id": task_id, "agentId": "synthetic-agent",
        }},
        ("mission_call_budget", "synthetic-call", 1): {"body": {
            "call_ref": call_ref, "mission_id": mission_id, "run_id": run_id,
        }},
    }

    class SavedOnly:
        def get(self, kind, id, version=None):
            return records.get((kind, id, version))

    store = SavedOnly()
    assert projection._source_free_preparation_failure(store, diagnostic, run_id) == "synthetic-attempt"
    unrelated = copy.deepcopy(diagnostic)
    unrelated["body"]["attempt_ref"]["id"] = "other-attempt"
    assert projection._source_free_preparation_failure(store, unrelated, run_id) is None
    assert projection._source_free_preparation_failure(store, diagnostic, "other-run") is None
    records[("model_request_intent", "model-intent:synthetic-call:1", None)] = {"body": {}}
    assert projection._source_free_preparation_failure(store, diagnostic, run_id) is None


def test_retrieved_parent_source_is_reused_by_exact_followup_snapshot(tmp_path, monkeypatch):
    calls = []
    host = None

    def model_transport(request, *, timeout):
        calls.append(json.loads(request.data))
        if len(calls) == 1:
            return tool("search_scholarly", {
                "route": "pubmed.abstract", "selected_id": 12345, "limit": 1,
            }, 1)
        if len(calls) == 2:
            hit = host.store.records("retrieval_hit")[0]
            return tool("inspect_hit", {"hit_id": hit["id"]}, 2)
        if len(calls) == 3:
            copy = host.store.records("source_copy")[0]
            return tool("publish_result", {
                "title": "Synthetic retrieved-source finding",
                "text": "The checked synthetic abstract describes a comparison.",
                "citations": [{"source_id": copy["id"], "span_id": "selected-1"}],
                "limitations": ["This is an offline synthetic publication fixture."],
            }, 3)
        return answer("Synthetic follow-up response.", len(calls))

    xml = (
        "<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>12345</PMID>"
        "<Article><ArticleTitle>Synthetic retrieved trial</ArticleTitle><Abstract>"
        "<AbstractText>Project-owned abstract fixture with a trial comparison.</AbstractText>"
        "</Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
    ).encode()
    host = service(tmp_path, monkeypatch, model_transport)
    host.discovery_transport = lambda *_args, **_kwargs: HTTPResult(200, xml, {})
    parent_run = host.start(LiveStartRequest(
        request_id="retrieved-source-parent", question="Inspect a synthetic trial",
        agent_ids=["agent-synthesis"],
    ))
    drain(host, parent_run.run_id)
    parent = host.state(parent_run.run_id).snapshot
    assert parent.artifacts and len(parent.sources) == 1
    parent_source = parent.inspection.sources[0]
    assert parent_source.origin == "retrieved"

    request = LiveFollowupRequest(
        request_id="retrieved-source-child", parent_run_id=parent_run.run_id,
        parent_snapshot_id=parent.snapshot_id, target_id=parent.artifacts[0].id,
        target_version=parent.artifacts[0].version,
        question="Reconsider the exact retrieved abstract", agent_ids=["agent-critique"],
    )
    child = host.followup(request)
    assert child.reused is False
    repeated = host.followup(request)
    assert repeated.reused is True and repeated.run_id == child.run_id
    child_snapshot = host.state(child.run_id).snapshot
    reused = child_snapshot.inspection.sources[0]
    assert reused.origin == "reused"
    assert reused.from_snapshot_id == parent.snapshot_id
    assert reused.copy_reference == parent_source.copy_reference
    assert reused.encounter_references == parent_source.encounter_references
    assert len(calls) == 3  # Snapshot construction and replay did not call the model.

    saved = load_snapshot(host.store, child_snapshot.snapshot_id)
    altered = copy.deepcopy(saved)
    altered["snapshotId"] = "unrelated-hit-not-inherited"
    altered["inspection"]["sources"][0].update(origin="retrieved", fromSnapshotId=None)
    inspection = altered["inspection"]
    with pytest.raises(StateError, match="accessible retrieval hit"):
        save_snapshot(
            host.store, "reject-unrelated-hit", altered,
            source_provenance=inspection["sources"],
            attempt_provenance=inspection["attempts"],
            detail_refs=[item["reference"] for item in inspection["records"]],
            accepted_outputs=inspection["acceptedOutputReferences"],
            parent_ref={"kind": "research_snapshot", "id": parent.snapshot_id, "version": 1},
            followup_target=inspection["followupTarget"],
            initial_source_ids=inspection["initialSourceIds"],
        )
    assert host.store.get("research_snapshot", altered["snapshotId"]) is None

    drain(host, child.run_id)
    completed_child = host.state(child.run_id).snapshot
    assert completed_child.artifacts
    grandchild = host.followup(LiveFollowupRequest(
        request_id="retrieved-source-grandchild", parent_run_id=child.run_id,
        parent_snapshot_id=completed_child.snapshot_id,
        target_id=completed_child.artifacts[0].id,
        target_version=completed_child.artifacts[0].version,
        question="Review the inherited abstract again", agent_ids=["agent-synthesis"],
    ))
    grandchild_source = host.state(grandchild.run_id).snapshot.inspection.sources[0]
    assert grandchild_source.origin == "reused"
    assert grandchild_source.from_snapshot_id == completed_child.snapshot_id
    assert grandchild_source.copy_reference == parent_source.copy_reference
    assert len(calls) == 4  # The grandchild snapshot itself made no model request.


def test_current_capacity_wait_is_visible_without_rewriting_attempt(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("unused", 1))
    try:
        run = host.start(LiveStartRequest(
            request_id="projection-capacity-wait", question="Read one source",
            agent_ids=["agent-synthesis"],
        ))
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        control = host._control(run.run_id, "agent-synthesis")
        body = control["body"]
        attempt = next(a for a in host.store.records("agent_attempt")
                       if a["body"].get("call_ref") == body["call_ref"])
        capacity = host._save("capacity_operation", "projection-saved-wait", {
            "mission_id": body["mission_id"], "run_id": run.run_id,
            "task_id": body["task_id"], "call_id": body["call_ref"]["id"],
            "phase": "waiting", "wake_at": 1900000000,
        }, mission_id=body["mission_id"])
        host._update_control(control, capacity_wait_ref=ref(capacity),
                             waiting_reason="Waiting for available token capacity")
        host._refresh_snapshot(run.run_id)
        snapshot = host.state(run.run_id).snapshot
        task = next(t for t in snapshot.tasks if t.id == body["task_id"])
        assert task.status == "waiting"
        assert task.waiting_until.timestamp() == 1900000000
        assert task.waiting_operation_reference.id == capacity["id"]
        assert next(a for a in snapshot.attempts if a.id == attempt["id"]).status == "waiting"
        assert host.store.get(**ref(attempt)) == attempt

        # An operation from another call cannot make this task look throttled.
        other = host._save("capacity_operation", "projection-other-wait", {
            **host.store.get(**capacity)["body"], "call_id": "another-call",
        }, mission_id=body["mission_id"])
        host._update_control(host._control(run.run_id, "agent-synthesis"),
                             capacity_wait_ref=ref(other))
        host._refresh_snapshot(run.run_id)
        task = next(t for t in host.state(run.run_id).snapshot.tasks if t.id == body["task_id"])
        assert task.status == "running"
        assert task.waiting_until is None and task.waiting_operation_reference is None
    finally:
        host.close()


def test_independent_reports_stay_separate_and_tasks_point_to_own_attempts(tmp_path, monkeypatch):
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        return answer(
            f"Independent finding {len(calls)} with a distinct interpretation.", len(calls)
        )

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="projection-independent",
            question="Compare interpretations",
            agent_ids=["agent-synthesis", "agent-critique"],
        )
    )
    drain(host, receipt.run_id)
    snapshot = host.state(receipt.run_id).snapshot
    assert snapshot.mission.status == "completed"
    assert len(snapshot.artifacts) == 2
    assert snapshot.mission.report_artifact_id is None
    assert len(snapshot.inspection.accepted_output_references) == 2
    root = next(task for task in snapshot.tasks if task.id == receipt.task_id)
    assert root.attempt_ids == []
    for task in snapshot.tasks:
        if task.id == root.id:
            continue
        assert task.target_artifact_id is None
        assert all(
            attempt.task_id == task.id
            for attempt in snapshot.attempts
            if attempt.id in task.attempt_ids
        )
    assert all(
        "Independent finding" in attempt.result_summary
        for attempt in snapshot.attempts
        if attempt.status == "completed"
    )


def test_saved_report_candidate_waits_for_exact_board_acceptance(tmp_path, monkeypatch):
    calls = []
    release_entered = threading.Event()
    permit_release = threading.Event()

    def transport(request, *, timeout):
        calls.append(request)
        return answer("A saved candidate contribution.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    original_release = host.executor._release_board

    def held_release(spec, fence):
        release_entered.set()
        if not permit_release.wait(5):
            raise AssertionError("Fixture did not release the exact executor claim")
        return original_release(spec, fence)

    monkeypatch.setattr(host.executor, "_release_board", held_release)
    try:
        run = host.start(LiveStartRequest(
            request_id="projection-report-candidate",
            question="Save a candidate before board acceptance",
            agent_ids=["agent-synthesis"],
        ))
        for _ in range(300):
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            if (release_entered.is_set() and control["body"].get("waiting_reason")
                    == "Accepted model result awaits current task claim release"):
                break
            time.sleep(.01)
        else:
            pytest.fail("Saved candidate did not reach the held claim release")
        candidate_ref = control["body"]["report_ref"]
        assert candidate_ref is not None
        assert host.store.get("board_proposal", control["body"]["task_id"])["body"]["status"] == "open"
        pending = host.state(run.run_id).snapshot
        assert pending.artifacts == []
        assert pending.inspection.accepted_output_references == []
        assert pending.mission.report_artifact_id is None
        assert any(item.reference.kind == "model_response_receipt"
                   for item in pending.inspection.records)
        assert len([item for item in pending.diagnostics if item.kind == "report_candidate"
                    and item.task_id == control["body"]["task_id"] and item.attempt_id]) == 1
        assert len(calls) == 1
        assert len(host.store.records("model_response_receipt")) == 1

        permit_release.set()
        drain(host, run.run_id, limit=20)
        accepted = host.state(run.run_id).snapshot
        assert [item.id for item in accepted.artifacts] == [candidate_ref["id"]]
        assert [item.model_dump(mode="json") for item in accepted.inspection.accepted_output_references] == [candidate_ref]
        assert accepted.mission.report_artifact_id == candidate_ref["id"]
        assert not any(item.kind == "report_candidate" for item in accepted.diagnostics)
        assert len(calls) == 1
    finally:
        permit_release.set()
        host.close()


def test_done_control_does_not_accept_a_report_missing_from_its_board_outcome(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("Accepted original.", 1))
    try:
        run = host.start(LiveStartRequest(
            request_id="projection-exact-board-outcome",
            question="Check exact report acceptance",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        control = host._control(run.run_id, "agent-synthesis")
        original_ref = control["body"]["report_ref"]
        original = host.store.get(**original_ref)
        candidate = host._save("report", "projection-unaccepted-candidate", {
            **original["body"], "id": "projection-unaccepted-candidate",
        }, mission_id=control["body"]["mission_id"])
        host._update_control(control, report_ref=ref(candidate))
        host._refresh_snapshot(run.run_id)
        snapshot = host.state(run.run_id).snapshot
        assert snapshot.artifacts == []
        assert snapshot.inspection.accepted_output_references == []
        assert snapshot.mission.report_artifact_id is None
        assert any(item.kind == "report_candidate" for item in snapshot.diagnostics)
        outcome_ref = host.store.get("board_proposal", control["body"]["task_id"])["body"]["outcome_ref"]
        assert original_ref in host.store.get(**outcome_ref)["body"]["result_refs"]
    finally:
        host.close()


def test_projection_marks_inactive_old_attempt_without_inventing_end_time(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("Accepted result.", 1))
    try:
        receipt = host.start(
            LiveStartRequest(
                request_id="projection-superseded-old-attempt",
                question="Project exact old attempt status",
                agent_ids=["agent-synthesis"],
            )
        )
        drain(host, receipt.run_id)
        control = host._control(receipt.run_id, "agent-synthesis")
        mission_id = control["body"]["mission_id"]
        completed = next(
            item
            for item in host.store.records("agent_attempt")
            if item["body"].get("run_id") == receipt.run_id
        )
        config = host.store.get(**completed["body"]["configuration_ref"])
        orphan_call = host.configurations.start_call(
            "projection-orphan-call",
            call_id="projection-orphan-call",
            configuration_id=config["id"],
            mission_id=mission_id,
            task_id=control["body"]["task_id"],
            attempt_id="projection-orphan-attempt",
            loaded_skill_ids=list(config["body"]["skills"]),
            exposed_tool_ids=host._exposed_tools(control, config)[0],
            require_common_tools=False,
        )
        host._save(
            "agent_attempt",
            "projection-orphan-attempt",
            {
                **completed["body"],
                "call_ref": ref(orphan_call),
                "status": "running",
                "endedAt": None,
                "report_ref": None,
            },
            mission_id=mission_id,
        )
        host._save(
            "agent_attempt",
            completed["id"],
            {**completed["body"], "status": "running", "endedAt": None},
            mission_id=mission_id,
        )
        host._refresh_snapshot(receipt.run_id)
        snapshot = host.state(receipt.run_id).snapshot
        projected = {item.id: item for item in snapshot.attempts}
        assert projected["projection-orphan-attempt"].status == "superseded"
        assert projected["projection-orphan-attempt"].ended_at is None
        assert projected[completed["id"]].status == "completed"
        assert projected[completed["id"]].ended_at is None
        assert (
            host.store.get("agent_attempt", "projection-orphan-attempt")["body"]["status"]
            == "running"
        )
        assert host.store.get("agent_attempt", completed["id"])["body"]["status"] == "running"
    finally:
        host.close()


def test_only_exact_next_preparation_shows_local_context_repair(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("unused", 1))
    try:
        run = host.start(LiveStartRequest(
            request_id="projection-context-preparation",
            question="Inspect saved context preparation",
            agent_ids=["agent-synthesis"],
        ))
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        control = host._control(run.run_id, "agent-synthesis")
        body = control["body"]
        prior_attempt = next(
            item for item in host.store.records("agent_attempt")
            if item["body"].get("call_ref") == body["call_ref"]
        )
        config = host.store.get(**prior_attempt["body"]["configuration_ref"])
        next_call_id, next_attempt_id = host._model_ids(control, body["sequence"] + 1)
        next_call = host.configurations.start_call(
            next_call_id,
            call_id=next_call_id,
            configuration_id=config["id"],
            mission_id=body["mission_id"],
            task_id=body["task_id"],
            attempt_id=next_attempt_id,
            loaded_skill_ids=list(config["body"]["skills"]),
            exposed_tool_ids=host._exposed_tools(control, config)[0],
            require_common_tools=False,
        )
        host._save("agent_attempt", next_attempt_id, {
            **prior_attempt["body"],
            "call_ref": ref(next_call),
            "status": "running",
            "endedAt": None,
            "report_ref": None,
        }, mission_id=body["mission_id"])
        host._save("mission_call_budget", next_call_id, {
            "mission_id": body["mission_id"], "run_id": run.run_id,
            "call_ref": ref(next_call),
        }, mission_id=body["mission_id"])
        host._update_control(
            control, phase="waiting", work_id=None,
            waiting_reason="ContextOverflow: this branch needs local repair",
        )
        host._refresh_snapshot(run.run_id)
        snapshot = host.state(run.run_id).snapshot
        projected = {item.id: item for item in snapshot.attempts}
        explanations = {item.attempt_id: item.explanation for item in snapshot.inspection.attempts}
        assert projected[next_attempt_id].status == "waiting"
        assert "no model request intent, provider dispatch, or response" in explanations[next_attempt_id]
        assert "original attempt record remains running" in explanations[next_attempt_id]
        assert projected[prior_attempt["id"]].status != "waiting"
        assert host.store.get("agent_attempt", next_attempt_id)["body"]["status"] == "running"
        assert host.store.get("model_request_intent", f"model-intent:{next_call_id}:1") is None
        assert host.store.records("model_dispatch_attempt") == []
        assert host.store.records("model_response_receipt") == []

        # An unrelated historical attempt cannot inherit the current control's wait.
        other_call = host.configurations.start_call(
            "projection-unrelated-call",
            call_id="projection-unrelated-call",
            configuration_id=config["id"],
            mission_id=body["mission_id"],
            task_id=body["task_id"],
            attempt_id="projection-unrelated-attempt",
            loaded_skill_ids=list(config["body"]["skills"]),
            exposed_tool_ids=host._exposed_tools(control, config)[0],
            require_common_tools=False,
        )
        host._save("agent_attempt", "projection-unrelated-attempt", {
            **prior_attempt["body"],
            "call_ref": ref(other_call), "status": "running", "endedAt": None,
            "report_ref": None,
        }, mission_id=body["mission_id"])
        host._refresh_snapshot(run.run_id)
        projected = {item.id: item for item in host.state(run.run_id).snapshot.attempts}
        assert projected[next_attempt_id].status == "waiting"
        assert projected["projection-unrelated-attempt"].status == "superseded"

        host._update_control(
            host._control(run.run_id, "agent-synthesis"),
            waiting_reason="An unrelated host wait",
        )
        host._refresh_snapshot(run.run_id)
        projected = {item.id: item for item in host.state(run.run_id).snapshot.attempts}
        assert projected[next_attempt_id].status == "superseded"
    finally:
        host.close()


def test_followup_target_is_parent_input_and_new_events_change_snapshot(tmp_path, monkeypatch):
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        return answer("Saved parent finding with a bounded answer.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    parent = host.start(
        LiveStartRequest(
            request_id="projection-parent",
            question="Parent question",
            agent_ids=["agent-synthesis"],
        )
    )
    drain(host, parent.run_id)
    parent_snapshot = host.state(parent.run_id).snapshot
    target = parent_snapshot.artifacts[0]
    child = host.followup(
        LiveFollowupRequest(
            request_id="projection-child",
            parent_run_id=parent.run_id,
            parent_snapshot_id=parent_snapshot.snapshot_id,
            target_id=target.id,
            target_version=target.version,
            question="Review parent finding",
            agent_ids=["agent-critique"],
        )
    )
    child_snapshot = host.state(child.run_id).snapshot
    assert all(
        task.target_artifact_id == target.id
        for task in child_snapshot.tasks
        if task.parent_task_id == child.task_id
    )
    run = host._run(child.run_id)
    controls = [host._control(child.run_id, alias) for alias in run["body"]["agent_ids"]]
    components_before = len(host.store.records("snapshot_component"))
    objects_before = len(list(host.store.blobs.root.iterdir()))
    same = persist_snapshot(host.store, run, controls)
    assert same["id"] == child_snapshot.snapshot_id
    assert len(host.store.records("snapshot_component")) == components_before
    assert len(list(host.store.blobs.root.iterdir())) == objects_before

    def add_event(tx):
        tx.emit(run["body"]["mission_id"], "peer_review_observed", {"summary": "Saved later event"})
        return {"saved": True}

    host.store.transact(
        "projection-later-event", {}, add_event, mission_id=run["body"]["mission_id"]
    )
    newer = persist_snapshot(host.store, run, controls)
    assert newer["id"] != same["id"]
    assert len(host.store.records("snapshot_component")) > components_before
    assert any(
        event["kind"] == "peer_review_observed"
        for event in load_snapshot(host.store, newer["id"])["events"]
    )
    components_after = len(host.store.records("snapshot_component"))
    objects_after = len(list(host.store.blobs.root.iterdir()))
    assert persist_snapshot(host.store, run, controls) == newer
    assert len(host.store.records("snapshot_component")) == components_after
    assert len(list(host.store.blobs.root.iterdir())) == objects_after


def test_unopened_discovery_hit_is_inspectable_without_becoming_evidence(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("No call needed.", 1))
    receipt = host.start(
        LiveStartRequest(
            request_id="projection-discovery",
            question="Review leads",
            agent_ids=["agent-synthesis"],
        )
    )
    run = host._run(receipt.run_id)
    control = host._control(receipt.run_id, "agent-synthesis")

    def save_hit(tx):
        return tx.put(
            "retrieval_hit",
            "unopened-lead",
            {
                "mission_id": run["body"]["mission_id"],
                "task_id": control["body"]["task_id"],
                "provider": "synthetic_search",
                "query": "adult comparison",
                "source": {
                    "title": "A saved search lead",
                    "original_url": "https://example.test/lead",
                    "access_surface": "metadata",
                    "rights_status": "unknown",
                },
            },
        )

    host.store.transact(
        "projection-discovery-hit", {}, save_hit, mission_id=run["body"]["mission_id"]
    )
    snapshot = load_snapshot(host.store, persist_snapshot(host.store, run, [control])["id"])
    hit = next(
        item
        for item in snapshot["inspection"]["records"]
        if item["reference"]["kind"] == "retrieval_hit"
        and item["reference"]["id"] == "unopened-lead"
    )
    assert hit["data"]["source"]["title"] == "A saved search lead"
    assert hit["data"]["query"] == "adult comparison"
    assert snapshot["sources"] == []


def test_peer_answer_stays_pending_until_requester_accepts(tmp_path, monkeypatch):
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        return answer(f"Independent answer {len(calls)}.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="projection-peer",
            question="Compare",
            agent_ids=["agent-synthesis", "agent-critique"],
        )
    )
    drain(host, receipt.run_id)
    run = host._run(receipt.run_id)
    controls = [host._control(receipt.run_id, alias) for alias in run["body"]["agent_ids"]]
    requester, responder = controls
    proposal = host.store.get("board_proposal", requester["body"]["task_id"])
    response_report = responder["body"]["report_ref"]
    sent = host.mailbox.send_request(
        "projection-peer-request",
        mission_id=run["body"]["mission_id"],
        request_id="projection-question",
        message_id="projection-question-message",
        requester_id=requester["body"]["actor_id"],
        recipient_id=responder["body"]["actor_id"],
        question="What is your independent assessment?",
        expected_artifact="Report",
        cause_ref=ref(proposal),
        task_ref=ref(proposal),
        payload={"question": "What is your independent assessment?"},
    )
    assert sent["status"] == "accepted"
    submitted = host.mailbox.record_answer(
        "projection-peer-answer",
        mission_id=run["body"]["mission_id"],
        request_id="projection-question",
        answer_id="projection-answer",
        responder_id=responder["body"]["actor_id"],
        artifact_ref=response_report,
    )
    assert submitted["status"] == "accepted"
    pending = load_snapshot(host.store, persist_snapshot(host.store, run, controls)["id"])
    answer_message = next(item for item in pending["messages"] if item["id"] == "projection-answer")
    assert answer_message["status"] == "awaiting_review"
    assert "Independent answer" in answer_message["summary"]
    reviewed = host.mailbox.review_answer(
        "projection-peer-review",
        mission_id=run["body"]["mission_id"],
        request_id="projection-question",
        reviewer_id=requester["body"]["actor_id"],
        answer_id="projection-answer",
        disposition="answered",
        reason="Relevant to question",
    )
    assert reviewed["status"] == "accepted"
    accepted = load_snapshot(host.store, persist_snapshot(host.store, run, controls)["id"])
    assert (
        next(item for item in accepted["messages"] if item["id"] == "projection-answer")["status"]
        == "fulfilled"
    )


def test_direct_invitation_is_inspectable_without_invented_delivery(tmp_path, monkeypatch):
    def transport(request, *, timeout):
        return answer("No model work is needed for this projection check.", 1)

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="projection-invitation",
            question="Compare",
            agent_ids=["agent-synthesis", "agent-critique"],
        )
    )
    run = host._run(receipt.run_id)
    controls = [host._control(receipt.run_id, alias) for alias in run["body"]["agent_ids"]]
    proposer, recipient = controls
    offered = host.board.invite(
        "projection-invitation-offer",
        mission_id=run["body"]["mission_id"],
        invitation_id="direct-invitation",
        proposal_id=proposer["body"]["task_id"],
        sender_id=proposer["body"]["actor_id"],
        recipient_id=recipient["body"]["actor_id"],
        objective="Review the proposed comparison",
        expected_artifact="Methods note",
    )
    assert offered["status"] == "accepted"
    snapshot = load_snapshot(host.store, persist_snapshot(host.store, run, controls)["id"])
    invitation = next(
        item
        for item in snapshot["inspection"]["records"]
        if item["reference"]["kind"] == "board_invitation"
    )
    assert invitation["data"]["status"] == "offered"
    assert invitation["data"]["recipient_ref"]["id"] == recipient["body"]["actor_id"]
    assert "delivered_at" not in invitation["data"]


def test_registered_later_actor_is_named_once_across_multiple_controls(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("Bounded result.", 1))
    receipt = host.start(
        LiveStartRequest(
            request_id="projection-later-actor",
            question="Review",
            agent_ids=["agent-synthesis"],
        )
    )
    run = host._run(receipt.run_id)
    control = host._control(receipt.run_id, "agent-synthesis")
    later = host.registry.register(
        "projection-register-later",
        mission_id=run["body"]["mission_id"],
        run_id=run["id"],
        role_id=host.role_choices["agent-critique"][1],
        display_name="Finch",
    )
    duplicate_actor_control = {
        **control,
        "body": {**control["body"], "phase": "waiting", "waiting_reason": "Provider quota wait"},
    }
    projected, *_ = build_snapshot(
        host.store,
        run,
        [control, duplicate_actor_control],
        snapshot_id="projection-later-actor-snapshot",
    )
    assert sum(agent["id"] == control["body"]["actor_id"] for agent in projected["agents"]) == 1
    assert (
        next(agent for agent in projected["agents"] if agent["id"] == later["id"])["name"]
        == "Finch"
    )
    assert (
        next(agent for agent in projected["agents"] if agent["id"] == later["id"])["status"]
        == "registered"
    )
    assert any(task.get("waitingReason") == "Provider quota wait" for task in projected["tasks"])


def test_deferred_control_has_its_own_saved_task_status(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("No call needed.", 1))
    receipt = host.start(
        LiveStartRequest(
            request_id="projection-deferred",
            question="Review",
            agent_ids=["agent-synthesis"],
        )
    )
    run = host._run(receipt.run_id)
    control = host._control(receipt.run_id, "agent-synthesis")
    deferred = {**control, "body": {**control["body"], "phase": "deferred"}}
    snapshot, *_ = build_snapshot(
        host.store, run, [deferred], snapshot_id="projection-deferred-snapshot"
    )
    task = next(task for task in snapshot["tasks"] if task["id"] == deferred["body"]["task_id"])
    assert task["status"] == "deferred"
    assert task["activeOrWaiting"] == "deferred"
    assert snapshot["agents"][0]["status"] == "deferred"


def test_published_report_is_not_shown_as_a_waiting_tool(tmp_path, monkeypatch):
    def transport(request, *, timeout):
        return tool(
            "publish_result",
            {
                "title": "Bounded answer",
                "text": "Saved exact report.",
                "citations": [],
                "limitations": ["No source supplied"],
            },
            1,
        )

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="projection-publish",
            question="Summarize",
            agent_ids=["agent-synthesis"],
        )
    )
    drain(host, receipt.run_id)
    snapshot = host.state(receipt.run_id).snapshot
    assert snapshot.mission.status == "completed"
    producer = next(
        attempt
        for attempt in snapshot.attempts
        if attempt.id == snapshot.artifacts[0].producer_attempt_id
    )
    assert [(call.name, call.status) for call in producer.tool_calls] == [
        ("publish_result", "published")
    ]


def test_rejected_host_tool_step_is_not_reported_as_completed(tmp_path, monkeypatch):
    count = 0

    def transport(request, *, timeout):
        nonlocal count
        count += 1
        if count == 1:
            return tool(
                "search_scholarly",
                {"route": "invalid-route", "query": "synthetic", "limit": 1},
                count,
            )
        return tool(
            "publish_result",
            {
                "title": "Bounded result",
                "text": "A saved result after local tool rejection.",
                "citations": [],
                "limitations": ["No source supplied"],
            },
            count,
        )

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="projection-rejected-tool",
            question="Summarize",
            agent_ids=["agent-synthesis"],
        )
    )
    drain(host, receipt.run_id)
    snapshot = host.state(receipt.run_id).snapshot
    rejected = next(
        attempt for attempt in snapshot.attempts
        if any(item.name == "search_scholarly" for item in attempt.tool_calls)
    )
    assert [(item.name, item.status) for item in rejected.tool_calls] == [
        ("search_scholarly", "rejected")
    ]
    assert rejected.result_summary is None
    assert snapshot.artifacts


def test_window_passage_must_match_exact_copy_hash_and_locator(monkeypatch):
    content = b"abcdefghij"
    digest = hashlib.sha256(content).hexdigest()
    copy_ref = {"kind": "source_copy", "id": "copy", "version": 1}
    encounter_ref = {"kind": "source_encounter", "id": "encounter", "version": 1}
    publication_ref = {"kind": "publication", "id": "publication", "version": 1}
    locator = "block=1; chars=2:6"
    selected = "cdef"
    stable = hashlib.sha256(f"copy:1:{locator}:{selected}".encode()).hexdigest()[:32]
    passage_ref = {"kind": "source_passage", "id": f"copy:window-{stable}", "version": 1}
    window_ref = {"kind": "source_read_window", "id": "window", "version": 1}
    records = {
        ("source_copy", "copy", 1): {
            "id": "copy",
            "version": 1,
            "body": {
                "encounter": encounter_ref,
                "publication": publication_ref,
                "passage_ids": [],
                "access_surface": "full_text",
                "retain_bytes": True,
                "content_sha256": digest,
                "mime_type": "text/plain",
                "blob": {"sha256": digest},
            },
        },
        ("source_encounter", "encounter", 1): {"body": {"route": "fixture.search"}},
        ("publication", "publication", 1): {"body": {"titles": ["Fixture source"]}},
        ("source_passage", passage_ref["id"], 1): {
            "id": passage_ref["id"],
            "body": {
                "copy_ref": copy_ref,
                "span_id": f"window-{stable}",
                "locator": locator,
                "text": selected,
                "inspection_state": "checked",
                "validation_record": "ora-read-window-v1:fixture-parser:exact-substring",
            },
        },
        ("source_read_window", "window", 1): {
            "body": {
                "copy_ref": copy_ref,
                "content_sha256": digest,
                "parser": "fixture-parser",
                "passage_refs": [passage_ref],
                "selected_locators": [locator],
            }
        },
    }

    class ReadOnlyRecords:
        def get(self, kind, id, version=None):
            return records.get((kind, id, version or 1))

        def read_blob(self, sha256):
            assert sha256 == digest
            return content

    monkeypatch.setattr(
        projection,
        "extract_blocks",
        lambda *_args, **_kwargs: (
            [SimpleNamespace(locator="block=1", text=content.decode())],
            "fixture-parser",
        ),
    )
    store = ReadOnlyRecords()
    view, _provenance = projection._source(store, copy_ref, [window_ref])
    assert view["passages"] == [
        {"spanId": f"window-{stable}", "locator": locator, "text": selected}
    ]
    records[("source_read_window", "window", 1)]["body"]["content_sha256"] = "wrong"
    with pytest.raises(StateError, match="exact source copy"):
        projection._source(store, copy_ref, [window_ref])


def test_projection_version_refreshes_derived_snapshot_without_changing_old_record(
    tmp_path, monkeypatch
):
    model_calls = []

    def transport(request, *, timeout):
        model_calls.append(request)
        return answer("A bounded saved finding.", len(model_calls))

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="projection-version",
            question="What is saved?",
            agent_ids=["agent-synthesis"],
        )
    )
    drain(host, receipt.run_id)
    run = host._run(receipt.run_id)
    controls = [host._control(receipt.run_id, alias) for alias in run["body"]["agent_ids"]]
    original_ref = persist_snapshot(host.store, run, controls)
    original = host.store.get(**original_ref)
    original_snapshot = load_snapshot(host.store, original_ref["id"])
    count = len(model_calls)

    monkeypatch.setattr(projection, "PROJECTION_VERSION", "stage4-atlas-v3-test")
    refreshed_ref = persist_snapshot(host.store, run, controls)
    assert refreshed_ref != original_ref
    assert persist_snapshot(host.store, run, controls) == refreshed_ref
    assert host.store.get(**original_ref) == original
    assert load_snapshot(host.store, original_ref["id"]) == original_snapshot
    assert load_snapshot(host.store, refreshed_ref["id"])["mission"] == original_snapshot["mission"]
    assert len(model_calls) == count


def test_peer_authored_estimate_and_first_contribution_keep_distinct_meanings(
    tmp_path, monkeypatch
):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("Bounded result.", 1))
    receipt = host.start(
        LiveStartRequest(
            request_id="projection-estimate",
            question="Inspect",
            agent_ids=["agent-synthesis"],
        )
    )
    drain(host, receipt.run_id)
    run = host._run(receipt.run_id)
    control = host._control(receipt.run_id, "agent-synthesis")
    mission_id = run["body"]["mission_id"]
    actor = host.store.get("agent_instance", control["body"]["actor_id"], 1)
    task = host.store.get("board_proposal", control["body"]["task_id"])
    attempt = next(
        item
        for item in host.store.records("agent_attempt")
        if item["body"].get("mission_id") == mission_id
    )
    call = host.store.get(**attempt["body"]["call_ref"])
    response = host.store.get(
        "model_response_receipt", f"model-response:model-intent:{call['id']}:{call['version']}"
    )
    save_expectation(
        host.store,
        "projection-estimate-save",
        mission_id=mission_id,
        run_id=run["id"],
        actor_ref=ref(actor),
        task_ref=ref(task),
        response_ref=ref(response),
        category="medium",
        basis="A focused source review is needed",
    )
    controls = [control]
    first = load_snapshot(host.store, persist_snapshot(host.store, run, controls)["id"])
    projected = next(item for item in first["tasks"] if item["id"] == task["id"])
    assert projected["latencyClass"] == "medium"
    assert projected["latencyBasis"] == "A focused source review is needed"
    assert projected["firstContributionAt"] is not None
    assert projected["firstUsefulAt"] is None
    assert first["mission"]["latencyClass"] is None

    def progress_version(tx):
        return ref(
            tx.put("board_proposal", task["id"], task["body"], expected_version=task["version"])
        )

    host.store.transact(
        "projection-task-progress-version", {}, progress_version, mission_id=mission_id
    )
    later = load_snapshot(host.store, persist_snapshot(host.store, run, controls)["id"])
    revised = next(item for item in later["tasks"] if item["id"] == task["id"])
    assert revised["latencyClass"] == "medium"
    assert f"earlier task version v{task['version']}" in revised["latencyBasis"]
    assert "without a recorded purpose change" in revised["latencyBasis"]
