"""Let provider feedback resolve an oversized forecast after a durable quiet window."""

import pytest

from ora.state import StateError
from tests.coordination.test_capacity import Clock, make

SCOPE = "provider/model/account"


def forecast(scheduler, name, demand=8700):
    return scheduler.enqueue(
        name, task_id=name, lane=name, kind="model", quota_scopes=(SCOPE,),
        estimated_tokens=demand,
        request_token_estimate={"tokens": min(6727, demand), "basis": "synthetic_exact_input"},
    )


def observe(scheduler, clock, remaining=500, reset=60, limit=8000):
    scheduler.observe_headers(
        SCOPE, remaining_requests=100, remaining_tokens=remaining,
        token_limit=limit, reset_after_tokens=reset, observed_at=clock(),
    )


def test_oversized_forecast_waits_then_runs_exclusively_across_restart(tmp_path):
    clock = Clock()
    scheduler = make(tmp_path, clock, model=2)
    observe(scheduler, clock)
    forecast(scheduler, "large")
    forecast(scheduler, "other", 100)
    selected = frozenset({"large"})
    assert scheduler.admit("one", "model", eligible_operation_ids=selected) is None
    assert scheduler.get("large")["wake_at"] == clock() + 60
    assert scheduler.get("large")["attempts"] == []
    clock.advance(60)
    restarted = make(tmp_path, clock, model=2)
    admitted = restarted.admit("one", "model", eligible_operation_ids=selected, lease_seconds=180)
    assert admitted["operation_id"] == "large"
    assert admitted["estimated_tokens"] == 8700  # Forecast is never falsified.
    assert restarted.admit("two", "model") is None
    restarted.dispatched("large", "one", admitted["generation"])
    clock.advance(61)
    assert restarted.admit("two", "model") is None  # Still in flight after a minute.
    restarted.settle("large", "one", admitted["generation"], used_tokens=8180, outcome="completed")
    assert restarted.get("large")["attempts"][0]["charged_tokens"] == 8180
    assert restarted.admit("two", "model")["operation_id"] == "other"


def test_provider_refusal_keeps_longer_wait_and_unknown_charge(tmp_path):
    clock = Clock()
    scheduler = make(tmp_path, clock)
    observe(scheduler, clock, remaining=8000)
    forecast(scheduler, "large")
    admitted = scheduler.admit("one", "model")
    scheduler.dispatched("large", "one", admitted["generation"])
    scheduler.rate_limited("large", "one", admitted["generation"], scope=SCOPE, retry_after=120)
    observe(scheduler, clock, remaining=8000, reset=1)
    clock.advance(60)
    restarted = make(tmp_path, clock)
    assert restarted.admit("two", "model") is None
    assert restarted.get("large")["attempts"][0]["usage_known"] is False
    assert restarted.get("large")["attempts"][0]["charged_tokens"] == 8700
    clock.advance(60)
    assert restarted.admit("two", "model")["operation_id"] == "large"
    assert len(restarted.get("large")["attempts"]) == 1


def test_explicit_local_ceiling_still_rejects_forecast(tmp_path):
    clock = Clock()
    scheduler = make(tmp_path, clock, tokens=8000)
    observe(scheduler, clock, remaining=16000, limit=16000)
    with pytest.raises(StateError, match="configured token ceiling"):
        forecast(scheduler, "large")
    assert scheduler.store.records("capacity_operation") == []


def test_probe_waits_for_existing_reservation_even_with_full_header(tmp_path):
    clock = Clock()
    scheduler = make(tmp_path, clock, model=2)
    observe(scheduler, clock, remaining=8000)
    forecast(scheduler, "small", 100)
    small = scheduler.admit("one", "model")
    forecast(scheduler, "large")
    assert scheduler.admit("two", "model") is None
    scheduler.dispatched("small", "one", small["generation"])
    scheduler.settle("small", "one", small["generation"], used_tokens=100, outcome="completed")
    assert scheduler.admit("two", "model") is None
    clock.advance(60)
    assert scheduler.admit("two", "model")["operation_id"] == "large"
