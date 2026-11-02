"""Revoked saved reports recover without replaying restricted text or accepting stale work."""

from __future__ import annotations

import asyncio
import json
import socket
import time
from pathlib import Path

import pytest

import ora.mission.service as service_module
from ora.coordination.board import ref
from ora.coordination.executor import ConcurrentExecutor
from ora.mission.report_lineage import report_text_access
from ora.state.store import canonical
from tests.mission.test_report_source_lineage import NOTE_TEXT, _run_note_report
from tests.mission.test_service import service, tool

SAFE_TEXT = "A fresh synthetic conclusion made without the revoked source."


def no_network(*_args, **_kwargs):
    raise AssertionError("Recovery fixture must not open a socket")


@pytest.mark.parametrize("restart", [False, True])
def test_saved_revoked_report_recovers_with_new_permitted_answer(tmp_path: Path, monkeypatch, restart):
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    original_release = ConcurrentExecutor._release_board
    held = []

    def hold_release(self, spec, fence):
        if spec.handler_id == "mission-tool":
            held.append((self, spec, fence))
            return None
        return original_release(self, spec, fence)

    host = reopened = None
    safe_packets = []
    try:
        with pytest.MonkeyPatch.context() as claim_patch:
            claim_patch.setattr(ConcurrentExecutor, "_release_board", hold_release)
            host, run, source, revoke, original_calls = _run_note_report(
                tmp_path, monkeypatch, direct=False, revoke_during_model=False,
            )
            first = host.store.records("report")[0]
            first_body = canonical(first["body"])
            first_response = host.store.records("model_response_receipt")[0]
            first_response_blob = first_response["body"]["blob"]["sha256"]
            first_intent = host.store.get(**first_response["body"]["intent_ref"])
            first_intent_body = canonical(first_intent["body"])
            control = host._control(run.run_id, "agent-synthesis")
            assert control["body"]["waiting_reason"] == "Saved report awaits current task claim release"
            assert host._work_for(control)["body"]["phase"] == "completed"
            assert report_text_access(host.store, first)["source_dependency_refs"] == [source["copy"]]
            assert len(held) == 1
            revoke()
            assert report_text_access(host.store, first)["status"] == "data_unavailable_due_source_use"
            for owner, spec, fence in held:
                original_release(owner, spec, fence)
        if restart:
            host.close()
            host = None
            reopened = service(tmp_path, monkeypatch, no_network)
            active = reopened
        else:
            active = host

        def revised_transport(request, *, timeout):
            packet = json.loads(request.data)
            messages = packet["messages"]
            safe_packets.append(packet)
            assert NOTE_TEXT not in request.data.decode()
            exchanges = [message for message in messages
                         if message.get("role") == "tool"
                         and message.get("name") == "publish_result"]
            assert exchanges
            assert json.loads(exchanges[-1]["content"])["status"] == "data_unavailable_due_source_use"
            assistant = next(message for message in messages
                             if message.get("role") == "assistant"
                             and message.get("tool_calls"))
            assert assistant["tool_calls"][0]["function"]["arguments"] == "{}"
            return tool("publish_result", {
                "title": "Fresh permitted synthetic result", "text": SAFE_TEXT,
                "citations": [], "limitations": ["No revoked source used"],
            }, 2)

        active.credential_profiles.is_run_enabled = lambda _profile, _run: True
        active.session.adapter.credential_profiles.is_enabled = lambda _profile: True
        active.session.adapter.transport = revised_transport
        # The first post-release drive must resolve the saved tool effect. A
        # later drive may ask the injected model for a genuinely new result.
        for _ in range(20):
            active._drive_run(active._run(run.run_id))
            recovered = active._control(run.run_id, "agent-synthesis")
            if recovered["body"]["phase"] == "ready_model":
                break
        assert recovered["body"]["phase"] == "ready_model", (
            recovered["body"]["waiting_reason"],
            recovered["body"].get("work_id"),
            active._work_for(recovered)["body"]["phase"] if active._work_for(recovered) else None,
        )
        assert len(active.store.records("report")) == 1
        assert not any(item["body"].get("completes_task") for item in active.store.records("board_outcome"))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            active._drive_run(active._run(run.run_id))
            mission_id = active._run(run.run_id)["body"]["mission_id"]
            active.executor._prepare(mission_id)
            asyncio.run(active.executor.run_ready(mission_id))
            if active._run(run.run_id)["body"]["status"] == "completed":
                break
            time.sleep(0.01)
        assert active._run(run.run_id)["body"]["status"] == "completed"
        assert len(original_calls) == 1
        assert len(safe_packets) == 1
        assert len(active.store.records("model_response_receipt")) == 2
        reports = active.store.records("report")
        assert len(reports) == 2
        assert canonical(active.store.get("report", first["id"])["body"]) == first_body
        assert active.store.get("model_response_receipt", first_response["id"])["body"][
            "blob"
        ]["sha256"] == first_response_blob
        assert canonical(active.store.get(**ref(first_intent))["body"]) == first_intent_body
        assert report_text_access(active.store, first)["status"] == "data_unavailable_due_source_use"
        assert report_text_access(active.store, first)["source_dependency_refs"] == [source["copy"]]
        accepted = [item for item in active.store.records("board_outcome")
                    if item["body"].get("completes_task")]
        assert len(accepted) == 1
        assert ref(first) not in accepted[0]["body"]["result_refs"]
        assert len(accepted[0]["body"]["result_refs"]) == 1
        final = active.store.get(**accepted[0]["body"]["result_refs"][0])
        assert final["body"]["body"] == SAFE_TEXT
        assert report_text_access(active.store, final)["status"] == "available"
        assert report_text_access(active.store, final)["source_dependency_refs"] == []
    finally:
        if reopened is not None:
            reopened.close()
        if host is not None:
            host.close()


def test_revocation_between_claim_and_accept_releases_exact_local_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    original_release = ConcurrentExecutor._release_board
    held = []

    def hold_release(self, spec, fence):
        if spec.handler_id == "mission-tool":
            held.append((self, spec, fence))
            return None
        return original_release(self, spec, fence)

    host = None
    try:
        with pytest.MonkeyPatch.context() as claim_patch:
            claim_patch.setattr(ConcurrentExecutor, "_release_board", hold_release)
            host, run, source, revoke, calls = _run_note_report(
                tmp_path, monkeypatch, direct=False, revoke_during_model=False,
            )
            assert len(held) == 1
            for owner, spec, fence in held:
                original_release(owner, spec, fence)
        original_accept = host.board.accept_result
        accepted_attempts = 0

        def revoke_before_accept(*args, **kwargs):
            nonlocal accepted_attempts
            accepted_attempts += 1
            if accepted_attempts == 1:
                revoke()
            return original_accept(*args, **kwargs)

        monkeypatch.setattr(host.board, "accept_result", revoke_before_accept)
        for _ in range(20):
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            if control["body"]["phase"] == "ready_model":
                break
        assert accepted_attempts == 1
        assert control["body"]["phase"] == "ready_model"
        assert len(calls) == 1
        assert len(host.store.records("report")) == 1
        assert report_text_access(host.store, host.store.records("report")[0])[
            "source_dependency_refs"
        ] == [source["copy"]]
        assert not any(item["body"].get("completes_task")
                       for item in host.store.records("board_outcome"))
        assert any(item["proposal_ref"]["id"] == control["body"]["task_id"]
                   for item in host.board.ready_work(control["body"]["mission_id"]))
    finally:
        if host is not None:
            host.close()


def test_incomplete_lineage_wait_is_stable_and_rechecks_after_verified_repair(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    original_release = ConcurrentExecutor._release_board
    held = []

    def hold_release(self, spec, fence):
        if spec.handler_id == "mission-tool":
            held.append((self, spec, fence))
            return None
        return original_release(self, spec, fence)

    host = None
    try:
        with pytest.MonkeyPatch.context() as claim_patch:
            claim_patch.setattr(ConcurrentExecutor, "_release_board", hold_release)
            host, run, source, _revoke, calls = _run_note_report(
                tmp_path, monkeypatch, direct=False, revoke_during_model=False,
            )
            assert len(held) == 1
            for owner, spec, fence in held:
                original_release(owner, spec, fence)
        report = host.store.records("report")[0]
        original_access = service_module.report_text_access

        def incomplete(reader, candidate):
            if candidate["id"] == report["id"]:
                return {"status": "data_unavailable_due_source_use",
                        "source_dependency_refs": [], "unavailable_source_refs": [],
                        "dependency_lineage_complete": False}
            return original_access(reader, candidate)

        monkeypatch.setattr(service_module, "report_text_access", incomplete)
        versions = []
        for _ in range(4):
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            versions.append(control["version"])
            assert control["body"]["phase"] == "waiting"
            assert control["body"]["waiting_reason"] == "Saved report lineage needs local repair"
        assert versions[1:] == [versions[0]] * 3
        assert len(calls) == 1
        assert len(host.store.records("model_request_intent")) == 1
        assert len(host.store.records("model_response_receipt")) == 1
        assert not host.store.records("context_tool_result")
        assert not any(item["body"].get("completes_task")
                       for item in host.store.records("board_outcome"))

        # Simulate a verified repair restoring the same exact ancestry. The
        # unchanged saved report can then be rechecked and accepted once.
        monkeypatch.setattr(service_module, "report_text_access", original_access)
        for _ in range(10):
            host._drive_run(host._run(run.run_id))
            if host._run(run.run_id)["body"]["status"] == "completed":
                break
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert len(calls) == 1
        assert len(host.store.records("report")) == 1
        assert report_text_access(host.store, report)["source_dependency_refs"] == [source["copy"]]
    finally:
        if host is not None:
            host.close()


def test_permitted_source_free_publish_replay_keeps_its_original_text(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    host, run, _source, _revoke, calls = _run_note_report(
        tmp_path, monkeypatch, direct=False, revoke_during_model=False,
        source_linked=False,
    )
    try:
        assert len(calls) == 1
        report = host.store.records("report")[0]
        response = host.store.records("model_response_receipt")[0]
        original = json.loads(host.store.read_blob(response["body"]["blob"]["sha256"]))
        host.context.save_tool_result(
            "source-free-report-replay", response_ref=ref(response),
            tool_call_id=original["tool_calls"][0]["id"], name="publish_result",
            content=canonical({"status": "saved_not_final"}), effect_ref=ref(report),
        )
        control = host._control(run.run_id, "agent-synthesis")
        exchange, compaction = host.context._tool_exchange(ref(response), scope={
            "mission_id": control["body"]["mission_id"],
            "actor_id": control["body"]["actor_id"],
            "task_id": control["body"]["task_id"],
        })
        assert NOTE_TEXT in canonical(exchange)
        assert compaction is None
    finally:
        host.close()
