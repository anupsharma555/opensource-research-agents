"""Offline concurrent callbacks, exact acceptance and durable recovery."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from ora.agents import AgentRegistry
from ora.coordination.board import ProposalBoard, ref
from ora.coordination.capacity import CapacityPolicy, CapacityScheduler, QuotaLimit
from ora.coordination.executor import ConcurrentExecutor, WorkResult, WorkSpec
from ora.coordination.mailbox import PeerMailbox
from ora.coordination.traces import TraceJournal
from ora.state import StateStore
from ora.state.store import StateConflict

ROOT = Path(__file__).resolve().parents[2]


def foundation(tmp_path, *, model=2, retrieval=1, handlers=None, quotas=None, clock=None,
               lease_seconds=60):
    store = StateStore(tmp_path, clock=clock) if clock else StateStore(tmp_path)
    registry = AgentRegistry(store, ROOT)
    actors = [registry.register(f"actor-{i}", mission_id="mission", run_id="run",
                                role_id="evidence_preparation_synthesis",
                                display_name=f"Peer {i}")["id"] for i in range(3)]
    configs = []
    for i, actor in enumerate(actors):
        config = store.transact(f"config-{i}", {"actor": actor}, lambda tx, i=i, actor=actor: tx.put(
            "agent_configuration", f"config-{i}",
            {"mission_id": "mission", "instance_id": actor, "configuration_sha256": f"fixture-{i}"}),
            mission_id="mission")
        configs.append(ref(config))
    board = ProposalBoard(store)
    capacity = CapacityScheduler(store, CapacityPolicy(
        permits={"model": model, "retrieval": retrieval, "compute": 1},
        quotas=quotas or {"provider": QuotaLimit(60, requests=10, tokens=100)},
    ))
    executor = ConcurrentExecutor(store, board, PeerMailbox(store), capacity,
                                  TraceJournal(store), handlers or {}, owner="host",
                                  lease_seconds=lease_seconds)
    return store, board, capacity, executor, actors, configs


def propose(board, actor, name, *, depends_on=()):
    return board.propose(f"propose-{name}", mission_id="mission", proposal_id=name,
                         actor_id=actor, objective=f"Inspect {name}", expected_artifact="Fixture receipt",
                         next_action="Run selected callback", depends_on=list(depends_on))["proposal_ref"]


def spec(name, proposal_ref, actor, config, *, kind="model", scopes=("provider",),
         handler_id="fixture", handler_version="1"):
    return WorkSpec(work_id=name, mission_id="mission", run_id="run",
                    proposal_ref=proposal_ref, actor_id=actor, configuration_ref=config,
                    input_refs=(), handler_id=handler_id, handler_version=handler_version,
                    kind=kind, lane=name, quota_scopes=scopes,
                    estimated_tokens=1 if kind == "model" else 0,
                    attempt_id=f"attempt-{name}")


def test_independent_overlap_and_fast_dependent_before_slow_finish(tmp_path):
    async def scenario():
        starts = []
        slow_release = asyncio.Event()
        dependent_done = asyncio.Event()

        async def callback(work):
            starts.append(work.work_id)
            if work.work_id == "slow":
                await slow_release.wait()
            if work.work_id == "dependent":
                assert not slow_release.is_set()
                dependent_done.set()
            return WorkResult(payload={"fixture_observation": work.work_id}, used_tokens=1)

        store, board, _, executor, actors, configs = foundation(
            tmp_path, handlers={("fixture", "1"): callback})
        fast = propose(board, actors[0], "fast")
        propose_slow = propose(board, actors[1], "slow")
        dependent = propose(board, actors[2], "dependent", depends_on=("fast",))
        for name, pointer, actor, config in (("fast", fast, actors[0], configs[0]),
                                              ("slow", propose_slow, actors[1], configs[1]),
                                              ("dependent", dependent, actors[2], configs[2])):
            executor.submit(spec(name, pointer, actor, config))
        running = asyncio.create_task(executor.run_ready("mission"))
        await asyncio.wait_for(dependent_done.wait(), timeout=3)
        assert store.get("board_proposal", "dependent")["body"]["status"] == "completed"
        assert store.get("board_proposal", "slow")["body"]["status"] == "open"
        slow_release.set()
        result = await asyncio.wait_for(running, timeout=3)
        assert result["work"] == {"fast": "completed", "slow": "completed", "dependent": "completed"}
        assert set(starts) == {"fast", "slow", "dependent"}
        assert len(store.records("executor_artifact")) == 3
        assert len(store.records("executor_receipt")) == 3
        assert len(store.records("coordination_trace")) == 3
        assert result["mission_status"] == "open"

    asyncio.run(scenario())


def test_late_submission_wakes_other_instance_runner_while_slow_work_remains_active(tmp_path):
    async def scenario():
        slow_started = asyncio.Event()
        slow_release = asyncio.Event()
        late_started = asyncio.Event()

        async def callback(work):
            if work.work_id == "slow":
                slow_started.set()
                await slow_release.wait()
            else:
                late_started.set()
            return WorkResult(payload={"observed": work.work_id}, used_tokens=1)

        store, board, capacity, executor, actors, configs = foundation(
            tmp_path, model=2, handlers={("fixture", "1"): callback})
        slow = propose(board, actors[0], "slow")
        executor.submit(spec("slow", slow, actors[0], configs[0]))
        runner = asyncio.create_task(executor.run_ready("mission"))
        await asyncio.wait_for(slow_started.wait(), timeout=3)
        other_store = StateStore(tmp_path)
        other = ConcurrentExecutor(other_store, ProposalBoard(other_store),
                                   PeerMailbox(other_store), CapacityScheduler(other_store, capacity.policy),
                                   TraceJournal(other_store), {("fixture", "1"): callback},
                                   owner="other-host")
        late = propose(board, actors[1], "late")
        other.submit(spec("late", late, actors[1], configs[1]))
        await asyncio.wait_for(late_started.wait(), timeout=3)
        assert not slow_release.is_set()
        assert store.get("board_proposal", "slow")["body"]["status"] == "open"
        slow_release.set()
        result = await asyncio.wait_for(runner, timeout=3)
        assert result["work"] == {"slow": "completed", "late": "completed"}

    asyncio.run(scenario())


def test_new_head_does_not_revoke_exact_frozen_input(tmp_path):
    def callback(_work):
        store.transact("new-copy-version", {}, lambda tx: tx.put(
            "source_copy", "copy-one", {"mission_id": "mission", "version_label": "new"},
            expected_version=1), mission_id="mission")
        return WorkResult(payload={"observed": "original copy"}, used_tokens=1)

    store, board, _, executor, actors, configs = foundation(
        tmp_path, handlers={("fixture", "1"): callback})
    source = store.transact("save-copy", {}, lambda tx: tx.put(
        "source_copy", "copy-one", {"mission_id": "mission", "version_label": "original"}),
        mission_id="mission")
    pointer = propose(board, actors[0], "frozen")
    work = replace(spec("frozen", pointer, actors[0], configs[0]), input_refs=(ref(source),))
    executor.submit(work)
    assert asyncio.run(executor.run_ready("mission"))["work"]["frozen"] == "completed"
    artifact = store.records("executor_artifact")[0]
    assert artifact["body"]["input_refs"] == [ref(source)]
    assert store.get("source_copy", "copy-one")["version"] == 2


def test_submission_replays_at_changed_time_with_exact_pinned_configuration(tmp_path):
    store, board, capacity, executor, actors, configs = foundation(
        tmp_path, handlers={("fixture", "1"): lambda work: WorkResult(payload={"id": work.work_id})})
    pointer = propose(board, actors[0], "one")
    work = spec("one", pointer, actors[0], configs[0])
    first = executor.submit(work)
    restarted = ConcurrentExecutor(StateStore(tmp_path), ProposalBoard(StateStore(tmp_path)),
                                   PeerMailbox(StateStore(tmp_path)), capacity,
                                   TraceJournal(StateStore(tmp_path)),
                                   {("fixture", "1"): lambda item: WorkResult(payload={"id": item.work_id})},
                                   owner="new-host")
    replay = restarted.submit(work)
    assert first["version"] == replay["version"] == 1
    assert replay["body"]["spec"]["configuration_ref"] == configs[0]


def test_atomic_result_failure_preserves_known_result_without_partial_acceptance(tmp_path):
    def callback(_work):
        triggered = False

        def fault(point):
            nonlocal triggered
            if point == "before_metadata_commit" and not triggered:
                triggered = True
                raise RuntimeError("fixture commit interruption")

        store.fault = fault
        return WorkResult(payload={"fixture": "not accepted"}, used_tokens=1,
                          messages=({"recipient_id": actors[1], "payload": {"status": "ready"}},))

    store, board, capacity, executor, actors, configs = foundation(
        tmp_path, model=1, handlers={("fixture", "1"): callback})
    pointer = propose(board, actors[0], "interrupted")
    executor.submit(spec("interrupted", pointer, actors[0], configs[0]))
    result = asyncio.run(executor.run_ready("mission"))
    assert result["work"]["interrupted"] == "result_unaccepted"
    assert store.records("executor_artifact") == []
    assert store.records("executor_receipt") == []
    assert store.records("mail_message") == []
    assert store.records("board_outcome") == []
    assert not capacity.get(executor._capacity_id(spec("interrupted", pointer, actors[0], configs[0])))["external_active"]
    observation = store.records("executor_unaccepted_result")
    assert len(observation) == 1
    assert observation[0]["body"]["result"]["payload"] == {"fixture": "not accepted"}
    assert observation[0]["body"]["capacity_reconciled"] is True
    transition = [item for item in store.events("mission")
                  if item["type"] == "executor_result_unaccepted"]
    assert len(transition) == 1
    assert transition[0]["body"]["observation_ref"] == ref(observation[0])
    assert store.get(**transition[0]["body"]["work_ref"])["body"]["phase"] == "result_unaccepted"
    assert "payload" not in transition[0]["body"]
    assert len(store.records("coordination_trace")) == 1
    repair = board.claim("claim-repair", mission_id="mission", proposal_id="interrupted",
                         actor_id=actors[1], ttl=30)
    assert repair["status"] == "accepted"


def test_revised_proposal_rejects_late_callback_without_overwrite(tmp_path):
    def callback(_work):
        changed = board.revise("revision", mission_id="mission", proposal_id="conflict",
                               actor_id=actors[1], expected_version=1,
                               reason="New input", next_action="Reconsider source")
        assert changed["status"] == "accepted"
        return WorkResult(payload={"obsolete": True}, used_tokens=1)

    store, board, capacity, executor, actors, configs = foundation(
        tmp_path, model=1, handlers={("fixture", "1"): callback})
    pointer = propose(board, actors[0], "conflict")
    executor.submit(spec("conflict", pointer, actors[0], configs[0]))
    result = asyncio.run(executor.run_ready("mission"))
    assert result["work"]["conflict"] == "result_unaccepted"
    assert store.get("board_proposal", "conflict")["version"] == 2
    assert store.get("board_proposal", "conflict")["body"]["status"] == "open"
    assert store.records("executor_artifact") == []
    assert capacity.get(executor._capacity_id(spec("conflict", pointer, actors[0], configs[0])))["attempts"][0]["charged_tokens"] == 1
    assert len(store.records("executor_unaccepted_result")) == 1


def test_old_owner_preserves_known_result_without_releasing_replacement_lease(tmp_path):
    now = [1000.0]
    replacement = {}
    calls = []

    def callback(_work):
        calls.append("called")
        now[0] += 6
        assert capacity.expire()[0]["phase"] == "uncertain"
        replacement.update(capacity.claim_uncertain(cap_id, "reconciler", lease_seconds=60))
        return WorkResult(payload={"observed": "late"}, used_tokens=2)

    store, board, capacity, executor, actors, configs = foundation(
        tmp_path, model=1, handlers={("fixture", "1"): callback},
        clock=lambda: now[0], lease_seconds=5)
    pointer = propose(board, actors[0], "late")
    work = spec("late", pointer, actors[0], configs[0])
    cap_id = executor._capacity_id(work)
    executor.submit(work)
    result = asyncio.run(executor.run_ready("mission"))
    assert result["work"]["late"] == "result_unaccepted"
    observation = store.records("executor_unaccepted_result")[0]
    assert observation["body"]["external_effect"] == "known"
    assert observation["body"]["capacity_reconciled"] is False
    assert capacity.get(cap_id)["owner"] == "reconciler"
    assert capacity.get(cap_id)["generation"] == replacement["generation"]
    assert capacity.get(cap_id)["reservation"] is True
    with pytest.raises(StateConflict):
        capacity.reconcile(cap_id, "host", observation["body"]["capacity_generation"],
                           effect="known_unaccepted", used_tokens=2,
                           output_ref=observation["id"])
    capacity.reconcile(cap_id, "reconciler", replacement["generation"],
                       effect="known_unaccepted", used_tokens=observation["body"]["used_tokens"],
                       output_ref=observation["id"])
    assert capacity.get(cap_id)["phase"] == "known_unaccepted"
    assert capacity.get(cap_id)["attempts"][0]["charged_tokens"] == 2
    assert asyncio.run(executor.run_ready("mission"))["work"]["late"] == "result_unaccepted"
    assert calls == ["called"]
    assert len(store.records("executor_unaccepted_result")) == 1


def test_saved_model_receipt_is_known_when_callback_faults_after_capture(tmp_path):
    def callback(_work):
        store.transact("capture-synthetic-response", {}, lambda tx: tx.put(
            "model_response_receipt", f"model-response:model-intent:{call_id}:1", {
                "schema": "ora.model_response_receipt.v1", "mission_id": "mission",
                "call_ref": call_ref, "configuration_ref": configs[0],
                "intent_ref": intent_ref, "request_sha256": "a" * 64,
                "usage": {"total_tokens": 2}, "status": "complete",
                "response_sha256": "c" * 64, "returned_model": "fixture-model",
                "finish_reason": "stop", "synthetic": True,
            }), mission_id="mission")
        raise RuntimeError("local packaging failed after saved provider receipt")

    store, board, capacity, executor, actors, configs = foundation(
        tmp_path, model=1, handlers={("fixture", "1"): callback})
    pointer = propose(board, actors[0], "captured")
    call_id = "call-captured"
    call_ref = {"kind": "agent_call", "id": call_id, "version": 1}
    store.transact("save-call", {}, lambda tx: tx.put("agent_call", call_id, {
        "mission_id": "mission", "instance_id": actors[0], "task_id": "captured",
        "attempt_id": "attempt-captured", "configuration_id": configs[0]["id"]}),
        mission_id="mission")
    intent = store.transact("save-intent", {}, lambda tx: tx.put(
        "model_request_intent", f"model-intent:{call_id}:1", {
            "mission_id": "mission", "call_ref": call_ref,
            "configuration_ref": configs[0], "request_sha256": "a" * 64,
            "synthetic": True}), mission_id="mission")
    intent_ref = ref(intent)
    work = replace(spec("captured", pointer, actors[0], configs[0]),
                   call_id=call_id, call_version=1)
    executor.submit(work)
    result = asyncio.run(executor.run_ready("mission"))
    assert result["work"]["captured"] == "result_unaccepted"
    observation = store.records("executor_unaccepted_result")[0]["body"]
    assert observation["result"] is None
    assert observation["external_effect"] == "known"
    assert observation["observed_receipt_ref"]["kind"] == "model_response_receipt"
    assert capacity.get(executor._capacity_id(work))["attempts"][0]["charged_tokens"] == 2
    assert capacity.get(executor._capacity_id(work))["phase"] == "known_unaccepted"
    assert store.records("executor_artifact") == []


def test_unaccepted_model_result_charges_saved_receipt_usage_not_callback_claim(tmp_path):
    def callback(_work):
        saved = store.transact("capture-mismatch", {}, lambda tx: tx.put(
            "model_response_receipt", f"model-response:model-intent:{call_id}:1", {
                "schema": "ora.model_response_receipt.v1", "mission_id": "mission",
                "call_ref": call_ref, "configuration_ref": configs[0],
                "intent_ref": intent_ref, "request_sha256": "b" * 64,
                "usage": {"total_tokens": 2}, "status": "complete",
                "response_sha256": "c" * 64, "returned_model": "fixture-model",
                "finish_reason": "stop", "synthetic": True,
            }), mission_id="mission")
        return WorkResult(payload={"observed": "mismatched claim"},
                          output_refs=(ref(saved),), used_tokens=99)

    store, board, capacity, executor, actors, configs = foundation(
        tmp_path, model=1, handlers={("fixture", "1"): callback})
    pointer = propose(board, actors[0], "mismatch")
    call_id = "call-mismatch"
    call_ref = {"kind": "agent_call", "id": call_id, "version": 1}
    store.transact("save-mismatch-call", {}, lambda tx: tx.put("agent_call", call_id, {
        "mission_id": "mission", "instance_id": actors[0], "task_id": "mismatch",
        "attempt_id": "attempt-mismatch", "configuration_id": configs[0]["id"]}),
        mission_id="mission")
    intent = store.transact("save-mismatch-intent", {}, lambda tx: tx.put(
        "model_request_intent", f"model-intent:{call_id}:1", {
            "mission_id": "mission", "call_ref": call_ref,
            "configuration_ref": configs[0], "request_sha256": "b" * 64,
            "synthetic": True}), mission_id="mission")
    intent_ref = ref(intent)
    work = replace(spec("mismatch", pointer, actors[0], configs[0]),
                   call_id=call_id, call_version=1)
    executor.submit(work)
    result = asyncio.run(executor.run_ready("mission"))
    assert result["work"]["mismatch"] == "result_unaccepted"
    observed = store.records("executor_unaccepted_result")[0]["body"]
    assert observed["result"]["used_tokens"] == 99
    assert observed["used_tokens"] == 2
    assert observed["capacity_reconciled"] is True
    cap = capacity.get(executor._capacity_id(work))
    assert cap["attempts"][0]["charged_tokens"] == 2
    assert cap["attempts"][0]["usage_known"] is True
    assert store.records("executor_artifact") == []


@pytest.mark.parametrize(
    ("status", "error_kind", "http_status", "expected_phase", "external_effect"),
    [("failed", "transport_error", None, "uncertain", None),
     ("partial", "interrupted_stream", None, "uncertain", None),
     ("failed", "http_error", 429, "result_unaccepted", "known_rejection"),
     ("failed", "credential_unavailable", 503, "result_unaccepted", "no_request")],
)
def test_captured_model_failure_does_not_assume_completed_external_effect(
        tmp_path, status, error_kind, http_status, expected_phase, external_effect):
    def callback(_work):
        store.transact("capture-failure", {}, lambda tx: tx.put(
            "model_response_receipt", f"model-response:model-intent:{call_id}:1", {
                "schema": "ora.model_response_receipt.v1", "mission_id": "mission",
                "call_ref": call_ref, "configuration_ref": configs[0],
                "intent_ref": intent_ref, "request_sha256": "d" * 64,
                "status": status, "error_kind": error_kind, "http_status": http_status,
                "response_sha256": "e" * 64 if status == "partial" else None,
                "returned_model": "fixture-model" if status == "partial" else None,
                "finish_reason": "length" if status == "partial" else None,
                "usage": {"total_tokens": 1} if status == "partial" else {},
                "synthetic": True,
            }), mission_id="mission")
        raise RuntimeError("local packaging failed after saved receipt")

    store, board, capacity, executor, actors, configs = foundation(
        tmp_path, model=1, handlers={("fixture", "1"): callback})
    pointer = propose(board, actors[0], "capture-failure")
    call_id = "call-failure"
    call_ref = {"kind": "agent_call", "id": call_id, "version": 1}
    store.transact("save-failure-call", {}, lambda tx: tx.put("agent_call", call_id, {
        "mission_id": "mission", "instance_id": actors[0], "task_id": "capture-failure",
        "attempt_id": "attempt-capture-failure", "configuration_id": configs[0]["id"]}),
        mission_id="mission")
    intent = store.transact("save-failure-intent", {}, lambda tx: tx.put(
        "model_request_intent", f"model-intent:{call_id}:1", {
            "mission_id": "mission", "call_ref": call_ref,
            "configuration_ref": configs[0], "request_sha256": "d" * 64,
            "synthetic": True}), mission_id="mission")
    intent_ref = ref(intent)
    work = replace(spec("capture-failure", pointer, actors[0], configs[0]),
                   call_id=call_id, call_version=1)
    executor.submit(work)
    result = asyncio.run(executor.run_ready("mission"))
    assert result["work"]["capture-failure"] == expected_phase
    cap = capacity.get(executor._capacity_id(work))
    assert cap["reservation"] is (expected_phase == "uncertain")
    assert cap["external_active"] is (expected_phase == "uncertain")
    if external_effect is None:
        assert store.records("executor_unaccepted_result") == []
    else:
        observation = store.records("executor_unaccepted_result")[0]["body"]
        assert observation["external_effect"] == external_effect
        assert observation["capacity_reconciled"] is True
        assert cap["attempts"][0]["usage_known"] is (external_effect == "no_request")


def test_cancellation_after_saved_terminal_receipt_keeps_known_accounting(tmp_path):
    async def scenario():
        started = asyncio.Event()

        async def callback(_work):
            started.set()
            await asyncio.Event().wait()

        store, board, capacity, executor, actors, configs = foundation(
            tmp_path, model=1, handlers={("fixture", "1"): callback})
        pointer = propose(board, actors[0], "cancelled")
        call_id = "call-cancelled"
        call_ref = {"kind": "agent_call", "id": call_id, "version": 1}
        store.transact("save-cancelled-call", {}, lambda tx: tx.put("agent_call", call_id, {
            "mission_id": "mission", "instance_id": actors[0], "task_id": "cancelled",
            "attempt_id": "attempt-cancelled", "configuration_id": configs[0]["id"]}),
            mission_id="mission")
        intent = store.transact("save-cancelled-intent", {}, lambda tx: tx.put(
            "model_request_intent", f"model-intent:{call_id}:1", {
                "mission_id": "mission", "call_ref": call_ref,
                "configuration_ref": configs[0], "request_sha256": "a" * 64,
                "synthetic": True}), mission_id="mission")
        work = replace(spec("cancelled", pointer, actors[0], configs[0]),
                       call_id=call_id, call_version=1)
        executor.submit(work)
        running = asyncio.create_task(executor.run_ready("mission"))
        await asyncio.wait_for(started.wait(), timeout=3)
        store.transact("capture-before-cancel", {}, lambda tx: tx.put(
            "model_response_receipt", f"model-response:model-intent:{call_id}:1", {
                "schema": "ora.model_response_receipt.v1", "mission_id": "mission",
                "call_ref": call_ref, "configuration_ref": configs[0],
                "intent_ref": ref(intent), "request_sha256": "a" * 64,
                "usage": {"total_tokens": 2}, "status": "complete",
                "response_sha256": "c" * 64, "returned_model": "fixture-model",
                "finish_reason": "stop", "synthetic": True,
            }), mission_id="mission")
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=3)
        cap = capacity.get(executor._capacity_id(work))
        assert cap["phase"] == "known_unaccepted"
        assert cap["attempts"][0]["charged_tokens"] == 2
        assert store.get("executor_work", executor._work_record_id(work))["body"]["phase"] == "result_unaccepted"
        assert store.records("executor_artifact") == []

    asyncio.run(scenario())


def test_rate_wait_frees_model_permit_for_healthy_provider(tmp_path):
    attempts = []

    def callback(work):
        attempts.append(work.work_id)
        if work.work_id == "a-limited":
            return WorkResult(disposition="rate_limited", rate_scope="provider-a",
                              retry_after=30, used_tokens=0)
        return WorkResult(payload={"fixture": "healthy route"}, used_tokens=1)

    store, board, capacity, executor, actors, configs = foundation(
        tmp_path, model=1, handlers={("fixture", "1"): callback},
        quotas={"provider-a": QuotaLimit(60, requests=10, tokens=100),
                "provider-b": QuotaLimit(60, requests=10, tokens=100)})
    limited = propose(board, actors[0], "a-limited")
    healthy = propose(board, actors[1], "z-healthy")
    executor.submit(spec("a-limited", limited, actors[0], configs[0], scopes=("provider-a",)))
    waiting = asyncio.run(executor.run_ready("mission"))
    assert waiting["work"]["a-limited"] == "waiting"
    executor.submit(spec("z-healthy", healthy, actors[1], configs[1], scopes=("provider-b",)))
    result = asyncio.run(executor.run_ready("mission"))
    assert attempts == ["a-limited", "z-healthy"]
    assert result["work"] == {"a-limited": "waiting", "z-healthy": "completed"}
    assert result["next_wake"] is not None
    assert not capacity.get(executor._capacity_id(spec("a-limited", limited, actors[0], configs[0], scopes=("provider-a",))))["reservation"]
    assert store.get("board_proposal", "z-healthy")["body"]["status"] == "completed"


def test_uncertain_external_callback_retains_model_capacity_across_restart(tmp_path):
    attempts = []

    def callback(work):
        attempts.append(work.work_id)
        return WorkResult(disposition="uncertain")

    store, board, capacity, executor, actors, configs = foundation(
        tmp_path, model=1, handlers={("fixture", "1"): callback})
    pointer = propose(board, actors[0], "external")
    work = spec("external", pointer, actors[0], configs[0])
    executor.submit(work)
    first = asyncio.run(executor.run_ready("mission"))
    assert first["work"]["external"] == "uncertain"
    assert capacity.get(executor._capacity_id(work))["external_active"]
    restarted_store = StateStore(tmp_path)
    restarted = ConcurrentExecutor(restarted_store, ProposalBoard(restarted_store),
                                   PeerMailbox(restarted_store),
                                   CapacityScheduler(restarted_store, capacity.policy),
                                   TraceJournal(restarted_store),
                                   {("fixture", "1"): callback}, owner="new-host")
    again = asyncio.run(restarted.run_ready("mission"))
    assert again["work"]["external"] == "uncertain"
    assert attempts == ["external"]


def test_restart_after_unused_admission_reclaims_without_old_owner_dispatch(tmp_path):
    clock = [1000.0]
    calls = []

    def callback(work):
        calls.append(work.work_id)
        return WorkResult(payload={"fixture": "completed after restart"}, used_tokens=1)

    store, board, capacity, executor, actors, configs = foundation(
        tmp_path, model=1, handlers={("fixture", "1"): callback},
        clock=lambda: clock[0], lease_seconds=5)
    pointer = propose(board, actors[0], "resume")
    work = spec("resume", pointer, actors[0], configs[0])
    executor.submit(work)
    cap_id = executor._capacity_id(work)
    executor._prepare("mission")
    original = capacity.admit("host", "model", mission_id="mission",
                              eligible_operation_ids=frozenset({cap_id}), lease_seconds=5)
    assert board.claim("old-claim", mission_id="mission", proposal_id="resume",
                       actor_id=actors[0], ttl=5)["status"] == "accepted"
    clock[0] += 6
    restarted_store = StateStore(tmp_path, clock=lambda: clock[0])
    restarted_capacity = CapacityScheduler(restarted_store, capacity.policy)
    restarted = ConcurrentExecutor(restarted_store, ProposalBoard(restarted_store),
                                   PeerMailbox(restarted_store), restarted_capacity,
                                   TraceJournal(restarted_store),
                                   {("fixture", "1"): callback}, owner="new-host",
                                   lease_seconds=5)
    result = asyncio.run(restarted.run_ready("mission"))
    assert result["work"]["resume"] == "completed"
    assert calls == ["resume"]
    with pytest.raises(StateConflict):
        capacity.dispatched(cap_id, "host", original["generation"])


def test_durable_rate_wake_reuses_work_once_then_replay_is_quiet(tmp_path):
    clock = [1000.0]
    calls = []

    def callback(work):
        calls.append(work.work_id)
        if len(calls) == 1:
            return WorkResult(disposition="rate_limited", rate_scope="provider",
                              retry_after=20, used_tokens=0)
        return WorkResult(payload={"fixture": "confirmed second attempt"}, used_tokens=2)

    store, board, capacity, executor, actors, configs = foundation(
        tmp_path, model=1, handlers={("fixture", "1"): callback}, clock=lambda: clock[0])
    pointer = propose(board, actors[0], "wake")
    work = spec("wake", pointer, actors[0], configs[0])
    executor.submit(work)
    first = asyncio.run(executor.run_ready("mission"))
    assert first["work"]["wake"] == "waiting"
    assert first["next_wake"] == 1020.0
    assert asyncio.run(executor.run_ready("mission"))["work"]["wake"] == "waiting"
    assert calls == ["wake"]
    clock[0] = 1020.0
    second = asyncio.run(executor.run_ready("mission"))
    assert second["work"]["wake"] == "completed"
    assert asyncio.run(executor.run_ready("mission"))["work"]["wake"] == "completed"
    assert calls == ["wake", "wake"]
    assert len(capacity.get(executor._capacity_id(work))["attempts"]) == 2
    assert len(store.records("executor_artifact")) == 1
    timing = store.records("executor_receipt")[0]["body"]["timing"]
    assert timing["provider_wait_seconds"] == 20
    assert timing["retry_wait_seconds"] == 0
    assert timing["peer_wait"] == "not_measured_by_executor"


def test_exact_later_call_manifest_version_reaches_trace(tmp_path):
    store, board, _, executor, actors, configs = foundation(
        tmp_path, model=1,
        handlers={("fixture", "1"): lambda work: WorkResult(payload={"fixture": work.work_id},
                                                                used_tokens=1)})
    pointer = propose(board, actors[0], "versioned")
    call_body = {"mission_id": "mission", "instance_id": actors[0],
                 "task_id": "versioned", "attempt_id": "attempt-versioned",
                 "configuration_id": configs[0]["id"]}

    def calls(tx):
        tx.put("agent_call", "call-versioned", {**call_body, "state": "prepared"})
        return tx.put("agent_call", "call-versioned", {**call_body, "state": "ready"},
                      expected_version=1)

    store.transact("save-versioned-call", {"id": "call-versioned"}, calls,
                   mission_id="mission")
    call_pointer = {"kind": "agent_call", "id": "call-versioned", "version": 2}
    request_hash = "a" * 64

    def saved_response(tx):
        intent = tx.put("model_request_intent", "fixture-intent", {
            "mission_id": "mission", "call_ref": call_pointer,
            "configuration_ref": configs[0], "request_sha256": request_hash,
            "synthetic": True,
        })
        return tx.put("model_response_receipt", "fixture-response", {
            "schema": "ora.model_response_receipt.v1", "mission_id": "mission",
            "call_ref": call_pointer, "configuration_ref": configs[0],
            "intent_ref": ref(intent), "request_sha256": request_hash,
            "status": "complete", "usage": {"total_tokens": 1},
            "response_sha256": "c" * 64, "returned_model": "fixture-model",
            "finish_reason": "stop",
            "synthetic": True,
        })

    model_receipt = store.transact("save-fixture-response", {}, saved_response,
                                   mission_id="mission")
    executor.handlers[("fixture", "1")] = lambda work: WorkResult(
        payload={"fixture": work.work_id}, used_tokens=1,
        output_refs=(ref(model_receipt),))
    work = replace(spec("versioned", pointer, actors[0], configs[0]),
                   call_id="call-versioned", call_version=2)
    executor.submit(work)
    with pytest.raises(StateConflict):
        executor.submit(replace(work, work_id="duplicate-work"))
    assert asyncio.run(executor.run_ready("mission"))["work"]["versioned"] == "completed"
    trace = store.records("coordination_trace")[0]["body"]
    assert trace["call_id"] == "call-versioned" and trace["call_version"] == 2


def test_pinned_model_call_cannot_omit_or_replace_source_copy_lineage(tmp_path):
    store, board, _, executor, actors, configs = foundation(
        tmp_path, handlers={("fixture", "1"): lambda _work: WorkResult()})
    first = store.transact("source-v1", {}, lambda tx: tx.put(
        "source_copy", "copy", {"mission_id": "mission", "text": "v1"}),
        mission_id="mission")
    second = store.transact("source-v2", {}, lambda tx: tx.put(
        "source_copy", "copy", {"mission_id": "mission", "text": "v2"},
        expected_version=1), mission_id="mission")
    pointer = propose(board, actors[0], "lineage")
    call_id = "call-lineage"
    store.transact("save-lineage-call", {}, lambda tx: tx.put("agent_call", call_id, {
        "mission_id": "mission", "instance_id": actors[0], "task_id": "lineage",
        "attempt_id": "attempt-lineage", "configuration_id": configs[0]["id"],
        "input_refs": [{**ref(first), "sha256": "a" * 64}],
    }), mission_id="mission")
    base = replace(spec("lineage", pointer, actors[0], configs[0]),
                   call_id=call_id, call_version=1)
    with pytest.raises(StateConflict, match="source copies"):
        executor.submit(base)
    with pytest.raises(StateConflict, match="source copies"):
        executor.submit(replace(base, work_id="lineage-v2", input_refs=(ref(second),)))
    assert store.records("executor_work") == []
    assert store.records("capacity_operation") == []


def test_reviewed_peer_answer_feeds_dependent_executor_work(tmp_path):
    observed = []

    def callback(work):
        observed.append(work.work_id)
        assert mailbox.request_view("peer-question")["status"] == "answered"
        if work.work_id == "parent":
            return WorkResult(payload={"fixture": "parent reviewed answer"}, used_tokens=1,
                              messages=({"recipient_id": actors[2],
                                         "payload": {"reviewed_answer": "answer-one"}},))
        return WorkResult(payload={"fixture": "dependent used reviewed answer"}, used_tokens=1)

    store, board, _, executor, actors, configs = foundation(
        tmp_path, handlers={("fixture", "1"): callback})
    mailbox = executor.mailbox
    parent = propose(board, actors[0], "parent")
    opened = mailbox.send_request(
        "ask-peer", mission_id="mission", request_id="peer-question",
        message_id="peer-question-message", requester_id=actors[0],
        recipient_id=actors[1], question="Inspect comparator in supplied fixture",
        expected_artifact="Methods note", cause_ref=parent, task_ref=parent,
        payload={"fixture": "source was supplied"})
    assert opened["status"] == "accepted"
    assert mailbox.mark_delivered("delivered", mission_id="mission",
                                  message_id="peer-question-message")["status"] == "accepted"
    assert mailbox.acknowledge("acknowledged", mission_id="mission",
                               message_id="peer-question-message",
                               recipient_id=actors[1])["status"] == "accepted"
    artifact = store.transact("peer-answer-artifact", {}, lambda tx: tx.put(
        "artifact", "peer-answer", {"mission_id": "mission", "synthetic": True,
                                     "observation": "fixture comparator present"}),
        mission_id="mission")
    answered = mailbox.record_answer("answer-peer", mission_id="mission",
                                     request_id="peer-question", answer_id="answer-one",
                                     responder_id=actors[1], artifact_ref=ref(artifact))
    assert answered["status"] == "accepted"
    assert mailbox.review_answer("review-peer", mission_id="mission",
                                 request_id="peer-question", reviewer_id=actors[0],
                                 answer_id="answer-one", disposition="answered",
                                 reason="The fixture note addresses the comparator question")["status"] == "accepted"
    dependent = board.propose(
        "propose-dependent", mission_id="mission", proposal_id="dependent",
        actor_id=actors[2], objective="Inspect reviewed peer answer",
        expected_artifact="Fixture result", next_action="Use the reviewed answer",
        depends_on=["parent"], prerequisite_refs=[ref(artifact)])["proposal_ref"]
    executor.submit(spec("parent", parent, actors[0], configs[0]))
    downstream = replace(spec("dependent", dependent, actors[2], configs[2]),
                         input_refs=(ref(artifact),))
    executor.submit(downstream)
    result = asyncio.run(executor.run_ready("mission"))
    assert result["work"] == {"parent": "completed", "dependent": "completed"}
    assert observed == ["parent", "dependent"]
    assert mailbox.request_view("peer-question")["accepted_answer_ref"] == answered["answer_ref"]
    assert len(mailbox.pending_outbox("mission")) == 1  # Parent's local result message.


def test_repeated_429_stops_at_transport_retry_limit(tmp_path):
    clock = [1000.0]
    calls = []

    def callback(work):
        calls.append(work.work_id)
        return WorkResult(disposition="rate_limited", rate_scope="provider",
                          retry_after=1, used_tokens=0)

    store, board, capacity, executor, actors, configs = foundation(
        tmp_path, model=1, handlers={("fixture", "1"): callback}, clock=lambda: clock[0])
    pointer = propose(board, actors[0], "bounded")
    work = spec("bounded", pointer, actors[0], configs[0])
    executor.submit(work)
    assert asyncio.run(executor.run_ready("mission"))["work"]["bounded"] == "waiting"
    clock[0] += 1
    assert asyncio.run(executor.run_ready("mission"))["work"]["bounded"] == "waiting"
    clock[0] += 1
    result = asyncio.run(executor.run_ready("mission"))
    assert result["work"]["bounded"] == "failed"
    assert result["observed"][0]["status"] == "failed"
    assert calls == ["bounded"] * 3
    assert len(capacity.get(executor._capacity_id(work))["attempts"]) == 3
    assert store.records("executor_artifact") == []
