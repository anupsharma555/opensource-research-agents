"""Local preparation failures retain exact, safe attempt provenance across retries."""

from __future__ import annotations

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import ContextOverflow
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


def test_source_free_preflight_fences_both_unfit_attempts_before_dispatch(tmp_path, monkeypatch):
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

        def cannot_fit(*_args, **_kwargs):
            raise ContextOverflow("PRIVATE model input and exception detail")

        monkeypatch.setattr(host.context, "assemble", cannot_fit)
        assert host._drive_run(host._run(run.run_id))[0]
        diagnostics = host.store.records("mission_host_diagnostic")
        assert len(diagnostics) == 2
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
        assert calls == []
        assert host.store.records("model_request_intent") == []
        assert host.store.records("model_dispatch_attempt") == []
        assert host.store.records("model_response_receipt") == []
        assert {item.status for item in host.state(run.run_id).snapshot.attempts} == {"failed"}
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
