"""A saved pre-intent overflow can prepare again without reusing stale inventory inputs."""

from __future__ import annotations

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission import service as service_module
from ora.mission.context import ContextOverflow
from ora.mission.source_context import save_source_inventory
from ora.state import StateConflict
from tests.mission.test_service import service


def test_old_source_free_preparation_reuses_call_with_fresh_inventory_operation(
    tmp_path, monkeypatch
):
    def no_provider_call(_request, *, timeout):
        pytest.fail("Preparation must not dispatch a model call")

    host = service(tmp_path, monkeypatch, no_provider_call)
    try:
        # Distinct values stay above the observed 12k complete-request bound
        # under the current tokenizer; a repeated sentence now compresses.
        evidence = "\n".join(
            f"Synthetic evidence passage {index:04d}: value {index * index:08d}."
            for index in range(1200)
        )
        run = host.start(LiveStartRequest(
            request_id="saved-source-free-overflow",
            question="Inspect the supplied synthetic source",
            evidence_text=evidence,
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        host._update_control(control, input_budget={
            "observed_limit_tokens": 12000, "target_fraction": .70,
        })
        control = host._control(run.run_id, "agent-synthesis")
        call_id, attempt_id = host._model_ids(control, control["body"]["sequence"] + 1)
        legacy_inventory_ref = save_source_inventory(
            host.store, f"mission-source-inventory:{call_id}", control_ref=ref(control)
        )
        old_packer = service_module.PACKER_VERSION
        monkeypatch.setattr(service_module, "PACKER_VERSION", "ora.working_context.packer.v21")

        def fail_after_budget(**_kwargs):
            raise ContextOverflow("Synthetic pre-intent preparation overflow")

        monkeypatch.setattr(host.session, "prepare", fail_after_budget)
        with pytest.raises(ContextOverflow, match="Synthetic pre-intent"):
            host._schedule_model(control)
        budget = host.store.get("mission_call_budget", call_id)
        assert budget["body"]["source_turn_mode"] == "source_free_recovery"
        assert budget["body"]["packer_version"] == "ora.working_context.packer.v21"
        assert host.store.get("agent_attempt", attempt_id)["body"]["status"] == "running"
        assert host.store.records("model_request_intent") == []
        assert host._record_context_preparation_failure(control)
        monkeypatch.setattr(service_module, "PACKER_VERSION", old_packer)
    finally:
        host.close()

    resumed = service(tmp_path, monkeypatch, no_provider_call)
    try:
        waiting = resumed._control(run.run_id, "agent-synthesis")
        assert resumed._recover_preparation(waiting)
        recovered = resumed._control(run.run_id, "agent-synthesis")
        assert recovered["body"]["phase"] == "ready_model"
        save_current_inventory = service_module.save_source_inventory

        def reuse_legacy_operation(store, _operation_id, *, control_ref):
            return save_current_inventory(
                store, f"mission-source-inventory:{call_id}", control_ref=control_ref
            )

        monkeypatch.setattr(service_module, "save_source_inventory", reuse_legacy_operation)
        with pytest.raises(StateConflict, match="Operation ID was already used"):
            resumed._schedule_model(recovered)
        assert resumed._control(run.run_id, "agent-synthesis") == recovered
        monkeypatch.setattr(service_module, "save_source_inventory", save_current_inventory)
        assert resumed._schedule_model(recovered)
        pending = resumed._control(run.run_id, "agent-synthesis")
        assert pending["body"]["phase"] == "pending_model"
        assert pending["body"]["call_ref"]["id"] == call_id
        assert resumed.store.get("model_request_intent", f"model-intent:{call_id}:1")
        latest_budget = resumed.store.get("mission_call_budget", call_id)
        assert latest_budget["body"]["packer_version"] == old_packer
        assert latest_budget["body"]["source_turn_mode"] == "source_free_recovery"
        current_inventory = resumed.store.get(**latest_budget["body"]["source_inventory_ref"])
        assert current_inventory["body"]["control_ref"] == ref(recovered)
        assert current_inventory["id"] != legacy_inventory_ref["id"]
        assert resumed.store.get(**legacy_inventory_ref)["body"]["control_ref"] == ref(control)
    finally:
        resumed.close()


def test_changed_operation_inputs_waits_locally_instead_of_spinning(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda _request, *, timeout: pytest.fail(
        "A local preparation conflict must not dispatch a model call"
    ))
    try:
        run = host.start(LiveStartRequest(
            request_id="saved-operation-conflict",
            question="Keep the local preparation conflict visible",
            agent_ids=["agent-synthesis"],
        ))

        def changed_operation(_control):
            raise StateConflict("Operation ID was already used with different inputs")

        monkeypatch.setattr(host, "_schedule_model", changed_operation)
        changed, _ = host._drive_run(host._run(run.run_id))
        assert changed
        control = host._control(run.run_id, "agent-synthesis")
        assert control["body"]["phase"] == "waiting"
        assert control["body"]["waiting_reason"] == (
            "StateConflict: saved operation ID has changed inputs; "
            "this branch needs local repair"
        )
        changed, _ = host._drive_run(host._run(run.run_id))
        assert not changed
        assert host._control(run.run_id, "agent-synthesis") == control
        assert host.store.records("model_request_intent") == []
    finally:
        host.close()
