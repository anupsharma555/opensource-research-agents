"""Offline operator cancellation through the real mission and executor paths."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import threading
import time

import pytest

from ora.contracts.models import LiveBranchCancelRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.coordination.executor import WorkSpec
from ora.mission.service import MissionError
from tests.mission.test_service import answer, service, tool


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("socket connection")
        raise AssertionError("Offline branch cancellation attempted network access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    yield
    assert attempts == []


def _cancel(host, run_id: str, task_id: str, *, request_id: str, reason: str = "Operator stopped this branch"):
    task = host.store.get("board_proposal", task_id)
    return host.cancel_branch(LiveBranchCancelRequest(
        request_id=request_id, run_id=run_id, task_id=task_id,
        expected_task_version=task["version"], reason=reason,
    ))


def _wait_until(host, run_id: str, predicate, *, seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Saved mission did not reach the expected state")


def test_cancel_queued_required_task_keeps_sibling_running_then_closes_cancelled(
    tmp_path, monkeypatch,
) -> None:
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        return answer("A bounded synthetic sibling answer.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="branch-cancel-two-required", question="Compare two synthetic analyses",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        stopped = host._control(run.run_id, "agent-synthesis")
        sibling = host._control(run.run_id, "agent-critique")
        inbound = host.mailbox.send_request(
            "before-cancel-inbound", mission_id=stopped["body"]["mission_id"],
            request_id="request-to-stopped-peer", message_id="message-to-stopped-peer",
            requester_id=sibling["body"]["actor_id"],
            recipient_id=stopped["body"]["actor_id"],
            question="Check the synthetic sibling result", expected_artifact="Peer note",
            cause_ref=sibling["body"]["proposal_ref"],
            payload={"question": "Check the synthetic sibling result"},
            task_ref=sibling["body"]["proposal_ref"],
        )
        assert inbound["status"] == "accepted"
        assert host._schedule_model(stopped)
        work_id = host._control(run.run_id, "agent-synthesis")["body"]["work_id"]
        receipt = _cancel(host, run.run_id, stopped["body"]["task_id"], request_id="cancel-sage")
        assert receipt.status == "cancelled"
        assert receipt.cancellation_ref.kind == "mission_branch_cancellation"
        assert host._run(run.run_id)["body"]["status"] == "active"
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "cancelled"
        assert host._control(run.run_id, "agent-critique") == sibling
        unavailable = host.store.get("mail_request", "request-to-stopped-peer")
        assert unavailable["body"]["status"] == "recipient_unavailable_due_branch_cancel"
        assert unavailable["body"]["cancellation_ref"] == receipt.cancellation_ref.model_dump()
        assert unavailable["body"]["requester_wake_ref"]
        work = next(item for item in host.executor._work(stopped["body"]["mission_id"])
                    if item["body"]["spec"]["work_id"] == work_id)
        assert work["body"]["phase"] == "cancelled"
        assert host.cancel_branch(LiveBranchCancelRequest(
            request_id="cancel-sage", run_id=run.run_id,
            task_id=stopped["body"]["task_id"], expected_task_version=1,
            reason="Operator stopped this branch",
        )).reused is True
        with pytest.raises(MissionError) as conflict:
            _cancel(host, run.run_id, stopped["body"]["task_id"], request_id="cancel-sage-again")
        assert conflict.value.status == 409

        _wait_until(host, run.run_id, lambda: host._run(run.run_id)["body"]["status"] == "cancelled")
        assert len(calls) == 1
        assert host._control(run.run_id, "agent-critique")["body"]["phase"] == "done"
        assert host._control(run.run_id, "agent-critique")["body"]["report_ref"]
        assert host._control(run.run_id, "agent-synthesis")["body"]["report_ref"] is None
        target = host.store.get("board_proposal", stopped["body"]["task_id"])
        revised = host.board.revise(
            "revise-cancelled-target", mission_id=stopped["body"]["mission_id"],
            proposal_id=target["id"], actor_id=stopped["body"]["actor_id"],
            expected_version=target["version"], reason="Synthetic reconsideration", status="open",
        )
        assert revised["status"] == "rejected"
        reopened = host.board.reopen(
            "reopen-cancelled-target", mission_id=stopped["body"]["mission_id"],
            affected_ref=ref(target), contradiction_ref=ref(target),
            actor_id=stopped["body"]["actor_id"], reason="Synthetic contradiction",
            next_action="Review the source",
        )
        assert reopened["status"] == "accepted" and ref(target) not in reopened["proposal_refs"]
        late = host.mailbox.send_request(
            "late-to-stopped-peer", mission_id=stopped["body"]["mission_id"],
            request_id="late-request-to-stopped-peer", message_id="late-message-to-stopped-peer",
            requester_id=sibling["body"]["actor_id"],
            recipient_id=stopped["body"]["actor_id"],
            question="Check a later synthetic result", expected_artifact="Peer note",
            cause_ref=sibling["body"]["proposal_ref"],
            payload={"question": "Check a later synthetic result"},
        )
        assert late["status"] == "accepted"
        assert host.store.get(**late["request_ref"])["body"]["status"] == "recipient_unavailable_due_branch_cancel"
        assert host._drive_run(host._run(run.run_id)) == (False, None)
    finally:
        host.close()
    reopened = service(
        tmp_path, monkeypatch,
        lambda *_args, **_kwargs: pytest.fail("Reopened cancelled run dispatched a model"),
    )
    try:
        assert reopened._run(run.run_id)["body"]["status"] == "cancelled"
        assert reopened._drive_run(reopened._run(run.run_id)) == (False, None)
        assert reopened._control(run.run_id, "agent-synthesis")["body"]["phase"] == "cancelled"
        assert reopened.cancel_branch(LiveBranchCancelRequest(
            request_id="cancel-sage", run_id=run.run_id,
            task_id=stopped["body"]["task_id"], expected_task_version=1,
            reason="Operator stopped this branch",
        )).reused is True
    finally:
        reopened.close()


def test_cancel_inflight_model_retains_late_receipt_and_usage_without_acceptance(
    tmp_path, monkeypatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def transport(request, *, timeout):
        started.set()
        assert release.wait(5)
        return answer("A late synthetic answer.", 1)

    host = service(tmp_path, monkeypatch, transport)
    worker = None
    try:
        run = host.start(LiveStartRequest(
            request_id="branch-cancel-inflight", question="Inspect a delayed synthetic call",
            agent_ids=["agent-synthesis"], required_agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        worker = threading.Thread(
            target=lambda: asyncio.run(host.executor.run_ready(control["body"]["mission_id"])),
            daemon=True,
        )
        worker.start()
        assert started.wait(5)
        receipt = _cancel(host, run.run_id, control["body"]["task_id"], request_id="cancel-inflight")
        assert receipt.status == "cancellation_requested"
        assert host._run(run.run_id)["body"]["status"] == "cancelled"
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert host.store.records("executor_artifact") == []
        observations = host.store.records("executor_unaccepted_result")
        assert len(observations) == 1
        assert observations[0]["body"]["reason_code"] in {"board_rejected", "callback_result_after_cancel"}
        assert observations[0]["body"]["used_tokens"] == 11
        assert host.store.records("model_response_receipt")
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "cancelled"
    finally:
        release.set()
        if worker is not None:
            worker.join(5)
        host.close()


def test_cancel_parent_fences_saved_search_children_and_later_submission(
    tmp_path, monkeypatch,
) -> None:
    host = service(tmp_path, monkeypatch,
                   lambda *_args, **_kwargs: pytest.fail("Cancellation dispatched a model"))
    try:
        run = host.start(LiveStartRequest(
            request_id="branch-cancel-search", question="Search synthetic sources",
            agent_ids=["agent-synthesis"], required_agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        config = host._configuration(control)
        spec = WorkSpec(
            work_id="synthetic-search-parent", mission_id=control["body"]["mission_id"],
            run_id=run.run_id, actor_id=control["body"]["actor_id"],
            proposal_ref=control["body"]["proposal_ref"], configuration_ref=ref(config),
            input_refs=(), handler_id="mission-tool", handler_version="1",
            kind="compute", lane=control["body"]["actor_id"],
            attempt_id="synthetic-search-attempt",
        )
        started = host.web_search.start(spec, {
            "mode": "quick", "information_need": "Locate a synthetic source",
            "queries": ["synthetic topic"],
        })
        operation = host.store.get(**started["operation_ref"])
        child_id = operation["body"]["items"][0]
        assert host.store.get("board_proposal", child_id)["body"]["status"] == "open"
        receipt = _cancel(host, run.run_id, control["body"]["task_id"], request_id="cancel-search")
        assert receipt.status == "cancelled"
        assert host.store.get("web_search_operation", operation["id"])["body"]["cancel_requested"]
        shared_proposal = host.store.get("board_proposal", child_id)
        assert shared_proposal["body"]["branch_of"] is None
        assert shared_proposal["body"]["status"] == "open"
        host.web_search._submit(operation, 0, "synthetic topic", "tavily", fallback_of=child_id)
        assert not any(
            item["body"].get("provider") == "tavily"
            for item in host.store.records("web_search_item")
        )
        assert asyncio.run(host.executor.run_ready(control["body"]["mission_id"]))["work"]
        assert host.store.get("web_shared_query_job", child_id)["body"]["phase"] == "known_no_dispatch"
    finally:
        host.close()


def test_optional_cancel_preserves_required_completion_and_rejects_wrong_or_stale_target(
    tmp_path, monkeypatch,
) -> None:
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        return answer("A bounded required answer.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="branch-cancel-optional", question="Complete one required analysis",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis"],
        ))
        optional = host._control(run.run_id, "agent-critique")
        other = host.start(LiveStartRequest(
            request_id="branch-cancel-other-run", question="Independent synthetic run",
            agent_ids=["agent-synthesis"], required_agent_ids=["agent-synthesis"],
        ))
        with pytest.raises(MissionError) as wrong:
            _cancel(host, other.run_id, optional["body"]["task_id"], request_id="wrong-run")
        assert wrong.value.status == 404
        proposal = host.store.get("board_proposal", optional["body"]["task_id"])
        revised = host.board.revise(
            "optional-version-change", mission_id=optional["body"]["mission_id"],
            proposal_id=proposal["id"], actor_id=optional["body"]["actor_id"],
            expected_version=proposal["version"], reason="Synthetic version change",
        )
        assert revised["status"] == "accepted"
        with pytest.raises(MissionError) as stale:
            host.cancel_branch(LiveBranchCancelRequest(
                request_id="stale-cancel", run_id=run.run_id,
                task_id=optional["body"]["task_id"], expected_task_version=proposal["version"],
                reason="Operator stopped this branch",
            ))
        assert stale.value.status == 409
        assert host.store.get("board_proposal", proposal["id"])["body"]["status"] == "open"
        receipt = _cancel(host, run.run_id, proposal["id"], request_id="cancel-optional")
        assert receipt.status == "cancelled"
        _wait_until(host, run.run_id, lambda: host._run(run.run_id)["body"]["status"] == "completed")
        required = host._control(run.run_id, "agent-synthesis")
        assert required["body"]["phase"] == "done"
        accepted_report = required["body"]["report_ref"]
        with pytest.raises(MissionError) as completed_target:
            _cancel(host, run.run_id, required["body"]["task_id"], request_id="cancel-completed")
        assert completed_target.value.status == 409
        assert host._control(run.run_id, "agent-synthesis")["body"]["report_ref"] == accepted_report
        assert host._control(run.run_id, "agent-critique")["body"]["phase"] == "cancelled"
    finally:
        host.close()


def test_cancel_inflight_navigation_preserves_one_local_effect_without_replay(
    tmp_path, monkeypatch,
) -> None:
    import ora.mission.service as service_module

    started = threading.Event()
    release = threading.Event()
    fetched = []
    calls = []
    chosen = {}
    html = b"<html><title>Synthetic venue</title><body>One saved lead.</body></html>"

    def fake_fetch(url, *, max_bytes):
        fetched.append(url)
        started.set()
        assert release.wait(5)
        return {
            "status": 200, "content": html,
            "content_sha256": hashlib.sha256(html).hexdigest(),
            "byte_length": len(html), "content_type": "text/html",
            "requested_url": url, "final_url": url, "redirect_chain": [],
            "access_basis": "public_https_unauthenticated", "authenticated": False,
            "retrieved_at": "2026-10-21T00:00:00Z",
        }

    def transport(request, *, timeout):
        body = json.loads(request.data)
        calls.append(body)
        step = len(calls)
        if step == 1:
            return tool("request_capability", {"tool_id": "list_venues"}, step)
        if step == 2:
            return tool("list_venues", {"limit": 1}, step)
        if step == 3:
            prior = json.loads(next(
                item["content"] for item in reversed(body["messages"])
                if item["role"] == "tool"
            ))
            chosen["catalog_ref"] = prior["entries"][0]["catalog_ref"]
            return tool("request_capability", {"tool_id": "navigate_venue"}, step)
        if step == 4:
            return tool("navigate_venue", {"catalog_ref": chosen["catalog_ref"]}, step)
        pytest.fail("Cancelled navigation started another model turn")

    monkeypatch.setattr(service_module, "fetch_public_source", fake_fetch)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="branch-cancel-navigation", question="Inspect one synthetic venue lead",
            agent_ids=["agent-synthesis"], required_agent_ids=["agent-synthesis"],
        ))
        _wait_until(host, run.run_id, started.is_set, seconds=8)
        control = host._control(run.run_id, "agent-synthesis")
        receipt = _cancel(host, run.run_id, control["body"]["task_id"], request_id="cancel-navigation")
        assert receipt.status == "cancellation_requested"
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not host.store.records("executor_unaccepted_result"):
            time.sleep(0.01)
        assert len(host.store.records("executor_unaccepted_result")) == 1
        assert len(host.store.records("venue_navigation")) == 1
        assert len(fetched) == 1
        assert len(calls) == 4
        assert host._run(run.run_id)["body"]["status"] == "cancelled"
    finally:
        release.set()
        host.close()
    reopened = service(
        tmp_path, monkeypatch,
        lambda *_args, **_kwargs: pytest.fail("Reopen dispatched a cancelled model"),
    )
    try:
        assert reopened._drive_run(reopened._run(run.run_id)) == (False, None)
        assert len(reopened.store.records("venue_navigation")) == 1
        assert len(fetched) == 1
    finally:
        reopened.close()


def test_open_reactivated_task_can_cancel_future_work_without_erasing_old_report(
    tmp_path, monkeypatch,
) -> None:
    host = service(tmp_path, monkeypatch,
                   lambda request, *, timeout: answer("A saved synthetic report.", 1))
    try:
        run = host.start(LiveStartRequest(
            request_id="branch-cancel-reactivated", question="Produce an initial synthetic report",
            agent_ids=["agent-synthesis"], required_agent_ids=["agent-synthesis"],
        ))
        original_maybe_complete = host._maybe_complete
        monkeypatch.setattr(host, "_maybe_complete", lambda _run: False)
        _wait_until(
            host, run.run_id,
            lambda: host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "done",
        )
        monkeypatch.setattr(host, "_maybe_complete", original_maybe_complete)
        completed = host._control(run.run_id, "agent-synthesis")
        report_ref = completed["body"]["report_ref"]
        assert report_ref and host.store.get(**report_ref)
        assert host._reactivate(completed, report_ref, "Review a later synthetic question")
        reopened = host._control(run.run_id, "agent-synthesis")
        assert reopened["body"]["phase"] == "ready_model"
        assert reopened["body"]["report_ref"] is None
        assert host.store.get("board_proposal", reopened["body"]["task_id"])["body"]["status"] == "open"
        receipt = _cancel(host, run.run_id, reopened["body"]["task_id"], request_id="cancel-reactivated")
        assert receipt.status == "cancelled"
        assert host.store.get(**report_ref)
        assert host.store.get(**ref(completed))["body"]["report_ref"] == report_ref
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "cancelled"
    finally:
        host.close()
