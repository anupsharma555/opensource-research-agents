"""Model-specific Groq quota identities and offline admission mechanics."""

import asyncio
import sqlite3

import pytest

from ora.coordination.capacity import CapacityPolicy, CapacityScheduler, QuotaLimit
from ora.coordination.executor import WorkResult
from ora.models.profiles import GPT_OSS_MODEL, QWEN_MODEL, model_quota_scope
from ora.state import StateConflict, StateStore
from ora.state.store import canonical
from tests.coordination.test_executor import foundation, propose, spec


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _split_then_select_models(store, initial):
    legacy = "groq-selected"
    backup = "groq-verified-backup"
    inherited_limit = initial.quotas[legacy]
    split = CapacityPolicy(initial.permits, {
        **initial.quotas, backup: inherited_limit,
    }, initial.review_reserve)
    CapacityScheduler.reconcile_additive_quota_domain(
        store, split, from_scope=legacy, to_scope=backup,
    )
    for model in (GPT_OSS_MODEL, QWEN_MODEL):
        scope = model_quota_scope(backup, model)
        split = CapacityPolicy(split.permits, {
            **split.quotas, scope: inherited_limit,
        }, split.review_reserve)
        CapacityScheduler.reconcile_additive_quota_domain(
            store, split, from_scope=backup, to_scope=scope, model_id=model,
        )
    return split


def test_backup_model_bridge_inherits_old_same_model_hold_and_later_reset(tmp_path):
    clock = Clock()
    legacy = "groq-selected"
    backup = "groq-verified-backup"
    gpt = model_quota_scope(backup, GPT_OSS_MODEL)
    qwen = model_quota_scope(backup, QWEN_MODEL)
    initial = CapacityPolicy(
        permits={"model": 3, "retrieval": 1, "compute": 1},
        quotas={legacy: QuotaLimit(60, requests=1, tokens=100)}, review_reserve=0,
    )
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, initial)

    def seed(tx):
        tx.put("agent_configuration", "old-gpt-config", {
            "mission_id": "mission", "model_id": GPT_OSS_MODEL,
        })
        tx.put("agent_call", "old-gpt-call", {
            "mission_id": "mission", "task_id": "old-gpt",
            "attempt_id": "old-gpt-attempt", "configuration_id": "old-gpt-config",
            "model_id": GPT_OSS_MODEL,
        })
        return {}

    store.transact("seed-old-gpt-identity", {}, seed)
    old.enqueue("old-gpt", task_id="old-gpt", lane="old-gpt", kind="model",
                quota_scopes=(legacy,), estimated_tokens=50, mission_id="mission",
                attempt_id="old-gpt-attempt", call_id="old-gpt-call",
                budget_version="old-gpt-config")
    admitted = old.admit("old-host", "model")
    old.dispatched("old-gpt", "old-host", admitted["generation"])
    old.rate_limited("old-gpt", "old-host", admitted["generation"],
                     scope=legacy, retry_after=60)
    # The saved cutover head identifies its producing GPT operation. A head
    # without that lineage must conservatively hold both model lanes.
    old.observe_headers(legacy, remaining_requests=1, remaining_tokens=100,
                        reset_after_tokens=60, observed_at=clock(),
                        source_operation_id="old-gpt", observation_id="old-gpt-cutover")
    saved_old = store.get("capacity_operation", "old-gpt")
    policy = _split_then_select_models(store, initial)
    # A later old-scope observation must flow through the existing credential
    # bridge without rewriting either saved model bridge or the old operation.
    old.observe_headers(legacy, remaining_requests=None, remaining_tokens=0,
                        reset_after_tokens=90, observed_at=clock(),
                        source_operation_id="old-gpt", observation_id="old-gpt-later")
    current = CapacityScheduler(store, policy)
    current.enqueue("new-gpt", task_id="new-gpt", lane="new-gpt", kind="model",
                    quota_scopes=(gpt,), estimated_tokens=10)
    current.enqueue("new-qwen", task_id="new-qwen", lane="new-qwen", kind="model",
                    quota_scopes=(qwen,), estimated_tokens=10)
    assert current.admit("new-host", "model", eligible_operation_ids=frozenset({
        "new-gpt",
    })) is None
    assert current.get("new-gpt")["wake_at"] == clock() + 90
    assert current.admit("new-host", "model", eligible_operation_ids=frozenset({
        "new-qwen",
    }))["operation_id"] == "new-qwen"
    assert store.get("capacity_operation", "old-gpt") == saved_old
    assert {item["id"] for item in store.get(
        "capacity_quota_bridge", backup,
    )["body"]["inherited_operation_refs"]} == {"old-gpt"}
    assert store.get("capacity_quota_bridge", gpt)["body"][
        "inherited_operation_refs"] == []
    clock.advance(91)
    assert current.admit("new-host", "model", eligible_operation_ids=frozenset({
        "new-gpt",
    }))["operation_id"] == "new-gpt"


def test_unknown_old_reservation_holds_both_models_but_not_independent_domain(tmp_path):
    clock = Clock()
    legacy = "groq-selected"
    backup = "groq-verified-backup"
    gpt = model_quota_scope(backup, GPT_OSS_MODEL)
    qwen = model_quota_scope(backup, QWEN_MODEL)
    independent = model_quota_scope("groq-independent", GPT_OSS_MODEL)
    initial = CapacityPolicy(
        permits={"model": 3, "retrieval": 1, "compute": 1},
        quotas={legacy: QuotaLimit(60, requests=1, tokens=100),
                independent: QuotaLimit(60, requests=1, tokens=100)},
        review_reserve=0,
    )
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, initial)
    old.enqueue("unknown", task_id="unknown", lane="unknown", kind="model",
                quota_scopes=(legacy,), estimated_tokens=20)
    admitted = old.admit("old-host", "model")
    old.dispatched("unknown", "old-host", admitted["generation"])
    policy = _split_then_select_models(store, initial)
    current = CapacityScheduler(store, policy)
    for name, scope in (("new-gpt", gpt), ("new-qwen", qwen),
                        ("independent", independent)):
        current.enqueue(name, task_id=name, lane=name, kind="model",
                        quota_scopes=(scope,), estimated_tokens=10)
    for name in ("new-gpt", "new-qwen"):
        assert current.admit("new-host", "model", eligible_operation_ids=frozenset({
            name,
        })) is None
    assert current.admit("new-host", "model", eligible_operation_ids=frozenset({
        "independent",
    }))["operation_id"] == "independent"
    changed = store.get("capacity_operation", "unknown")
    store.transact("tamper-old-bridge-member", {}, lambda tx: tx.put(
        "capacity_operation", "unknown", {
            **changed["body"], "quota_scopes": [independent],
        }, expected_version=changed["version"],
    ))
    with pytest.raises(StateConflict, match="Inherited quota operation changed scope"):
        current.admit("new-host", "model", eligible_operation_ids=frozenset({
            "new-gpt",
        }))


def test_settled_legacy_charge_crosses_two_bridges_once(tmp_path):
    clock = Clock()
    legacy = "groq-selected"
    old_gpt = model_quota_scope(legacy, GPT_OSS_MODEL)
    backup_gpt = model_quota_scope("groq-verified-backup", GPT_OSS_MODEL)
    initial = CapacityPolicy(
        permits={"model": 2, "retrieval": 1, "compute": 1},
        quotas={legacy: QuotaLimit(60, requests=2, tokens=100),
                old_gpt: QuotaLimit(60, requests=2, tokens=100)}, review_reserve=0,
    )
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, initial)
    old.enqueue("settled", task_id="settled", lane="settled", kind="model",
                quota_scopes=(legacy, old_gpt), estimated_tokens=30)
    admitted = old.admit("old-host", "model")
    old.dispatched("settled", "old-host", admitted["generation"])
    old.settle("settled", "old-host", admitted["generation"],
               used_tokens=30, outcome="completed")
    prior = store.get("capacity_operation", "settled")
    policy = _split_then_select_models(store, initial)
    credential_bridge = store.get("capacity_quota_bridge", "groq-verified-backup")["body"]
    assert {item["id"] for item in credential_bridge["inherited_operation_refs"]} == {
        "settled",
    }
    assert {item["id"] for item in credential_bridge["child_scope_snapshot"][
        "sources"][0]["inherited_operation_refs"]} == {"settled"}
    current = CapacityScheduler(store, policy)
    current.enqueue("new-gpt", task_id="new-gpt", lane="new-gpt", kind="model",
                    quota_scopes=(backup_gpt,), estimated_tokens=10)
    # Requests=2 permits exactly the settled old call plus this new one. A
    # duplicated ancestor charge would incorrectly block the new request.
    assert current.admit("new-host", "model", eligible_operation_ids=frozenset({
        "new-gpt",
    }))["operation_id"] == "new-gpt"
    assert store.get("capacity_operation", "settled") == prior


@pytest.mark.parametrize("linked_receipt", [True, False])
def test_transitive_old_reset_needs_exact_saved_receipt(tmp_path, linked_receipt):
    clock = Clock()
    legacy = "groq-selected"
    backup_gpt = model_quota_scope("groq-verified-backup", GPT_OSS_MODEL)
    initial = CapacityPolicy(
        permits={"model": 2, "retrieval": 1, "compute": 1},
        quotas={legacy: QuotaLimit(60, requests=2, tokens=100)}, review_reserve=0,
    )
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, initial)
    old.enqueue("prior", task_id="prior", lane="prior", kind="model",
                quota_scopes=(legacy,), estimated_tokens=1,
                mission_id="mission", attempt_id="attempt-prior", call_id="call-prior")
    old.cancel("prior")
    old.observe_headers(legacy, remaining_requests=2, remaining_tokens=0,
                        token_limit=100, observed_at=clock(),
                        source_operation_id="prior", observation_id="old-unparsed-reset")

    def save_receipt(tx):
        call = tx.put("agent_call", "call-prior", {
            "mission_id": "mission", "task_id": "prior", "attempt_id": "attempt-prior",
        })
        call_ref = {"kind": "agent_call", "id": call["id"], "version": call["version"]}
        intent = tx.put("model_request_intent", "model-intent:call-prior:1", {
            "call_ref": call_ref, "request_sha256": "a" * 64,
        })
        tx.put("model_response_receipt", "model-response:model-intent:call-prior:1", {
            "call_ref": call_ref if linked_receipt else {**call_ref, "version": 2},
            "intent_ref": {"kind": intent["kind"], "id": intent["id"],
                           "version": intent["version"]},
            "request_sha256": "a" * 64,
            "mission_id": "mission", "task_id": "prior", "attempt_id": "attempt-prior",
            "quota_domain": legacy, "rate_headers": {
                "x-ratelimit-limit-tokens": "100",
                "x-ratelimit-remaining-tokens": "0",
                "x-ratelimit-reset-tokens": "1s",
            },
        })
        return {}

    store.transact("seed-old-exact-reset-receipt", {}, save_receipt)
    policy = _split_then_select_models(store, initial)
    current = CapacityScheduler(store, policy)
    current.enqueue("new-gpt", task_id="new-gpt", lane="new-gpt", kind="model",
                    quota_scopes=(backup_gpt,), estimated_tokens=10)
    assert current.admit("new-host", "model", eligible_operation_ids=frozenset({
        "new-gpt",
    })) is None
    if linked_receipt:
        assert current.get("new-gpt")["wake_at"] == clock() + 1
        clock.advance(1.001)
        assert current.admit("new-host", "model", eligible_operation_ids=frozenset({
            "new-gpt",
        }))["operation_id"] == "new-gpt"
    else:
        clock.advance(2)
        assert current.admit("new-host", "model", eligible_operation_ids=frozenset({
            "new-gpt",
        })) is None


def test_old_model_lane_is_frozen_at_credential_split_and_later_hold_flows(tmp_path):
    clock = Clock()
    legacy = "groq-selected"
    old_gpt = model_quota_scope(legacy, GPT_OSS_MODEL)
    backup = "groq-verified-backup"
    new_gpt = model_quota_scope(backup, GPT_OSS_MODEL)
    new_qwen = model_quota_scope(backup, QWEN_MODEL)
    initial = CapacityPolicy(
        permits={"model": 3, "retrieval": 1, "compute": 1},
        quotas={legacy: QuotaLimit(60, requests=2, tokens=100),
                old_gpt: QuotaLimit(60, requests=2, tokens=100)},
        review_reserve=0,
    )
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, initial)

    def seed(tx):
        tx.put("agent_configuration", "old-model-config", {
            "mission_id": "mission", "model_id": GPT_OSS_MODEL,
        })
        tx.put("agent_call", "old-model-call", {
            "mission_id": "mission", "task_id": "old-model",
            "attempt_id": "old-model-attempt", "configuration_id": "old-model-config",
            "model_id": GPT_OSS_MODEL,
        })
        return {}

    store.transact("seed-old-model-call", {}, seed)
    old.enqueue("old-model", task_id="old-model", lane="old-model", kind="model",
                quota_scopes=(old_gpt,), estimated_tokens=50, mission_id="mission",
                attempt_id="old-model-attempt", call_id="old-model-call",
                budget_version="old-model-config")
    admitted = old.admit("old-host", "model")
    old.dispatched("old-model", "old-host", admitted["generation"])
    before = store.get("capacity_operation", "old-model")
    policy = _split_then_select_models(store, initial)
    bridge = store.get("capacity_quota_bridge", backup)["body"]
    sources = bridge["child_scope_snapshot"]["sources"]
    assert bridge["child_scope_snapshot"]["version"] == 1
    assert {entry["source_scope"] for entry in sources} == {old_gpt}
    assert {pointer["id"] for pointer in sources[0]["inherited_operation_refs"]} == {
        "old-model",
    }
    assert store.get("capacity_operation", "old-model") == before
    current = CapacityScheduler(store, policy)
    current.enqueue("new-gpt", task_id="new-gpt", lane="new-gpt", kind="model",
                    quota_scopes=(new_gpt,), estimated_tokens=10)
    current.enqueue("new-qwen", task_id="new-qwen", lane="new-qwen", kind="model",
                    quota_scopes=(new_qwen,), estimated_tokens=10)
    assert current.admit("new-host", "model", eligible_operation_ids=frozenset({
        "new-gpt",
    })) is None  # Old, still active same-model reservation.
    assert current.admit("new-host", "model", eligible_operation_ids=frozenset({
        "new-qwen",
    }))["operation_id"] == "new-qwen"
    old.observe_headers(old_gpt, remaining_requests=2, remaining_tokens=0,
                        reset_after_tokens=60, observed_at=clock(),
                        source_operation_id="old-model", observation_id="old-model-later")
    updated_bridge = store.get("capacity_quota_bridge", backup)["body"]
    assert updated_bridge["child_scope_snapshot"] == bridge["child_scope_snapshot"]
    assert updated_bridge["later_quota_events"][-1]["source_scope"] == old_gpt
    old.settle("old-model", "old-host", admitted["generation"],
               used_tokens=50, outcome="completed")
    assert current.admit("new-host", "model", eligible_operation_ids=frozenset({
        "new-gpt",
    })) is None
    assert current.get("new-gpt")["wake_at"] == clock() + 60
    clock.advance(61)
    assert current.admit("new-host", "model", eligible_operation_ids=frozenset({
        "new-gpt",
    }))["operation_id"] == "new-gpt"


@pytest.mark.parametrize("alteration", [
    "old_snapshot_absent", "old_snapshot_later_head", "old_snapshot_unattributed_head",
    "new_snapshot_absent", "postcutover_head",
    "queued_old", "no_effect_old", "expired_old",
    "bad_version", "missing_member", "malformed_member", "mutation_after_cutover",
])
def test_credential_bridge_snapshot_fails_closed_only_for_frozen_old_debt(
    tmp_path, alteration,
):
    clock = Clock()
    legacy = "groq-selected"
    old_gpt = model_quota_scope(legacy, GPT_OSS_MODEL)
    new_gpt = model_quota_scope("groq-verified-backup", GPT_OSS_MODEL)
    independent = model_quota_scope("groq-independent", GPT_OSS_MODEL)
    initial = CapacityPolicy(
        permits={"model": 2, "retrieval": 1, "compute": 1},
        quotas={legacy: QuotaLimit(60, requests=2, tokens=100),
                old_gpt: QuotaLimit(60, requests=2, tokens=100),
                independent: QuotaLimit(60, requests=2, tokens=100)},
        review_reserve=0,
    )
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, initial)
    if alteration != "new_snapshot_absent":
        old.enqueue("old", task_id="old", lane="old", kind="model",
                    quota_scopes=(old_gpt,), estimated_tokens=20)
        if alteration == "no_effect_old":
            old.cancel("old")
        elif alteration != "queued_old":
            admitted = old.admit("old-host", "model")
            old.dispatched("old", "old-host", admitted["generation"])
            if alteration in {"expired_old", "old_snapshot_later_head",
                              "old_snapshot_unattributed_head", "postcutover_head"}:
                old.settle("old", "old-host", admitted["generation"],
                           used_tokens=20, outcome="completed")
                clock.advance(61)
    policy = _split_then_select_models(store, initial)
    bridge = store.get("capacity_quota_bridge", "groq-verified-backup")
    body = dict(bridge["body"])
    if alteration in {"old_snapshot_absent", "old_snapshot_later_head",
                      "old_snapshot_unattributed_head", "new_snapshot_absent",
                      "postcutover_head",
                      "queued_old", "no_effect_old", "expired_old"}:
        body.pop("child_scope_snapshot")
    elif alteration == "mutation_after_cutover":
        body["child_scope_snapshot"] = {
            **body["child_scope_snapshot"], "sources": [],
        }
    elif alteration == "bad_version":
        body["child_scope_snapshot"] = {**body["child_scope_snapshot"], "version": 99}
    elif alteration in {"missing_member", "malformed_member"}:
        sources = [dict(entry) for entry in body["child_scope_snapshot"]["sources"]]
        sources[0]["inherited_operation_refs"] = [{
            "kind": "capacity_operation",
            "id": "missing" if alteration == "missing_member" else "old",
            "version": 1 if alteration == "missing_member" else "malformed",
        }]
        body["child_scope_snapshot"] = {**body["child_scope_snapshot"], "sources": sources}
    assert bridge["version"] == 1
    if alteration == "mutation_after_cutover":
        store.transact("rewrite-frozen-credential-snapshot", {}, lambda tx: tx.put(
            "capacity_quota_bridge", bridge["id"], body,
            expected_version=bridge["version"],
        ))
    else:
        # Represent an older or malformed originally saved v1 bridge, rather
        # than a later attempted rewrite of its frozen membership.
        with sqlite3.connect(tmp_path / "state.sqlite3") as db:
            db.execute(
                "UPDATE records SET body=? WHERE kind='capacity_quota_bridge' "
                "AND id=? AND version=1",
                (canonical(body), bridge["id"]),
            )
    if alteration in {"new_snapshot_absent", "postcutover_head"}:
        clock.advance(1)
        CapacityScheduler(store, policy).enqueue(
            "later", task_id="later", lane="later", kind="model",
            quota_scopes=(old_gpt,), estimated_tokens=20,
        )
    if alteration in {"old_snapshot_later_head", "old_snapshot_unattributed_head",
                      "postcutover_head"}:
        clock.advance(1)
        CapacityScheduler(store, policy).observe_headers(
            old_gpt, remaining_requests=2, remaining_tokens=0,
            reset_after_tokens=100, observed_at=clock(),
            source_operation_id=(None if alteration == "old_snapshot_unattributed_head"
                                 else "later" if alteration == "postcutover_head" else "old"),
            observation_id="later-old-model-head",
        )
    if alteration == "old_snapshot_absent":
        separate = CapacityScheduler(store, policy)
        separate.enqueue("separate", task_id="separate", lane="separate", kind="model",
                         quota_scopes=(independent,), estimated_tokens=1)
        separate.observe_headers(independent, remaining_requests=2, remaining_tokens=100,
                                 source_operation_id="separate", observation_id="separate-head",
                                 observed_at=clock())
        separate.cancel("separate")
    current = CapacityScheduler(store, policy)
    legacy_gap = alteration in {
        "old_snapshot_absent", "old_snapshot_later_head",
        "old_snapshot_unattributed_head",
    }
    current.enqueue("new", task_id="new", lane="new", kind="model",
                    quota_scopes=(new_gpt,), estimated_tokens=10,
                    review=alteration == "old_snapshot_absent")
    if legacy_gap:
        current.enqueue("z-independent", task_id="z-independent", lane="z-independent",
                        kind="model", quota_scopes=(independent,), estimated_tokens=10)
    if alteration in {"new_snapshot_absent", "postcutover_head",
                      "queued_old", "no_effect_old", "expired_old"}:
        assert current.admit("new-host", "model", eligible_operation_ids=frozenset({
            "new",
        }))["operation_id"] == "new"
    elif legacy_gap:
        admitted = current.admit("new-host", "model")
        assert admitted["operation_id"] == "z-independent"
        held = current.get("new")
        assert held["phase"] == "waiting"
        assert held["wake_at"] is None
        assert held["diagnostic"] == "legacy_model_scope_reconciliation_needed"
        assert held["reservation"] is False and held["attempts"] == []
        if alteration == "old_snapshot_later_head":
            clock.advance(101)
            recovered = current.admit("new-host", "model", eligible_operation_ids=frozenset({
                "new",
            }))
            assert recovered["operation_id"] == "new"
            assert recovered["diagnostic"] is None
            assert recovered["attempts"] == []
    else:
        with pytest.raises(StateConflict):
            current.admit("new-host", "model", eligible_operation_ids=frozenset({
                "new",
            }))


def test_scope_uses_verified_org_domain_and_model_not_key_alias():
    org = "groq-verified-org"
    primary_domain = backup_domain = org
    gpt = model_quota_scope(primary_domain, GPT_OSS_MODEL)
    qwen_text = model_quota_scope(primary_domain, QWEN_MODEL)
    qwen_vision = model_quota_scope(backup_domain, QWEN_MODEL)
    assert gpt != qwen_text
    assert qwen_text == qwen_vision
    assert model_quota_scope("groq-other-org", QWEN_MODEL) != qwen_text
    assert model_quota_scope(org, QWEN_MODEL, legacy=True) == org
    assert "primary" not in qwen_text and "backup" not in qwen_text


@pytest.mark.parametrize(("domain", "model", "legacy"), [
    ("invalid-domain", QWEN_MODEL, False),
    ("groq-verified-org", "unknown/model", False),
    ("groq-verified-org", QWEN_MODEL, 1),
])
def test_invalid_scope_inputs_fail_closed(domain, model, legacy):
    with pytest.raises(ValueError):
        model_quota_scope(domain, model, legacy=legacy)


def test_independent_model_headers_and_waits_share_one_scheduler(tmp_path):
    clock = Clock()
    gpt = model_quota_scope("groq-verified-org", GPT_OSS_MODEL)
    qwen = model_quota_scope("groq-verified-org", QWEN_MODEL)
    store = StateStore(tmp_path, clock=clock)
    scheduler = CapacityScheduler(store, CapacityPolicy(
        permits={"model": 2, "retrieval": 1, "compute": 1},
        quotas={gpt: QuotaLimit(60, requests=2, tokens=100),
                qwen: QuotaLimit(60, requests=2, tokens=100)},
        review_reserve=0,
    ))
    for name, scope in (("a-gpt", gpt), ("b-qwen", qwen)):
        scheduler.enqueue(name, task_id=name, lane=name, kind="model",
                          quota_scopes=(scope,), estimated_tokens=10)
    first = scheduler.admit("host", "model")
    second = scheduler.admit("host", "model")
    assert {first["operation_id"], second["operation_id"]} == {"a-gpt", "b-qwen"}
    for admitted in (first, second):
        scheduler.dispatched(admitted["operation_id"], "host", admitted["generation"])
    scheduler.observe_rate_headers(
        gpt, {"x-ratelimit-remaining-tokens": "0", "x-ratelimit-limit-tokens": "100",
              "x-ratelimit-reset-tokens": "60s"},
        source_operation_id="a-gpt", observation_id="gpt-head", observed_at=clock(),
    )
    scheduler.observe_rate_headers(
        qwen, {"x-ratelimit-remaining-tokens": "80", "x-ratelimit-limit-tokens": "100",
               "x-ratelimit-reset-tokens": "60s"},
        source_operation_id="b-qwen", observation_id="qwen-head", observed_at=clock(),
    )
    assert store.get("capacity_quota", gpt)["body"]["remaining_tokens"] == 0
    assert store.get("capacity_quota", qwen)["body"]["remaining_tokens"] == 80
    for admitted in (first, second):
        scheduler.settle(admitted["operation_id"], "host", admitted["generation"],
                         used_tokens=10, outcome="completed")
    for name, scope in (("c-gpt", gpt), ("d-qwen", qwen)):
        scheduler.enqueue(name, task_id=name, lane=name, kind="model",
                          quota_scopes=(scope,), estimated_tokens=10)
    assert scheduler.admit("host", "model")["operation_id"] == "d-qwen"
    assert scheduler.get("c-gpt")["phase"] == "waiting"


def test_qwen_text_and_vision_share_consumed_org_model_budget(tmp_path):
    clock = Clock()
    qwen = model_quota_scope("groq-verified-org", QWEN_MODEL)
    scheduler = CapacityScheduler(StateStore(tmp_path, clock=clock), CapacityPolicy(
        permits={"model": 2, "retrieval": 1, "compute": 1},
        quotas={qwen: QuotaLimit(60, requests=1, tokens=100)}, review_reserve=0,
    ))
    for name in ("qwen-text", "qwen-vision"):
        scheduler.enqueue(name, task_id=name, lane=name, kind="model",
                          quota_scopes=(qwen,), estimated_tokens=10)
    first = scheduler.admit("host", "model")
    assert first["operation_id"] == "qwen-text"
    assert scheduler.admit("host", "model") is None
    scheduler.dispatched("qwen-text", "host", first["generation"])
    scheduler.settle("qwen-text", "host", first["generation"],
                     used_tokens=10, outcome="completed")
    assert scheduler.admit("host", "model") is None
    clock.advance(61)
    assert scheduler.admit("host", "model")["operation_id"] == "qwen-vision"


def test_limited_model_callback_does_not_block_other_model(tmp_path):
    clock = Clock()
    gpt = model_quota_scope("groq-verified-org", GPT_OSS_MODEL)
    qwen = model_quota_scope("groq-verified-org", QWEN_MODEL)
    called = []

    def callback(work):
        called.append(work.work_id)
        if work.work_id == "a-limited":
            return WorkResult(disposition="rate_limited", rate_scope=gpt,
                              retry_after=30, used_tokens=0)
        return WorkResult(payload={"synthetic": "completed"}, used_tokens=1)

    store, board, capacity, executor, actors, configs = foundation(
        tmp_path, model=2, clock=clock, handlers={("fixture", "1"): callback},
        quotas={gpt: QuotaLimit(60, requests=2, tokens=100),
                qwen: QuotaLimit(60, requests=2, tokens=100)},
    )
    limited = propose(board, actors[0], "a-limited")
    executor.submit(spec("a-limited", limited, actors[0], configs[0], scopes=(gpt,)))
    first = asyncio.run(executor.run_ready("mission"))
    assert first["work"]["a-limited"] == "waiting"
    healthy = propose(board, actors[1], "z-healthy")
    executor.submit(spec("z-healthy", healthy, actors[1], configs[1], scopes=(qwen,)))
    second = asyncio.run(executor.run_ready("mission"))
    assert called == ["a-limited", "z-healthy"]
    assert second["work"] == {"a-limited": "waiting", "z-healthy": "completed"}
    assert store.get("capacity_quota", gpt)["body"]["blocked_until"] > clock()
    assert capacity.get(executor._capacity_id(spec(
        "z-healthy", healthy, actors[1], configs[1], scopes=(qwen,),
    )))["phase"] == "completed"
