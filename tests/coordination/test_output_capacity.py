"""Output-minute admission uses exact model scopes and durable local reservations."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.capacity import CapacityPolicy, CapacityScheduler, QuotaLimit
from ora.coordination.executor import WorkResult
from ora.models.output_limits import output_limit_observation
from ora.models.profiles import GPT_OSS_MODEL, QWEN_MODEL, model_quota_scope
from ora.state.store import StateConflict, StateError, StateStore
from tests.coordination.test_capacity import Clock
from tests.coordination.test_executor import foundation, propose, spec
from tests.mission.test_cancelled_saved_response_reconciliation import _saved_cancelled_response
from tests.mission.test_service import service

ORG = "groq-synthetic-org"
GPT = model_quota_scope(ORG, GPT_OSS_MODEL)
QWEN = model_quota_scope(ORG, QWEN_MODEL)


def scheduler(root, clock):
    return CapacityScheduler(StateStore(root, clock=clock), CapacityPolicy(
        permits={"model": 3, "retrieval": 1, "compute": 1},
        quotas={GPT: QuotaLimit(60, requests=10, tokens=10000),
                QWEN: QuotaLimit(60, requests=10, tokens=10000)}, review_reserve=0,
    ))


def enqueue(capacity, name, scope, output):
    return capacity.enqueue(name, task_id=name, lane=name, kind="model",
                            quota_scopes=(scope,), estimated_tokens=10,
                            output_reservation_tokens=output)


def test_parser_accepts_only_exact_model_otpm_and_never_returns_error_text():
    error = {"type": "tokens", "code": "rate_limit_exceeded", "message":
             "Rate limit for organization synthetic-secret and model `qwen/qwen3.8-27b` "
             "on output tokens per minute (OTPM): Limit 1,000, Requested 1,024."}
    observed = output_limit_observation(429, error, QWEN_MODEL)
    assert observed == {"scope": "output_tokens_per_minute", "limit_tokens": 1000,
                        "used_tokens": None, "requested_tokens": 1024}
    assert "synthetic-secret" not in str(observed)
    assert output_limit_observation(400, error, QWEN_MODEL) is None
    assert output_limit_observation(429, error, GPT_OSS_MODEL) is None
    assert output_limit_observation(429, {**error, "code": "other"}, QWEN_MODEL) is None
    assert output_limit_observation(429, {**error, "message": error["message"].replace(
        "output tokens per minute (OTPM)", "input tokens per minute (TPM)")}, QWEN_MODEL) is None
    assert output_limit_observation(429, {**error, "message": error["message"].replace(
        "1,000", "0")}, QWEN_MODEL) is None


def test_oversized_request_waits_without_cap_change_and_other_model_dispatches(tmp_path):
    clock = Clock()
    capacity = scheduler(tmp_path, clock)
    enqueue(capacity, "qwen-seed", QWEN, 1024)
    first = capacity.admit("host", "model")
    capacity.dispatched("qwen-seed", "host", first["generation"])
    observed = {"scope": "output_tokens_per_minute", "limit_tokens": 1000,
                "used_tokens": None, "requested_tokens": 1024}
    capacity.observe_output_rate_limit(QWEN, observed, source_operation_id="qwen-seed",
                                       observation_id="receipt-429", observed_at=clock())
    capacity.rate_limited("qwen-seed", "host", first["generation"],
                          scope=QWEN, retry_after=30, used_tokens=None)
    assert capacity.get("qwen-seed")["phase"] == "waiting"
    assert capacity.get("qwen-seed")["wake_at"] is None
    assert capacity.store.get("capacity_quota", QWEN) is None
    enqueue(capacity, "a-qwen", QWEN, 1024)
    enqueue(capacity, "b-gpt", GPT, 1024)
    admitted = capacity.admit("host", "model")
    assert admitted["operation_id"] == "b-gpt"
    assert capacity.get("a-qwen")["phase"] == "waiting"
    assert capacity.get("a-qwen")["wake_at"] is None
    assert capacity.get("a-qwen")["output_reservation_tokens"] == 1024
    assert capacity.output_capacity_status("a-qwen") == {
        "state": "request_exceeds_observed_output_minute_limit", "scope": QWEN,
        "requested_tokens": 1024, "limit_tokens": 1000,
    }
    assert capacity.get("qwen-seed")["attempts"][0]["charged_output_tokens"] == 1024
    clock.advance(120)
    assert scheduler(tmp_path, clock).admit("other", "model") is None


def test_replenishable_output_window_survives_restart_and_serializes_workers(tmp_path):
    clock = Clock()
    first = scheduler(tmp_path, clock)
    enqueue(first, "seed", QWEN, 500)
    admission = first.admit("one", "model")
    first.dispatched("seed", "one", admission["generation"])
    observed = {"scope": "output_tokens_per_minute", "limit_tokens": 1000,
                "used_tokens": 600, "requested_tokens": 500}
    first.observe_output_rate_limit(QWEN, observed, source_operation_id="seed",
                                    observation_id="saved-429", observed_at=clock(),
                                    retry_after=5)
    version = first.store.get("capacity_output_quota", QWEN)["version"]
    first.observe_output_rate_limit(QWEN, observed, source_operation_id="seed",
                                    observation_id="saved-429", observed_at=clock(),
                                    retry_after=5)
    assert first.store.get("capacity_output_quota", QWEN)["version"] == version
    with pytest.raises(StateConflict):
        first.observe_output_rate_limit(QWEN, {**observed, "limit_tokens": 999},
            source_operation_id="seed", observation_id="saved-429", observed_at=clock(),
            retry_after=5)
    first.settle("seed", "one", admission["generation"], used_tokens=None,
                 outcome="failed")
    enqueue(first, "a", QWEN, 600)
    enqueue(first, "b", QWEN, 600)
    assert first.admit("one", "model") is None
    assert first.get("a")["wake_at"] == clock() + 60
    clock.advance(61)
    left, right = scheduler(tmp_path, clock), scheduler(tmp_path, clock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda pair: pair[0].admit(pair[1], "model"),
                                 ((left, "left"), (right, "right"))))
    assert sum(item is not None for item in outcomes) == 1
    assert {item["operation_id"] for item in outcomes if item} <= {"a", "b"}


def test_known_completion_replaces_reservation_but_unknown_keeps_it(tmp_path):
    clock = Clock()
    capacity = scheduler(tmp_path, clock)
    enqueue(capacity, "seed", QWEN, 800)
    first = capacity.admit("host", "model")
    capacity.dispatched("seed", "host", first["generation"])
    capacity.observe_output_rate_limit(QWEN, {"scope": "output_tokens_per_minute",
        "limit_tokens": 1000, "used_tokens": None, "requested_tokens": 800},
        source_operation_id="seed", observation_id="observation", observed_at=clock())
    capacity.settle("seed", "host", first["generation"],
                    used_tokens=150, used_output_tokens=100, outcome="completed")
    assert capacity.get("seed")["attempts"][0]["charged_output_tokens"] == 100
    enqueue(capacity, "next", QWEN, 800)
    assert capacity.admit("host", "model")["operation_id"] == "next"


def test_model_bridge_inherits_matching_and_unknown_legacy_debt_only(tmp_path):
    clock = Clock()
    base = ORG
    initial = CapacityPolicy(permits={"model": 3, "retrieval": 1, "compute": 1},
                             quotas={base: QuotaLimit(60, requests=10, tokens=10000)},
                             review_reserve=0)
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, initial)

    def seed(tx):
        for name, model in (("gpt", GPT_OSS_MODEL), ("qwen", QWEN_MODEL)):
            tx.put("agent_configuration", f"config-{name}",
                   {"mission_id": "mission", "model_id": model})
            tx.put("agent_call", f"call-{name}", {
                "mission_id": "mission", "task_id": name, "attempt_id": f"attempt-{name}",
                "configuration_id": f"config-{name}", "model_id": model,
            })
        return {}

    store.transact("seed-exact-models", {}, seed)
    for name in ("gpt", "qwen", "unknown"):
        old.enqueue(name, task_id=name, lane=name, kind="model",
                    quota_scopes=(base,), estimated_tokens=10,
                    mission_id="mission", attempt_id=f"attempt-{name}",
                    call_id=f"call-{name}" if name != "unknown" else "",
                    budget_version=f"config-{name}" if name != "unknown" else "")
    old.observe_headers(base, remaining_requests=0, remaining_tokens=0,
                        reset_after=30, observed_at=clock(),
                        source_operation_id="gpt", observation_id="gpt-head")
    gpt_policy = CapacityPolicy(initial.permits,
                                {**initial.quotas, GPT: QuotaLimit(60, tokens=10000)}, 0)
    CapacityScheduler.reconcile_additive_quota_domain(
        store, gpt_policy, from_scope=base, to_scope=GPT, model_id=GPT_OSS_MODEL)
    qwen_policy = CapacityPolicy(initial.permits,
                                 {**gpt_policy.quotas, QWEN: QuotaLimit(60, tokens=10000)}, 0)
    CapacityScheduler.reconcile_additive_quota_domain(
        store, qwen_policy, from_scope=base, to_scope=QWEN, model_id=QWEN_MODEL)
    gpt_bridge = store.get("capacity_quota_bridge", GPT)["body"]
    qwen_bridge = store.get("capacity_quota_bridge", QWEN)["body"]
    assert {r["id"] for r in gpt_bridge["inherited_operation_refs"]} == {"gpt", "unknown"}
    assert {r["id"] for r in qwen_bridge["inherited_operation_refs"]} == {"qwen", "unknown"}
    assert gpt_bridge["cutover_quota_ref"] is not None
    assert qwen_bridge["cutover_quota_ref"] is None
    current = CapacityScheduler(store, qwen_policy)
    current.enqueue("new-qwen", task_id="new-qwen", lane="new-qwen", kind="model",
                    quota_scopes=(QWEN,), estimated_tokens=10)
    assert current.admit("host", "model")["operation_id"] == "new-qwen"


def test_executor_preserves_legacy_intent_and_carries_new_output_reservation(tmp_path):
    store, board, capacity, executor, actors, configs = foundation(
        tmp_path, handlers={("fixture", "1"): lambda _work: WorkResult()},
        quotas={QWEN: QuotaLimit(60, tokens=10000)})
    pointer = propose(board, actors[0], "selected")
    legacy = spec("selected", pointer, actors[0], configs[0], scopes=(QWEN,))
    assert "output_reservation_tokens" not in legacy.intent()
    assert "used_output_tokens" not in WorkResult().intent()
    selected = replace(legacy, output_reservation_tokens=512)
    assert selected.intent()["output_reservation_tokens"] == 512
    executor.submit(selected)
    executor._prepare("mission")
    saved = capacity.get(executor._capacity_id(selected))
    assert saved["output_reservation_tokens"] == 512
    assert store.get(executor.KIND, executor._work_record_id(selected))["body"]["spec"] == (
        selected.intent())


def test_unproved_active_legacy_model_holds_both_new_model_scopes(tmp_path):
    clock = Clock()
    initial = CapacityPolicy(permits={"model": 3, "retrieval": 1, "compute": 1},
                             quotas={ORG: QuotaLimit(60, tokens=10000)}, review_reserve=0)
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, initial)
    old.enqueue("unknown", task_id="unknown", lane="unknown", kind="model",
                quota_scopes=(ORG,), estimated_tokens=10)
    admitted = old.admit("old", "model")
    old.dispatched("unknown", "old", admitted["generation"])
    gpt_policy = CapacityPolicy(initial.permits,
                                {**initial.quotas, GPT: QuotaLimit(60, tokens=10000)}, 0)
    CapacityScheduler.reconcile_additive_quota_domain(
        store, gpt_policy, from_scope=ORG, to_scope=GPT, model_id=GPT_OSS_MODEL)
    qwen_policy = CapacityPolicy(initial.permits,
                                 {**gpt_policy.quotas, QWEN: QuotaLimit(60, tokens=10000)}, 0)
    CapacityScheduler.reconcile_additive_quota_domain(
        store, qwen_policy, from_scope=ORG, to_scope=QWEN, model_id=QWEN_MODEL)
    current = CapacityScheduler(store, qwen_policy)
    enqueue(current, "new-gpt", GPT, 500)
    enqueue(current, "new-qwen", QWEN, 500)
    assert current.admit("new", "model") is None
    assert {r["id"] for r in store.get("capacity_quota_bridge", GPT)["body"][
        "inherited_operation_refs"]} == {"unknown"}
    assert {r["id"] for r in store.get("capacity_quota_bridge", QWEN)["body"][
        "inherited_operation_refs"]} == {"unknown"}


def test_model_bridge_rejects_scope_named_for_another_model(tmp_path):
    clock = Clock()
    initial = CapacityPolicy(permits={"model": 1, "retrieval": 1, "compute": 1},
                             quotas={ORG: QuotaLimit(60)}, review_reserve=0)
    store = StateStore(tmp_path, clock=clock)
    CapacityScheduler(store, initial)
    expanded = CapacityPolicy(initial.permits,
                              {**initial.quotas, QWEN: QuotaLimit(60)}, 0)
    with pytest.raises(StateError, match="scope differs from selected model"):
        CapacityScheduler.reconcile_additive_quota_domain(
            store, expanded, from_scope=ORG, to_scope=QWEN, model_id=GPT_OSS_MODEL)


@pytest.mark.parametrize(("binding_version", "expected"), [(1, 1), (None, 0)])
def test_cancelled_new_model_scope_needs_saved_binding_and_reconciles_exact_completion(
        tmp_path, monkeypatch, binding_version, expected):
    run_id, old_spec, cap_id, calls, transport = _saved_cancelled_response(
        tmp_path, monkeypatch)
    host = service(tmp_path, monkeypatch, transport)
    try:
        intent = host.store.get("model_request_intent",
                                f"model-intent:{old_spec.call_id}:{old_spec.call_version}")
        request = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        cap = request["max_completion_tokens"]
        scope = model_quota_scope(intent["body"]["quota_domain"], GPT_OSS_MODEL)
        selected = replace(old_spec, quota_scopes=(scope,), output_reservation_tokens=cap)
        work = host.store.get(host.executor.KIND, host.executor._work_record_id(old_spec))
        operation = host.store.get("capacity_operation", cap_id)
        run = host.store.get("mission_run", run_id)
        receipt = host.store.get("model_response_receipt", f"model-response:{intent['id']}")
        marker = host.store.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}")

        def convert(tx):
            run_body = dict(run["body"])
            if binding_version is None:
                run_body.pop("agent_model_binding_version", None)
            else:
                run_body["agent_model_binding_version"] = binding_version
            tx.put("mission_run", run_id, run_body,
                   expected_version=run["version"])
            tx.put(host.executor.KIND, work["id"], {**work["body"], "spec": selected.intent()},
                   expected_version=work["version"])
            attempts = list(operation["body"]["attempts"])
            attempts[-1] = {**attempts[-1], "reserved_output_tokens": cap,
                            "charged_output_tokens": cap, "output_usage_known": False}
            tx.put("capacity_operation", cap_id, {
                **operation["body"], "quota_scopes": [scope],
                "output_reservation_tokens": cap, "attempts": attempts,
            }, expected_version=operation["version"])
            updated_receipt = tx.put("model_response_receipt", receipt["id"], {
                **receipt["body"], "usage": {"total_tokens": 11, "completion_tokens": 4},
            }, expected_version=receipt["version"])
            tx.put("model_dispatch_attempt", marker["id"], {
                **marker["body"], "receipt_ref": {"kind": "model_response_receipt",
                    "id": receipt["id"], "version": updated_receipt["version"]},
            }, expected_version=marker["version"])
            return {}

        host.store.transact("convert-cancelled-new-model-scope", {}, convert,
                            mission_id=old_spec.mission_id)
        assert host.executor.reconcile_cancelled_saved_model_responses(run_id) == expected
        accounted = host.capacity.get(cap_id)["attempts"][-1]
        assert accounted["charged_output_tokens"] == (4 if expected else cap)
        assert accounted["output_usage_known"] is bool(expected)
        assert calls == []
    finally:
        host.close()


def test_mission_model_under_reservation_is_rejected_before_transport(tmp_path, monkeypatch):
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append("provider")
        raise AssertionError("Malformed output reservation reached transport")

    host = service(tmp_path, monkeypatch, forbidden)
    try:
        run = host.start(LiveStartRequest(
            request_id="output-reservation-check", question="Check a synthetic claim",
            agent_ids=["agent-synthesis"],
            agent_models={"agent-synthesis": {"model": QWEN_MODEL,
                                             "max_completion_tokens": 512}},
        ))
        control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        work = host._work_for(host._control(run.run_id, "agent-synthesis"))
        correct = host.executor._spec_from(work)
        assert correct.output_reservation_tokens == 512
        tampered = replace(correct, output_reservation_tokens=511)
        with pytest.raises(StateConflict, match="Output reservation differs"):
            host.executor.submit(tampered)
        assert calls == []
        assert host.capacity.get(host.executor._capacity_id(correct)) is None
        assert host.store.get(host.executor.KIND, work["id"])["body"]["spec"] == correct.intent()
    finally:
        host.close()
