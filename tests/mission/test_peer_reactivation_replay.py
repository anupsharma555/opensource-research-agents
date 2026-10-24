"""A repeated peer answer may reopen a newly completed task without replay drift."""

from __future__ import annotations

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import service

REASON = "A saved peer answer needs requester review"


def _fixture(host):
    run = host.start(LiveStartRequest(
        request_id="same-peer-cause-reopen", question="Review a synthetic peer answer.",
        agent_ids=["agent-synthesis", "agent-critique"],
    ))
    sage = host._control(run.run_id, "agent-synthesis")
    mxra = host._control(run.run_id, "agent-critique")
    mission_id = sage["body"]["mission_id"]
    request = host.mailbox.send_request(
        "fixture-peer-request", mission_id=mission_id,
        request_id="fixture-peer-request", message_id="fixture-peer-message",
        requester_id=sage["body"]["actor_id"],
        recipient_id=mxra["body"]["actor_id"],
        question="Check this synthetic limitation.", expected_artifact="A report",
        cause_ref=sage["body"]["proposal_ref"],
        payload={"question": "Check this synthetic limitation."},
        task_ref=sage["body"]["proposal_ref"],
    )
    assert request["status"] == "accepted"
    host.mailbox.mark_delivered("fixture-peer-delivery", mission_id=mission_id,
                                message_id="fixture-peer-message")
    report = host.store.transact(
        "fixture-peer-report", {}, lambda tx: tx.put("report", "fixture-peer-report", {
            "mission_id": mission_id, "title": "Synthetic peer reply",
            "body": "A bounded synthetic reply.", "citations": [], "limitations": [],
        }), mission_id=mission_id,
    )
    answered = host.mailbox.record_answer(
        "fixture-peer-answer", mission_id=mission_id,
        request_id="fixture-peer-request", answer_id="fixture-peer-answer",
        responder_id=mxra["body"]["actor_id"], artifact_ref=ref(report),
    )
    assert host.store.get(**answered["request_ref"])["body"]["status"] == "awaiting_review"
    return run.run_id, answered["request_ref"]


def _complete_sage(host, run_id: str, operation_id: str) -> dict:
    control = host._control(run_id, "agent-synthesis")
    proposal = host.store.get("board_proposal", control["body"]["task_id"])
    host.store.transact(
        operation_id, {}, lambda tx: tx.put(
            "board_proposal", proposal["id"],
            {**proposal["body"], "status": "completed"},
            expected_version=proposal["version"],
        ), mission_id=control["body"]["mission_id"],
    )
    host._update_control(control, phase="done")
    return host._control(run_id, "agent-synthesis")


def _proposal(host, control: dict) -> dict:
    return host.store.get("board_proposal", control["body"]["task_id"])


def test_same_answer_cause_reopens_after_later_completion_without_duplicate(
    tmp_path, monkeypatch,
) -> None:
    def no_model(*_args, **_kwargs):
        raise AssertionError("Reactivation test must not call a provider")

    host = service(tmp_path, monkeypatch, no_model)
    try:
        run_id, cause = _fixture(host)
        first_done = _complete_sage(host, run_id, "fixture-complete-first")
        before = _proposal(host, first_done)["version"]
        assert host._reactivate(first_done, cause, REASON)
        first_reopen = host._control(run_id, "agent-synthesis")
        assert first_reopen["body"]["phase"] == "ready_model"
        assert _proposal(host, first_reopen)["version"] == before + 1
        assert not host._reactivate(first_done, cause, REASON)  # stale control head
        assert _proposal(host, first_reopen)["version"] == before + 1

        second_done = _complete_sage(host, run_id, "fixture-complete-second")
        completed_version = _proposal(host, second_done)["version"]
        assert host._reactivate(second_done, cause, REASON)
        second_reopen = host._control(run_id, "agent-synthesis")
        assert second_reopen["body"]["phase"] == "ready_model"
        assert _proposal(host, second_reopen)["version"] == completed_version + 1
        assert second_reopen["body"]["proposal_ref"] == ref(_proposal(host, second_reopen))
        assert not host._reactivate(second_done, cause, REASON)
        stable_version = _proposal(host, second_reopen)["version"]
    finally:
        host.close()

    reopened = service(tmp_path, monkeypatch, no_model)
    try:
        current = reopened._control(run_id, "agent-synthesis")
        assert not reopened._reactivate(current, cause, REASON)
        assert _proposal(reopened, current)["version"] == stable_version
    finally:
        reopened.close()


@pytest.mark.parametrize("legacy_operation", [False, True])
def test_restart_finishes_board_committed_reopen_without_second_revision(
    tmp_path, monkeypatch, legacy_operation,
) -> None:
    def no_model(*_args, **_kwargs):
        raise AssertionError("Reactivation replay test must not call a provider")

    host = service(tmp_path, monkeypatch, no_model)
    try:
        run_id, cause = _fixture(host)
        done = _complete_sage(host, run_id, "fixture-complete-before-crash")
        if legacy_operation:
            proposal = _proposal(host, done)
            result = host.board.revise(
                f"mission-peer-reactivate:{cause['id']}:{cause['version']}:"
                f"{done['body']['task_id']}",
                mission_id=done["body"]["mission_id"],
                proposal_id=done["body"]["task_id"],
                actor_id=done["body"]["actor_id"],
                expected_version=proposal["version"], reason=REASON,
                next_action="Review current peer request and saved evidence",
                status="open", evidence_refs=[cause],
            )
            assert result["status"] == "accepted"
        else:
            def crash_after_board_commit(*_args, **_kwargs):
                raise RuntimeError("crash after board commit")

            monkeypatch.setattr(host, "_update_control", crash_after_board_commit)
            with pytest.raises(RuntimeError, match="crash after board commit"):
                host._reactivate(done, cause, REASON)
        committed_version = _proposal(host, done)["version"]
        assert _proposal(host, done)["body"]["status"] == "open"
        assert host._control(run_id, "agent-synthesis")["body"]["phase"] == "done"
    finally:
        host.close()

    reopened = service(tmp_path, monkeypatch, no_model)
    try:
        pending = reopened._control(run_id, "agent-synthesis")
        assert reopened._reactivate(pending, cause, REASON)
        ready = reopened._control(run_id, "agent-synthesis")
        assert ready["body"]["phase"] == "ready_model"
        assert ready["body"]["proposal_ref"] == ref(_proposal(reopened, ready))
        assert _proposal(reopened, ready)["version"] == committed_version
        assert not reopened._reactivate(pending, cause, REASON)
        monkeypatch.setattr(reopened, "_schedule_model", lambda *_args, **_kwargs: False)
        for _ in range(3):
            reopened._drive_run(reopened._run(run_id))
        assert _proposal(reopened, ready)["version"] == committed_version
    finally:
        reopened.close()
