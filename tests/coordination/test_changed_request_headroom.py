"""A provider retry time does not admit a larger request without rechecking capacity.

These controlled-clock cases combine changed request sizes, a shared provider wait,
and another worker's reservation. They establish local admission behavior only;
they do not infer the provider's daily reset or unobserved account consumption.
"""

from tests.coordination.test_capacity import Clock, make, queue


def test_changed_demand_rechecks_fresh_headroom_after_long_wait_and_restart(tmp_path):
    clock = Clock()
    scheduler = make(tmp_path, clock, model=2)
    scope = "provider/model/account"
    queue(scheduler, "original", tokens=80)
    original = scheduler.admit("original-owner", "model")
    scheduler.dispatched("original", "original-owner", original["generation"])
    scheduler.rate_limited(
        "original", "original-owner", original["generation"],
        scope=scope, retry_after=1796,
    )
    queue(scheduler, "larger", tokens=300)
    queue(scheduler, "peer", tokens=250)
    queue(scheduler, "smaller", tokens=200)

    clock.advance(1795)
    scheduler.observe_headers(
        scope, remaining_requests=20, remaining_tokens=500, token_limit=8000,
        reset_after_requests=3600, reset_after_tokens=60,
        observed_at=clock(), observation_id="fresh-observation",
    )
    assert scheduler.admit(
        "peer-owner", "model", eligible_operation_ids=frozenset({"peer"}),
    ) is None

    clock.advance(1)
    restarted = make(tmp_path, clock, model=2)
    peer = restarted.admit(
        "peer-owner", "model", eligible_operation_ids=frozenset({"peer"}),
    )
    assert peer["operation_id"] == "peer"
    assert restarted.get("peer")["reservation"] is True
    assert restarted.admit(
        "larger-owner", "model", eligible_operation_ids=frozenset({"larger"}),
    ) is None
    assert restarted.get("larger")["wake_at"] == 1000 + 1795 + 60
    assert restarted.get("larger")["attempts"] == []

    # A genuinely smaller candidate can use the remaining observed allowance.
    smaller = restarted.admit(
        "smaller-owner", "model", eligible_operation_ids=frozenset({"smaller"}),
    )
    assert smaller["operation_id"] == "smaller"
    assert restarted.get("smaller")["estimated_tokens"] == 200
    assert restarted.get("larger")["estimated_tokens"] == 300
    assert restarted.get("original")["estimated_tokens"] == 80
    assert len(restarted.get("original")["attempts"]) == 1

    observation = restarted.store.get("capacity_quota", scope)["body"]
    assert observation["observed_at"] == 1000 + 1795
    assert observation["remaining_tokens"] == 500
    assert observation["explicit_block_until"] == 1000 + 1796
    restarted.enqueue("source-read", task_id="source-task", lane="read", kind="retrieval")
    assert restarted.admit("source-owner", "retrieval")["operation_id"] == "source-read"


def test_rising_daily_refusal_demand_keeps_each_wait_and_attempt_after_restart(tmp_path):
    clock = Clock()
    scheduler = make(tmp_path, clock, model=2)
    scope = "provider/model/account"
    originals = []
    for index, (demand, delay) in enumerate(((4500, 1796), (5100, 245), (5800, 312))):
        operation = f"changed-request-{index}"
        # The fresh exact request owns its estimate; the previous refused size
        # and a full minute bucket do not establish current daily headroom.
        queue(scheduler, operation, tokens=demand)
        selected = frozenset({operation})
        admitted = scheduler.admit("owner", "model", eligible_operation_ids=selected)
        assert admitted["operation_id"] == operation
        scheduler.dispatched(operation, "owner", admitted["generation"])
        scheduler.observe_rate_headers(
            scope, {"x-ratelimit-limit-tokens": "8000",
                    "x-ratelimit-remaining-tokens": "8000",
                    "x-ratelimit-reset-tokens": "1m"},
            source_operation_id=operation, observation_id=f"refusal-{index}",
            observed_at=clock(),
        )
        scheduler.rate_limited(
            operation, "owner", admitted["generation"], scope=scope, retry_after=delay,
        )
        originals.append((operation, demand))
        assert scheduler.get(operation)["attempts"][0]["usage_known"] is False
        wake = clock() + delay
        restarted = make(tmp_path, clock, model=2)
        clock.advance(delay - 1)
        assert restarted.admit("owner", "model", eligible_operation_ids=selected) is None
        assert restarted.store.get("capacity_quota", scope)["body"]["explicit_block_until"] == wake
        for previous, estimate in originals:
            record = restarted.get(previous)
            assert record["estimated_tokens"] == estimate
            assert len(record["attempts"]) == 1
            assert record["attempts"][0]["status"] == "rate_limited"
        clock.advance(1)
        scheduler = restarted
