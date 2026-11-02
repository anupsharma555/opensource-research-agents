"""Local preparation failures retain exact, safe attempt provenance across retries."""

from __future__ import annotations

import time
import urllib.error
from io import BytesIO

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import ContextOverflow
from ora.mission.tool_catalog import TOOL_SCHEMAS
from ora.models.token_estimate import TOKENIZER_BASIS
from tests.mission.test_service import answer, drain, service


def _failed_preparation(tmp_path, monkeypatch):
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        return answer("Saved later response.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(
        request_id="preparation-diagnostic",
        question="Inspect a locally failed preparation",
        agent_ids=["agent-synthesis"],
    ))
    control = host._control(run.run_id, "agent-synthesis")
    call_id, attempt_id = host._model_ids(control, control["body"]["sequence"] + 1)
    original_dependencies = host.context.context_source_dependency_refs

    def cannot_fit(_pack):
        raise ContextOverflow("PRIVATE model input and exception detail")

    # Fail after the host has saved the call, attempt, and budget but before
    # ModelSession.prepare writes any request intent or dispatch record.
    monkeypatch.setattr(host.context, "context_source_dependency_refs", cannot_fit)
    assert host._drive_run(host._run(run.run_id))[0]
    monkeypatch.setattr(host.context, "context_source_dependency_refs", original_dependencies)
    call = host.store.get("agent_call", call_id)
    attempt = host.store.get("agent_attempt", attempt_id)
    budget = host.store.get("mission_call_budget", call_id)
    diagnostic, = host.store.records("mission_host_diagnostic")
    assert call and attempt and budget
    saved = diagnostic["body"]
    assert saved["category"] == "local_context_preparation"
    assert saved["error_class"] == "ContextOverflow"
    assert (saved["control_ref"], saved["call_ref"], saved["attempt_ref"], saved["budget_ref"]) == (
        ref(control), ref(call), ref(attempt), ref(budget)
    )
    assert "PRIVATE" not in str(saved)
    assert calls == []
    assert host.store.records("model_request_intent") == []
    assert host.store.records("model_dispatch_attempt") == []
    assert host.store.records("model_response_receipt") == []
    assert next(item for item in host.state(run.run_id).snapshot.attempts
                if item.id == attempt_id).status == "waiting"
    return host, run, attempt, diagnostic, calls


def test_context_failure_survives_repair_that_prepares_a_new_call(tmp_path, monkeypatch):
    host, run, attempt, diagnostic, calls = _failed_preparation(tmp_path, monkeypatch)
    try:
        waiting = host._control(run.run_id, "agent-synthesis")
        host._update_control(waiting, phase="ready_model", sequence=1, waiting_reason=None)
        assert host._drive_run(host._run(run.run_id))[0]
        next_call_ref = host._control(run.run_id, "agent-synthesis")["body"]["call_ref"]
        assert next_call_ref != attempt["body"]["call_ref"]
        drain(host, run.run_id)
        snapshot = host.state(run.run_id).snapshot
        old = next(item for item in snapshot.attempts if item.id == attempt["id"])
        provenance = next(item for item in snapshot.inspection.attempts
                          if item.attempt_id == attempt["id"])
        assert old.status == "superseded"
        assert provenance.explanation.startswith("Context preparation failed locally for this exact call")
        assert host.store.get("agent_attempt", attempt["id"])["body"]["status"] == "running"
        assert host.store.get("mission_host_diagnostic", diagnostic["id"]) == diagnostic
        assert len(calls) == 1
    finally:
        host.close()


def test_context_failure_remains_secondary_when_same_call_later_succeeds(tmp_path, monkeypatch):
    host, run, attempt, diagnostic, calls = _failed_preparation(tmp_path, monkeypatch)
    try:
        waiting = host._control(run.run_id, "agent-synthesis")
        host._update_control(waiting, phase="ready_model", waiting_reason=None)
        assert host._drive_run(host._run(run.run_id))[0]
        assert host._control(run.run_id, "agent-synthesis")["body"]["call_ref"] == attempt["body"]["call_ref"]
        pending_provenance = next(
            item for item in host.state(run.run_id).snapshot.inspection.attempts
            if item.attempt_id == attempt["id"]
        )
        assert not pending_provenance.explanation.startswith("Context preparation failed locally")
        assert "earlier ContextOverflow" in pending_provenance.explanation
        drain(host, run.run_id)
        snapshot = host.state(run.run_id).snapshot
        projected = next(item for item in snapshot.attempts if item.id == attempt["id"])
        provenance = next(item for item in snapshot.inspection.attempts
                          if item.attempt_id == attempt["id"])
        assert projected.status == "completed"
        assert not provenance.explanation.startswith("Context preparation failed locally")
        assert "earlier ContextOverflow" in provenance.explanation
        assert host.store.get("mission_host_diagnostic", diagnostic["id"]) == diagnostic
        assert len(calls) == 1
        assert len(host.store.records("model_response_receipt")) == 1
    finally:
        host.close()


@pytest.mark.parametrize("planning_selected", [False, True])
def test_preflight_fences_unfit_attempts_before_dispatch(tmp_path, monkeypatch, planning_selected):
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        return answer("Unexpected response.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="source-free-preflight-diagnostic",
            question="Inspect an unfit local preparation",
            agent_ids=["agent-synthesis"],
        ))

        if planning_selected:
            host._update_control(host._control(run.run_id, "agent-synthesis"),
                                 active_skill_ids=["task-planning"])

        def cannot_fit(*_args, **_kwargs):
            raise ContextOverflow("PRIVATE model input and exception detail")

        monkeypatch.setattr(host.context, "assemble", cannot_fit)
        assert host._drive_run(host._run(run.run_id))[0]
        diagnostics = host.store.records("mission_host_diagnostic")
        assert len(diagnostics) == 3
        modes = {
            (bool(b["body"].get("deferred_core_tool_ids")), b["body"]["source_turn_mode"])
            for b in host.store.records("mission_call_budget")
        }
        assert modes == {
            (False, "all_allowed_sources_fit"), (True, "all_allowed_sources_fit"),
            (True, "source_free_recovery"),
        }
        assert {item["body"]["call_ref"]["id"] for item in diagnostics} == {
            item["id"] for item in host.store.records("agent_call")
        }
        for diagnostic in diagnostics:
            saved = diagnostic["body"]
            call = host.store.get(**saved["call_ref"])
            attempt = host.store.get(**saved["attempt_ref"])
            budget = host.store.get(**saved["budget_ref"])
            assert saved["category"] == "local_context_source_free_recovery"
            assert saved["error_class"] == "ContextOverflow"
            assert call and attempt and budget
            assert attempt["body"]["status"] == "failed"
            assert call["body"]["attempt_id"] == attempt["id"]
            assert budget["body"]["call_ref"] == ref(call)
            assert "PRIVATE" not in str(saved)
            assert "preview_stage_token_estimate" not in saved
        assert calls == []
        assert host.store.records("model_request_intent") == []
        assert host.store.records("model_dispatch_attempt") == []
        assert host.store.records("model_response_receipt") == []
        assert {item.status for item in host.state(run.run_id).snapshot.attempts} == {"failed"}
    finally:
        host.close()


def test_complete_rejected_preview_saves_stage_counts_without_intent_or_dispatch(
    tmp_path, monkeypatch,
):
    def rejected(request, *, timeout):
        raise urllib.error.HTTPError(
            "https://example.invalid", 413, "too large",
            {"x-ratelimit-limit-tokens": "8000", "Retry-After": "0.01"},
            BytesIO(b'{"error":{"code":"request_too_large"}}'),
        )

    host = service(tmp_path, monkeypatch, rejected)
    try:
        run = host.start(LiveStartRequest(
            request_id="complete-preview-stage-diagnostic",
            question="Inspect synthetic source.",
            evidence_text="DOI: 10.1234/preview. " + "A" * 30000,
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        control = host._control(run.run_id, "agent-synthesis")
        work = host._work_for(control)
        spec = host.executor._spec_from(work)
        capacity_id = host.executor._capacity_id(spec)
        host.dispatch.poll(control["body"]["mission_id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            receipt = host.store.get(
                "model_response_receipt",
                f"model-response:model-intent:{spec.call_id}:{spec.call_version}",
            )
            capacity = host.capacity.get(capacity_id)
            if receipt and capacity and capacity["phase"] == "failed":
                break
            time.sleep(.01)
        assert receipt and receipt["body"]["http_status"] == 413
        assert host._repair_oversize(control, host._work_for(control), capacity)
        control = host._control(run.run_id, "agent-synthesis")
        host._update_control(
            # Explicitly selected function schemas cannot all fit this small
            # observed window, even after optional context has been packed.
            control, requested_capabilities=sorted(set(TOOL_SCHEMAS) - {"browser_search"}),
            feedback={"status": "synthetic_followup",
                      "next_action": "Review all required source handles. " * 80},
        )
        with pytest.raises(ContextOverflow):
            host._prepare_model(host._control(run.run_id, "agent-synthesis"))
        diagnostics = [item["body"] for item in host.store.records("mission_host_diagnostic")
                       if item["body"].get("reason") == "required_context_not_admitted"]
        assert len(diagnostics) == 1
        saved = diagnostics[0]
        stage = saved["preview_stage_token_estimate"]
        assert stage["preview_status"] == "complete_request_not_dispatched"
        assert stage["final_input_tokens"] == saved["complete_request_estimate_tokens"]
        assert stage["final_input_tokens"] > saved["observed_input_target_tokens"] == 5600
        assert stage["basis"] == TOKENIZER_BASIS
        assert len(stage["request_sha256"]) == 64
        assert sum(item["marginal_tokens"] for item in stage["stages"]) == stage["final_input_tokens"]
        assert "intent_ref" not in stage and "usage" not in stage
        assert "Review all required source handles." not in str(stage)
        assert len(host.store.records("model_request_intent")) == 1
        assert len(host.store.records("model_dispatch_attempt")) == 1
        failed_call = host.store.get(**saved["call_ref"])
        assert host.store.get(
            "model_request_intent", f"model-intent:{failed_call['id']}:{failed_call['version']}"
        ) is None
        assert not any(item["body"].get("call_id") == failed_call["id"]
                       for item in host.store.records("capacity_operation"))
        assert host.store.get("mission_call_budget", failed_call["id"])["body"].get(
            "request_stage_token_estimate"
        ) is None
    finally:
        host.close()


def test_context_wait_without_exact_prepared_records_has_no_attempt_diagnostic(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("unused", 1))
    try:
        run = host.start(LiveStartRequest(
            request_id="preparation-without-attempt",
            question="Keep an unattributed local wait",
            agent_ids=["agent-synthesis"],
        ))

        def cannot_fit(_control):
            raise ContextOverflow("PRIVATE model input and exception detail")

        monkeypatch.setattr(host, "_schedule_model", cannot_fit)
        assert host._drive_run(host._run(run.run_id))[0]
        assert host.store.records("mission_host_diagnostic") == []
        control = host._control(run.run_id, "agent-synthesis")
        assert control["body"]["phase"] == "waiting"
        assert control["body"]["waiting_reason"] == "ContextOverflow: this branch needs local repair"
        assert host.state(run.run_id).snapshot.attempts == []
    finally:
        host.close()
