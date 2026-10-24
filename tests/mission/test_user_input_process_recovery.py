"""Recover a captured response across a real process stop and later user input."""

from __future__ import annotations

import json
import multiprocessing
import socket
import threading
import time
from pathlib import Path

import pytest

from ora.contracts.models import LiveStartRequest
from ora.credentials import GroqCredentialProfiles
from ora.state import StateStore
from tests.mission.test_service import ROOT, answer, service
from tests.mission.test_user_input_delivery import _command

UPDATE = "New direction: qualify the corrected synthetic count before concluding."


def _deny_sockets(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Process recovery attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _child_captured_before_accept(state_dir: str, sender) -> None:
    import ora
    import tests.mission.test_service as helper

    assert Path(ora.__file__).resolve().is_relative_to(ROOT)
    assert Path(helper.__file__).resolve().is_relative_to(ROOT)
    patch = pytest.MonkeyPatch()
    _deny_sockets(patch)
    original_profiles = GroqCredentialProfiles.__init__

    def private_profiles(instance, root):
        selected = Path(state_dir) / "private-profile-fixture" if Path(root).resolve() == ROOT else root
        original_profiles(instance, selected)

    patch.setattr(GroqCredentialProfiles, "__init__", private_profiles)
    observed = []

    def transport(request, *, timeout):
        observed.append(bytes(request.data))
        return answer("Old synthetic count without the later correction.", 1)

    host = service(Path(state_dir), patch, transport)
    try:
        def interrupted(*_args, **_kwargs):
            raise RuntimeError("Synthetic interruption before captured-result acceptance")

        host.executor._accept = interrupted
        host.model_recovery.accept_captured_response = lambda **_kwargs: {"status": "not_eligible"}
        run = host.start(LiveStartRequest(
            request_id="process-input-recovery", question="Assess a synthetic count.",
            agent_ids=["agent-synthesis"], required_agent_ids=["agent-synthesis"],
        ))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            host._drive_run(host._run(run.run_id))
            work = next((item for item in host.store.records("executor_work")
                         if item["body"].get("phase") == "result_unaccepted"), None)
            if work is not None:
                fresh = StateStore(Path(state_dir))
                assert len(observed) == 1
                assert len(fresh.records("model_response_receipt")) == 1
                assert fresh.records("executor_artifact") == []
                assert fresh.records("board_outcome") == []
                intent = fresh.records("model_request_intent")[0]
                assert fresh.read_blob(intent["body"]["blob"]["sha256"]) == observed[0]
                sender.send({
                    "run_id": run.run_id, "work_id": work["id"],
                    "intent_id": intent["id"], "request_sha256": intent["body"]["request_sha256"],
                })
                threading.Event().wait()  # The parent stops this exact child PID.
            time.sleep(0.005)
        raise AssertionError("Captured response did not reach the saved unaccepted boundary")
    except Exception as exc:
        sender.send({"error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        host.close()
        patch.undo()
        sender.close()


def _kill_at_captured_boundary(state_dir: Path) -> dict:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=_child_captured_before_accept, args=(str(state_dir), sender))
    try:
        child.start()
        sender.close()
        assert receiver.poll(12), f"Child did not capture a response; exit={child.exitcode}"
        checkpoint = receiver.recv()
        assert "error" not in checkpoint, checkpoint
        assert child.is_alive()
        child.kill()
        child.join(5)
        assert child.exitcode is not None
        return checkpoint
    finally:
        if child.pid is not None:
            if child.is_alive():
                child.kill()
            child.join(5)
            child.close()
        receiver.close()


def test_process_stop_then_new_input_accepts_known_result_once_and_revises(
    tmp_path, monkeypatch,
):
    checkpoint = _kill_at_captured_boundary(tmp_path)
    _deny_sockets(monkeypatch)
    new_requests = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        new_requests.append(bytes(request.data))
        assert len(new_requests) == 1
        assert UPDATE in json.dumps(packet["messages"])
        return answer("The corrected synthetic count needs qualification.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run_id = checkpoint["run_id"]
        old_intent = host.store.get("model_request_intent", checkpoint["intent_id"])
        old_bytes = host.store.read_blob(old_intent["body"]["blob"]["sha256"])
        assert old_intent["body"]["request_sha256"] == checkpoint["request_sha256"]
        assert UPDATE not in old_bytes.decode()
        old_work = host.store.get("executor_work", checkpoint["work_id"])
        old_spec = host.executor._spec_from(old_work)
        cap_id = host.executor._capacity_id(old_spec)
        settled_cap = host.store.get("capacity_operation", cap_id)
        assert settled_cap["body"]["phase"] == "known_unaccepted"
        assert host._run(run_id)["body"]["status"] == "active"
        receipt = host.submit_user_input(_command(host, run_id, UPDATE))
        assert host.store.get(**receipt.input_ref.model_dump())["body"]["text"] == UPDATE
        # The stopped owner cannot release its lease; advance the offline clock.
        host.store.clock = lambda: time.time() + host.executor.lease_seconds + 1
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            host._drive_run(host._run(run_id))
            if host._run(run_id)["body"]["status"] == "completed":
                break
            time.sleep(0.01)
        assert host._run(run_id)["body"]["status"] == "completed"
        assert len(new_requests) == 1
        assert host.store.read_blob(old_intent["body"]["blob"]["sha256"]) == old_bytes
        assert host.store.get("capacity_operation", cap_id) == settled_cap
        assert len(host.store.records("model_dispatch_attempt")) == 2
        assert len(host.store.records("model_response_receipt")) == 2
        assert len(host.store.records("mission_user_input")) == 1
        final_ref = host._control(run_id, "agent-synthesis")["body"]["report_ref"]
        assert "corrected synthetic count" in host.store.get(**final_ref)["body"]["body"]
        assert "Old synthetic count" not in host.store.get(**final_ref)["body"]["body"]
        host._drive_run(host._run(run_id))
        assert len(new_requests) == 1
    finally:
        host.close()
