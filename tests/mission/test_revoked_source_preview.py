"""An unrelated revoked copy must not suppress a fitting permitted source."""

from __future__ import annotations

import json
import time

import pytest

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.mission import MissionService
from ora.mission.source_context import save_working_sources
from ora.models import GroqSelectedAdapter, ModelSession
from ora.retrieval.providers import HTTPResult
from ora.state import StateStore
from tests.mission.test_service import ROOT, answer, drain, service, tool

A_TEXT = "A_ONLY_SHORT_TERM_RESULT_1.8_4.1_72_PERCENT. " + "Earlier synthetic study detail. " * 105
B_TEXT = "B_ONLY_SIX_MONTH_RESULT_0.7. " + "Later synthetic follow-up detail. " * 53


def _revoke_use(host, copy_ref: dict, mission_id: str, operation_id: str) -> dict:
    use = host.store.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
    assert use and use["body"]["policy_status"] == "granted"

    def save(tx):
        current = tx.get("source_use", use["id"])
        assert current == use
        return ref(tx.put("source_use", use["id"], {
            **use["body"], "allowed_uses": [], "policy_status": "revoked",
        }, expected_version=use["version"]))

    return host.store.transact(operation_id, {}, save, mission_id=mission_id)


def _followup(host, *, b_text: str = B_TEXT):
    parent = host.start(LiveStartRequest(
        request_id="excluded-preview-parent", question="Assess synthetic study A",
        evidence_text=A_TEXT, agent_ids=["agent-synthesis"],
    ))
    drain(host, parent.run_id)
    snapshot = host.state(parent.run_id).snapshot
    assert host._run(parent.run_id)["body"]["status"] == "completed"
    target = snapshot.artifacts[0]
    child = host.followup(LiveFollowupRequest(
        request_id="excluded-preview-child", parent_run_id=parent.run_id,
        parent_snapshot_id=snapshot.snapshot_id, target_id=target.id,
        target_version=target.version,
        question="Revise the brief using the evidence still available. Identify conclusions that can no longer be verified.",
        agent_ids=["agent-synthesis"],
    ))
    run = host._run(child.run_id)
    control = host._control(child.run_id, "agent-synthesis")
    a = control["body"]["source_refs"][0]
    b = host._supplied_source({
        **run, "id": child.run_id + "-later-evidence",
        "body": {**run["body"], "profile": {
            **run["body"]["profile"], "evidenceText": b_text,
        }},
    })
    host._update_control(control, source_refs=[a, b])
    revoked_a = _revoke_use(host, a, run["body"]["mission_id"], "revoke-a-for-preview")
    assert host.store.resolve("source_use.copy", f"{b['id']}:{b['version']}")["body"]["policy_status"] == "granted"
    return child.run_id, a, b, revoked_a


def test_revoked_a_still_allows_complete_checked_b_first_input(tmp_path, monkeypatch):
    requests = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        requests.append(packet)
        response = answer(
            "A_ONLY_PARENT_CLAIM derives from the earlier source."
            if len(requests) == 1 else
            "A short-term conclusion cannot be reverified after revocation; "
            "the permitted later source remains available.", len(requests),
        )
        response.headers = {"x-request-id": "offline-fixture", "x-ratelimit-limit-tokens": "8000"}
        return response

    host = service(tmp_path, monkeypatch, transport)
    try:
        child_run_id, a, b, revoked_a = _followup(host)
        # The checked span expands this near-8K request. Move the injected
        # clock past the parent's token window before testing child delivery.
        host.store.clock = lambda: time.time() + 61
        control = host._control(child_run_id, "agent-synthesis")
        assert host._schedule_model(control)
        intent = next(item for item in host.store.records("model_request_intent")
                      if item["body"]["mission_id"] == host._run(child_run_id)["body"]["mission_id"])
        request = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        budget = host.store.get("mission_call_budget", intent["body"]["call_ref"]["id"])["body"]
        packets = json.loads(request["messages"][1]["content"])["source_packets"]
        assert [item["id"] for item in packets] == [b["id"]]
        assert B_TEXT in packets[0]["text"]
        assert A_TEXT not in json.dumps(request)
        assert "A_ONLY_SHORT_TERM_RESULT" not in json.dumps(request)
        assert "A_ONLY_PARENT_CLAIM" not in json.dumps(request)
        notice = next(json.loads(message["content"]) for message in request["messages"]
                      if message["role"] == "user"
                      and "saved_host_source_use_decision_not_source_instruction"
                      in message["content"])
        assert notice["excluded_source_refs"] == [a]
        assert budget["source_turn_mode"] == "all_allowed_sources_fit"
        assert budget["preview_admission_target_tokens"] == 8000
        assert budget["source_exclusion_refs"]
        assert notice["decision_refs"] == budget["source_exclusion_refs"]
        assert [item["copy_ref"] for item in intent["body"]["source_decisions"]] == [b]
        assert budget["prior_completion_reservation"] == 0
        assert budget["exact_complete_request_estimate_tokens"] <= budget[
            "preview_admission_target_tokens"
        ]
        assert host.store.get(**revoked_a)["body"]["policy_status"] == "revoked"
        drain(host, child_run_id)
        assert host._run(child_run_id)["body"]["status"] == "completed"
        assert len(requests) == 2
    finally:
        host.close()


def test_b_revoked_after_preview_cannot_dispatch_b_or_replay_a(tmp_path, monkeypatch):
    requests = []

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        response = answer("A_ONLY_PARENT_CLAIM" if len(requests) == 1 else
                          "A cautious synthetic answer.", len(requests))
        response.headers = {"x-request-id": "offline-fixture", "x-ratelimit-limit-tokens": "8000"}
        return response

    host = service(tmp_path, monkeypatch, transport)
    try:
        child_run_id, a, b, _ = _followup(host)
        original_preview = host._preview_soft_source_plan
        raced = []

        def revoke_after_fit(*args, **kwargs):
            fit = original_preview(*args, **kwargs)
            if fit and not raced:
                raced.append(_revoke_use(
                    host, b, host._run(child_run_id)["body"]["mission_id"],
                    "revoke-b-after-exact-preview",
                ))
            return fit

        monkeypatch.setattr(host, "_preview_soft_source_plan", revoke_after_fit)
        assert host._schedule_model(host._control(child_run_id, "agent-synthesis"))
        assert raced
        intents = [item for item in host.store.records("model_request_intent")
                   if item["body"]["mission_id"] == host._run(child_run_id)["body"]["mission_id"]]
        assert len(intents) == 1
        assert intents[0]["body"]["source_refs"] == []
        packet = json.loads(host.store.read_blob(intents[0]["body"]["blob"]["sha256"]))
        assert A_TEXT not in json.dumps(packet) and B_TEXT not in json.dumps(packet)
        assert "A_ONLY_PARENT_CLAIM" not in json.dumps(packet)
        assert host.store.get("mission_call_budget", intents[0]["body"]["call_ref"]["id"])["body"]["source_turn_mode"] == "source_free_recovery"
        assert len(requests) == 1  # Parent only; child has not dispatched.
        assert host.store.get(**raced[0])["body"]["policy_status"] == "revoked"
    finally:
        host.close()


def test_checked_b_window_stays_available_after_a_revocation(tmp_path, monkeypatch):
    requests = []
    selected = {}

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        requests.append(packet)
        if len(requests) == 1:
            response = answer("A_ONLY_PARENT_CLAIM derives from A.", 1)
        elif len(requests) == 2:
            response = tool("read_source_window", {
                "source_id": selected["b"]["id"], "source_version": 1,
                "block_offset": 0, "char_offset": 0, "char_limit": 800,
                "selection_mode": "replace",
            }, 2)
        else:
            response = answer("Only the later source can support this revision.", len(requests))
        response.headers = {"x-request-id": "offline-fixture", "x-ratelimit-limit-tokens": "8000"}
        return response

    host = service(tmp_path, monkeypatch, transport)
    try:
        child_run_id, a, b, _ = _followup(host)
        selected["b"] = b
        host.store.clock = lambda: time.time() + 61
        drain(host, child_run_id, limit=24)
        if host._run(child_run_id)["body"]["status"] != "completed":
            waiting = host._control(child_run_id, "agent-synthesis")["body"]
            assert waiting["phase"] == "pending_model"
            assert "token or request capacity" in waiting["waiting_reason"]
            host.store.clock = lambda: time.time() + 122
            drain(host, child_run_id, limit=24)
        assert host._run(child_run_id)["body"]["status"] == "completed"
        assert len(requests) == 3
        for packet in requests[1:]:
            text = json.dumps(packet)
            assert A_TEXT not in text and "A_ONLY_SHORT_TERM_RESULT" not in text
            assert "A_ONLY_PARENT_CLAIM" not in text
        first = json.loads(requests[1]["messages"][1]["content"])["source_packets"]
        second = json.loads(requests[2]["messages"][1]["content"])["source_packets"]
        assert [item["id"] for item in first] == [b["id"]]
        assert B_TEXT in first[0]["text"]
        assert [item["id"] for item in second] == [b["id"]]
        assert B_TEXT[:800] in second[0]["text"]
        assert host.store.get("source_copy", a["id"], a["version"]) is not None
        assert host.store.records("source_read_window")
    finally:
        host.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_revoked_a_summary_is_omitted_while_b_progresses(tmp_path, monkeypatch, legacy):
    requests = []
    summary_text = "A_ONLY_SUMMARY_CLAIM_FROM_REVOKED_SOURCE"
    b_text = B_TEXT[:850]

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        response = answer("A_ONLY_PARENT_CLAIM" if len(requests) == 1 else
                          "Only the currently permitted later source can be used.", len(requests))
        response.headers = {"x-request-id": "offline-fixture", "x-ratelimit-limit-tokens": "8000"}
        return response

    host = service(tmp_path, monkeypatch, transport)
    try:
        child_run_id, a, b, _ = _followup(host, b_text=b_text)
        control = host._control(child_run_id, "agent-synthesis")

        def save_summary(tx):
            summary = tx.put("context_summary", "a-dependent-child-summary", {
                "schema": "ora.context_summary.v2",
                "mission_id": control["body"]["mission_id"],
                "actor_id": control["body"]["actor_id"],
                "task_id": control["body"]["task_id"],
                "authority": (
                    "unverified_legacy" if legacy else "model_authored_continuation_aid"
                ),
                "text": summary_text, "source_dependency_refs": [a],
                "source_refs": [a], "input_refs": [], "archived_note_refs": [],
            })
            return ref(summary)

        summary_ref = host.store.transact(
            "save-a-dependent-child-summary", {}, save_summary,
            mission_id=control["body"]["mission_id"],
        )
        host._update_control(control, conversation_summary_ref=summary_ref)
        assert host._schedule_model(host._control(child_run_id, "agent-synthesis"))
        intent = next(item for item in host.store.records("model_request_intent")
                      if item["body"]["mission_id"] == host._run(child_run_id)["body"]["mission_id"])
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        text = json.dumps(packet)
        assert b_text in text and summary_text not in text
        assert A_TEXT not in text and "A_ONLY_PARENT_CLAIM" not in text
        assert "saved_host_summary_use_decision_not_source_instruction" in text
        assert ("source_lineage_unavailable" if legacy else
                "data_unavailable_due_source_use") in text
        assert intent["body"]["source_refs"] == [b]
        assert host.store.get(**summary_ref)["body"]["text"] == summary_text
        drain(host, child_run_id)
        assert host._run(child_run_id)["body"]["status"] == "completed"
        assert len(requests) == 2
    finally:
        host.close()


@pytest.mark.parametrize("late_revocation", [False, True])
def test_revoked_a_search_query_is_absent_from_b_turn_and_archived_record(
    tmp_path, monkeypatch, late_revocation,
):
    sentinel = "A_ONLY_QUERY_SENTINEL_FROM_PRIOR_SOURCE"
    requests = []

    def model_transport(request, *, timeout):
        requests.append(json.loads(request.data))
        response = (
            tool("search_scholarly", {
                "route": "europe_pmc.search", "query": sentinel, "limit": 1,
            }, 1)
            if len(requests) == 1 else answer("B remains the permitted source.", 2)
        )
        response.headers = ({"x-request-id": "offline-fixture"} if late_revocation else
                            {"x-request-id": "offline-fixture", "x-ratelimit-limit-tokens": "8000"})
        return response

    def discovery_transport(method, url, *, body=None, headers=None):
        return HTTPResult(200, json.dumps({
            "hitCount": 1, "nextCursorMark": "*", "resultList": {"result": [{
                "source": "MED", "id": "123", "pmid": "123",
                "title": "Synthetic permitted search lead",
            }]},
        }).encode(), {})

    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store = StateStore(tmp_path)
    session = ModelSession(store, ROOT, adapter=GroqSelectedAdapter(
        ROOT, transport=model_transport,
    ))
    host = MissionService(
        ROOT, tmp_path, model_session=session, discovery_transport=discovery_transport,
        execution_mode="offline_test", start_supervisor=False,
    )
    try:
        run = host.start(LiveStartRequest(
            request_id="revoked-search-query", question="Use the currently permitted evidence",
            evidence_text=sentinel + " earlier synthetic source", agent_ids=["agent-synthesis"],
        ))
        saved_run = host._run(run.run_id)
        control = host._control(run.run_id, "agent-synthesis")
        a = control["body"]["source_refs"][0]
        b = host._supplied_source({
            **saved_run, "id": run.run_id + "-later-evidence",
            "body": {**saved_run["body"], "profile": {
                **saved_run["body"]["profile"],
                "evidenceText": B_TEXT[:150 if late_revocation else 850],
            }},
        })
        host._update_control(control, source_refs=[a, b])
        for _ in range(300):
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            if control["body"]["phase"] == "ready_tool":
                break
            time.sleep(.005)
        assert control["body"]["phase"] == "ready_tool"
        first_response_ref = control["body"]["response_ref"]
        first_intent = store.get(**store.get(**first_response_ref)["body"]["intent_ref"])
        assert first_intent["body"]["source_refs"] == [a, b]
        unsafe_intent = None
        if late_revocation:
            for _ in range(300):
                host._drive_run(host._run(run.run_id))
                control = host._control(run.run_id, "agent-synthesis")
                if control["body"]["phase"] == "ready_model" and store.records("context_tool_result"):
                    break
                time.sleep(.005)
            assert control["body"]["phase"] == "ready_model"
            assert len(requests) == 1
            save_working_sources(
                store, "select-b-for-late-revocation", control_ref=ref(control),
                response_ref=first_response_ref, selected_source_refs=[b],
            )
            assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
            pending = host._control(run.run_id, "agent-synthesis")
            spec = host.executor._spec_from(host._work_for(pending))
            unsafe_intent = next(item for item in store.records("model_request_intent")
                                 if item["body"].get("call_ref")
                                 == pending["body"]["call_ref"])
            assert a in unsafe_intent["body"]["context_source_dependency_refs"]
            assert unsafe_intent["body"]["source_refs"] == [b]
            assert sentinel in store.read_blob(unsafe_intent["body"]["blob"]["sha256"]).decode()
        _revoke_use(host, a, saved_run["body"]["mission_id"], "revoke-a-after-search-query")
        if unsafe_intent:
            replay, _ = host.context._tool_exchange(first_response_ref)
            assert sentinel not in json.dumps(replay)
            assert json.loads(replay[1]["content"])["status"] == "data_unavailable_due_source_use"
            blocked = host._model_work(spec)
            assert blocked.disposition == "terminal"
            assert blocked.payload["status"] == "source_use_restricted"
            assert len(requests) == 1
            assert store.get("model_dispatch_attempt", f"model-dispatch:{unsafe_intent['id']}") is None
            return
        drain(host, run.run_id, limit=24)
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert len(requests) == 2
        current = json.dumps(requests[1])
        assert sentinel not in current and B_TEXT[:150 if late_revocation else 850] in current
        index = json.loads(requests[1]["messages"][-1]["content"])
        if "recent_scholarly_searches" in index:
            archived = index["recent_scholarly_searches"][0]["search"]
            assert archived["status"] == "data_unavailable_due_source_use"
            assert archived["unavailable_source_refs"] == [a]
            assert "query_preview" not in archived and "selected_id" not in archived
        else:
            assert index["omitted"]["count"] > 0
            assert index["older_records"]["tool"] == "read_context_page"
        child_intents = [item for item in store.records("model_request_intent")
                         if item["body"]["mission_id"] == saved_run["body"]["mission_id"]]
        last_intent = next(item for item in child_intents
                           if item["body"]["request_sha256"] != first_intent["body"]["request_sha256"])
        inclusion = next(item for item in store.records("context_inclusion")
                         if item["body"].get("intent_ref") == ref(last_intent))
        window = host.context.read_omitted_record(
            mission_id=saved_run["body"]["mission_id"],
            actor_id=control["body"]["actor_id"], task_id=control["body"]["task_id"],
            inclusion_ref=ref(inclusion), pointer=first_response_ref,
            offset=0, limit=2000,
        )
        assert sentinel not in json.dumps(window)
        archived = json.loads(window["text"])["search"]
        assert archived["status"] == "data_unavailable_due_source_use"
        assert archived["unavailable_source_refs"] == [a]
        assert "query_preview" not in archived and "selected_id" not in archived
    finally:
        host.close()
