"""A task-scoped message arriving after model input must precede publication."""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import answer, service, tool

OLD = "Synthetic archive draft: seven sealed boxes were counted on 2 October."
NEW = ("Synthetic later pasted correction: three boxes, not seven; "
       "four boxes may have been transferred, but no recount document was supplied.")


def drive_until(host, run_id, predicate, *, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Expected saved mission boundary was not reached")


def enable_synthetic_transport(host):
    # The isolated fake transport must not depend on the operator's current
    # private live-profile selection; this does not edit that selection.
    host.credential_profiles.is_run_enabled = lambda _profile, _run: True
    host.session.adapter.credential_profiles.is_enabled = lambda _profile: True


def stage_message(host, run_id, *, task_scoped=True):
    control = host._control(run_id, "agent-synthesis")
    task = host.store.get("board_proposal", control["body"]["task_id"])
    sent = host.mailbox.send_message(
        "freshness-later-task-message", mission_id=control["body"]["mission_id"],
        message_id="freshness-later-message", sender_id=control["body"]["actor_id"],
        recipient_id=control["body"]["actor_id"],
        correlation_id="freshness-later-correction",
        payload={"type": "simulated_user_text", "text": NEW},
        task_ref=ref(task) if task_scoped else None, evidence_refs=[],
    )
    assert sent["status"] == "accepted"
    return sent["message_ref"]


def start_and_hold(tmp_path, monkeypatch, transport, *, hold):
    host = service(tmp_path, monkeypatch, transport)
    enable_synthetic_transport(host)
    host.executor.lease_seconds = 0.2
    boundary = {}
    if hold == "before_model_advance":
        original = host._advance_model
        def held(control, work):
            if work["body"]["phase"] == "completed":
                boundary["work"] = work["id"]
                return False
            return original(control, work)
        host._advance_model = held
    elif hold == "before_tool_work":
        host._schedule_tool = lambda *_args, **_kwargs: False
    else:
        raise AssertionError(hold)
    run = host.start(LiveStartRequest(
        request_id=f"post-response-{hold}-{tmp_path.name}",
        question="Compare the supplied draft with any later task correction.",
        scope="Use only saved synthetic material. Qualify an unverified pasted note.",
        evidence_text=OLD, agent_ids=["agent-synthesis"],
        required_agent_ids=["agent-synthesis"],
    ))
    predicate = (
        (lambda: bool(boundary)) if hold == "before_model_advance" else
        (lambda: host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_tool")
    )
    drive_until(host, run.run_id, predicate)
    assert len(host.store.records("model_response_receipt")) == 1
    assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] != "done"
    host.close()
    return run.run_id


@pytest.mark.parametrize("first_kind,hold", [
    ("direct", "before_model_advance"),
    ("tool", "before_model_advance"),
    ("tool", "before_tool_work"),
])
def test_new_task_message_after_response_gets_fresh_turn(
    tmp_path, monkeypatch, first_kind, hold,
):
    packets = []
    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            assert NEW not in json.dumps(packet)
            if first_kind == "tool":
                return tool("publish_result", {
                    "title": "Draft archive count", "text": "Seven boxes in Room A.",
                    "citations": [], "limitations": [],
                }, 1)
            return answer("The synthetic draft records seven boxes in Room A.", 1)
        assert NEW in json.dumps(packet)
        return answer("The draft records seven boxes; a later unverified pasted note "
                      "asserts three, but its recount document was not supplied.", 2)

    run_id = start_and_hold(tmp_path, monkeypatch, transport, hold=hold)
    resumed = service(tmp_path, monkeypatch, transport)
    enable_synthetic_transport(resumed)
    try:
        message_ref = stage_message(resumed, run_id)
        drive_until(resumed, run_id, lambda: resumed._run(run_id)["body"]["status"] == "completed")
        assert len(packets) == 2
        assert message_ref["id"] in json.dumps(packets[1])
        assert len(resumed.store.records("model_response_receipt")) == 2
        control = resumed._control(run_id, "agent-synthesis")
        final = resumed.store.get(**control["body"]["report_ref"])
        assert "three" in final["body"]["body"]
        assert "unverified" in final["body"]["body"]
        if first_kind == "tool":
            results = resumed.store.records("context_tool_result")
            assert len(results) == 1
            payload = json.loads(resumed.store.read_blob(results[0]["body"]["blob"]["sha256"]))
            assert payload["tool_call_id"] == "tool-1"
            assert payload["name"] == "publish_result"
            assert json.loads(payload["content"])["reason"] == "new_task_message_after_model_input"
            assert len(resumed.store.records("report")) == 1
    finally:
        resumed.close()


def test_unscoped_message_does_not_refresh_completed_direct_response(tmp_path, monkeypatch):
    packets = []
    def transport(request, *, timeout):
        packets.append(json.loads(request.data))
        return answer("The synthetic draft records seven boxes in Room A.", len(packets))

    run_id = start_and_hold(tmp_path, monkeypatch, transport, hold="before_model_advance")
    resumed = service(tmp_path, monkeypatch, transport)
    enable_synthetic_transport(resumed)
    try:
        stage_message(resumed, run_id, task_scoped=False)
        drive_until(resumed, run_id, lambda: resumed._run(run_id)["body"]["status"] == "completed")
        assert len(packets) == 1
        assert len(resumed.store.records("model_response_receipt")) == 1
    finally:
        resumed.close()


def test_message_between_publish_callback_and_acceptance_needs_fresh_turn(tmp_path, monkeypatch):
    packets = []
    def transport(request, *, timeout):
        packets.append(json.loads(request.data))
        if len(packets) == 1:
            return tool("publish_result", {
                "title": "Draft archive count", "text": "Seven boxes in Room A.",
                "citations": [], "limitations": [],
            }, 1)
        assert NEW in json.dumps(packets[-1])
        return answer("The draft records seven; a later unverified note asserts "
                      "three without a supplied recount document.", 2)

    run_id = start_and_hold(tmp_path, monkeypatch, transport, hold="before_tool_work")
    resumed = service(tmp_path, monkeypatch, transport)
    enable_synthetic_transport(resumed)
    original_accept = resumed.executor._accept
    staged = {}
    def stage_before_accept(spec, cap, fence, result, **kwargs):
        if (spec.handler_id == "mission-tool"
                and any(pointer["kind"] == "report" for pointer in result.output_refs)
                and not staged):
            staged["message_ref"] = stage_message(resumed, run_id)
        return original_accept(spec, cap, fence, result, **kwargs)
    resumed.executor._accept = stage_before_accept
    try:
        drive_until(resumed, run_id, lambda: resumed._run(run_id)["body"]["status"] == "completed")
        assert staged
        assert len(packets) == 2
        assert staged["message_ref"]["id"] in json.dumps(packets[1])
        report = resumed.store.get(**resumed._control(run_id, "agent-synthesis")["body"]["report_ref"])
        assert "three" in report["body"]["body"]
    finally:
        resumed.close()


def test_older_saved_completed_publish_result_advances_without_republication(
    tmp_path, monkeypatch,
):
    packets = []
    def transport(request, *, timeout):
        packets.append(json.loads(request.data))
        return tool("publish_result", {
            "title": "Draft archive count", "text": "Seven boxes in Room A.",
            "citations": [], "limitations": [],
        }, len(packets))

    host = service(tmp_path, monkeypatch, transport)
    enable_synthetic_transport(host)
    host.executor.lease_seconds = 0.2
    prior_handler = host.executor.handlers[("mission-tool", "1")]
    def legacy_handler(spec):
        result = prior_handler(spec)
        return replace(result, disposition="completed") if any(
            pointer["kind"] == "report" for pointer in result.output_refs
        ) else result
    host.executor.handlers[("mission-tool", "1")] = legacy_handler
    host._advance_tool = lambda *_args, **_kwargs: False
    run = host.start(LiveStartRequest(
        request_id="older-saved-publish-before-control-advance",
        question="Summarize the synthetic draft.", evidence_text=OLD,
        agent_ids=["agent-synthesis"], required_agent_ids=["agent-synthesis"],
    ))
    drive_until(host, run.run_id, lambda: (
        host.store.get("board_proposal", host._control(
            run.run_id, "agent-synthesis")["body"]["task_id"])["body"]["status"]
        == "completed"
    ))
    assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] != "done"
    host.close()

    resumed = service(tmp_path, monkeypatch, transport)
    enable_synthetic_transport(resumed)
    try:
        drive_until(resumed, run.run_id,
                    lambda: resumed._run(run.run_id)["body"]["status"] == "completed")
        assert len(packets) == 1
        assert resumed._control(run.run_id, "agent-synthesis")["body"]["phase"] == "done"
        assert len(resumed.store.records("report")) == 1
        assert len(resumed.store.records("board_outcome")) == 2
    finally:
        resumed.close()


@pytest.mark.parametrize("renew_scope", [False, True])
def test_late_message_invalidates_only_old_scope_disposition(
    tmp_path, monkeypatch, renew_scope,
):
    packets = []
    def transport(request, *, timeout):
        packets.append(json.loads(request.data))
        step = len(packets)
        if step == 1:
            return tool("publish_result", {
                "title": "Old draft", "text": "Seven boxes in Room A.",
                "citations": [], "limitations": [],
                "scope_completion": {"reason": "Old draft scope", "decisions": []},
            }, step)
        assert step == 2
        assert NEW in json.dumps(packets[-1])
        if renew_scope:
            return tool("publish_result", {
                "title": "Revised note",
                "text": "Draft seven; later unverified note asserts three.",
                "citations": [], "limitations": ["Recount document unavailable"],
                "scope_completion": {"reason": "Updated after the later note", "decisions": []},
            }, step)
        return answer("The draft says seven; a later unverified note asserts "
                      "three without a supplied recount document.", step)

    def hold_optional(host):
        original = host._schedule_model
        host._schedule_model = lambda control, **kwargs: (
            False if control["body"]["alias"] == "agent-critique"
            else original(control, **kwargs)
        )

    host = service(tmp_path, monkeypatch, transport)
    enable_synthetic_transport(host)
    host.executor.lease_seconds = 0.2
    hold_optional(host)
    host._schedule_tool = lambda *_args, **_kwargs: False
    run = host.start(LiveStartRequest(
        request_id=f"scope-after-late-message-{renew_scope}",
        question="Summarize the synthetic draft and optional review scope.",
        evidence_text=OLD,
        agent_ids=["agent-synthesis", "agent-critique"],
        required_agent_ids=["agent-synthesis"],
    ))
    drive_until(host, run.run_id, lambda: host._control(
        run.run_id, "agent-synthesis")["body"]["phase"] == "ready_tool")
    host.close()

    resumed = service(tmp_path, monkeypatch, transport)
    enable_synthetic_transport(resumed)
    hold_optional(resumed)
    original_accept = resumed.executor._accept
    staged = {}
    def stage_before_accept(spec, cap, fence, result, **kwargs):
        if (spec.handler_id == "mission-tool"
                and any(pointer["kind"] == "report" for pointer in result.output_refs)
                and not staged):
            staged["message_ref"] = stage_message(resumed, run.run_id)
        return original_accept(spec, cap, fence, result, **kwargs)
    resumed.executor._accept = stage_before_accept
    try:
        drive_until(resumed, run.run_id, lambda: resumed._control(
            run.run_id, "agent-synthesis")["body"]["phase"] == "done")
        assert staged and len(packets) == 2
        dispositions = resumed.store.records("mission_scope_disposition")
        assert len(dispositions) == (2 if renew_scope else 1)
        old = next(item for item in dispositions if item["body"]["reason"] == "Old draft scope")
        assert resumed._control(run.run_id, "agent-critique")["body"]["phase"] != "done"
        if renew_scope:
            fresh = next(item for item in dispositions
                         if item["body"]["reason"] == "Updated after the later note")
            drive_until(resumed, run.run_id,
                        lambda: resumed._run(run.run_id)["body"]["status"] == "completed")
            assert resumed._run(run.run_id)["body"]["scope_disposition_ref"] == ref(fresh)
        else:
            assert not resumed.work.disposition_is_current(ref(old))
            for _ in range(3):
                resumed._drive_run(resumed._run(run.run_id))
            assert resumed._run(run.run_id)["body"]["status"] == "active"
    finally:
        resumed.close()


def test_scope_closure_rechecks_message_inside_final_transaction(tmp_path, monkeypatch):
    packets = []
    def transport(request, *, timeout):
        packets.append(json.loads(request.data))
        return answer("The synthetic draft records seven boxes.", len(packets))

    host = service(tmp_path, monkeypatch, transport)
    enable_synthetic_transport(host)
    host.executor.lease_seconds = 0.2
    schedule = host._schedule_model
    host._schedule_model = lambda control, **kwargs: (
        False if control["body"]["alias"] == "agent-critique"
        else schedule(control, **kwargs)
    )
    advance = host._advance_model
    boundary = {}
    def hold_before_advance(control, work):
        if work["body"]["phase"] == "completed":
            boundary["work_id"] = work["id"]
            return False
        return advance(control, work)
    host._advance_model = hold_before_advance
    complete = host._maybe_complete
    host._maybe_complete = lambda _run: False
    run = host.start(LiveStartRequest(
        request_id="scope-final-transaction-message-race",
        question="Summarize the supplied draft.", evidence_text=OLD,
        agent_ids=["agent-synthesis", "agent-critique"],
        required_agent_ids=["agent-synthesis"],
    ))
    try:
        drive_until(host, run.run_id, lambda: bool(boundary))
        control = host._control(run.run_id, "agent-synthesis")
        saved = host.work.record_scope_disposition(
            "preclosure-scope-proposal", run_ref=ref(host._run(run.run_id)),
            actor_id=control["body"]["actor_id"], source_control_ref=ref(control),
            decisions=[], reason="Optional work can be handled in another scope",
        )
        assert host.work.disposition_is_current(saved["disposition_ref"])
        host._advance_model = advance
        drive_until(host, run.run_id, lambda: host._control(
            run.run_id, "agent-synthesis")["body"]["phase"] == "done")
        assert host._run(run.run_id)["body"]["status"] == "active"
        original_transact = host.store.transact
        injected = {}
        def inject_before_commit(operation_id, request, action, **kwargs):
            if operation_id.startswith("mission-complete:") and not injected:
                injected["message_ref"] = stage_message(host, run.run_id)
            return original_transact(operation_id, request, action, **kwargs)
        host.store.transact = inject_before_commit
        assert complete(host._run(run.run_id)) is False
        assert injected and host._run(run.run_id)["body"]["status"] == "active"
        assert not host.work.disposition_is_current(saved["disposition_ref"])
        assert host._control(run.run_id, "agent-critique")["body"]["phase"] != "deferred"
        assert len(packets) == 1
    finally:
        host.close()
