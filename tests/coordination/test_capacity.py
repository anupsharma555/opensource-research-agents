"""Clock-controlled admission and recovery fixtures; no provider is called."""

import hashlib
import json
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from multiprocessing import get_context

import pytest

from ora.coordination.capacity import CapacityPolicy, CapacityScheduler, QuotaLimit
from ora.state.store import StateConflict, StateError, StateStore


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def make(tmp_path, clock, *, model=1, retrieval=1, compute=1, requests=None, tokens=None):
    store = StateStore(tmp_path, clock=clock)
    policy = CapacityPolicy(
        permits={"model": model, "retrieval": retrieval, "compute": compute},
        quotas={"provider/model/account": QuotaLimit(60, requests, tokens)},
    )
    return CapacityScheduler(store, policy)


def queue(scheduler, name, *, lane="a", tokens=3, review=False):
    return scheduler.enqueue(name, task_id=f"task-{name}", lane=lane, kind="model",
                             quota_scopes=("provider/model/account",), estimated_tokens=tokens,
                             review=review)


def _process_admit(root, owner):
    scheduler = make(root, Clock(), model=2, requests=1)
    return scheduler.admit(owner, "model")


def test_ten_workers_queue_at_one_model_while_retrieval_and_compute_overlap(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    for i in range(10):
        queue(s, f"m{i}")
    s.enqueue("r", task_id="r", lane="search", kind="retrieval")
    s.enqueue("c", task_id="c", lane="compute", kind="compute")
    first = s.admit("host", "model")
    assert first is not None
    assert s.admit("host", "model") is None
    assert s.admit("host", "retrieval")["operation_id"] == "r"
    assert s.admit("host", "compute")["operation_id"] == "c"
    s.dispatched(first["operation_id"], "host", first["generation"])
    s.settle(first["operation_id"], "host", first["generation"], used_tokens=2, outcome="completed")
    assert s.admit("host", "model") is not None
    assert sum(s.get(f"m{i}")["phase"] == "queued" for i in range(10)) == 8


def test_two_connections_compete_for_last_permit_and_allowance(tmp_path):
    clock = Clock()
    left = make(tmp_path, clock, model=2, requests=1)
    right = make(tmp_path, clock, model=2, requests=1)
    queue(left, "a")
    queue(left, "b")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: pair[0].admit(pair[1], "model"), [(left, "one"), (right, "two")]))
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result)
    assert left.get(winner["operation_id"])["reservation"] is True


def test_two_processes_share_final_request_allowance(tmp_path):
    clock = Clock()
    scheduler = make(tmp_path, clock, model=2, requests=1)
    queue(scheduler, "a")
    queue(scheduler, "b")
    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as pool:
        futures = [pool.submit(_process_admit, tmp_path, owner) for owner in ("one", "two")]
        results = [future.result() for future in futures]
    assert sum(item is not None for item in results) == 1


def test_queue_active_and_retry_waits_are_separate_clock_measurements(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    queue(s, "timed")
    clock.advance(2)
    first = s.admit("host", "model")
    assert first["queue_wait_seconds"] == 2
    s.dispatched("timed", "host", first["generation"])
    clock.advance(3)
    s.transport_failure("timed", "host", first["generation"],
                        classification="temporary_no_effect", retry_after=5)
    clock.advance(5)
    second = s.admit("host", "model")
    assert second["retry_wait_seconds"] == 5
    assert second["active_seconds"] == 3
    assert second["provider_wait_seconds"] == 0
    s.dispatched("timed", "host", second["generation"])
    clock.advance(2)
    finished = s.settle("timed", "host", second["generation"],
                        used_tokens=1, outcome="completed")
    assert finished["active_seconds"] == 5
    assert finished["queue_wait_seconds"] == 2


def test_crash_before_dispatch_releases_reservation_then_old_owner_is_fenced(tmp_path):
    clock = Clock()
    first = make(tmp_path, clock, requests=1)
    queue(first, "a")
    original = first.admit("old", "model", lease_seconds=5)
    clock.advance(6)
    restarted = make(tmp_path, clock, requests=1)
    assert restarted.expire() == [{"operation_id": "a", "phase": "queued"}]
    replacement = restarted.admit("new", "model")
    assert replacement["generation"] > original["generation"]
    with pytest.raises(StateConflict):
        first.dispatched("a", "old", original["generation"])


def test_429_wait_is_durable_and_releases_permit(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    queue(s, "limited")
    queue(s, "other", tokens=0)
    a = s.admit("host", "model")
    s.dispatched("limited", "host", a["generation"])
    waiting = s.rate_limited("limited", "host", a["generation"], scope="provider/model/account", retry_after=15)
    assert waiting["phase"] == "waiting" and not waiting["reservation"]
    assert waiting["attempts"][0]["charged_tokens"] == 3
    assert waiting["attempts"][0]["usage_known"] is False
    assert s.due() == []
    restarted = make(tmp_path, clock)
    assert restarted.admit("host", "model") is None  # Shared provider scope still blocked.
    restarted.enqueue("retrieval", task_id="retrieval", lane="search", kind="retrieval")
    assert restarted.admit("host", "retrieval")["operation_id"] == "retrieval"
    clock.advance(15)
    assert restarted.rate_limited("limited", "host", a["generation"],
                                  scope="provider/model/account", retry_after=15)["phase"] == "waiting"
    assert restarted.due() == ["limited", "other"]
    assert restarted.due() == ["limited", "other"]
    resumed = restarted.admit("new", "model")
    assert resumed["operation_id"] == "limited"
    assert len(resumed["attempts"]) == 1


def test_oversized_request_is_local_and_other_work_continues(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock, tokens=4)
    with pytest.raises(StateError):
        queue(s, "too-large", tokens=5)
    queue(s, "valid", tokens=4)
    assert s.admit("host", "model")["operation_id"] == "valid"


def test_uncertain_external_effect_retains_capacity_and_rejects_late_owner(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    queue(s, "slow")
    clock.advance(1)
    queue(s, "replacement")
    a = s.admit("old", "model", lease_seconds=5)
    s.dispatched("slow", "old", a["generation"])
    clock.advance(6)
    assert s.expire() == [{"operation_id": "slow", "phase": "uncertain"}]
    assert s.get("slow")["external_active"] is True
    assert s.get("slow")["local_active"] is False
    assert s.admit("new", "model") is None
    with pytest.raises(StateConflict):
        s.settle("slow", "old", a["generation"], used_tokens=3, outcome="completed")
    claimed = s.claim_uncertain("slow", "reconciler")
    s.reconcile("slow", "reconciler", claimed["generation"], effect="completed", used_tokens=2, output_ref="result:1")
    assert s.admit("new", "model")["operation_id"] == "replacement"
    assert s.get("slow")["attempts"][0]["charged_tokens"] == 2


def test_cancel_queued_or_waiting_work_without_canceling_other_branch(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    queue(s, "cancel")
    queue(s, "continue")
    assert s.cancel("cancel")["phase"] == "canceled"
    assert s.admit("host", "model")["operation_id"] == "continue"


def test_review_and_quiet_lane_progress_under_noisy_queue(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock, model=2)
    for i in range(8):
        queue(s, f"noise-{i}", lane="noise")
    queue(s, "quiet", lane="quiet")
    queue(s, "review", lane="review", review=True)
    first = s.admit("one", "model")
    second = s.admit("two", "model")
    assert first["operation_id"] == "review"
    assert second["operation_id"] == "noise-0"
    for item in (first, second):
        s.dispatched(item["operation_id"], item["owner"], item["generation"])
        s.settle(item["operation_id"], item["owner"], item["generation"], used_tokens=1, outcome="completed")
    following = s.admit("three", "model")
    assert following["operation_id"] == "quiet"


def test_old_large_request_wins_shared_refill_before_newer_lanes(tmp_path):
    clock = Clock()
    scheduler = make(tmp_path, clock, model=2)
    for lane, count in (("older", 63), ("newer", 48)):
        record = queue(scheduler, f"history-{lane}", lane=lane)

        def finish_history(tx):
            current = tx.get("capacity_operation", f"history-{lane}")
            return tx.put("capacity_operation", current["id"], {
                **current["body"], "phase": "completed",
                "attempts": [
                    {"at": clock() - 61, "charged_tokens": 1, "status": "completed"}
                    for _ in range(count)
                ],
            }, expected_version=current["version"])

        scheduler.store.transact(f"seed-history-{lane}", {}, finish_history)
        assert record["lane"] == lane
    scheduler.observe_rate_headers(
        "provider/model/account",
        {"x-ratelimit-limit-tokens": "8000",
         "x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "1s"},
        source_operation_id="prior-receipt", observation_id="shared-refill",
        observed_at=clock(),
    )
    queue(scheduler, "older-work", lane="older", tokens=6527)
    clock.advance(0.1)
    queue(scheduler, "newer-work", lane="newer", tokens=6968)
    clock.advance(0.1)
    queue(scheduler, "new-lane-work", lane="new-lane", tokens=1000)
    assert scheduler.admit("one", "model") is None
    clock.advance(0.8)
    first = scheduler.admit("one", "model")
    assert first["operation_id"] == "older-work"
    scheduler.dispatched("older-work", "one", first["generation"])
    next_lane = scheduler.admit("two", "model")
    assert next_lane["operation_id"] == "new-lane-work"
    scheduler.dispatched("new-lane-work", "two", next_lane["generation"])
    scheduler.settle("older-work", "one", first["generation"],
                     used_tokens=6527, outcome="completed")
    scheduler.settle("new-lane-work", "two", next_lane["generation"],
                     used_tokens=1000, outcome="completed")
    assert scheduler.admit("three", "model") is None
    clock.advance(60)
    assert make(tmp_path, clock, model=2).admit("three", "model")["operation_id"] == "newer-work"


def test_recent_token_use_outweighs_recent_call_count(tmp_path):
    clock = Clock()
    scheduler = make(tmp_path, clock)
    for lane, charges in (("heavy", (5000,)), ("light", (1000, 1000))):
        for index, charge in enumerate(charges):
            name = f"past-{lane}-{index}"
            queue(scheduler, name, lane=lane, tokens=charge)
            admitted = scheduler.admit("host", "model")
            assert admitted["operation_id"] == name
            scheduler.dispatched(name, "host", admitted["generation"])
            scheduler.settle(name, "host", admitted["generation"],
                             used_tokens=charge, outcome="completed")
    queue(scheduler, "older-heavy", lane="heavy")
    clock.advance(0.1)
    queue(scheduler, "newer-light", lane="light")
    assert scheduler.admit("host", "model")["operation_id"] == "newer-light"


def test_review_reserve_applies_across_missions_and_blocked_review_does_not_idle(tmp_path):
    clock = Clock()
    store = StateStore(tmp_path, clock=clock)
    scheduler = CapacityScheduler(store, CapacityPolicy(
        permits={"model": 2, "retrieval": 1, "compute": 1},
        quotas={"normal": QuotaLimit(60, requests=10, tokens=100),
                "review": QuotaLimit(60, requests=10, tokens=100)},
        review_reserve=1))
    for name in ("normal-1", "normal-2"):
        scheduler.enqueue(name, task_id=name, lane=name, kind="model",
                          mission_id="mission-a", quota_scopes=("normal",),
                          estimated_tokens=1)
    scheduler.enqueue("review-b", task_id="review-b", lane="review", kind="model",
                      mission_id="mission-b", quota_scopes=("review",),
                      estimated_tokens=1, review=True)
    first = scheduler.admit("host-a", "model", mission_id="mission-a")
    assert first["operation_id"] == "normal-1"
    assert scheduler.admit("host-a", "model", mission_id="mission-a") is None
    assert scheduler.admit("host-b", "model", mission_id="mission-b")["operation_id"] == "review-b"

    other = StateStore(tmp_path / "blocked", clock=clock)
    scheduler = CapacityScheduler(other, scheduler.policy)
    for name in ("normal-1", "normal-2"):
        scheduler.enqueue(name, task_id=name, lane=name, kind="model",
                          mission_id="mission-a", quota_scopes=("normal",),
                          estimated_tokens=1)
    scheduler.enqueue("review-b", task_id="review-b", lane="review", kind="model",
                      mission_id="mission-b", quota_scopes=("review",),
                      estimated_tokens=1, review=True)
    scheduler.observe_headers("review", remaining_requests=0,
                              remaining_tokens=100, reset_after=20)
    assert scheduler.admit("host-a", "model", mission_id="mission-a")["operation_id"] == "normal-1"
    assert scheduler.admit("host-a", "model", mission_id="mission-a")["operation_id"] == "normal-2"


def test_same_mission_review_excluded_by_readiness_filter_does_not_reserve(tmp_path):
    clock = Clock()
    scheduler = make(tmp_path, clock, model=1)
    scheduler.enqueue("normal", task_id="normal", lane="normal", kind="model",
                      mission_id="mission", quota_scopes=("provider/model/account",),
                      estimated_tokens=1)
    scheduler.enqueue("unready-review", task_id="review", lane="review", kind="model",
                      mission_id="mission", quota_scopes=("provider/model/account",),
                      estimated_tokens=1, review=True)
    admitted = scheduler.admit("host", "model", mission_id="mission",
                               eligible_operation_ids=frozenset({"normal"}))
    assert admitted["operation_id"] == "normal"


def test_request_and_token_usage_reconciled_per_call_across_window(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock, requests=2, tokens=10)
    queue(s, "initial", tokens=6)
    clock.advance(1)
    queue(s, "continuation", tokens=6)
    initial = s.admit("host", "model")
    s.dispatched("initial", "host", initial["generation"])
    assert s.admit("host", "model") is None
    s.settle("initial", "host", initial["generation"], used_tokens=3,
             cached_tokens=1, outcome="completed")
    assert s.get("initial")["attempts"][0]["charged_tokens"] == 3
    assert s.get("initial")["attempts"][0]["cached_tokens"] == 1
    continuation = s.admit("host", "model")
    assert continuation["operation_id"] == "continuation"
    s.dispatched("continuation", "host", continuation["generation"])
    s.settle("continuation", "host", continuation["generation"], used_tokens=4, outcome="completed")
    queue(s, "repair", tokens=1)
    assert s.admit("host", "model") is None
    clock.advance(61)
    assert s.admit("host", "model")["operation_id"] == "repair"


def test_local_timeout_keeps_external_capacity_until_confirmed_no_effect(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    queue(s, "timed-out")
    queue(s, "next")
    first = s.admit("old", "model")
    s.dispatched(first["operation_id"], "old", first["generation"])
    timed_out = s.timeout(first["operation_id"], "old", first["generation"])
    assert timed_out["phase"] == "uncertain"
    assert timed_out["external_active"] and not timed_out["local_active"]
    assert s.admit("new", "model") is None
    with pytest.raises(StateConflict):
        s.settle(first["operation_id"], "old", first["generation"], used_tokens=1, outcome="completed")
    reconciler = s.claim_uncertain(first["operation_id"], "new")
    s.reconcile(first["operation_id"], "new", reconciler["generation"], effect="no_effect")
    assert s.admit("new", "model") is not None


def test_atomic_result_acceptance_and_capacity_release(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    queue(s, "call")
    admitted = s.admit("host", "model")
    s.dispatched("call", "host", admitted["generation"])

    def accept(tx):
        tx.put("accepted_result", "result-1", {"value": "fixture"})
        s.settle_tx(tx, "call", "host", admitted["generation"], used_tokens=2,
                    outcome="completed", output_ref="result-1")
        return {"accepted": True}

    assert s.store.transact("accept-fixture", {"id": "result-1"}, accept) == {"accepted": True}
    assert s.store.get("accepted_result", "result-1") is not None
    assert s.get("call")["phase"] == "completed"


def test_bounded_429_retries_and_observed_zero_headroom(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    queue(s, "retry", tokens=1)
    s.observe_headers("provider/model/account", remaining_requests=0,
                      remaining_tokens=None, reset_after=5)
    assert s.admit("host", "model") is None
    assert s.get("retry")["phase"] == "waiting"
    clock.advance(5)
    for attempt in range(3):
        admitted = s.admit("host", "model")
        assert admitted is not None
        s.dispatched("retry", "host", admitted["generation"])
        result = s.rate_limited("retry", "host", admitted["generation"],
                                scope="provider/model/account", retry_after=1,
                                used_tokens=0)
        if attempt < 2:
            assert result["phase"] == "waiting"
            clock.advance(1)
        else:
            assert result["phase"] == "failed"
            assert result["diagnostic"] == "transport_retry_limit"
    assert len(s.get("retry")["attempts"]) == 3
    assert s.admit("host", "model") is None


def test_observed_low_token_headroom_waits_for_its_reset_then_expires(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    queue(s, "completed", tokens=3000)
    first = s.admit("host", "model")
    s.dispatched("completed", "host", first["generation"])
    s.settle("completed", "host", first["generation"], used_tokens=2852, outcome="completed")
    s.observe_rate_headers(
        "provider/model/account",
        {
            "x-ratelimit-remaining-requests": "997",
            "x-ratelimit-reset-requests": "4m19.2s",
            "x-ratelimit-remaining-tokens": "80",
            "x-ratelimit-reset-tokens": "59.4s",
        },
        source_operation_id="completed",
        observation_id="receipt-completed-v1",
        observed_at=clock(),
    )
    queue(s, "next", tokens=3000)
    assert s.admit("host", "model") is None
    assert s.get("next")["wake_at"] == pytest.approx(clock() + 59.4)
    s.enqueue("unaffected", task_id="other", lane="search", kind="retrieval")
    assert s.admit("host", "retrieval")["operation_id"] == "unaffected"
    restarted = make(tmp_path, clock)
    assert restarted.admit("host", "model") is None
    clock.advance(59.4)
    assert restarted.admit("host", "model")["operation_id"] == "next"


def test_observed_request_and_token_clocks_and_retry_after_are_independent(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    queue(s, "blocked", tokens=3)
    s.observe_rate_headers(
        "provider/model/account",
        {
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-reset-requests": "2m",
            "x-ratelimit-remaining-tokens": "100",
            "x-ratelimit-reset-tokens": "5s",
        },
        source_operation_id="earlier",
        observation_id="request-empty",
        observed_at=clock(),
    )
    assert s.admit("host", "model") is None
    assert s.get("blocked")["wake_at"] == clock() + 120
    clock.advance(120)
    admitted = s.admit("host", "model")
    s.dispatched("blocked", "host", admitted["generation"])
    s.observe_rate_headers(
        "provider/model/account",
        {
            "x-ratelimit-remaining-requests": "500",
            "x-ratelimit-reset-requests": "3m",
            "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-reset-tokens": "5s",
        },
        source_operation_id="blocked",
        observation_id="token-empty",
        observed_at=clock(),
    )
    s.rate_limited(
        "blocked", "host", admitted["generation"],
        scope="provider/model/account", retry_after=20,
    )
    queue(s, "following", tokens=1)
    assert s.admit("host", "model") is None
    assert s.get("following")["wake_at"] == clock() + 20
    clock.advance(5)
    assert s.admit("host", "model") is None
    clock.advance(15)
    assert s.admit("host", "model")["operation_id"] == "blocked"


def test_concurrent_reservations_consume_observed_headroom_across_restart(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock, model=2)
    s.observe_headers(
        "provider/model/account",
        remaining_requests=10,
        remaining_tokens=500,
        reset_after_requests=3600,
        reset_after_tokens=30,
        observed_at=clock(),
        observation_id="headroom",
    )
    queue(s, "a", tokens=300)
    queue(s, "b", tokens=300)
    first = s.admit("one", "model")
    assert first["operation_id"] == "a"
    other = make(tmp_path, clock, model=2)
    assert other.admit("two", "model") is None
    assert other.get("b")["wake_at"] == clock() + 30
    other.dispatched("a", "one", first["generation"])
    assert other.admit("two", "model") is None
    clock.advance(30)
    assert other.admit("two", "model")["operation_id"] == "b"


def test_observed_token_limit_still_bounds_shared_admission_after_reset(tmp_path):
    clock = Clock()
    scheduler = make(tmp_path, clock, model=2)
    scheduler.observe_rate_headers(
        "provider/model/account",
        {
            "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-remaining-tokens": "3674",
            "x-ratelimit-reset-tokens": "1s",
            "x-ratelimit-remaining-requests": "995",
            "x-ratelimit-reset-requests": "7m",
        },
        source_operation_id="earlier-completed-call",
        observation_id="observed-8000-tpm",
        observed_at=clock(),
    )
    queue(scheduler, "first", tokens=6760)
    queue(scheduler, "second", tokens=4477)
    assert scheduler.admit("one", "model") is None
    clock.advance(1)
    restarted = make(tmp_path, clock, model=2)
    first = restarted.admit("one", "model")
    assert first["operation_id"] == "first"
    assert restarted.admit("two", "model") is None
    restarted.dispatched("first", "one", first["generation"])
    assert restarted.admit("two", "model") is None
    assert restarted.get("second")["wake_at"] == pytest.approx(clock() + 60)
    restarted.settle("first", "one", first["generation"],
                     used_tokens=6760, outcome="completed")
    clock.advance(60)
    assert make(tmp_path, clock, model=2).admit("two", "model")["operation_id"] == "second"


def test_fresh_observed_token_limit_uses_new_receipt_identity(tmp_path):
    clock = Clock()
    scheduler = make(tmp_path, clock)
    old = scheduler.observe_rate_headers(
        "provider/model/account",
        {"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "1s"},
        source_operation_id="prior", observation_id="replayed-prior", observed_at=clock(),
    )
    enhanced = scheduler.observe_rate_headers(
        "provider/model/account",
        {"x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "0",
         "x-ratelimit-reset-tokens": "1s"},
        source_operation_id="prior", observation_id="replayed-prior", observed_at=clock(),
    )
    assert old.get("observed_token_limit") is None
    assert enhanced["observed_token_limit"] == 8000
    assert scheduler.observe_rate_headers(
        "provider/model/account",
        {"x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "0",
         "x-ratelimit-reset-tokens": "1s"},
        source_operation_id="prior", observation_id="replayed-prior", observed_at=clock(),
    ) == enhanced


def test_prior_quota_head_recovers_observed_limit_from_exact_saved_response(tmp_path):
    clock = Clock()
    scheduler = make(tmp_path, clock, model=2)
    scheduler.enqueue(
        "historical", task_id="earlier", lane="model", kind="model",
        quota_scopes=("provider/model/account",), estimated_tokens=1,
        call_id="call-historical",
    )
    scheduler.cancel("historical")

    def seed(tx):
        call = tx.put("agent_call", "call-historical", {"synthetic": True})
        tx.put("model_response_receipt", "model-response:model-intent:call-historical:1", {
            "call_ref": {"kind": call["kind"], "id": call["id"], "version": call["version"]},
            "rate_headers": {"x-ratelimit-limit-tokens": "8000"},
        })
        return tx.put("capacity_quota", "provider/model/account", {
            "blocked_until": 0, "observed_at": clock(),
            "source_operation_id": "historical",
            "remaining_requests": 995, "requests_reset_at": clock() + 420,
            "remaining_tokens": 3674, "tokens_reset_at": clock() + 1,
        })

    scheduler.store.transact("seed-old-quota-head", {}, seed)
    queue(scheduler, "first", tokens=6760)
    queue(scheduler, "second", tokens=4477)
    clock.advance(1)
    first = scheduler.admit("one", "model")
    assert first["operation_id"] == "first"
    assert scheduler.admit("two", "model") is None
    updated = scheduler.observe_headers(
        "provider/model/account", remaining_requests=994, remaining_tokens=5000,
        reset_after_tokens=30, observed_at=clock(), observation_id="new-head-without-limit",
    )
    assert updated["observed_token_limit"] == 8000


def test_replayed_or_expired_header_does_not_refresh_provider_wait(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    old = s.observe_rate_headers(
        "provider/model/account",
        {"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "5s"},
        source_operation_id="old",
        observation_id="old-receipt",
        observed_at=clock(),
    )
    clock.advance(2)
    new = s.observe_rate_headers(
        "provider/model/account",
        {"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "10s"},
        source_operation_id="new",
        observation_id="new-receipt",
        observed_at=clock(),
    )
    assert new["tokens_reset_at"] == 1012
    assert s.observe_rate_headers(
        "provider/model/account",
        {"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "5s"},
        source_operation_id="old",
        observation_id="old-receipt",
        observed_at=1000.0,
    ) == old
    assert s.store.get("capacity_quota", "provider/model/account")["body"] == new
    queue(s, "after", tokens=1)
    assert s.admit("host", "model") is None
    clock.advance(10)
    assert s.admit("host", "model")["operation_id"] == "after"


def test_long_provider_retry_after_survives_restart_without_blocking_retrieval(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    queue(s, "daily-limited", tokens=10)
    admitted = s.admit("host", "model")
    s.dispatched("daily-limited", "host", admitted["generation"])
    s.rate_limited(
        "daily-limited", "host", admitted["generation"],
        scope="provider/model/account", retry_after=1796,
    )
    for observation in ("sibling-success-1", "sibling-success-2"):
        s.observe_rate_headers(
            "provider/model/account",
            {
                "x-ratelimit-remaining-requests": "500",
                "x-ratelimit-reset-requests": "1m",
                "x-ratelimit-remaining-tokens": "3000",
                "x-ratelimit-reset-tokens": "5s",
            },
            source_operation_id=observation,
            observation_id=observation,
            observed_at=clock(),
        )
        assert s.store.get("capacity_quota", "provider/model/account")["body"][
            "blocked_until"
        ] == 1000 + 1796
        clock.advance(1)
    restarted = make(tmp_path, clock)
    restarted.enqueue("source-search", task_id="source-search", lane="research", kind="retrieval")
    assert restarted.admit("host", "retrieval")["operation_id"] == "source-search"
    assert restarted.admit("host", "model") is None
    clock.advance(1793)
    assert restarted.admit("host", "model") is None
    clock.advance(1)
    assert restarted.admit("host", "model")["operation_id"] == "daily-limited"


def test_restart_reestimates_only_legacy_queued_model_from_exact_saved_intent(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    request = b"x" * 4000
    blob = s.store.put_blob(request)
    work_id = "historical-work"
    capacity_id = "executor-cap:" + hashlib.sha256(
        json.dumps(["mission", work_id], separators=(",", ":")).encode()
    ).hexdigest()

    def save_fixture(tx):
        call = tx.put("agent_call", "historical-call", {"mission_id": "mission"})
        tx.put("executor_work", "historical-executor-work", {
            "spec": {
                "mission_id": "mission", "run_id": "run", "work_id": work_id,
                "call_id": call["id"], "call_version": call["version"],
                "attempt_id": "attempt", "proposal_ref": {"id": "task"},
                "configuration_ref": {"id": "configuration"},
            }
        })
        tx.put(
            "model_request_intent", "model-intent:historical-call:1",
            {
                "call_ref": {"kind": call["kind"], "id": call["id"], "version": call["version"]},
                "mission_id": "mission", "task_id": "task", "attempt_id": "attempt",
                "parent_call_ref": None,
                "request_sha256": hashlib.sha256(request).hexdigest(),
                "blob": blob,
            },
        )
        return {"saved": True}

    s.store.transact("historical-model-fixture", {}, save_fixture)
    s.enqueue(
        capacity_id, task_id="task", lane="agent", kind="model",
        quota_scopes=("provider/model/account",), estimated_tokens=8192,
        call_id="historical-call", mission_id="mission", run_id="run",
        attempt_id="attempt", budget_version="configuration",
    )

    def mark_historical(tx):
        record = tx.get("capacity_operation", capacity_id)
        body = dict(record["body"])
        body.pop("estimate_basis")
        tx.put("capacity_operation", capacity_id, body, expected_version=record["version"])
        return {"historical": True}

    s.store.transact("mark-historical-capacity", {}, mark_historical)
    queue(s, "active", tokens=8192)
    active = s.admit("host", "model")
    assert active["operation_id"] == "active"
    s.dispatched("active", "host", active["generation"])
    changes = s.reestimate_legacy_pending_models()
    assert len(changes) == 1
    corrected = s.get(capacity_id)
    assert corrected["estimated_tokens"] == 1250
    assert corrected["estimated_tokens_original"] == 8192
    assert corrected["estimate_basis"] == "recovered_from_exact_saved_intent_bytes_v1"
    assert corrected["attempts"] == []
    assert s.get("active")["estimated_tokens"] == 8192
    assert s.store.read_blob(blob["sha256"]) == request
    assert s.reestimate_legacy_pending_models() == []
    s.settle("active", "host", active["generation"], used_tokens=0, outcome="completed")
    s.observe_headers(
        "provider/model/account", remaining_requests=100,
        remaining_tokens=2000, reset_after_tokens=60, observed_at=clock(),
    )
    assert s.admit("host", "model")["operation_id"] == capacity_id


def test_distinct_provider_and_shared_account_scopes(tmp_path):
    clock = Clock()
    store = StateStore(tmp_path, clock=clock)
    policy = CapacityPolicy(
        permits={"model": 3, "retrieval": 1, "compute": 1},
        quotas={"provider-a": QuotaLimit(60, requests=1),
                "provider-b": QuotaLimit(60, requests=2),
                "account": QuotaLimit(60, requests=2)},
    )
    s = CapacityScheduler(store, policy)
    for name, scopes in (("a", ("provider-a", "account")),
                         ("b", ("provider-b", "account")),
                         ("c", ("provider-b", "account"))):
        s.enqueue(name, task_id=name, lane=name, kind="model", quota_scopes=scopes,
                  estimated_tokens=1)
    first = s.admit("host", "model")
    second = s.admit("host", "model")
    assert {first["operation_id"], second["operation_id"]} == {"a", "b"}
    assert s.admit("host", "model") is None


def test_replay_after_restart_uses_stable_intent_not_current_clock(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    queue(s, "stable")
    first = s.admit("host", "model")
    s.dispatched("stable", "host", first["generation"])
    s.settle("stable", "host", first["generation"], used_tokens=2, outcome="completed")
    clock.advance(10)
    restarted = make(tmp_path, clock)
    assert queue(restarted, "stable")["phase"] == "completed"
    assert restarted.settle("stable", "host", first["generation"], used_tokens=2,
                            outcome="completed")["phase"] == "completed"
    assert len(restarted.get("stable")["attempts"]) == 1
    with pytest.raises(StateConflict):
        restarted.enqueue("stable", task_id="different", lane="a", kind="model",
                          quota_scopes=("provider/model/account",), estimated_tokens=3)


def test_typed_transport_failure_retries_only_confirmed_no_effect(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    queue(s, "outage")
    s.enqueue("healthy", task_id="healthy", lane="search", kind="retrieval")
    a = s.admit("host", "model")
    s.dispatched("outage", "host", a["generation"])
    waiting = s.transport_failure("outage", "host", a["generation"],
                                  classification="temporary_no_effect", retry_after=5)
    assert waiting["phase"] == "waiting" and waiting["wake_at"] == clock() + 5
    assert s.admit("host", "retrieval")["operation_id"] == "healthy"
    clock.advance(5)
    retry = s.admit("host", "model")
    s.dispatched("outage", "host", retry["generation"])
    uncertain = s.transport_failure("outage", "host", retry["generation"],
                                    classification="uncertain")
    assert uncertain["phase"] == "uncertain" and uncertain["external_active"]
    assert s.admit("host", "model") is None


def test_mission_filter_preserves_global_permit(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    for mission in ("one", "two"):
        s.enqueue(f"call-{mission}", task_id=f"task-{mission}", lane=mission,
                  kind="model", quota_scopes=("provider/model/account",),
                  estimated_tokens=1, mission_id=mission)
    assert s.admit("host-two", "model", mission_id="two")["operation_id"] == "call-two"
    assert s.admit("host-one", "model", mission_id="one") is None
    assert s.admit("host", "model", mission_id="absent") is None


def test_limited_retrieval_provider_leaves_other_route_eligible(tmp_path):
    clock = Clock()
    store = StateStore(tmp_path, clock=clock)
    s = CapacityScheduler(store, CapacityPolicy(
        permits={"model": 1, "retrieval": 1, "compute": 1},
        quotas={"search-a": QuotaLimit(60, requests=1),
                "search-b": QuotaLimit(60, requests=1)},
    ))
    s.enqueue("a", task_id="a", lane="search", kind="retrieval", quota_scopes=("search-a",))
    s.enqueue("b", task_id="b", lane="search", kind="retrieval", quota_scopes=("search-b",))
    s.observe_headers("search-a", remaining_requests=0, remaining_tokens=None, reset_after=20)
    admitted = s.admit("host", "retrieval")
    assert admitted["operation_id"] == "b"
    assert s.get("a")["phase"] == "waiting"
    assert s.get("a")["wake_at"] == clock() + 20


def test_live_heartbeat_without_progress_records_diagnosis_not_timeout(tmp_path):
    clock = Clock()
    s = make(tmp_path, clock)
    queue(s, "long-call")
    s.enqueue("independent", task_id="independent", lane="search", kind="retrieval")
    admitted = s.admit("host", "model", lease_seconds=5)
    s.dispatched("long-call", "host", admitted["generation"])
    clock.advance(4)
    s.heartbeat("long-call", "host", admitted["generation"], lease_seconds=5)
    assert s.diagnose_stalls(after_seconds=3) == ["long-call"]
    assert s.diagnose_stalls(after_seconds=3) == []
    assert s.get("long-call")["phase"] == "dispatched"
    assert s.admit("host", "retrieval")["operation_id"] == "independent"
    s.settle("long-call", "host", admitted["generation"], used_tokens=2, outcome="completed")
