"""A captured auxiliary-model 429 pauses the selected shared model quota."""

from ora.coordination.capacity import CapacityPolicy, CapacityScheduler, QuotaLimit
from ora.state import StateStore


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


def scheduler(tmp_path, clock):
    store = StateStore(tmp_path, clock=clock)
    policy = CapacityPolicy(
        permits={"model": 2, "retrieval": 1, "compute": 1},
        quotas={"groq-selected": QuotaLimit(60, 100, 100000)},
    )
    return CapacityScheduler(store, policy)


def test_captured_retry_wait_survives_replay_headroom_and_restart(tmp_path):
    clock = Clock()
    capacity = scheduler(tmp_path, clock)
    for operation in ("figure", "next-gpt-oss"):
        capacity.enqueue(
            operation, task_id=f"task-{operation}", lane=operation,
            kind="model", quota_scopes=("groq-selected",), estimated_tokens=100,
        )
    first = capacity.admit("host", "model")
    assert first["operation_id"] == "figure"
    capacity.dispatched("figure", "host", first["generation"])
    observed = capacity.observe_retry_after(
        "groq-selected", retry_after=3, observed_at=clock(),
        source_operation_id="figure", observation_id="saved-qwen-429",
    )
    assert observed["blocked_until"] == 1003
    assert capacity.admit("host", "model") is None

    clock.value = 1001
    replay = capacity.observe_retry_after(
        "groq-selected", retry_after=3, observed_at=1000.0,
        source_operation_id="figure", observation_id="saved-qwen-429",
    )
    assert replay["blocked_until"] == 1003
    capacity.observe_rate_headers(
        "groq-selected", {
            "x-ratelimit-remaining-requests": "99",
            "x-ratelimit-reset-requests": "1m",
            "x-ratelimit-remaining-tokens": "90000",
            "x-ratelimit-reset-tokens": "1m",
        }, source_operation_id="figure", observation_id="later-headroom",
        observed_at=clock(),
    )
    restarted = scheduler(tmp_path, clock)
    quota = restarted.store.get("capacity_quota", "groq-selected")["body"]
    assert quota["blocked_until"] == 1003
    assert restarted.admit("host", "model") is None
    clock.value = 1003
    assert restarted.admit("host", "model")["operation_id"] == "next-gpt-oss"
