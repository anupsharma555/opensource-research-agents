"""Offline service tests for scope closure around committed peer events."""

from __future__ import annotations

import time

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import answer, service


def _advance_until(host, run_id: str, predicate) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(0.005)  # The injected model runs in the normal dispatch thread.
    raise AssertionError("Mission did not reach the expected saved state")


def _completed_required_work(host, monkeypatch, *, request_id: str, agent_ids: list[str]):
    run = host.start(LiveStartRequest(
        request_id=request_id,
        question="Give a bounded synthetic answer",
        agent_ids=agent_ids,
        required_agent_ids=agent_ids,
    ))
    original = host._maybe_complete
    monkeypatch.setattr(host, "_maybe_complete", lambda _run: False)
    try:
        _advance_until(host, run.run_id, lambda: all(
            host._control(run.run_id, alias)["body"]["phase"] == "done"
            for alias in agent_ids
        ))
    finally:
        monkeypatch.setattr(host, "_maybe_complete", original)
    assert host._run(run.run_id)["body"]["status"] == "active"
    return run


def _request(host, run_id: str, *, request_id: str):
    sage = host._control(run_id, "agent-synthesis")
    mira = host._control(run_id, "agent-critique")
    return host.mailbox.send_request(
        f"open:{request_id}", mission_id=sage["body"]["mission_id"],
        request_id=request_id, message_id=f"message:{request_id}",
        requester_id=sage["body"]["actor_id"],
        recipient_id=mira["body"]["actor_id"],
        question="Check the newly disputed claim", expected_artifact="Independent check",
        cause_ref=sage["body"]["proposal_ref"],
        payload={"text": "Check the newly disputed claim"},
    )


def test_required_request_committed_just_before_closure_rejects_stale_completion(
    tmp_path, monkeypatch,
) -> None:
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        return answer("A bounded synthetic answer.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = _completed_required_work(
            host, monkeypatch, request_id="scope-before-close",
            agent_ids=["agent-synthesis", "agent-critique"],
        )
        before = host._run(run.run_id)
        sage = host._control(run.run_id, "agent-synthesis")
        assert host.store.get("board_proposal", sage["body"]["task_id"])["body"][
            "status"
        ] == "completed"
        original_transact = host.store.transact
        opened = []

        def transact(operation_id, *args, **kwargs):
            if operation_id.startswith("mission-complete:") and not opened:
                committed = _request(host, run.run_id, request_id="required-before-close")
                assert committed["status"] == "accepted"
                opened.append(committed["request_ref"])
            return original_transact(operation_id, *args, **kwargs)

        monkeypatch.setattr(host.store, "transact", transact)
        assert host._maybe_complete(before) is False
        assert len(opened) == 1
        assert host._run(run.run_id) == before
        assert host.store.get(**opened[0])["body"]["status"] == "unanswered"
        assert not any(
            event["type"] == "mission_scope_completed"
            for event in host.store.events(before["body"]["mission_id"])
        )
        assert host._maybe_complete(host._run(run.run_id)) is False
        assert len(opened) == 1
        assert len(calls) == 2
    finally:
        host.close()


def test_late_request_has_linked_followup_without_rewriting_completed_scope(
    tmp_path, monkeypatch,
) -> None:
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        return answer("A bounded synthetic answer.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = _completed_required_work(
            host, monkeypatch, request_id="scope-after-close",
            agent_ids=["agent-synthesis", "agent-critique"],
        )
        assert host._maybe_complete(host._run(run.run_id)) is True
        completed = host._run(run.run_id)
        assert completed["body"]["status"] == "completed"
        late = _request(host, run.run_id, request_id="required-after-close")
        assert late["status"] == "accepted"
        assert host._activate_peer_obligations(completed) is True
        dispositions = host.store.records("mission_post_scope_disposition")
        assert len(dispositions) == 1
        saved = dispositions[0]["body"]
        assert saved["decision"] == "followup_required"
        assert saved["completed_run_ref"] == ref(completed)
        assert saved["request_ref"] == late["request_ref"]
        assert saved["opening_event_sequence"] > completed["body"][
            "closure_event_through"
        ]
        assert host._run(run.run_id) == completed
        assert host._activate_peer_obligations(completed) is False
        assert len(host.store.records("mission_post_scope_disposition")) == 1
        assert len(calls) == 2
    finally:
        host.close()


def test_optional_extension_can_continue_after_supported_scope_completion(
    tmp_path, monkeypatch,
) -> None:
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        return answer("Requested scope answered; another method remains optional.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = _completed_required_work(
            host, monkeypatch, request_id="scope-optional-control",
            agent_ids=["agent-synthesis"],
        )
        root = host._control(run.run_id, "agent-synthesis")
        mission_id = root["body"]["mission_id"]
        optional = host.board.propose(
            "optional-scope-proposal", mission_id=mission_id,
            proposal_id="optional-scope-proposal", actor_id=root["body"]["actor_id"],
            objective="Explore another method", expected_artifact="Optional comparison",
            next_action="Continue only if useful",
        )
        assert optional["status"] == "accepted"
        control = host._save("mission_task_control", "optional-scope-control", {
            **root["body"], "task_id": optional["proposal_ref"]["id"],
            "proposal_ref": optional["proposal_ref"], "scope_required": False,
            "scope_status": None, "phase": "ready_model", "sequence": 0,
            "work_id": None, "call_ref": None, "response_ref": None,
            "tool_call": None, "tool_turn_refs": [], "report_ref": None,
            "feedback": None, "waiting_reason": None,
        }, mission_id=mission_id)
        disposition = host.work.record_scope_disposition(
            "optional-scope-choice", run_ref=ref(host._run(run.run_id)),
            actor_id=root["body"]["actor_id"], source_control_ref=ref(control),
            decisions=[{"control_ref": ref(control), "disposition": "continue"}],
            reason="The requested answer is supported; the method comparison is optional",
        )
        assert disposition["status"] == "recorded"
        assert host._maybe_complete(host._run(run.run_id)) is True
        completed = host._run(run.run_id)
        assert completed["body"]["status"] == "completed"
        assert completed["body"]["scope_disposition_ref"] == disposition[
            "disposition_ref"
        ]
        current_optional = host.store.get("mission_task_control", control["id"])
        assert current_optional["body"]["scope_status"] == "continued"
        assert current_optional["body"]["phase"] == "ready_model"
        assert len(calls) == 1
    finally:
        host.close()


def test_late_request_cannot_resume_cancelled_mission(tmp_path, monkeypatch) -> None:
    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("Cancelled mission made a model call")

    host = service(tmp_path, monkeypatch, unexpected_call)
    try:
        run = host.start(LiveStartRequest(
            request_id="scope-cancelled", question="Preserve cancellation",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        original = host._run(run.run_id)
        host._save(
            "mission_run", original["id"], {**original["body"], "status": "cancelled"},
            mission_id=original["body"]["mission_id"],
        )
        cancelled = host._run(run.run_id)
        controls = host._controls(run.run_id)
        late = _request(host, run.run_id, request_id="request-after-cancellation")
        assert late["status"] == "accepted"
        assert host._drive_run(cancelled) == (False, None)
        assert host._activate_peer_obligations(cancelled) is False
        assert host._maybe_complete(cancelled) is False
        assert host._run(run.run_id) == cancelled
        assert host._controls(run.run_id) == controls
        assert not host.store.records("mission_post_scope_disposition")
        assert not host.store.records("model_request_intent")
    finally:
        host.close()
