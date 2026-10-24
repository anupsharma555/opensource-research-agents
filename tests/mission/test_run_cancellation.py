"""Offline operator stop for one exact mission run."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ora.contracts.models import LiveRunCancelRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.mission.service import MissionError
from ora.mission.work import WorkError
from tests.mission.test_service import answer, service


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Run cancellation attempted network access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def _request(host, run_id, request_id="stop-run", *, reason="Stop this run"):
    return LiveRunCancelRequest(
        request_id=request_id, run_id=run_id,
        expected_run_version=host._run(run_id)["version"], reason=reason,
    )


def test_stop_fences_selected_queued_run_and_preserves_other_run(tmp_path, monkeypatch):
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        return answer("Separate synthetic result.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    reopened = None
    try:
        selected = host.start(LiveStartRequest(
            request_id="selected-run", question="Stop this synthetic analysis",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        other = host.start(LiveStartRequest(
            request_id="unaffected-run", question="Continue another analysis",
            agent_ids=["agent-synthesis"],
        ))
        request = _request(host, selected.run_id)
        selected_control = host._control(selected.run_id, "agent-synthesis")
        other_control = host._control(other.run_id, "agent-synthesis")
        assert host._schedule_model(selected_control)
        work_id = host._control(selected.run_id, "agent-synthesis")["body"]["work_id"]
        receipt = host.cancel_run(request)
        assert receipt.status == "cancelled"
        assert receipt.run_ref.id == selected.run_id
        assert host.cancel_run(request).model_dump() == {
            **receipt.model_dump(), "reused": True,
        }
        assert host.state(selected.run_id).runs[0].version is not None
        assert host._run(selected.run_id)["body"]["status"] == "cancelled"
        assert all(item["body"]["phase"] == "cancelled"
                   for item in host._controls(selected.run_id))
        assert host._control(other.run_id, "agent-synthesis") == other_control
        assert next(item for item in host.executor._work(
            selected_control["body"]["mission_id"]
        ) if item["body"]["spec"]["work_id"] == work_id)["body"]["phase"] == "cancelled"
        assert host._drive_run(host._run(selected.run_id)) == (False, None)
        assert not calls
        with pytest.raises(MissionError) as changed:
            host.cancel_run(request.model_copy(update={"reason": "Changed reason"}))
        assert changed.value.status == 409
        with pytest.raises(MissionError) as new_id:
            host.cancel_run(_request(host, selected.run_id, "another-stop"))
        assert new_id.value.status == 409
        with pytest.raises(MissionError) as missing:
            host.cancel_run(LiveRunCancelRequest(
                request_id="wrong-workspace", run_id="missing-run",
                expected_run_version=1, reason="Stop unavailable run",
            ))
        assert missing.value.status == 404
        host.close()
        reopened = service(tmp_path, monkeypatch, transport)
        assert reopened.cancel_run(request).reused
        assert reopened._drive_run(reopened._run(selected.run_id)) == (False, None)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and reopened._run(other.run_id)["body"]["status"] != "completed":
            reopened._drive_run(reopened._run(other.run_id))
            time.sleep(0.01)
        assert reopened._run(other.run_id)["body"]["status"] == "completed"
        assert len(calls) == 1
    finally:
        if reopened is not None:
            reopened.close()
        else:
            host.close()


def test_distinct_commands_racing_same_run_version_have_one_winner(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run = host.start(LiveStartRequest(
            request_id="race-stop-run", question="Synthetic concurrent stop",
            agent_ids=["agent-synthesis"],
        ))
        first = _request(host, run.run_id, "stop-a")
        second = _request(host, run.run_id, "stop-b")
        barrier = threading.Barrier(2)

        def stop(command):
            barrier.wait(timeout=5)
            try:
                return host.cancel_run(command)
            except MissionError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(stop, [first, second]))
        assert len([item for item in results if not isinstance(item, MissionError)]) == 1
        assert len([item for item in results if isinstance(item, MissionError)
                    and item.status == 409]) == 1
        assert len(host.store.records("mission_run_cancellation")) == 1
    finally:
        host.close()


def test_completed_run_with_continuing_work_keeps_completion_history(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run = host.start(LiveStartRequest(
            request_id="completed-continuation-stop", question="Synthetic continued work",
            agent_ids=["agent-synthesis"],
        ))
        saved = host._run(run.run_id)
        completed = {**saved["body"], "status": "completed", "completed_at": "2026-01-01T00:00:00Z"}
        host._save("mission_run", run.run_id, completed,
                   mission_id=saved["body"]["mission_id"])
        receipt = host.cancel_run(_request(host, run.run_id))
        assert receipt.status == "cancelled"
        latest = host._run(run.run_id)
        assert latest["body"]["completed_at"] == completed["completed_at"]
        assert latest["body"]["status"] == "cancelled"
        assert host.store.get("mission_run", run.run_id, saved["version"])["body"] == saved["body"]
    finally:
        host.close()


def test_inflight_model_keeps_late_usage_without_accepted_output(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def transport(request, *, timeout):
        started.set()
        assert release.wait(5)
        return answer("Late synthetic answer.", 1)

    host = service(tmp_path, monkeypatch, transport)
    worker = None
    try:
        run = host.start(LiveStartRequest(
            request_id="inflight-stop-run", question="Inspect delayed synthetic call",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        worker = threading.Thread(
            target=lambda: asyncio.run(host.executor.run_ready(control["body"]["mission_id"])),
            daemon=True,
        )
        worker.start()
        assert started.wait(5)
        command = _request(host, run.run_id)
        receipt = host.cancel_run(command)
        assert receipt.status == "cancellation_requested"
        assert host.cancel_run(command).status == "cancellation_requested"
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert not host.store.records("executor_artifact")
        observations = host.store.records("executor_unaccepted_result")
        assert len(observations) == 1
        assert observations[0]["body"]["used_tokens"] == 11
        assert host.store.records("model_response_receipt")
    finally:
        release.set()
        if worker is not None:
            worker.join(5)
        host.close()


def test_cancel_covers_search_child_and_pending_peer_request(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run = host.start(LiveStartRequest(
            request_id="search-mail-stop-run", question="Synthetic search and peer request",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        source = host._control(run.run_id, "agent-synthesis")
        recipient = host._control(run.run_id, "agent-critique")
        mission_id = source["body"]["mission_id"]
        proposed = host.board.propose(
            "synthetic-child", mission_id=mission_id, proposal_id="synthetic-search-child",
            actor_id=source["body"]["actor_id"], objective="Inspect child",
            expected_artifact="Synthetic child result", next_action="Inspect",
            branch_of=source["body"]["task_id"],
        )
        assert proposed["status"] == "accepted"
        host.store.transact(
            "synthetic-search-operation", {},
            lambda tx: tx.put("web_search_operation", "synthetic-search-operation", {
                "mission_id": mission_id, "run_id": run.run_id,
                "task_id": source["body"]["task_id"], "cancel_requested": False,
                "items": ["synthetic-search-child"], "deliveries": [],
            }), mission_id=mission_id,
        )
        sent = host.mailbox.send_request(
            "synthetic-peer-request", mission_id=mission_id,
            request_id="synthetic-peer-request", message_id="synthetic-peer-message",
            requester_id=source["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"],
            question="Check the synthetic child", expected_artifact="Peer note",
            cause_ref=source["body"]["proposal_ref"],
            payload={"question": "Check the synthetic child"},
            task_ref=source["body"]["proposal_ref"],
        )
        assert sent["status"] == "accepted"
        receipt = host.cancel_run(_request(host, run.run_id))
        assert receipt.status == "cancelled"
        assert host.store.get("board_proposal", proposed["proposal_ref"]["id"])["body"]["status"] == "cancelled"
        assert host.store.get("web_search_operation", "synthetic-search-operation")["body"]["cancel_requested"]
        request = host.store.get("mail_request", "synthetic-peer-request")
        assert request["body"]["status"] == "cancelled"
        assert request["body"]["cancellation_ref"] == receipt.cancellation_ref.model_dump()
        assert host.store.get("mail_outbox", "synthetic-peer-message")["body"]["status"] == "cancelled"
        assert host.store.get(**ref(host._run(run.run_id)))["body"]["status"] == "cancelled"
    finally:
        host.close()


def test_cancel_before_peer_activation_cannot_open_new_control(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    entered = threading.Event()
    release = threading.Event()
    try:
        run = host.start(LiveStartRequest(
            request_id="peer-activation-stop", question="Synthetic peer activation",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        source = host._control(run.run_id, "agent-synthesis")
        recipient = host._control(run.run_id, "agent-critique")
        sent = host.mailbox.send_request(
            "activate-peer-request", mission_id=source["body"]["mission_id"],
            request_id="activate-peer-request", message_id="activate-peer-message",
            requester_id=source["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"],
            question="Review synthetic result", expected_artifact="Peer note",
            cause_ref=source["body"]["proposal_ref"],
            payload={"question": "Review synthetic result"},
        )
        request = host.store.get(**sent["request_ref"])
        saved = host._run(run.run_id)
        host._save("mission_run", run.run_id, {
            **saved["body"], "status": "completed", "completed_at": "2026-01-01T00:00:00Z",
        }, mission_id=saved["body"]["mission_id"])
        original_propose = host.board.propose

        def pause_after_proposal(*args, **kwargs):
            result = original_propose(*args, **kwargs)
            entered.set()
            assert release.wait(5)
            return result

        monkeypatch.setattr(host.board, "propose", pause_after_proposal)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                host._open_post_scope_response, host._run(run.run_id),
                request, host._controls(run.run_id),
            )
            assert entered.wait(5)
            host.cancel_run(_request(host, run.run_id))
            release.set()
            with pytest.raises(Exception):
                future.result(timeout=5)
        controls = host._controls(run.run_id)
        assert all(item["body"]["phase"] in {"cancelled", "done", "deferred"}
                   for item in controls)
        assert not any(item["body"].get("control_type") == "peer_response"
                       for item in controls)
    finally:
        release.set()
        host.close()


def test_done_continued_peer_cannot_author_new_scope_after_stop(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run = host.start(LiveStartRequest(
            request_id="done-peer-stop", question="Synthetic continued peer work",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        done = host._control(run.run_id, "agent-synthesis")
        host._update_control(done, phase="done", scope_status="continued")
        saved = host._run(run.run_id)
        host._save("mission_run", run.run_id, {
            **saved["body"], "status": "completed", "completed_at": "2026-01-01T00:00:00Z",
        }, mission_id=saved["body"]["mission_id"])
        host.cancel_run(_request(host, run.run_id))
        preserved_done = host._control(run.run_id, "agent-synthesis")
        assert preserved_done["body"]["phase"] == "done"
        with pytest.raises(WorkError, match="Cancelled run"):
            host.work.record_scope_disposition(
                "late-scope-decision", run_ref=ref(host._run(run.run_id)),
                actor_id=done["body"]["actor_id"],
                source_control_ref=ref(preserved_done),
                decisions=[], reason="Late synthetic scope choice",
            )
        assert not host.store.records("mission_scope_disposition")
    finally:
        host.close()


def test_early_cancel_rejects_until_initialization_is_readable(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    entered = threading.Event()
    release = threading.Event()
    original_propose = host.board.propose

    def pause_before_proposal(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_propose(*args, **kwargs)

    monkeypatch.setattr(host.board, "propose", pause_before_proposal)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(host.start, LiveStartRequest(
                request_id="initialize-stop-race", question="Synthetic start race",
                agent_ids=["agent-synthesis"],
            ))
            assert entered.wait(5)
            saved_run = host.store.records("mission_run")[0]
            with pytest.raises(MissionError) as initializing:
                host.cancel_run(_request(host, saved_run["id"]))
            assert initializing.value.status == 409
            assert not host.store.records("mission_run_cancellation")
            release.set()
            started = future.result(timeout=5)
        receipt = host.cancel_run(_request(host, started.run_id))
        assert receipt.status == "cancelled"
        assert host._run(saved_run["id"])["body"]["status"] == "cancelled"
        assert all(item["body"]["phase"] == "cancelled"
                   for item in host._controls(saved_run["id"]))
        assert host.state(started.run_id).snapshot.mission.status == "cancelled"
    finally:
        release.set()
        host.close()
