"""Offline recovery after killing a child at three durable mission boundaries."""

from __future__ import annotations

import multiprocessing
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from ora.contracts.models import LiveStartRequest
from ora.credentials import GroqCredentialProfiles
from ora.mission import MissionService
from ora.models import GroqSelectedAdapter, ModelSession
from ora.state import StateStore
from tests.mission.test_service import ROOT, answer, service


def _deny_sockets(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Offline process recovery attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def _child_at_boundary(state_dir: str, boundary: str, sender) -> None:
    """Confirm committed state through another connection, then await a real kill."""
    import ora
    import tests.mission.test_service as helper

    assert Path(ora.__file__).resolve().is_relative_to(ROOT)
    assert Path(helper.__file__).resolve().is_relative_to(ROOT)
    assert helper.ROOT == ROOT
    patch = pytest.MonkeyPatch()
    _deny_sockets(patch)
    # Spawned children do not inherit pytest's autouse profile isolation.
    original_profiles = GroqCredentialProfiles.__init__

    def fixture_profiles(instance, root):
        config_root = Path(state_dir) / "private-profile-fixture" if Path(root).resolve() == ROOT else root
        original_profiles(instance, config_root)

    patch.setattr(GroqCredentialProfiles, "__init__", fixture_profiles)
    host = service(Path(state_dir), patch, lambda *_args, **_kwargs: answer("Synthetic answer.", 1))
    try:
        if boundary == "work":
            run = host.start(LiveStartRequest(
                request_id="process-work", question="Save synthetic work before notification",
                agent_ids=["agent-synthesis"], required_agent_ids=["agent-synthesis"],
            ))
            control = host._control(run.run_id, "agent-synthesis")

            def held_notify(mission_id):
                fresh = StateStore(Path(state_dir))
                work = next(item for item in fresh.records("executor_work")
                            if item["body"]["mission_id"] == mission_id)
                matching = fresh.get("mission_task_control", control["id"])
                assert work["body"]["phase"] == "pending"
                assert matching["body"]["work_id"] == work["body"]["spec"]["work_id"]
                assert matching["body"]["phase"] == "pending_model"
                sender.send({"run_id": run.run_id, "work_id": work["id"],
                             "control_id": control["id"]})
                threading.Event().wait()

            patch.setattr(host.executor, "notify", held_notify)
            host._schedule_model(control)
        elif boundary == "result":
            run = host.start(LiveStartRequest(
                request_id="process-result", question="Save synthetic result before polling",
                agent_ids=["agent-synthesis"], required_agent_ids=["agent-synthesis"],
            ))
            control = host._control(run.run_id, "agent-synthesis")
            original_accept = host.executor._accept

            def held_accept(*args, **kwargs):
                saved = original_accept(*args, **kwargs)
                fresh = StateStore(Path(state_dir))
                work = next(item for item in fresh.records("executor_work")
                            if item["body"]["mission_id"] == control["body"]["mission_id"])
                assert work["body"]["phase"] == "completed"
                assert len(fresh.records("model_response_receipt")) == 1
                assert fresh.get("mission_task_control", control["id"])["body"]["phase"] != "done"
                sender.send({"run_id": run.run_id, "work_id": work["id"],
                             "control_id": control["id"]})
                threading.Event().wait()
                return saved

            patch.setattr(host.executor, "_accept", held_accept)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                host._drive_run(host._run(run.run_id))
                time.sleep(0.005)
            raise AssertionError("Accepted result boundary was not reached")
        elif boundary == "quota_wait":
            run = host.start(LiveStartRequest(
                request_id="process-quota-wait", question="Wait for synthetic token reset",
                agent_ids=["agent-synthesis"], required_agent_ids=["agent-synthesis"],
            ))
            control = host._control(run.run_id, "agent-synthesis")
            original_update = host._update_control

            def held_wait(item, **changes):
                saved = original_update(item, **changes)
                if changes.get("waiting_reason") == (
                    "Waiting for available token or request capacity before dispatch"
                ):
                    fresh = StateStore(Path(state_dir))
                    current = fresh.get("mission_task_control", item["id"])
                    works = fresh.records("executor_work")
                    assert current["body"]["phase"] == "pending_model"
                    assert current["body"]["waiting_reason"] == (
                        "Waiting for available token or request capacity before dispatch"
                    )
                    assert len(works) == 1 and works[0]["body"]["phase"] == "pending"
                    cap = fresh.records("capacity_operation")[0]
                    assert cap["body"]["phase"] == "waiting"
                    assert cap["body"]["wake_at"] > time.time()
                    assert fresh.records("model_response_receipt") == []
                    sender.send({"run_id": run.run_id, "control_id": item["id"],
                                 "wake_at": cap["body"]["wake_at"]})
                    threading.Event().wait()
                return saved

            patch.setattr(host, "_update_control", held_wait)
            assert host._schedule_model(control)
            work = host._work_for(host._control(run.run_id, "agent-synthesis"))
            spec = host.executor._spec_from(work)
            model_scopes = [scope for scope in spec.quota_scopes if scope.startswith("model:")]
            assert model_scopes
            host.capacity.observe_headers(
                model_scopes[0], remaining_requests=100, remaining_tokens=0,
                token_limit=50000, reset_after_tokens=3,
                observation_id="synthetic-process-token-reset",
            )
            cap_id = host.executor._capacity_id(spec)
            assert cap_id in host.executor._prepare(spec.mission_id)
            assert host.capacity.admit(
                host.executor.owner, "model", mission_id=spec.mission_id,
                eligible_operation_ids=frozenset({cap_id}),
                lease_seconds=host.executor.lease_seconds,
            ) is None
            assert host.capacity.get(cap_id)["phase"] == "waiting"
            assert host._advance(host._control(run.run_id, "agent-synthesis"))
            raise AssertionError("Quota wait callback returned without process interruption")
        else:
            raise AssertionError(f"Unexpected child boundary: {boundary}")
    except Exception as exc:
        sender.send({"error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        # A successful case is killed while blocked, so this is error cleanup.
        host.close()
        patch.undo()
        sender.close()


def _kill_after_checkpoint(tmp_path: Path, boundary: str) -> dict:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_child_at_boundary,
                              args=(str(tmp_path), boundary, sender))
    original_path = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT)))
    try:
        process.start()
        sender.close()
        if not receiver.poll(12):
            raise AssertionError(
                f"Child did not reach {boundary}; exitcode={process.exitcode}"
            )
        try:
            checkpoint = receiver.recv()
        except EOFError as exc:
            raise AssertionError(
                f"Child exited before {boundary} checkpoint; exitcode={process.exitcode}"
            ) from exc
        if "error" in checkpoint:
            raise AssertionError(f"Child failed before {boundary}: {checkpoint['error']}")
        assert process.is_alive()
        process.kill()  # Only this known child PID; its state was checked first.
        process.join(5)
        assert process.exitcode is not None
        return checkpoint
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.kill()
            process.join(5)
            process.close()
        receiver.close()
        if original_path is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = original_path


def _drive_until(host, run_id: str, predicate, *, timeout: float = 8) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Saved work did not resume at the expected boundary")


def test_process_loss_after_atomic_work_before_notify_recovers_once(tmp_path, monkeypatch):
    checkpoint = _kill_after_checkpoint(tmp_path, "work")
    _deny_sockets(monkeypatch)
    calls = []

    def transport(*_args, **_kwargs):
        calls.append("model")
        return answer("Recovered synthetic work.", 1)

    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    session = ModelSession(
        StateStore(tmp_path), ROOT, adapter=GroqSelectedAdapter(ROOT, transport=transport),
    )
    host = MissionService(
        ROOT, tmp_path, model_session=session, execution_mode="offline_test",
        start_supervisor=True,
    )
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and host._run(checkpoint["run_id"])["body"]["status"] != "completed":
            time.sleep(0.02)
        assert host._run(checkpoint["run_id"])["body"]["status"] == "completed"
        for _ in range(3):
            host.executor.notify(host._run(checkpoint["run_id"])["body"]["mission_id"])
        time.sleep(0.1)
        assert len(calls) == 1
        assert len(host.store.records("model_response_receipt")) == 1
        assert len(host.store.records("executor_artifact")) == 1
        assert host.store.get("executor_work", checkpoint["work_id"])["body"]["phase"] == "completed"
        assert host.store.get("mission_task_control", checkpoint["control_id"])["body"]["phase"] == "done"
    finally:
        host.close()


def test_process_loss_after_accepted_result_before_poll_does_not_replay(tmp_path, monkeypatch):
    checkpoint = _kill_after_checkpoint(tmp_path, "result")
    _deny_sockets(monkeypatch)
    host = service(
        tmp_path, monkeypatch,
        lambda *_args, **_kwargs: pytest.fail("Accepted response was replayed"),
    )
    try:
        _drive_until(host, checkpoint["run_id"], lambda: (
            host.store.get("mission_task_control", checkpoint["control_id"])["body"].get(
                "waiting_reason"
            ) == "Accepted model result awaits current task claim release"
        ))
        # The killed owner cannot release its claim; advance the offline clock
        # past the saved lease instead of waiting a minute in real time.
        host.store.clock = lambda: time.time() + host.executor.lease_seconds + 1
        _drive_until(host, checkpoint["run_id"],
                     lambda: host._run(checkpoint["run_id"])["body"]["status"] == "completed")
        assert len(host.store.records("model_response_receipt")) == 1
        assert len(host.store.records("executor_artifact")) == 1
        assert host.store.get("executor_work", checkpoint["work_id"])["body"]["phase"] == "completed"
        assert host.store.get("mission_task_control", checkpoint["control_id"])["body"]["phase"] == "done"
    finally:
        host.close()


def test_process_loss_during_saved_quota_wait_resumes_on_timer(tmp_path, monkeypatch):
    checkpoint = _kill_after_checkpoint(tmp_path, "quota_wait")
    _deny_sockets(monkeypatch)
    calls = []

    def transport(*_args, **_kwargs):
        calls.append("model")
        return answer("Synthetic answer after quota reset.", 1)

    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    session = ModelSession(
        StateStore(tmp_path), ROOT, adapter=GroqSelectedAdapter(ROOT, transport=transport),
    )
    host = MissionService(
        ROOT, tmp_path, model_session=session, execution_mode="offline_test",
        start_supervisor=True,
    )
    try:
        wait = max(0, checkpoint["wake_at"] - time.time())
        if wait:
            time.sleep(wait + 0.02)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and host._run(checkpoint["run_id"])["body"]["status"] != "completed":
            time.sleep(0.02)
        assert host._run(checkpoint["run_id"])["body"]["status"] == "completed"
        assert len(calls) == 1
        assert len(host.store.records("model_dispatch_attempt")) == 1
        assert len(host.store.records("model_response_receipt")) == 1
        assert host.store.get("mission_task_control", checkpoint["control_id"])["body"]["phase"] == "done"
    finally:
        host.close()
