"""Offline integration checks for peer-directed recovery of a saved stranded wait."""

from __future__ import annotations

import json
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.mission import service as service_module
from ora.state import StateConflict
from tests.mission.test_service import serialized_peer_name, service, tool


def _host(tmp_path, monkeypatch):
    def forbidden_transport(*_args, **_kwargs):
        raise AssertionError("Wait diagnosis must not dispatch a model call")

    host = service(tmp_path, monkeypatch, forbidden_transport)
    run = host.start(LiveStartRequest(
        request_id="stranded-wait-resume", question="Review a synthetic claim",
        agent_ids=["agent-synthesis", "agent-critique"],
        required_agent_ids=["agent-synthesis", "agent-critique"],
    ))
    return host, run


def test_empty_wait_replans_only_affected_peer_once_across_restart(tmp_path, monkeypatch):
    host, run = _host(tmp_path, monkeypatch)
    reopened = None
    try:
        affected = host._control(run.run_id, "agent-synthesis")
        sibling = host._control(run.run_id, "agent-critique")
        host._update_control(
            affected, phase="waiting", work_id=None, wait_for=[],
            waiting_reason="No future wake was recorded",
        )
        affected = host._control(run.run_id, "agent-synthesis")
        assert host._resume_explicit_wait(affected)
        recovered = host._control(run.run_id, "agent-synthesis")
        assert recovered["body"]["phase"] == "ready_model"
        assert recovered["body"]["feedback"]["status"] == "wait_needs_peer_replan"
        assert recovered["body"]["feedback"]["stranded_waits"][0]["reason"] == (
            "no_recorded_future_wake"
        )
        assert host._control(run.run_id, "agent-critique") == sibling
        assert not host._resume_explicit_wait(recovered)
        host.close()
        reopened = _host_reopen(tmp_path, monkeypatch)
        assert reopened._control(run.run_id, "agent-synthesis") == recovered
        assert not reopened._resume_explicit_wait(recovered)
        assert reopened._control(run.run_id, "agent-critique") == sibling
    finally:
        if reopened is not None:
            reopened.close()
        else:
            host.close()


def _host_reopen(tmp_path, monkeypatch):
    def forbidden_transport(*_args, **_kwargs):
        raise AssertionError("Reopening wait state must not dispatch a model call")

    return service(tmp_path, monkeypatch, forbidden_transport)


def test_empty_wait_does_not_release_operator_hold_or_saved_timer(tmp_path, monkeypatch):
    host, run = _host(tmp_path, monkeypatch)
    try:
        held = host._control(run.run_id, "agent-synthesis")
        host._update_control(
            held, phase="waiting", work_id=None, wait_for=[],
            operator_credential_hold={"phase": "ready_model", "waiting_reason": None},
            waiting_reason="Credential paused by operator",
        )
        held = host._control(run.run_id, "agent-synthesis")
        assert not host._resume_explicit_wait(held)
        assert host._control(run.run_id, "agent-synthesis") == held

        timed = host._control(run.run_id, "agent-critique")
        host._update_control(
            timed, phase="waiting", work_id=None, wait_for=[],
            retry_at=host.store.clock() + 60,
        )
        timed = host._control(run.run_id, "agent-critique")
        assert not host._resume_explicit_wait(timed)
        assert host._control(run.run_id, "agent-critique") == timed
    finally:
        host.close()


def test_stranded_peer_uses_serialized_diagnosis_while_sibling_finishes(tmp_path, monkeypatch):
    seen = {"diagnosis": False, "sibling": False}
    saved_wait_ref = None

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        role = serialized_peer_name(packet)
        if role == "Sage":
            feedback = next(
                json.loads(item["content"])["host_feedback"]
                for item in packet["messages"]
                if item["role"] == "user"
                and '"wait_needs_peer_replan"' in item["content"]
            )
            assert feedback["status"] == "wait_needs_peer_replan"
            assert feedback["stranded_waits"][0]["control_ref"] == saved_wait_ref
            assert feedback["stranded_waits"][0]["reason"] == "no_recorded_future_wake"
            seen["diagnosis"] = True
            return tool("publish_result", {
                "title": "Qualified synthetic continuation",
                "text": "The saved wait had no future wake; this peer records a limited conclusion.",
                "citations": [], "limitations": ["No external evidence was supplied"],
            }, 1)
        seen["sibling"] = True
        return tool("publish_result", {
            "title": "Independent synthetic continuation",
            "text": "The healthy peer completed its independent bounded check.",
            "citations": [], "limitations": ["No external evidence was supplied"],
        }, 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="stranded-wait-serialized-continuation",
            question="Record two independent synthetic checks",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        affected = host._control(run.run_id, "agent-synthesis")
        host._update_control(affected, phase="waiting", work_id=None, wait_for=[],
                             waiting_reason="No saved future wake")
        affected = host._control(run.run_id, "agent-synthesis")
        saved_wait_ref = {key: affected[key] for key in ("kind", "id", "version")}
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            host._drive_run(host._run(run.run_id))
            if host._run(run.run_id)["body"]["status"] == "completed":
                break
            time.sleep(.005)
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert seen == {"diagnosis": True, "sibling": True}
        for alias in ("agent-synthesis", "agent-critique"):
            control = host._control(run.run_id, alias)
            assert control["body"]["phase"] == "done"
            assert host.store.get(**control["body"]["report_ref"])
    finally:
        host.close()


@pytest.mark.parametrize("race", ["operator_hold", "run_cancel"])
def test_diagnosis_cannot_release_changed_control_or_cancelled_run(
    tmp_path, monkeypatch, race,
):
    host, run = _host(tmp_path, monkeypatch)
    try:
        affected = host._control(run.run_id, "agent-synthesis")
        host._update_control(affected, phase="waiting", work_id=None, wait_for=[])
        affected = host._control(run.run_id, "agent-synthesis")
        original = service_module.wait_replan_notification

        def change_after_inspection(*args, **kwargs):
            notice = original(*args, **kwargs)
            assert notice is not None
            if race == "operator_hold":
                host._update_control(affected, operator_credential_hold={
                    "phase": "ready_model", "waiting_reason": None,
                })
            else:
                current_run = host._run(run.run_id)
                host._save("mission_run", current_run["id"], {
                    **current_run["body"], "status": "cancelled",
                }, mission_id=current_run["body"]["mission_id"])
            return notice

        monkeypatch.setattr(service_module, "wait_replan_notification", change_after_inspection)
        with pytest.raises(StateConflict):
            host._resume_explicit_wait(affected)
        current = host._control(run.run_id, "agent-synthesis")
        assert current["body"]["phase"] == "waiting"
        assert (current["body"].get("feedback") or {}).get("status") != "wait_needs_peer_replan"
        if race == "operator_hold":
            assert current["body"]["operator_credential_hold"]
        else:
            assert host._run(run.run_id)["body"]["status"] == "cancelled"
    finally:
        host.close()
