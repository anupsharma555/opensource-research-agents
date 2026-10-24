"""A local evaluation cap is a confirmed no-effect model dispatch."""

from __future__ import annotations

import pytest

from ora.mission.evaluation_settlement import unsettled_peer_intents
from ora.retrieval.agent_validation_budget import AgentBudget, BudgetStop
from tests.mission.test_peer_queued_freshness import _start_queued
from tests.mission.test_service import service


def _claimed_model(host):
    run, pending, intent, _work, spec, cap_id = _start_queued(host, "budget-admission-recovery")
    owner = host.executor.owner
    admitted = host.capacity.admit(
        owner, "model", mission_id=pending["body"]["mission_id"],
        eligible_operation_ids=frozenset({cap_id}),
    )
    assert admitted and admitted["operation_id"] == cap_id
    host.capacity.dispatched(cap_id, owner, admitted["generation"])
    return intent, spec, cap_id, owner, admitted["generation"]


def test_budget_refusal_settles_zero_without_provider_attempt(tmp_path, monkeypatch):
    opened = []

    def original_open(_opener, _request, **_kwargs):
        opened.append(True)
        raise AssertionError("The local cap should block HTTP")

    with AgentBudget(tmp_path / "budget.json", token_limit=10) as budget:
        host = service(
            tmp_path / "state", monkeypatch,
            lambda request, *, timeout: budget.open(
                original_open, object(), request, timeout=timeout,
            ),
        )
        try:
            intent, spec, cap_id, owner, generation = _claimed_model(host)
            result = host._model_work(spec)
            assert result.disposition == "terminal_no_effect"
            assert result.used_tokens == 0
            assert result.payload["external_action"] == "none"
            settled = host.capacity.transport_failure(
                cap_id, owner, generation, classification=result.disposition,
            )
            assert settled["phase"] == "failed"
            assert settled["attempts"][-1]["status"] == "terminal_no_effect"
            assert settled["attempts"][-1]["charged_tokens"] == 0
            assert settled["attempts"][-1]["usage_known"] is True
            marker = host.store.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}")
            assert marker["body"]["status"] == "blocked_before_external_effect"
            assert host.store.records("model_response_receipt") == []
            assert budget.data["model_attempts"] == []
            assert opened == []
            active, unsafe = unsettled_peer_intents(host.store, {spec.actor_id})
            assert [item["id"] for item in active] == [intent["id"]]
            assert unsafe == []
            control = host._control(spec.run_id, "agent-synthesis")
            assert host._supersede_blocked_model(control)
            retired = host.store.get("executor_work", host.executor._work_record_id(spec))
            assert retired["body"]["phase"] == "superseded"
            assert retired["body"]["superseded_intent_ref"] == {
                "kind": "model_request_intent", "id": intent["id"], "version": intent["version"],
            }
            assert unsettled_peer_intents(host.store, {spec.actor_id}) == ([], [])
        finally:
            host.close()


def test_budget_uncertain_transport_retains_external_reservation(tmp_path, monkeypatch):
    opened = []

    def original_open(_opener, _request, **_kwargs):
        opened.append(True)
        raise TimeoutError("synthetic opening uncertainty")

    with AgentBudget(tmp_path / "budget.json", token_limit=55_000) as budget:
        host = service(
            tmp_path / "state", monkeypatch,
            lambda request, *, timeout: budget.open(
                original_open, object(), request, timeout=timeout,
            ),
        )
        try:
            intent, spec, cap_id, owner, generation = _claimed_model(host)
            with pytest.raises(BudgetStop, match="uncertain_transport"):
                host._model_work(spec)
            marker = host.store.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}")
            assert marker["body"]["status"] == "attempted_external_effect"
            uncertain = host.capacity.timeout(cap_id, owner, generation)
            assert uncertain["phase"] == "uncertain"
            assert uncertain["reservation"] is True
            assert uncertain["external_active"] is True
            assert budget.data["model_attempts"][0]["state"] == "uncertain"
            assert opened == [True]
        finally:
            host.close()


def test_restart_settles_blocked_marker_without_replaying_model(tmp_path, monkeypatch):
    opened = []

    def original_open(_opener, _request, **_kwargs):
        opened.append(True)
        raise AssertionError("The blocked model request must not reach HTTP")

    state = tmp_path / "state"
    with AgentBudget(tmp_path / "budget.json", token_limit=10) as budget:
        host = service(
            state, monkeypatch,
            lambda request, *, timeout: budget.open(
                original_open, object(), request, timeout=timeout,
            ),
        )
        try:
            intent, spec, cap_id, owner, generation = _claimed_model(host)
            assert host._model_work(spec).disposition == "terminal_no_effect"
            # Simulate a process loss before the executor settles its callback.
            assert host.capacity.timeout(cap_id, owner, generation)["phase"] == "uncertain"
        finally:
            host.close()

    resumed = service(
        state, monkeypatch,
        lambda *_args, **_kwargs: pytest.fail("Restart replayed a blocked model request"),
    )
    try:
        active, unsafe = unsettled_peer_intents(resumed.store, {spec.actor_id})
        assert [item["id"] for item in active] == [intent["id"]]
        assert unsafe == []
        resumed._schedule_model = lambda *_args, **_kwargs: False
        assert resumed._drive_run(resumed._run(spec.run_id))[0]
        settled = resumed.capacity.get(cap_id)
        assert settled["phase"] == "failed"
        assert settled["attempts"][-1]["status"] == "terminal_no_effect"
        assert settled["attempts"][-1]["charged_tokens"] == 0
        assert settled["attempts"][-1]["usage_known"] is True
        assert resumed.store.get("executor_work", resumed.executor._work_record_id(spec))[
            "body"]["phase"] == "superseded"
        assert resumed._control(spec.run_id, "agent-synthesis")["body"]["work_id"] is None
        assert unsettled_peer_intents(resumed.store, {spec.actor_id}) == ([], [])
        assert opened == []
    finally:
        resumed.close()
