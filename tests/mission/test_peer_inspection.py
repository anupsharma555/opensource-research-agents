"""Exact peer-content reads are bounded by saved run snapshots and mail lineage."""

from __future__ import annotations

import json

import pytest

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.mission.peer_inspection import PeerInspectionError, inspect_peer_record
from tests.mission.test_service import answer, drain, service


def _peers(host, run_id):
    controls = {item["body"]["alias"]: item for item in host._controls(run_id)}
    return controls["agent-synthesis"], controls["agent-critique"]


def test_exact_saved_request_and_answer_content_is_redacted_without_exposing_other_records(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: pytest.fail("Provider not needed"))
    try:
        run = host.start(LiveStartRequest(
            request_id="peer-content-run", question="Synthetic peer exchange",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        sender, recipient = _peers(host, run.run_id)
        mission_id = host._run(run.run_id)["body"]["mission_id"]
        payload = {
            "question": "First line\nSecond line",
            "metadata": {"api_key": "SYNTHETIC-KEY"},
            "instructions": "Read https://example.invalid/item?sig=SYNTHETIC-SIG\nThen reply.",
            "encoded": '{"access_token":"SYNTHETIC-ENCODED"}',
            "ordinary_json_text": '{ "observation": [1, 2] }',
        }
        sent = host.mailbox.send_request(
            "send-peer-content", mission_id=mission_id,
            request_id="peer-content-request", message_id="peer-content-message",
            requester_id=sender["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"],
            question=payload["question"], expected_artifact="Scientific critique",
            cause_ref=ref(host.store.get("board_proposal", sender["body"]["task_id"])),
            payload=payload,
        )
        assert sent["status"] == "accepted"
        assert host._schedule_model(recipient)
        attempt = next(item for item in host.store.records("agent_attempt")
                       if item["body"]["agentId"] == recipient["body"]["actor_id"])
        report_body = {
            "mission_id": mission_id, "run_id": run.run_id, "kind": "research_contribution",
            "title": "Exact critique", "body": "Peer finding\nSecond paragraph. token=SYNTHETIC-TOKEN",
            "citations": [], "limitations": ["Uncertain."],
            "producer_attempt_ref": ref(attempt),
            "private_instruction": "SYNTHETIC-HIDDEN",
        }
        report_ref = host.store.transact(
            "save-peer-report", report_body,
            lambda tx: ref(tx.put("report", "peer-content-report", report_body)),
            mission_id=mission_id,
        )
        recorded = host.mailbox.record_answer(
            "answer-peer-content", mission_id=mission_id,
            request_id="peer-content-request", answer_id="peer-content-answer",
            responder_id=recipient["body"]["actor_id"], artifact_ref=report_ref,
        )
        assert recorded["status"] == "accepted"
        host._refresh_snapshot(run.run_id)

        message = host.peer_record(run.run_id, "mail_message", "peer-content-message", 1)
        assert message["status"] == "available"
        assert message["originRunId"] == run.run_id
        assert message["contentReference"] == sent["message_ref"]
        assert message["senderId"] == sender["body"]["actor_id"]
        assert message["recipientId"] == recipient["body"]["actor_id"]
        assert message["content"]["question"] == "First line\nSecond line"
        assert message["content"]["instructions"].endswith("\nThen reply.")
        assert message["content"]["ordinary_json_text"] == payload["ordinary_json_text"]
        assert message["redacted"] is True
        assert "SYNTHETIC-KEY" not in json.dumps(message)
        assert "SYNTHETIC-SIG" not in json.dumps(message)
        assert "SYNTHETIC-ENCODED" not in json.dumps(message)

        peer_answer = host.peer_record(run.run_id, "mail_answer", "peer-content-answer", 1)
        assert peer_answer["status"] == "available"
        assert peer_answer["contentReference"] == report_ref
        assert peer_answer["content"]["body"] == "Peer finding\nSecond paragraph. token=[REDACTED]"
        assert peer_answer["content"]["title"] == "Exact critique"
        assert "private_instruction" not in json.dumps(peer_answer)
        assert peer_answer["redacted"] is True
        assert host.peer_record(run.run_id, "mail_message", "peer-content-message", 2)["status"] == "unavailable"
        assert host.peer_record(run.run_id, "mail_answer", "peer-content-answer", 2)["status"] == "unavailable"
    finally:
        host.close()


def test_child_reads_only_exact_frozen_parent_message_not_later_versions_or_siblings(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: answer("Synthetic accepted answer.", 1))
    try:
        parent = host.start(LiveStartRequest(
            request_id="peer-parent", question="Synthetic parent question",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        drain(host, parent.run_id)
        sender, recipient = _peers(host, parent.run_id)
        mission_id = host._run(parent.run_id)["body"]["mission_id"]
        sent = host.mailbox.send_message(
            "parent-message", mission_id=mission_id, message_id="parent-mail",
            sender_id=sender["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"],
            correlation_id="parent-correlation", payload={"text": "Frozen parent message."},
        )
        assert sent["status"] == "accepted"
        formatted_json = '{ "observation": [1, 2] }'
        assert host.mailbox.send_message(
            "parent-json-message", mission_id=mission_id, message_id="parent-json-mail",
            sender_id=sender["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"],
            correlation_id="parent-json-correlation", payload=formatted_json,
        )["status"] == "accepted"
        host._refresh_snapshot(parent.run_id)
        parent_view = host.state(parent.run_id).snapshot
        target = parent_view.artifacts[0]
        child = host.followup(LiveFollowupRequest(
            request_id="peer-child", parent_run_id=parent.run_id,
            parent_snapshot_id=parent_view.snapshot_id,
            target_id=target.id, target_version=target.version,
            question="Review exact parent output", agent_ids=["agent-critique"],
        ))
        inherited = inspect_peer_record(host.store, child.run_id, "mail_message", "parent-mail", 1)
        assert inherited["status"] == "available"
        assert inherited["originRunId"] == parent.run_id
        assert inherited["content"] == {"text": "Frozen parent message."}
        unchanged = inspect_peer_record(host.store, child.run_id, "mail_message", "parent-json-mail", 1)
        assert unchanged["content"] == formatted_json
        assert unchanged["redacted"] is False
        assert host.mailbox.mark_delivered("deliver-parent-message", mission_id=mission_id,
                                           message_id="parent-mail")["status"] == "accepted"
        host._refresh_snapshot(parent.run_id)
        assert inspect_peer_record(host.store, child.run_id, "mail_message", "parent-mail", 2)["status"] == "unavailable"
        assert inspect_peer_record(host.store, parent.run_id, "mail_message", "parent-mail", 1)["status"] == "unavailable"
        assert inspect_peer_record(host.store, parent.run_id, "mail_message", "parent-mail", 2)["status"] == "available"
        sibling = host.start(LiveStartRequest(
            request_id="peer-sibling", question="Separate synthetic work", agent_ids=["agent-synthesis"],
        ))
        assert inspect_peer_record(host.store, sibling.run_id, "mail_message", "parent-mail", 2)["status"] == "unavailable"
    finally:
        host.close()


def test_invalid_selector_and_non_scientific_answer_artifact_do_not_disclose_content(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: pytest.fail("Provider not needed"))
    try:
        run = host.start(LiveStartRequest(
            request_id="peer-private-artifact", question="Synthetic request",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        sender, recipient = _peers(host, run.run_id)
        mission_id = host._run(run.run_id)["body"]["mission_id"]
        sent = host.mailbox.send_request(
            "send-private-answer", mission_id=mission_id,
            request_id="private-request", message_id="private-message",
            requester_id=sender["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"],
            question="Review", expected_artifact="Critique",
            cause_ref=ref(host.store.get("board_proposal", sender["body"]["task_id"])),
            payload="Exact request body",
        )
        assert sent["status"] == "accepted"
        private = {"mission_id": mission_id, "payload": {"secret": "SYNTHETIC-PRIVATE"}}
        private_ref = host.store.transact(
            "save-private-artifact", private,
            lambda tx: ref(tx.put("executor_artifact", "private-artifact", private)),
            mission_id=mission_id,
        )
        assert host.mailbox.record_answer(
            "answer-with-private-artifact", mission_id=mission_id,
            request_id="private-request", answer_id="private-answer",
            responder_id=recipient["body"]["actor_id"], artifact_ref=private_ref,
        )["status"] == "accepted"
        host._refresh_snapshot(run.run_id)
        unavailable = host.peer_record(run.run_id, "mail_answer", "private-answer", 1)
        assert unavailable["status"] == "unavailable"
        assert unavailable["content"] is None
        assert "SYNTHETIC-PRIVATE" not in json.dumps(unavailable)
        with pytest.raises(PeerInspectionError):
            inspect_peer_record(host.store, run.run_id, "executor_artifact", "private-artifact", 1)
        with pytest.raises(PeerInspectionError):
            inspect_peer_record(host.store, run.run_id, "mail_message", "private-message", 0)
    finally:
        host.close()
