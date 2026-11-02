"""An explicit peer reply ID must name a current request to the publisher."""

from __future__ import annotations

import json
import socket
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import answer, serialized_peer_name, service, tool


def _drive_until(host, run_id: str, predicate) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Peer reply validation did not reach its saved boundary")


def test_wrong_outgoing_reply_id_rejects_before_report_then_correct_id_answers(
    tmp_path, monkeypatch,
) -> None:
    def no_socket(*_args, **_kwargs):
        raise AssertionError("Offline reply-ID test must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(socket.socket, "connect_ex", no_socket)
    host = None
    seen = {"sage_turns": 0, "feedback": False}
    incoming_id = "request-to-sage"
    outgoing_id = "request-to-mxra"

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        role = serialized_peer_name(packet)
        if role == "Mxra":
            return answer("No additional synthetic finding.", 99)
        seen["sage_turns"] += 1
        if seen["sage_turns"] == 1:
            assert incoming_id in json.dumps(packet["messages"])
            return tool("publish_result", {
                "title": "Synthetic peer report", "text": "A bounded synthetic reply.",
                "citations": [], "limitations": ["No source was supplied"],
                "answer_request_id": outgoing_id,
            }, 1)
        assert seen["sage_turns"] == 2
        result = next(json.loads(message["content"])
                      for message in reversed(packet["messages"])
                      if message["role"] == "tool" and message.get("name") == "publish_result")
        assert result["status"] == "rejected"
        assert result["field"] == "answer_request_id"
        assert result["external_action"] == "none"
        assert result["valid_incoming_requests"] == [{
            "request_ref": {"kind": "mail_request", "id": incoming_id, "version": 2},
            "question": "How should we frame this synthetic claim?",
        }]
        assert incoming_id in result["next_action"] or "valid_incoming_requests" in result["next_action"]
        assert host.store.records("report") == []
        assert host.store.records("mail_answer") == []
        seen["feedback"] = True
        return tool("publish_result", {
            "title": "Synthetic peer report", "text": "A bounded synthetic reply.",
            "citations": [], "limitations": ["No source was supplied"],
            "answer_request_id": incoming_id,
        }, 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="explicit-reply-id-validation", question="Review a synthetic claim.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        sage = host._control(run.run_id, "agent-synthesis")
        mxra = host._control(run.run_id, "agent-critique")
        mission_id = sage["body"]["mission_id"]
        for request_id, sender, recipient, question in (
            (outgoing_id, sage, mxra, "Check this synthetic claim independently."),
            (incoming_id, mxra, sage, "How should we frame this synthetic claim?"),
        ):
            sent = host.mailbox.send_request(
                f"send-{request_id}", mission_id=mission_id,
                request_id=request_id, message_id=f"message-{request_id}",
                requester_id=sender["body"]["actor_id"],
                recipient_id=recipient["body"]["actor_id"], question=question,
                expected_artifact="A short report", cause_ref=sender["body"]["proposal_ref"],
                payload={"question": question}, task_ref=sender["body"]["proposal_ref"],
            )
            assert sent["status"] == "accepted"
            host.mailbox.mark_delivered(f"deliver-{request_id}", mission_id=mission_id,
                                        message_id=f"message-{request_id}")
        host._update_control(mxra, phase="retry_at", retry_at=time.time() + 3600,
                             waiting_reason="Fixture holds peer")
        _drive_until(host, run.run_id, lambda: seen["feedback"])
        _drive_until(host, run.run_id, lambda: bool(host.store.get(
            "mail_request", incoming_id,
        )["body"]["answer_refs"]))
        assert seen["sage_turns"] == 2
        assert len(host.store.records("report")) == 1
        answer_ref = host.store.get("mail_request", incoming_id)["body"]["answer_refs"][0]
        assert host.store.get(**answer_ref)["body"]["artifact_ref"]["kind"] == "report"
        assert host.store.get("mail_request", outgoing_id)["body"]["answer_refs"] == []
    finally:
        host.close()


def test_answered_outgoing_id_guides_requester_to_review_without_publishing(
    tmp_path, monkeypatch,
) -> None:
    def no_socket(*_args, **_kwargs):
        raise AssertionError("Offline review-recovery test must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(socket.socket, "connect_ex", no_socket)
    host = None
    request_id = "request-from-sage"
    answer_id = "answer-from-mxra"
    seen = {"sage_turns": 0, "feedback": False}

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        if serialized_peer_name(packet) == "Mxra":
            return answer("The synthetic reply is already saved.", 99)
        seen["sage_turns"] += 1
        if seen["sage_turns"] == 1:
            return tool("publish_result", {
                "title": "Misaddressed synthetic report", "text": "No new evidence.",
                "citations": [], "limitations": ["Synthetic fixture"],
                "answer_request_id": request_id,
            }, 1)
        assert seen["sage_turns"] == 2
        result = next(json.loads(message["content"])
                      for message in reversed(packet["messages"])
                      if message["role"] == "tool" and message.get("name") == "publish_result")
        assert result["status"] == "rejected"
        assert result["field"] == "answer_request_id"
        assert result["external_action"] == "none"
        assert result["valid_incoming_requests"] == []
        assert result["outgoing_pending_review"] == {
            "request_ref": ref(host.store.get("mail_request", request_id)),
            "answer_ref": ref(host.store.get("mail_answer", answer_id)),
        }
        assert "review_peer_answer" in result["next_action"]
        assert "omit answer_request_id" in result["next_action"]
        assert len(host.store.records("report")) == 1  # Only the fixture answer exists.
        seen["feedback"] = True
        return tool("review_peer_answer", {
            "request_id": request_id, "answer_id": answer_id,
            "disposition": "answered",
            "reason": "The saved synthetic answer resolves this narrow question.",
        }, 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="outgoing-review-recovery", question="Review a synthetic answer.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        sage = host._control(run.run_id, "agent-synthesis")
        mxra = host._control(run.run_id, "agent-critique")
        mission_id = sage["body"]["mission_id"]
        sent = host.mailbox.send_request(
            "send-outgoing-review", mission_id=mission_id,
            request_id=request_id, message_id="message-outgoing-review",
            requester_id=sage["body"]["actor_id"],
            recipient_id=mxra["body"]["actor_id"],
            question="Does the synthetic answer resolve this claim?",
            expected_artifact="A short report", cause_ref=sage["body"]["proposal_ref"],
            payload={"question": "Does the synthetic answer resolve this claim?"},
            task_ref=sage["body"]["proposal_ref"],
        )
        assert sent["status"] == "accepted"
        host.mailbox.mark_delivered(
            "deliver-outgoing-review", mission_id=mission_id,
            message_id="message-outgoing-review",
        )
        fixture_report = host.store.transact(
            "answer-fixture-report", {}, lambda tx: tx.put(
                "report", "answer-fixture-report", {
                    "mission_id": mission_id, "title": "Prior peer answer",
                    "body": "A limited synthetic answer.",
                    "citations": [], "limitations": ["Synthetic fixture"],
                }), mission_id=mission_id,
        )
        saved = host.mailbox.record_answer(
            "answer-outgoing-review", mission_id=mission_id,
            request_id=request_id, answer_id=answer_id,
            responder_id=mxra["body"]["actor_id"],
            artifact_ref=ref(fixture_report),
        )
        assert saved["status"] == "accepted"
        host._update_control(mxra, phase="retry_at", retry_at=time.time() + 3600,
                             waiting_reason="Fixture holds peer")
        _drive_until(host, run.run_id, lambda: seen["feedback"])
        _drive_until(host, run.run_id, lambda: host.store.get(
            "mail_request", request_id,
        )["body"]["status"] == "answered")
        reviewed = host.store.get("mail_request", request_id)
        assert reviewed["body"]["accepted_answer_ref"] == ref(host.store.get(
            "mail_answer", answer_id,
        ))
        assert len(host.store.records("report")) == 1
        assert seen["sage_turns"] == 2
    finally:
        host.close()


@pytest.mark.parametrize("invalid_target", ["foreign_mission", "already_answered"])
def test_noncurrent_reply_target_rejects_without_new_report(
    tmp_path, monkeypatch, invalid_target,
) -> None:
    def no_socket(*_args, **_kwargs):
        raise AssertionError("Offline reply-ID test must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(socket.socket, "connect_ex", no_socket)
    host = None
    seen = {"turns": 0, "feedback": False}
    invalid_id = "invalid-answer-target"
    report_count = 0
    answer_count = 0

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        if serialized_peer_name(packet) == "Mxra":
            return answer("No additional synthetic finding.", 99)
        seen["turns"] += 1
        if seen["turns"] == 1:
            return tool("publish_result", {
                "title": "Wrong-target publication", "text": "A synthetic note.",
                "citations": [], "limitations": ["Synthetic fixture"],
                "answer_request_id": invalid_id,
            }, 1)
        assert seen["turns"] == 2
        result = next(json.loads(message["content"])
                      for message in reversed(packet["messages"])
                      if message["role"] == "tool" and message.get("name") == "publish_result")
        assert result["status"] == "rejected"
        assert result["field"] == "answer_request_id"
        assert [item["request_ref"]["id"] for item in result["valid_incoming_requests"]] == [
            "valid-incoming",
        ]
        assert "outgoing_pending_review" not in result
        assert len(host.store.records("report")) == report_count
        assert len(host.store.records("mail_answer")) == answer_count
        seen["feedback"] = True
        return tool("board_read", {}, 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"reply-id-{invalid_target}", question="Review a synthetic claim.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        sage = host._control(run.run_id, "agent-synthesis")
        mxra = host._control(run.run_id, "agent-critique")
        mission_id = sage["body"]["mission_id"]

        def send(request_id, mission, requester, recipient, cause):
            sent = host.mailbox.send_request(
                f"send-{request_id}", mission_id=mission,
                request_id=request_id, message_id=f"message-{request_id}",
                requester_id=requester["body"]["actor_id"],
                recipient_id=recipient["body"]["actor_id"],
                question=f"Check {request_id}.", expected_artifact="A report",
                cause_ref=cause["body"]["proposal_ref"],
                payload={"question": f"Check {request_id}."},
                task_ref=cause["body"]["proposal_ref"],
            )
            assert sent["status"] == "accepted"
            host.mailbox.mark_delivered(f"deliver-{request_id}", mission_id=mission,
                                        message_id=f"message-{request_id}")

        send("valid-incoming", mission_id, mxra, sage, mxra)
        if invalid_target == "foreign_mission":
            foreign = host.start(LiveStartRequest(
                request_id="separate-reply-id-mission", question="Separate synthetic claim.",
                agent_ids=["agent-synthesis", "agent-critique"],
            ))
            foreign_sage = host._control(foreign.run_id, "agent-synthesis")
            foreign_mxra = host._control(foreign.run_id, "agent-critique")
            send(invalid_id, foreign_sage["body"]["mission_id"],
                 foreign_mxra, foreign_sage, foreign_mxra)
            foreign_report = host.store.transact(
                "foreign-answer-fixture-report", {}, lambda tx: tx.put(
                    "report", "foreign-answer-report", {
                        "mission_id": foreign_sage["body"]["mission_id"],
                        "title": "Foreign answer", "body": "Separate synthetic finding.",
                        "citations": [], "limitations": [],
                    }), mission_id=foreign_sage["body"]["mission_id"],
            )
            host.mailbox.record_answer(
                "foreign-answer-fixture", mission_id=foreign_sage["body"]["mission_id"],
                request_id=invalid_id, answer_id="foreign-answer",
                responder_id=foreign_sage["body"]["actor_id"],
                artifact_ref=ref(foreign_report),
            )
        else:
            send(invalid_id, mission_id, mxra, sage, mxra)
            fixture_report = host.store.transact(
                "already-answered-fixture-report", {}, lambda tx: tx.put(
                    "report", "already-answered-report", {
                        "mission_id": mission_id, "title": "Prior answer",
                        "body": "Synthetic prior answer.", "citations": [], "limitations": [],
                    }), mission_id=mission_id,
            )
            saved = host.mailbox.record_answer(
                "already-answered-fixture-answer", mission_id=mission_id,
                request_id=invalid_id, answer_id="prior-answer", responder_id=sage["body"]["actor_id"],
                artifact_ref=ref(fixture_report),
            )
            host.mailbox.review_answer(
                "already-answered-fixture-review", mission_id=mission_id,
                request_id=invalid_id, reviewer_id=mxra["body"]["actor_id"],
                answer_id=saved["answer_ref"]["id"], disposition="answered",
                reason="The prior synthetic response is sufficient.",
            )
        report_count = len(host.store.records("report"))
        answer_count = len(host.store.records("mail_answer"))
        assert host.store.get("mail_request", invalid_id)["body"]["status"] == (
            "awaiting_review" if invalid_target == "foreign_mission" else "answered"
        )
        host._update_control(mxra, phase="retry_at", retry_at=time.time() + 3600,
                             waiting_reason="Fixture holds peer")
        _drive_until(host, run.run_id, lambda: seen["feedback"])
        assert len(host.store.records("report")) == report_count
    finally:
        host.close()


@pytest.mark.parametrize("include_null", [False, True])
def test_ordinary_publication_keeps_optional_reply_id_omitted_or_null(
    tmp_path, monkeypatch, include_null,
) -> None:
    def transport(_request, *, timeout):
        arguments = {
            "title": "Ordinary synthetic report", "text": "A limited synthetic claim.",
            "citations": [], "limitations": ["No source was supplied"],
        }
        if include_null:
            arguments["answer_request_id"] = None
        return tool("publish_result", arguments, 1)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"ordinary-reply-id-{include_null}",
            question="State a limited synthetic finding.",
            agent_ids=["agent-synthesis"],
        ))
        _drive_until(host, run.run_id, lambda: bool(host.store.records("report")))
        assert len(host.store.records("report")) == 1
        assert host.store.records("mail_answer") == []
    finally:
        host.close()
