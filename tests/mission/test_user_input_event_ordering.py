"""Controlled event ordering preserves both current input and cancellation fences."""

from __future__ import annotations

import json
import time

import pytest

from ora.contracts.models import LiveRunCancelRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.mission.user_input import UserInputError
from tests.mission.test_branch_cancellation import _cancel
from tests.mission.test_pending_review_tools import _setup
from tests.mission.test_post_response_task_message_freshness import start_and_hold
from tests.mission.test_service import answer, service, tool
from tests.mission.test_user_input_delivery import _command, _until

UPDATE = "Amend the current task: distinguish the preliminary count from the later count."
PEER_TEXT = "The synthetic peer reports a later count of three; the preliminary count was seven."


@pytest.mark.parametrize("input_first", [True, False])
@pytest.mark.parametrize("first_output", ["direct", "publication"])
@pytest.mark.parametrize("cancel_after_updates", [False, True])
def test_peer_reply_and_input_during_dispatch_both_reach_next_request(
    tmp_path, monkeypatch, input_first, first_output, cancel_after_updates,
):
    packets, errors, saved = [], [], {}

    def transport(request, *, timeout):
        try:
            packets.append(request.data)
            if len(packets) == 1:
                before = request.data

                def user_input():
                    saved["input"] = host.submit_user_input(_command(host, run.run_id, UPDATE))

                def peer_reply():
                    peer = host._control(run.run_id, "agent-critique")
                    report = host.store.transact(
                        "ordering-peer-report", {}, lambda tx: tx.put("report", "ordering-report", {
                            "mission_id": mission_id, "body": PEER_TEXT,
                            "citations": [], "limitations": ["Synthetic fixture"],
                        }), mission_id=mission_id,
                    )
                    host._update_control(peer, report_ref=ref(report))
                    saved["answer"] = host.mailbox.record_answer(
                        "ordering-peer-answer", mission_id=mission_id,
                        request_id="ordering-request", answer_id="ordering-answer",
                        responder_id=peer["body"]["actor_id"], artifact_ref=ref(report),
                    )
                    assert saved["answer"]["status"] == "accepted"

                for action in ([user_input, peer_reply] if input_first else [peer_reply, user_input]):
                    action()
                assert request.data == before
                assert UPDATE not in before.decode() and PEER_TEXT not in before.decode()
                if cancel_after_updates:
                    control = host._control(run.run_id, "agent-synthesis")
                    saved["cancel"] = _cancel(
                        host, run.run_id, control["body"]["task_id"],
                        request_id="cancel-after-combined-update",
                    )
                if first_output == "direct":
                    return answer("The preliminary count is seven; no further input was read.", 1)
                return tool("publish_result", {
                    "title": "Stale draft", "text": "The preliminary count is seven.",
                    "citations": [], "limitations": [],
                }, 1)
            assert len(packets) == 2, "Unexpected extra model turn in ordering diagnostic"
            packet = json.loads(request.data)
            index = json.loads(packet["messages"][-1]["content"])
            assert index["current_user_direction"]["text"] == UPDATE
            assert index["current_user_direction"]["ref"] == saved["input"].input_ref.model_dump()
            assert saved["answer"]["answer_ref"]["id"] in json.dumps(index)
            assert "ordering-request" in json.dumps(index)
            saved["next_packet"] = packet
            return tool("discover_capabilities", {}, 2)
        except BaseException as exc:
            errors.append(repr(exc))
            raise

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="combined-ordering", question="Compare synthetic preliminary and later counts.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        requester = host._control(run.run_id, "agent-synthesis")
        peer = host._control(run.run_id, "agent-critique")
        host._update_control(peer, phase="retry_at", retry_at=time.time() + 3600,
                             waiting_reason="Fixture holds peer execution")
        mission_id = requester["body"]["mission_id"]
        sent = host.mailbox.send_request(
            "ordering-request-send", mission_id=mission_id, request_id="ordering-request",
            message_id="ordering-message", requester_id=requester["body"]["actor_id"],
            recipient_id=peer["body"]["actor_id"], question="Inspect the later synthetic count.",
            expected_artifact="Peer note", cause_ref=requester["body"]["proposal_ref"],
            task_ref=requester["body"]["proposal_ref"], payload={"question": "Inspect the later count."},
        )
        assert sent["status"] == "accepted"
        if cancel_after_updates:
            _until(host, run.run_id, lambda: (
                bool(host.store.records("model_response_receipt")) or bool(errors)
            ))
            for _ in range(3):
                host._drive_run(host._run(run.run_id))
            assert "cancel" in saved
            assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "cancelled"
        else:
            _until(host, run.run_id, lambda: "next_packet" in saved or bool(errors))
        assert not errors
        assert len(packets) == (1 if cancel_after_updates else 2)
        assert host._control(run.run_id, "agent-synthesis")["body"]["report_ref"] is None
        assert host._control(run.run_id, "agent-critique")["body"]["phase"] == "retry_at"
        assert host._run(run.run_id)["body"]["status"] == "active"
        current = host.store.get("mail_request", "ordering-request")
        assert saved["answer"]["answer_ref"] in current["body"]["answer_refs"]
        assert len(host.store.records("mission_user_input")) == 1
    finally:
        host.close()


def test_short_input_and_peer_review_cue_share_small_context(tmp_path, monkeypatch):
    host, control, _config, saved = _setup(tmp_path, monkeypatch)
    try:
        run_id = control["body"]["run_id"]
        receipt = host.submit_user_input(_command(host, run_id, UPDATE))
        host._update_control(host._control(run_id, "agent-synthesis"), input_budget={
            "observed_limit_tokens": 8000, "target_fraction": .70,
            "basis": "synthetic_limit", "actual_provider_observation": True,
        })
        assert host._prepare_model(host._control(run_id, "agent-synthesis"), defer_core_tools=True)
        control = host._control(run_id, "agent-synthesis")
        call = control["body"]["call_ref"]
        intent = host.store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        index = json.loads(packet["messages"][-1]["content"])
        assert index["current_user_direction"]["ref"] == receipt.input_ref.model_dump()
        assert index["current_user_direction"]["text"] == UPDATE
        assert saved["answer_ref"]["id"] in json.dumps(index)
        assert saved["request_ref"]["id"] in json.dumps(index)
        assert "review_peer_answer" in json.dumps(index)
        assert intent["body"]["request_token_estimate"]["tokens"] <= 5600
    finally:
        host.close()


@pytest.mark.parametrize("cancel_kind", ["branch", "run"])
@pytest.mark.parametrize("input_first", [True, False])
@pytest.mark.parametrize("boundary", ["queued", "saved_response"])
def test_input_cancel_order_never_accepts_stale_response_or_reopens(
    tmp_path, monkeypatch, cancel_kind, input_first, boundary,
):
    packets = []

    def transport(request, *, timeout):
        packets.append(request.data)
        assert len(packets) == 1
        return answer("A saved answer made before the amendment.", 1)

    if boundary == "saved_response":
        run_id = start_and_hold(tmp_path, monkeypatch, transport, hold="before_model_advance")
        host = service(tmp_path, monkeypatch, transport)
    else:
        host = service(tmp_path, monkeypatch, transport)
        run_id = host.start(LiveStartRequest(
            request_id="cancel-order-queued", question="Inspect the synthetic count.",
            agent_ids=["agent-synthesis"], required_agent_ids=["agent-synthesis"],
        )).run_id
        assert host._schedule_model(host._control(run_id, "agent-synthesis"))
    command = _command(host, run_id, UPDATE)
    try:
        def cancel():
            if cancel_kind == "branch":
                return _cancel(host, run_id, command.task_id, request_id="cancel-ordered-branch")
            return host.cancel_run(LiveRunCancelRequest(
                request_id="cancel-ordered-run", run_id=run_id,
                expected_run_version=host._run(run_id)["version"], reason="Stop synthetic ordering case",
            ))

        if input_first:
            receipt = host.submit_user_input(command)
            cancel()
            assert host.store.get(**receipt.input_ref.model_dump())["body"]["text"] == UPDATE
        else:
            cancel()
            current_version = host.store.get("board_proposal", command.task_id)["version"]
            with pytest.raises(UserInputError) as rejected:
                host.submit_user_input(command.model_copy(update={
                    "expected_task_version": current_version,
                }))
            assert rejected.value.status == 409
        expected_calls = int(boundary == "saved_response")
        assert len(packets) == expected_calls
        for _ in range(3):
            host._drive_run(host._run(run_id))
        assert len(packets) == expected_calls
        assert host._control(run_id, "agent-synthesis")["body"]["phase"] == "cancelled"
        assert host._control(run_id, "agent-synthesis")["body"]["report_ref"] is None
        assert len(host.store.records("mission_user_input")) == int(input_first)
        assert len(host.store.records("model_response_receipt")) == expected_calls
    finally:
        host.close()
    reopened = service(tmp_path, monkeypatch, transport)
    try:
        assert reopened._drive_run(reopened._run(run_id)) == (False, None)
        assert reopened._control(run_id, "agent-synthesis")["body"]["phase"] == "cancelled"
        assert len(packets) == int(boundary == "saved_response")
        assert len(reopened.store.records("mission_user_input")) == int(input_first)
    finally:
        reopened.close()
