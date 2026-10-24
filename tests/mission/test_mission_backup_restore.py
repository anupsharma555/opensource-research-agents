"""Restore active synthetic missions through the real StateStore backup boundary."""

from __future__ import annotations

import json
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.mission import MissionService
from ora.models import GroqSelectedAdapter, ModelSession
from ora.state import StateStore
from tests.mission.test_service import ROOT, answer, drain, serialized_peer_name, service, tool


def _restored_host(store: StateStore, transport) -> MissionService:
    session = ModelSession(store, ROOT, adapter=GroqSelectedAdapter(ROOT, transport=transport))
    return MissionService(
        ROOT, store.root, model_session=session,
        execution_mode="offline_test", start_supervisor=False,
    )


def _settle_pending(host: MissionService, run_id: str, actor_id: str, next_phase: str) -> dict:
    mission_id = host._run(run_id)["body"]["mission_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        host.dispatch.poll(mission_id)
        control = host._control(run_id, actor_id)
        work = host._work_for(control)
        if work and work["body"]["phase"] == "completed":
            assert host._advance(control)
            updated = host._control(run_id, actor_id)
            assert updated["body"]["phase"] == next_phase
            return updated
        time.sleep(.005)
    pytest.fail(f"{actor_id} did not reach {next_phase} from saved pending work")


def test_backup_restores_checked_input_and_prepared_model_without_replaying_tool(
    tmp_path, monkeypatch
):
    calls = []
    source_ref = None

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        if len(calls) == 1:
            return tool("read_source_window", {
                "source_id": source_ref["id"], "source_version": source_ref["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 800,
                "selection_mode": "replace",
            }, 1)
        return answer("The checked synthetic passage supports a bounded result.", len(calls))

    original_dir = tmp_path / "original"
    host = service(original_dir, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="backup-checked-model", question="Review the synthetic passage",
            evidence_text="DOI: 10.1234/backup-checked. " + "A" * 1200,
            agent_ids=["agent-synthesis"],
        ))
        source_ref = host._control(run.run_id, "agent-synthesis")["body"]["source_refs"][0]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            host._drive_run(host._run(run.run_id))
            current = host._control(run.run_id, "agent-synthesis")
            if current["body"]["phase"] == "ready_model" and current["body"]["tool_turn_refs"]:
                break
            time.sleep(.005)
        else:
            pytest.fail("The first checked source read was not accepted")
        assert len(calls) == 1
        window = host.store.records("source_read_window")[0]
        assert window["body"]["passage_refs"]
        assert host._schedule_model(current)
        pending = host._control(run.run_id, "agent-synthesis")
        assert pending["body"]["phase"] == "pending_model"
        call_ref = pending["body"]["call_ref"]
        intent = host.store.get("model_request_intent", f"model-intent:{call_ref['id']}:1")
        inclusion = host.store.get("context_inclusion", f"context:{intent['id']}")
        assert inclusion is not None
        prepared_bytes = host.store.read_blob(intent["body"]["blob"]["sha256"])
        assert window["body"]["passage_refs"][0]["id"].encode() in prepared_bytes
        configuration = host.store.get(
            "agent_configuration", host.store.get(**call_ref)["body"]["configuration_id"], 1
        )
        source = host.store.get(**source_ref)
        accepted_tool_results = host.store.records("context_tool_result")
        manifest = host.store.backup(tmp_path / "backup")
        assert manifest["objects"]
    finally:
        host.close()

    restored_store = StateStore.restore(tmp_path / "backup", tmp_path / "restored")
    resumed = _restored_host(restored_store, transport)
    try:
        assert resumed.store.get(**source_ref) == source
        assert resumed.store.get("source_read_window", window["id"]) == window
        assert resumed.store.get("agent_configuration", configuration["id"]) == configuration
        assert resumed.store.get("model_request_intent", intent["id"]) == intent
        assert resumed.store.get("context_inclusion", inclusion["id"]) == inclusion
        assert resumed.store.read_blob(intent["body"]["blob"]["sha256"]) == prepared_bytes
        drain(resumed, run.run_id, limit=30)
        snapshot = resumed.state(run.run_id).snapshot
        assert snapshot.mission.status == "completed"
        assert any("bounded result" in item.body for item in snapshot.artifacts)
        assert len(calls) == 2  # The accepted source-reading model turn was not replayed.
        assert calls[1] == json.loads(prepared_bytes)  # Restore dispatched the pinned request.
        assert resumed.store.records("context_tool_result") == accepted_tool_results
        assert len(resumed.store.records("source_read_window")) == 1
        assert len(resumed.store.records("model_dispatch_attempt")) == 2
    finally:
        resumed.close()


def test_backup_restores_pending_peer_tool_then_accepts_one_answer(
    tmp_path, monkeypatch
):
    calls = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        role = serialized_peer_name(body)
        calls.append((role, body))
        index = sum(item[0] == role for item in calls)
        if role == "Sage" and index == 1:
            return tool("peer_request", {
                "target_instance_id": "agent-critique",
                "question": "Check the synthetic comparator\nand its uncertainty.",
            }, 1)
        if role == "Mxra":
            return tool("publish_result", {
                "title": "Independent peer check", "text": "The comparator remains uncertain.",
                "citations": [], "limitations": ["Synthetic fixture"],
            }, 10 + index)
        if index == 2:
            request_record = resumed.store.records("mail_request")[0]
            messages = json.dumps(body["messages"])
            assert request_record["id"] in messages
            assert request_record["body"]["answer_refs"][0]["id"] in messages
            return tool("review_peer_answer", {
                "request_id": request_record["id"],
                "answer_id": request_record["body"]["answer_refs"][0]["id"],
                "disposition": "answered", "reason": "The independent check resolves the request",
            }, 2)
        return tool("publish_result", {
            "title": "Reviewed synthesis", "text": "The peer confirmed comparator uncertainty.",
            "citations": [], "limitations": ["Synthetic fixture"],
        }, 3)

    original_dir = tmp_path / "original"
    host = service(original_dir, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="backup-peer-tool", question="Review a synthetic comparator",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        sage = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(sage)
        sage = _settle_pending(host, run.run_id, "agent-synthesis", "ready_tool")
        assert len(calls) == 1
        assert host._schedule_tool(sage)
        queued = host._control(run.run_id, "agent-synthesis")
        assert queued["body"]["phase"] == "pending_tool"
        work = host._work_for(queued)
        first_call = host.store.get(**queued["body"]["call_ref"])
        first_intent = host.store.get("model_request_intent", f"model-intent:{first_call['id']}:1")
        first_bytes = host.store.read_blob(first_intent["body"]["blob"]["sha256"])
        assert host.store.records("mail_request") == []
        host.store.backup(tmp_path / "backup")
    finally:
        host.close()

    restored_store = StateStore.restore(tmp_path / "backup", tmp_path / "restored")
    resumed = _restored_host(restored_store, transport)
    try:
        assert resumed.store.get("model_request_intent", first_intent["id"]) == first_intent
        assert resumed.store.read_blob(first_intent["body"]["blob"]["sha256"]) == first_bytes
        assert resumed.store.get("executor_work", work["id"]) == work
        sage = _settle_pending(resumed, run.run_id, "agent-synthesis", "ready_model")
        request_record = resumed.store.records("mail_request")[0]
        assert request_record["body"]["status"] == "unanswered"
        assert len(resumed.store.records("mail_request")) == 1
        mxra = resumed._control(run.run_id, "agent-critique")
        assert mxra["body"]["phase"] == "ready_model"
        assert resumed._schedule_model(mxra)
        mxra = _settle_pending(resumed, run.run_id, "agent-critique", "ready_tool")
        assert resumed._schedule_tool(mxra)
        _settle_pending(resumed, run.run_id, "agent-critique", "done")
        answered = resumed.store.get("mail_request", request_record["id"])
        assert answered["body"]["answer_refs"]
        assert len(resumed.store.records("mail_answer")) == 1
        assert resumed._schedule_model(sage)
        sage = _settle_pending(resumed, run.run_id, "agent-synthesis", "ready_tool")
        assert resumed._schedule_tool(sage)
        sage = _settle_pending(resumed, run.run_id, "agent-synthesis", "ready_model")
        assert resumed._schedule_model(sage)
        sage = _settle_pending(resumed, run.run_id, "agent-synthesis", "ready_tool")
        assert resumed._schedule_tool(sage)
        _settle_pending(resumed, run.run_id, "agent-synthesis", "done")
        resumed._drive_run(resumed._run(run.run_id))
        assert resumed.state(run.run_id).snapshot.mission.status == "completed"
        assert resumed.store.get("mail_request", request_record["id"])["body"]["status"] == "answered"
        assert len(resumed.store.records("mail_request")) == 1
        assert len(resumed.store.records("mail_answer")) == 1
        assert len([item for item in calls if item[0] == "Sage"]) == 3
        assert len([item for item in calls if item[0] == "Mxra"]) == 1
        assert len(resumed.store.records("model_dispatch_attempt")) == 4
        assert any("peer confirmed" in item.body.lower()
                   for item in resumed.state(run.run_id).snapshot.artifacts)
    finally:
        resumed.close()
