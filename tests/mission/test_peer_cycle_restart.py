"""A saved reciprocal peer wait survives process loss beside independent work."""

from __future__ import annotations

import json
import multiprocessing
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from ora.contracts.models import LiveStartRequest
from ora.mission import service as service_module
from ora.mission.waits import inspect_waits
from ora.state import StateStore
from tests.mission.test_service import ROOT, answer, serialized_peer_name, service, tool


def _deny_sockets(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Offline peer cycle attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def _allow_offline_fixture_profile(monkeypatch, state_dir: Path) -> None:
    # This process-loss test uses a synthetic adapter, independent of the
    # operator's current selection for real provider credentials.
    path = state_dir / "offline-credential-profiles.json"
    path.write_text(json.dumps({
        "version": 1, "profiles": {"primary": {"enabled": True}},
    }), encoding="utf-8")
    monkeypatch.setattr("ora.credentials.PROFILE_CONFIG_PATH", str(path))


def _last_tool(body: dict) -> tuple[str, dict]:
    for message in reversed(body["messages"]):
        if message["role"] == "tool":
            return message["name"], json.loads(message["content"])
    return "", {}


def _cycle_child(state_dir: str, sender) -> None:
    import ora
    import tests.mission.test_service as helper

    assert Path(ora.__file__).resolve().is_relative_to(ROOT)
    assert Path(helper.__file__).resolve().is_relative_to(ROOT)
    assert helper.ROOT == ROOT
    patch = pytest.MonkeyPatch()
    _deny_sockets(patch)
    _allow_offline_fixture_profile(patch, Path(state_dir))
    turns = {"Sage": 0, "Mxra": 0}
    requests = {}
    host = None
    run = None

    def transport(request, *, timeout):
        body = json.loads(request.data)
        role = serialized_peer_name(body)
        assert role in turns
        target = "agent-critique" if role == "Sage" else "agent-synthesis"
        turns[role] += 1
        step = turns[role]
        response_index = step if role == "Sage" else 100 + step
        if step == 1:
            return tool("peer_request", {
                "target_instance_id": target,
                "question": f"Check {role}'s synthetic limitation independently.",
            }, response_index)
        if step == 2:
            name, result = _last_tool(body)
            assert name == "peer_request" and result["status"] == "request_saved"
            requests[role] = result["request_id"]
            return tool("request_capability", {"tool_id": "wait_for_dependencies"}, response_index)
        if step == 3:
            outgoing = host.store.get("mail_request", requests[role])
            target_control = host._control(run.run_id, target)
            return tool("wait_for_dependencies", {"dependencies": [{
                "kind": "peer_request", "request_id": outgoing["id"],
                "request_version": outgoing["version"],
                "target_control_id": target_control["id"],
                "target_control_version": target_control["version"],
            }], "reason": "Wait for an exact independent peer check."}, response_index)
        raise AssertionError("A waiting peer called the model before the checkpoint")

    try:
        host = service(Path(state_dir), patch, transport)
        run = host.start(LiveStartRequest(
            request_id="process-reciprocal-cycle",
            question="Compare synthetic peer limitations across three branches",
            evidence_text="Synthetic source for all three peers.",
            agent_ids=["agent-synthesis", "agent-critique", "agent-data-management"],
            required_agent_ids=["agent-synthesis", "agent-critique", "agent-data-management"],
        ))
        independent = host._control(run.run_id, "agent-data-management")
        host._update_control(independent, phase="retry_at", retry_at=time.time() + 3600,
                             waiting_reason="Fixture holds independent branch until reopen")
        original_notice = service_module.wait_replan_notification

        def defer_notice_until_both_waits(*args, **kwargs):
            if len(host.store.records("mission_wait_decision")) < 2:
                return None
            return original_notice(*args, **kwargs)

        patch.setattr(service_module, "wait_replan_notification", defer_notice_until_both_waits)
        original_update = host._update_control

        def held_wait(control, **changes):
            saved = original_update(control, **changes)
            if changes.get("phase") == "waiting" and changes.get("wait_for"):
                fresh = StateStore(Path(state_dir))
                decisions = fresh.records("mission_wait_decision")
                peer_actors = {
                    host._control(run.run_id, alias)["body"]["actor_id"]
                    for alias in ("agent-synthesis", "agent-critique")
                }
                peer_controls = [
                    item for item in fresh.records("mission_task_control")
                    if item["body"].get("actor_id") in peer_actors
                ]
                both_current_waits = len(peer_controls) == 2 and all(
                    item["body"].get("phase") == "waiting"
                    and item["body"].get("work_id") is None
                    and item["body"].get("wait_for")
                    for item in peer_controls
                )
                if len(decisions) >= 2 and both_current_waits:
                    mission_id = host._run(run.run_id)["body"]["mission_id"]
                    diagnosis = inspect_waits(fresh, mission_id)
                    assert len(diagnosis["cycles"]) == 1
                    assert len(diagnosis["cycles"][0]["edges"]) == 2
                    assert all(item["body"]["phase"] not in {
                        "admitted", "dispatched", "uncertain"
                    } for item in fresh.records("capacity_operation"))
                    assert all(item["body"].get("status") == "captured"
                               for item in fresh.records("model_dispatch_attempt"))
                    assert {item["body"]["actor_id"] for item in peer_controls} == set(
                        diagnosis["cycles"][0]["actor_ids"]
                    )
                    assert len(fresh.records("mail_request")) == 2
                    sender.send({
                        "run_id": run.run_id, "mission_id": mission_id,
                        "requests": requests, "cycle_fingerprint": diagnosis["cycles"][0]["fingerprint"],
                        "source_refs": host._run(run.run_id)["body"]["source_refs"],
                        "settings": host._run(run.run_id)["body"]["profile"]["modelSettings"],
                    })
                    threading.Event().wait()
            return saved

        patch.setattr(host, "_update_control", held_wait)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            host._drive_run(host._run(run.run_id))
            time.sleep(0.005)
        raise AssertionError("Two saved peer waits were not reached")
    except Exception:
        import traceback

        sender.send({"error": traceback.format_exc()})
        raise
    finally:
        if host is not None:
            host.close()
        patch.undo()
        sender.close()


def _kill_after_wait_checkpoint(tmp_path: Path) -> dict:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_cycle_child, args=(str(tmp_path), sender))
    original_path = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT)))
    try:
        process.start()
        sender.close()
        if not receiver.poll(18):
            raise AssertionError(f"Peer cycle checkpoint absent; child exit={process.exitcode}")
        try:
            checkpoint = receiver.recv()
        except EOFError as exc:
            raise AssertionError(
                f"Child exited before current peer waits; exitcode={process.exitcode}"
            ) from exc
        if "error" in checkpoint:
            raise AssertionError(checkpoint["error"])
        assert process.is_alive()
        process.kill()
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


def test_three_peer_cycle_restart_keeps_independent_branch_and_exact_reviews(
    tmp_path, monkeypatch,
):
    checkpoint = _kill_after_wait_checkpoint(tmp_path)
    _deny_sockets(monkeypatch)
    _allow_offline_fixture_profile(monkeypatch, tmp_path)
    requests = checkpoint["requests"]
    host = None
    observed = {"feedback": set(), "cycle_fingerprints": set(),
                "delivered": set(), "reviewed": set(), "independent": 0,
                "independent_while_unresolved": False, "calls": [], "errors": []}
    report_text = {
        "Sage": "Sage's synthetic limited response.",
        "Mxra": "Mxra's synthetic limited response.",
    }
    revised_sage_text = "Sage's revised limited response after reviewing Mxra's saved report."

    def scripted_transport(request, *, timeout):
        body = json.loads(request.data)
        role = serialized_peer_name(body, ("Sage", "Mxra", "Remy"))
        if role == "Remy":
            observed["independent"] += 1
            observed["independent_while_unresolved"] = any(
                host.store.get("mail_request", request_id)["body"].get("accepted_answer_ref") is None
                for request_id in requests.values()
            )
            return answer("Remy's independent synthetic analysis.", 300)
        other = "Mxra" if role == "Sage" else "Sage"
        outgoing = host.store.get("mail_request", requests[role])
        incoming = host.store.get("mail_request", requests[other])
        name, result = _last_tool(body)
        observed["calls"].append((role, name, bool(outgoing["body"]["answer_refs"]),
                                  bool(incoming["body"]["answer_refs"])))
        feedback_messages = [
            json.loads(message["content"])["host_feedback"]
            for message in body["messages"]
            if message["role"] == "user"
            and '"wait_needs_peer_replan"' in message["content"]
        ]
        if feedback_messages:
            observed["feedback"].add(role)
            observed["cycle_fingerprints"].update(
                cycle["fingerprint"] for feedback in feedback_messages
                for cycle in feedback["cycles"]
            )
        if outgoing["body"]["answer_refs"] and role not in observed["delivered"]:
            context = json.loads(body["messages"][-1]["content"])
            answer_ref = outgoing["body"]["answer_refs"][0]
            peer_report_ref = host.store.get(**answer_ref)["body"]["artifact_ref"]
            preview = next(item for item in context["peer_answer_previews"]
                           if item["answer_ref"] == answer_ref)
            assert preview["request_ref"]["id"] == outgoing["id"]
            assert preview["request_status"] in {"awaiting_review", "partial_awaiting_review"}
            assert preview["question"] == f"Check {role}'s synthetic limitation independently."
            assert preview["report_ref"] == peer_report_ref
            assert preview["report"]["body"] == report_text[other]
            assert preview["report_delivery"] == "complete_in_input"
            assert "review_peer_answer" in {
                item["function"]["name"] for item in body["tools"]
            }
            observed["delivered"].add(role)
            return tool("review_peer_answer", {
                "request_id": outgoing["id"],
                "answer_id": outgoing["body"]["answer_refs"][0]["id"],
                "disposition": "answered",
                "reason": "The exact saved peer report addresses the synthetic check.",
            }, 401 if role == "Sage" else 501)
        if name == "review_peer_answer":
            assert result["status"] == "review_saved"
            observed["reviewed"].add(role)
            return tool("publish_result", {
                "title": f"{role}'s bounded synthetic conclusion",
                "text": revised_sage_text if role == "Sage" else report_text[role],
                "citations": [],
                "limitations": ["Only synthetic evidence was supplied"],
                **({} if role == "Sage" else {"answer_request_id": incoming["id"]}),
            }, 402 if role == "Sage" else 502)
        if role == "Sage" and not incoming["body"]["answer_refs"]:
            assert role in observed["feedback"]
            return tool("publish_result", {
                "title": "Sage's independent limited reply",
                "text": report_text[role], "citations": [],
                "limitations": ["Only synthetic evidence was supplied"],
                "answer_request_id": incoming["id"],
                "scope_completion": {
                    "reason": "Answer the incoming request despite reciprocal waits.",
                    "decisions": [],
                },
            }, 403)
        if role == "Mxra" and not outgoing["body"]["answer_refs"]:
            assert role in observed["feedback"]
            target = host._control(checkpoint["run_id"], "agent-synthesis")
            return tool("wait_for_dependencies", {"dependencies": [{
                "kind": "peer_request", "request_id": outgoing["id"],
                "request_version": outgoing["version"],
                "target_control_id": target["id"],
                "target_control_version": target["version"],
            }], "reason": "Await the saved partial peer answer."}, 503)
        raise AssertionError(f"Unexpected {role} continuation after {name}")

    def transport(request, *, timeout):
        try:
            return scripted_transport(request, timeout=timeout)
        except Exception as exc:
            observed["errors"].append(f"{type(exc).__name__}: {exc}")
            raise

    host = service(tmp_path, monkeypatch, transport)
    receipt_ids = set()
    report_refs = []
    try:
        saved = host._run(checkpoint["run_id"])
        assert saved["body"]["source_refs"] == checkpoint["source_refs"]
        assert saved["body"]["profile"]["modelSettings"] == checkpoint["settings"]
        diagnosis = inspect_waits(host.store, checkpoint["mission_id"])
        assert len(diagnosis["cycles"]) == 1
        assert diagnosis["cycles"][0]["fingerprint"] == checkpoint["cycle_fingerprint"]
        assert len(diagnosis["cycles"][0]["edges"]) == 2
        assert all(item["body"]["status"] == "model_selected_wait"
                   for item in host.store.records("mission_wait_decision"))
        independent = host._control(checkpoint["run_id"], "agent-data-management")
        assert independent["body"]["actor_id"] not in diagnosis["cycles"][0]["actor_ids"]
        host._update_control(independent, phase="ready_model", waiting_reason=None,
                             retry_at=None)
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            host._drive_run(host._run(checkpoint["run_id"]))
            if host._run(checkpoint["run_id"])["body"]["status"] == "completed":
                break
            if any(item["body"]["phase"] == "model_response_lost"
                   for item in host.store.records("executor_work")):
                break
            if observed["errors"]:
                break
            time.sleep(0.01)
        assert host._run(checkpoint["run_id"])["body"]["status"] == "completed", observed
        assert observed["independent"] == 1
        assert observed["independent_while_unresolved"] is True
        assert observed["feedback"]
        assert observed["cycle_fingerprints"] == {checkpoint["cycle_fingerprint"]}
        assert observed["delivered"] == {"Sage", "Mxra"}
        assert observed["reviewed"] == {"Sage", "Mxra"}
        assert len(host.store.records("mail_request")) == 2
        assert len(host.store.records("mail_answer")) == 2
        for request_id in requests.values():
            request = host.store.get("mail_request", request_id)
            assert request["body"]["accepted_answer_ref"] == request["body"]["answer_refs"][0]
            assert request["body"]["reviewer_id"] == request["body"]["requester_id"]
        assert all(host._control(checkpoint["run_id"], alias)["body"]["phase"] == "done"
                   for alias in ("agent-synthesis", "agent-critique", "agent-data-management"))
        report_refs = [host._control(checkpoint["run_id"], alias)["body"]["report_ref"]
                       for alias in ("agent-synthesis", "agent-critique", "agent-data-management")]
        assert all(report_refs)
        assert len({pointer["id"] for pointer in report_refs}) == 3
        assert host.store.get(**report_refs[0])["body"]["body"] == revised_sage_text
        receipt_ids = {item["id"] for item in host.store.records("model_response_receipt")}
        assert host._run(checkpoint["run_id"])["body"]["source_refs"] == checkpoint["source_refs"]
        assert host._run(checkpoint["run_id"])["body"]["profile"]["modelSettings"] == checkpoint["settings"]
    finally:
        host.close()

    reopened = service(
        tmp_path, monkeypatch,
        lambda *_args, **_kwargs: pytest.fail("Completed peer cycle replayed a model call"),
    )
    try:
        for _ in range(3):
            reopened._drive_run(reopened._run(checkpoint["run_id"]))
        assert reopened._run(checkpoint["run_id"])["body"]["status"] == "completed"
        assert {item["id"] for item in reopened.store.records("model_response_receipt")} == receipt_ids
        assert [reopened._control(checkpoint["run_id"], alias)["body"]["report_ref"]
                for alias in ("agent-synthesis", "agent-critique", "agent-data-management")] == report_refs
        assert reopened._run(checkpoint["run_id"])["body"]["source_refs"] == checkpoint["source_refs"]
        assert reopened._run(checkpoint["run_id"])["body"]["profile"]["modelSettings"] == checkpoint["settings"]
    finally:
        reopened.close()
