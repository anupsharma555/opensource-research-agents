"""A saved provider reset can free a queued call after older parsing lost it."""

import pytest

from ora.coordination.capacity import CapacityPolicy, CapacityScheduler, QuotaLimit, _reset_seconds
from ora.state.store import Transaction
from tests.coordination.test_capacity import Clock, make

SCOPE = "provider/model/account"


@pytest.mark.parametrize(("value", "seconds"), [
    ("862ms", 0.862), ("1m862ms", 60.862), ("2m59.56s", 179.56),
    ("1h2m3s4ms", 3723.004),
    ("862mss", None), ("1.2.3ms", None), ("ms", None), ("1m 2ms", None),
])
def test_duration_units_and_malformed_values(value, seconds):
    if seconds is None:
        assert _reset_seconds(value) is None
    else:
        assert _reset_seconds(value) == pytest.approx(seconds)


def test_new_ms_header_saves_its_exact_reset(tmp_path):
    clock = Clock()
    scheduler = make(tmp_path, clock)
    head = scheduler.observe_rate_headers(
        SCOPE, {"x-ratelimit-limit-tokens": "8000",
                "x-ratelimit-remaining-tokens": "7885",
                "x-ratelimit-reset-tokens": "862ms"},
        source_operation_id="new-response", observation_id="new-ms-header",
        observed_at=clock(),
    )
    assert head["tokens_reset_at"] == pytest.approx(1000.862)


def seed_legacy_head(
    scheduler, clock, *, reset="862ms", linked=True, header_remaining="7885",
    request_reset=345.6, header_request_reset="5m45.6s",
):
    scheduler.enqueue(
        "prior", task_id="task-prior", lane="prior", kind="model",
        quota_scopes=(SCOPE,), estimated_tokens=1,
        mission_id="mission-prior", attempt_id="attempt-prior", call_id="call-prior",
    )
    scheduler.cancel("prior")
    scheduler.observe_headers(
        SCOPE, remaining_requests=996, remaining_tokens=7885, token_limit=8000,
        reset_after_requests=request_reset, observed_at=clock(),
        source_operation_id="prior", observation_id="legacy-unparsed-ms",
    )

    def save(tx):
        call = tx.put("agent_call", "call-prior", {
            "mission_id": "mission-prior", "task_id": "task-prior",
            "attempt_id": "attempt-prior",
        })
        call_ref = {"kind": "agent_call", "id": call["id"], "version": call["version"]}
        intent = tx.put("model_request_intent", "model-intent:call-prior:1", {
            "call_ref": call_ref, "request_sha256": "a" * 64,
        })
        return tx.put("model_response_receipt", "model-response:model-intent:call-prior:1", {
            "call_ref": call_ref if linked else {**call_ref, "version": 2},
            "intent_ref": {"kind": intent["kind"], "id": intent["id"], "version": intent["version"]},
            "request_sha256": "a" * 64,
            "mission_id": "mission-prior", "task_id": "task-prior",
            "attempt_id": "attempt-prior", "quota_domain": SCOPE,
            "rate_headers": {
                "x-ratelimit-limit-tokens": "8000",
                "x-ratelimit-remaining-tokens": header_remaining,
                "x-ratelimit-reset-tokens": reset,
                "x-ratelimit-remaining-requests": "996",
                "x-ratelimit-reset-requests": header_request_reset,
            },
        })

    scheduler.store.transact("seed-exact-response", {}, save)
    scheduler.enqueue(
        "resumed", task_id="task-resumed", lane="resumed", kind="model",
        quota_scopes=(SCOPE,), estimated_tokens=8282,
        request_token_estimate={"tokens": 7949, "basis": "synthetic_exact_input"},
    )


def test_legacy_ms_header_wakes_only_after_exact_saved_reset(tmp_path):
    clock = Clock()
    scheduler = make(tmp_path, clock, model=2)
    seed_legacy_head(scheduler, clock)
    assert scheduler.admit("one", "model") is None
    assert scheduler.get("resumed")["wake_at"] == pytest.approx(1000.862)
    assert scheduler.get("resumed")["attempts"] == []
    clock.advance(0.861)
    assert make(tmp_path, clock, model=2).admit("one", "model") is None
    clock.advance(0.001)
    restarted = make(tmp_path, clock, model=2)
    admitted = restarted.admit("one", "model")
    assert admitted["operation_id"] == "resumed"
    assert restarted.admit("two", "model") is None  # Exclusive observed-window probe.
    assert restarted.get("resumed")["attempts"] == []  # No HTTP was simulated.


@pytest.mark.parametrize("head", [
    {"reset": "not-a-duration"}, {"linked": False}, {"header_remaining": "7884"},
])
def test_missing_or_mismatched_receipt_keeps_reset_unknown(tmp_path, head):
    clock = Clock()
    scheduler = make(tmp_path, clock)
    seed_legacy_head(scheduler, clock, **head)
    clock.advance(120)
    assert scheduler.admit("one", "model") is None
    assert scheduler.get("resumed")["phase"] == "queued"


def test_explicit_provider_hold_outlasts_recovered_token_reset(tmp_path):
    clock = Clock()
    scheduler = make(tmp_path, clock)
    seed_legacy_head(scheduler, clock)

    def hold(tx):
        record = tx.get("capacity_quota", SCOPE)
        return tx.put("capacity_quota", SCOPE, {
            **record["body"], "blocked_until": 1019,
            "explicit_block_until": 1019,
        }, expected_version=record["version"])

    scheduler.store.transact("seed-explicit-hold", {}, hold)
    clock.advance(1)
    assert scheduler.admit("one", "model") is None
    assert scheduler.get("resumed")["wake_at"] == 1019
    clock.advance(18)
    assert scheduler.admit("one", "model")["operation_id"] == "resumed"


def test_inherited_head_is_kept_until_its_recovered_reset(tmp_path):
    clock = Clock()
    old = make(tmp_path, clock)
    seed_legacy_head(old, clock, request_reset=None, header_request_reset=None)
    expanded = CapacityPolicy(
        old.policy.permits,
        {**old.policy.quotas, "new-domain": QuotaLimit(60)},
        old.policy.review_reserve,
    )
    CapacityScheduler.reconcile_additive_quota_domain(
        old.store, expanded, from_scope=SCOPE, to_scope="new-domain",
    )
    current = CapacityScheduler(old.store, expanded)
    current.enqueue(
        "new-work", task_id="new-work", lane="new-work", kind="model",
        quota_scopes=("new-domain",), estimated_tokens=10,
    )

    def inherited_count():
        db = old.store._connect()
        try:
            tx = Transaction(old.store, db)
            operations = old.store.records("capacity_operation")
            candidate = current.get("new-work")
            _, heads = current._bridged_operations(
                tx, "new-domain", candidate, operations, clock(), 60,
            )
            return len(heads)
        finally:
            db.close()

    assert inherited_count() == 1  # Candidate fits headroom; reset still keeps the bridge.
    clock.advance(0.863)
    assert inherited_count() == 0
