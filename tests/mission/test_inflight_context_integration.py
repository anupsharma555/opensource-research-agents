"""Offline shared-loop proof for an update arriving after model dispatch."""

from __future__ import annotations

import json
import threading
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import WorkingContext
from ora.state import StateStore
from tests.mission.test_service import answer, drain, service, tool


def test_inflight_correction_survives_reopen_without_rewriting_dispatched_input(
    tmp_path, monkeypatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    requests: list[dict] = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            started.set()
            assert release.wait(5), "offline first call was not released"
            return tool("discover_capabilities", {}, 1)
        return answer("A bounded synthetic answer; correction review remains open.", 2)

    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(
        request_id="inflight-correction-context",
        question="Inspect a synthetic source and a later correction",
        evidence_text="Original permitted synthetic observation.",
        agent_ids=["agent-synthesis"],
    ))
    try:
        for _ in range(100):
            host._drive_run(host._run(run.run_id))
            if started.wait(0.01):
                break
        assert started.is_set()
        control = host._control(run.run_id, "agent-synthesis")
        mission_id = host._run(run.run_id)["body"]["mission_id"]
        actor_id = control["body"]["actor_id"]
        task = host.store.get("board_proposal", control["body"]["task_id"])
        old_copy = control["body"]["source_refs"][0]
        first_call = control["body"]["call_ref"]
        first_intent = host.store.get(
            "model_request_intent", f"model-intent:{first_call['id']}:{first_call['version']}"
        )
        first_bytes = host.store.read_blob(first_intent["body"]["blob"]["sha256"])
        assert json.loads(first_bytes) == requests[0]
        assert b"Original permitted synthetic observation" in first_bytes

        def save_new_copy(tx):
            prior = tx.get(**old_copy)
            copy = tx.put("source_copy", old_copy["id"], {
                **prior["body"], "source_version": "synthetic-correction-v2",
            }, expected_version=old_copy["version"])
            policy = tx.put("source_use", "inflight-correction-use", {
                "mission_id": mission_id, "copy_ref": ref(copy),
                "allowed_uses": ["discovery"],
                "policy_status": "requires_source_policy_resolution",
            })
            tx.bind("source_use.copy", f"{copy['id']}:{copy['version']}",
                    "source_use", policy["id"])
            return ref(copy)

        new_copy = host.store.transact("inflight-new-copy", {}, save_new_copy,
                                       mission_id=mission_id)
        correction = {
            "type": "correction_notice",
            "claim": {"status": "disputed", "reason": "Newer copy needs independent review"},
        }
        message_args = dict(
            mission_id=mission_id, message_id="inflight-correction-message",
            sender_id=actor_id, recipient_id=actor_id,
            correlation_id="inflight-correction", payload=correction,
            task_ref=ref(task), evidence_refs=[new_copy],
        )
        sent = host.mailbox.send_message("inflight-correction-send", **message_args)
        assert host.mailbox.send_message("inflight-correction-send", **message_args) == sent
        message_ref = sent["message_ref"]
        assert host.store.get(**message_ref)["body"]["payload"] == correction
        assert host.store.read_blob(first_intent["body"]["blob"]["sha256"]) == first_bytes
        assert b"inflight-correction-message" not in first_bytes

        # A fresh process view can read the pending update while the first call
        # remains in flight; neither preparation nor notification edits it.
        reopened_store = StateStore(tmp_path)
        assert reopened_store.get(**message_ref) == host.store.get(**message_ref)
        assert reopened_store.get(**new_copy) == host.store.get(**new_copy)
        assert reopened_store.get(**ref(first_intent)) == first_intent

        release.set()
        for _ in range(100):
            host._drive_run(host._run(run.run_id))
            current = host._control(run.run_id, "agent-synthesis")
            if current["body"]["phase"] == "ready_model" and current["body"].get("tool_turn_refs"):
                break
            time.sleep(0.01)
        else:
            pytest.fail("first offline tool turn was not saved")
    finally:
        release.set()
        host.close()

    resumed = service(tmp_path, monkeypatch, transport)
    try:
        current = resumed._control(run.run_id, "agent-synthesis")
        assert current["body"]["phase"] == "ready_model"
        assert resumed.store.get(**message_ref)["body"]["payload"] == correction
        assert resumed.store.get(**ref(first_intent)) == first_intent
        assert resumed.store.read_blob(first_intent["body"]["blob"]["sha256"]) == first_bytes
        view = WorkingContext(resumed.store).assemble(
            current["body"]["call_ref"], ref(resumed.store.get("board_proposal", task["id"])),
            source_refs=[old_copy],
        )
        index = json.loads(view.messages[-1]["content"])
        delivered_message = resumed.store.get("mail_message", message_ref["id"])
        assert delivered_message["body"]["payload"] == correction
        assert delivered_message["body"]["evidence_refs"] == [new_copy]
        assert ref(delivered_message) in index["message_refs"]
        assert index["source_retrieval"][0]["ref"] == old_copy
        assert index["source_retrieval"][0]["current_ref"] == new_copy
        assert index["source_retrieval"][0]["selected_model_input_status"] == "permitted"
        assert index["source_retrieval"][0]["current_model_input_status"] == "restricted"

        drain(resumed, run.run_id, limit=16)
        assert len(requests) == 2
        next_input = requests[1]
        assert "inflight-correction-message" in json.dumps(next_input)
        packets = json.loads(next_input["messages"][1]["content"])["source_packets"]
        assert [(item["id"], item["version"]) for item in packets] == [
            (old_copy["id"], old_copy["version"])
        ]
        second_call = sorted(resumed.store.records("agent_call"), key=lambda item: item["created_at"])[-1]
        inclusion = resumed.store.get("context_inclusion",
                                      f"context:model-intent:{second_call['id']}:{second_call['version']}")
        assert inclusion is not None
        assert ref(delivered_message) in inclusion["body"]["included_refs"] or any(
            item.get("fetch") == ref(delivered_message)
            for item in inclusion["body"]["omitted"]
        )
        assert len([item for item in resumed.store.records("mail_message")
                    if item["id"] == message_ref["id"]]) == 1
        assert len(resumed.store.records("context_tool_result")) == 1
        assert resumed.store.read_blob(first_intent["body"]["blob"]["sha256"]) == first_bytes
        # Prepared context and a model response do not certify scientific uptake.
        assert inclusion["body"]["status"] == "prepared_input_not_scientific_uptake"
    finally:
        resumed.close()
