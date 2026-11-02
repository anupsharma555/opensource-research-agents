"""A lost model response may continue without replaying an uncertain tool."""

from __future__ import annotations

import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.executor import WorkResult
from ora.mission.model_recovery import ModelRecovery
from ora.state import StateConflict, StateError
from tests.mission.test_service import answer, service, tool


def _drive_until(host, run_id, condition, *, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("Controlled executor state did not arrive")


def _old_lease(host, mission_id, task_id):
    db = host.store._connect()
    try:
        row = db.execute(
            "SELECT mission_id,task_id,owner,generation,expires_at "
            "FROM leases WHERE mission_id=? AND task_id=?",
            (mission_id, task_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


@pytest.mark.parametrize("qwen", [False, True])
def test_model_response_loss_fences_old_call_and_accepts_fresh_linked_turn(
    tmp_path,
    monkeypatch,
    qwen,
):
    transport_calls = []
    behavior = {"lose": True}

    def transport(request, *, timeout):
        transport_calls.append(request)
        if behavior["lose"]:
            raise RuntimeError("Synthetic response lost after dispatch")
        return answer("Fresh linked answer after local response loss.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        receipt = host.start(
            LiveStartRequest(
                request_id="model-response-lost-recovery",
                question="Give an independent appraisal",
                agent_ids=["agent-synthesis"],
                agent_models=({"agent-synthesis": {
                    "model": "qwen/qwen3.8-27b", "reasoning_effort": "medium",
                    "max_completion_tokens": 960,
                }} if qwen else None),
            )
        )
        run = host._run(receipt.run_id)
        mission_id = run["body"]["mission_id"]
        _drive_until(
            host,
            receipt.run_id,
            lambda: any(
                item["body"]["phase"] == "uncertain"
                for item in host.store.records("capacity_operation")
            ),
        )
        control = host._control(receipt.run_id, "agent-synthesis")
        work = host._work_for(control)
        old_spec = host.executor._spec_from(work)
        old_cap_id = host.executor._capacity_id(old_spec)
        old_cap = host.store.get("capacity_operation", old_cap_id)
        old_lease = _old_lease(host, mission_id, old_spec.proposal_ref["id"])
        old_call_ref = control["body"]["call_ref"]
        old_intent_ref = {
            "kind": "model_request_intent",
            "id": f"model-intent:{old_spec.call_id}:{old_spec.call_version}",
            "version": 1,
        }
        prepared = host.session.load(old_intent_ref)
        if qwen:
            assert prepared.request.body["reasoning_format"] == "parsed"
        assert all(item["type"] == "function" for item in prepared.request.body["tools"])
        assert prepared.request.body["parallel_tool_calls"] is False
        assert host.store.records("model_response_receipt") == []

        host.close()
        host = service(tmp_path, monkeypatch, transport)
        assert len(transport_calls) == 1  # Restart does not resend the old intent.
        recovery = ModelRecovery(host.store, host.executor, host.session)
        result = recovery.supersede_unknown_response(
            "supersede-lost-model-response",
            control_ref={
                "kind": control["kind"],
                "id": control["id"],
                "version": control["version"],
            },
            work_ref={"kind": work["kind"], "id": work["id"], "version": work["version"]},
        )
        assert result["old_call_ref"] == old_call_ref
        assert result["old_intent_ref"] == old_intent_ref
        config = host.store.get(**result["pinned_configuration_ref"])["body"]
        assert result["pinned_model_id"] == config["model_id"]
        assert result["pinned_settings"] == config["settings"]
        assert result["pinned_reasoning_settings"] == config["reasoning_settings"]
        assert result["retry_at"] > host.store.clock()
        assert (
            recovery.supersede_unknown_response(
                "supersede-lost-model-response",
                control_ref={
                    "kind": control["kind"],
                    "id": control["id"],
                    "version": control["version"],
                },
                work_ref={"kind": work["kind"], "id": work["id"], "version": work["version"]},
            )
            == result
        )

        after_cap = host.store.get("capacity_operation", old_cap_id)["body"]
        assert after_cap["phase"] != "uncertain"
        assert after_cap["reservation"] is False
        assert after_cap["attempts"][-1]["usage_known"] is False
        assert (
            after_cap["attempts"][-1]["charged_tokens"]
            == old_cap["body"]["attempts"][-1]["charged_tokens"]
        )
        new_lease = _old_lease(host, mission_id, old_spec.proposal_ref["id"])
        assert new_lease["generation"] > old_lease["generation"]
        assert new_lease["expires_at"] <= host.store.clock()
        assert (
            host.store.get(**old_intent_ref)["body"]["request_sha256"]
            == prepared.request.request_sha256
        )
        assert (
            host.store.get("model_dispatch_attempt", f"model-dispatch:{old_intent_ref['id']}")[
                "body"
            ]["status"]
            == "attempted_external_effect"
        )
        if qwen:
            # The Qwen branch covers parsed-packet recovery and reservation.
            # The synthetic final answer below exercises the GPT-OSS fixture.
            return

        # The service applies the accepted receipt before starting a different
        # call.  The test mirrors that handoff without editing service.py.
        current = host._control(receipt.run_id, "agent-synthesis")
        host._update_control(
            current,
            phase="ready_model",
            work_id=None,
            waiting_reason=None,
            model_response_lost_ref=result["recovery_ref"],
            feedback={"model_response_lost_ref": result["recovery_ref"]},
        )
        behavior["lose"] = False
        fresh = host._control(receipt.run_id, "agent-synthesis")
        assert host._schedule_model(fresh)
        new_call_ref = host._control(receipt.run_id, "agent-synthesis")["body"]["call_ref"]
        assert new_call_ref != old_call_ref
        new_intent = host.store.get(
            "model_request_intent", f"model-intent:{new_call_ref['id']}:{new_call_ref['version']}"
        )
        assert new_intent["body"]["parent_call_ref"] is None
        new_budget = host.store.get("mission_call_budget", new_call_ref["id"])
        assert new_budget["body"]["model_response_lost_ref"] == result["recovery_ref"]
        assert host.store.get(**result["recovery_ref"])["body"]["call_ref"] == old_call_ref
        assert host.store.get(**result["recovery_ref"])["body"]["intent_ref"] == old_intent_ref
        _drive_until(
            host,
            receipt.run_id,
            lambda: host._run(receipt.run_id)["body"]["status"] == "completed",
        )
        assert len(transport_calls) == 2
        assert host.store.records("report")

        # A response from the original call arrives after the linked call has
        # completed. Its tool request is only an unaccepted observation.
        late = host.session.adapter.decode(
            prepared.request,
            tool(
                "publish_result",
                {
                    "title": "Late old answer",
                    "text": "Must never publish",
                    "citations": [],
                    "limitations": ["Synthetic fixture"],
                },
                99,
            ).payload,
        )
        late_ref = host.session.capture(old_intent_ref, late)
        old_result = WorkResult(
            disposition="continue_task",
            output_refs=(late_ref,),
            payload={"response_ref": late_ref, "status": "tool_requested"},
            used_tokens=late.usage.get("total_tokens"),
        )
        try:
            accepted = host.executor._accept(old_spec, old_cap["body"], old_lease, old_result)
        except StateConflict:
            accepted = {"status": "rejected"}
        assert accepted["status"] != "accepted"
        observed = host.executor._observe_or_unknown(
            old_spec, old_cap["body"]["generation"], old_lease, old_result, "board_rejected"
        )
        assert observed["status"] == "result_unaccepted"
        assert observed["capacity_reconciled"] is False
        assert host.store.get(**result["recovery_ref"])["body"]["work_ref"] == {
            "kind": work["kind"],
            "id": work["id"],
            "version": work["version"],
        }
        assert (
            host.store.get("capacity_operation", old_cap_id)["body"]["phase"]
            == "model_response_lost"
        )
        assert host.store.get("capacity_operation", old_cap_id)["body"]["reservation"] is False
        assert (
            recovery.supersede_unknown_response(
                "supersede-lost-model-response",
                control_ref={
                    "kind": control["kind"],
                    "id": control["id"],
                    "version": control["version"],
                },
                work_ref={"kind": work["kind"], "id": work["id"], "version": work["version"]},
            )
            == result
        )
        assert not any(
            item["body"]["spec"]["call_id"] == old_spec.call_id
            and item["body"]["spec"]["kind"] != "model"
            for item in host.store.records("executor_work")
        )
        assert not any(
            item["body"].get("work_id") == old_spec.work_id
            for item in host.store.records("executor_artifact")
        )
    finally:
        host.close()


def test_restart_applies_committed_recovery_before_a_fresh_call(tmp_path, monkeypatch):
    calls = []
    lose = {"response": True}

    def transport(request, *, timeout):
        calls.append(request)
        if lose["response"]:
            raise RuntimeError("Synthetic response lost after dispatch")
        return answer("A fresh answer after the saved recovery handoff.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    try:
        receipt = host.start(
            LiveStartRequest(
                request_id="model-recovery-crash-gap",
                question="Give a bounded appraisal",
                agent_ids=["agent-synthesis"],
            )
        )
        _drive_until(
            host,
            receipt.run_id,
            lambda: any(
                item["body"]["phase"] == "uncertain"
                for item in host.store.records("capacity_operation")
            ),
        )
        control = host._control(receipt.run_id, "agent-synthesis")
        old_call_ref = control["body"]["call_ref"]
        work = host._work_for(control)
        old_spec = host.executor._spec_from(work)
        old_cap = host.store.get("capacity_operation", host.executor._capacity_id(old_spec))
        old_lease = _old_lease(host, old_spec.mission_id, old_spec.proposal_ref["id"])
        old_intent_ref = {
            "kind": "model_request_intent",
            "id": f"model-intent:{old_spec.call_id}:{old_spec.call_version}",
            "version": 1,
        }
        prepared = host.session.load(old_intent_ref)
        result = host.model_recovery.supersede_unknown_response(
            "model-recovery-before-control-crash",
            control_ref={key: control[key] for key in ("kind", "id", "version")},
            work_ref={key: work[key] for key in ("kind", "id", "version")},
        )
        assert (
            host._control(receipt.run_id, "agent-synthesis")["body"]["work_id"]
            == work["body"]["spec"]["work_id"]
        )
        late = host.session.adapter.decode(
            prepared.request,
            tool(
                "publish_result",
                {
                    "title": "Late unseen output",
                    "text": "Must never publish",
                    "citations": [],
                    "limitations": ["Synthetic fixture"],
                },
                99,
            ).payload,
        )
        late_ref = host.session.capture(old_intent_ref, late)
        observed = host.executor._observe_or_unknown(
            old_spec,
            old_cap["body"]["generation"],
            old_lease,
            WorkResult(
                disposition="continue_task",
                output_refs=(late_ref,),
                payload={"response_ref": late_ref, "status": "tool_requested"},
                used_tokens=late.usage.get("total_tokens"),
            ),
            "board_rejected",
        )
        assert observed["status"] == "result_unaccepted"
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "result_unaccepted"
        host.close()
        host = service(tmp_path, monkeypatch, transport)
        _drive_until(
            host,
            receipt.run_id,
            lambda: host._control(receipt.run_id, "agent-synthesis")["body"].get(
                "model_response_lost_ref"
            )
            == result["recovery_ref"],
        )
        assert len(calls) == 1
        assert host.store.records("report") == []
        assert host._control(receipt.run_id, "agent-synthesis")["body"]["work_id"] is None
        lose["response"] = False
        _drive_until(
            host,
            receipt.run_id,
            lambda: host._run(receipt.run_id)["body"]["status"] == "completed",
            timeout=8,
        )
        assert len(calls) == 2
        fresh_call = host._control(receipt.run_id, "agent-synthesis")["body"]["call_ref"]
        assert fresh_call != old_call_ref
        intent = host.store.get(
            "model_request_intent", f"model-intent:{fresh_call['id']}:{fresh_call['version']}"
        )
        assert intent["body"]["parent_call_ref"] is None
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "result_unaccepted"
    finally:
        host.close()


def test_supervisor_supersedes_uncertain_model_call_without_replay(tmp_path, monkeypatch):
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise RuntimeError("Synthetic first response lost")
        return answer("A fresh response completes the branch.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    try:
        receipt = host.start(
            LiveStartRequest(
                request_id="model-recovery-automatic",
                question="Provide a bounded answer",
                agent_ids=["agent-synthesis"],
            )
        )
        _drive_until(
            host,
            receipt.run_id,
            lambda: host._run(receipt.run_id)["body"]["status"] == "completed",
            timeout=8,
        )
        assert len(calls) == 2
        losses = host.store.records("model_response_lost")
        assert len(losses) == 1
        assert losses[0]["body"]["remote_response"] == "unknown"
        assert host.store.records("model_dispatch_attempt")
    finally:
        host.close()


def test_uncertain_side_effecting_tool_cannot_use_model_recovery(tmp_path, monkeypatch):
    def transport(request, *, timeout):
        return tool(
            "publish_result",
            {
                "title": "Synthetic result",
                "text": "Needs local effect",
                "citations": [],
                "limitations": ["Synthetic fixture"],
            },
            1,
        )

    host = service(tmp_path, monkeypatch, transport)

    def lost_tool(_spec):
        raise RuntimeError("Synthetic uncertain tool effect")

    host.executor.handlers[("mission-tool", "1")] = lost_tool
    try:
        receipt = host.start(
            LiveStartRequest(
                request_id="uncertain-tool-remains-fenced",
                question="Publish a result",
                agent_ids=["agent-synthesis"],
            )
        )
        _drive_until(
            host,
            receipt.run_id,
            lambda: any(
                item["body"]["phase"] == "uncertain" and item["body"]["kind"] == "compute"
                for item in host.store.records("capacity_operation")
            ),
        )
        control = host._control(receipt.run_id, "agent-synthesis")
        work = host._work_for(control)
        assert host.executor._spec_from(work).kind == "compute"
        recovery = ModelRecovery(host.store, host.executor, host.session)
        with pytest.raises(StateError, match="unresolved model work"):
            recovery.supersede_unknown_response(
                "must-not-supersede-tool",
                control_ref={
                    "kind": control["kind"],
                    "id": control["id"],
                    "version": control["version"],
                },
                work_ref={"kind": work["kind"], "id": work["id"], "version": work["version"]},
            )
        assert not host.store.records("model_response_lost")
        cap = host.capacity.get(host.executor._capacity_id(host.executor._spec_from(work)))
        assert cap["phase"] == "uncertain" and cap["reservation"] is True
    finally:
        host.close()
