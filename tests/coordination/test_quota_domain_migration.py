"""Offline account-scope cutover retains the exact old ledger and provider waits."""

import pytest

from ora.coordination.capacity import CapacityPolicy, CapacityScheduler, QuotaLimit
from ora.state.store import StateConflict, StateStore


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def policy(*, backup=False, requests=3):
    quotas = {"groq-selected": QuotaLimit(60, requests=requests, tokens=100)}
    if backup:
        quotas["groq-verified-backup"] = QuotaLimit(60, requests=requests, tokens=100)
    return CapacityPolicy({"model": 3, "retrieval": 1, "compute": 1}, quotas)


def queue(scheduler, name, scope="groq-selected"):
    return scheduler.enqueue(
        name, task_id=name, lane=name, kind="model", quota_scopes=(scope,),
        estimated_tokens=10,
    )


def admit(scheduler, name):
    result = scheduler.admit("host", "model", eligible_operation_ids=frozenset({name}))
    assert result is not None and result["operation_id"] == name
    return result


def migrate(store):
    return CapacityScheduler.reconcile_additive_quota_domain(
        store, policy(backup=True), from_scope="groq-selected",
        to_scope="groq-verified-backup",
    )


def test_cutover_carries_wait_and_reservation_without_changing_old_records(tmp_path):
    clock = Clock()
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, policy())
    queue(old, "old-reservation")
    reserved = admit(old, "old-reservation")
    queue(old, "old-rate")
    limited = admit(old, "old-rate")
    old.dispatched("old-rate", "host", limited["generation"])
    old.rate_limited("old-rate", "host", limited["generation"],
                     scope="groq-selected", retry_after=20)
    before = {(kind, identifier): store.get(kind, identifier) for kind, identifier in (
        ("capacity_operation", "old-reservation"),
        ("capacity_operation", "old-rate"),
        ("capacity_quota", "groq-selected"),
    )}
    receipt = migrate(store)
    assert migrate(store) == receipt
    for (kind, identifier), record in before.items():
        assert store.get(kind, identifier) == record
    bridge = store.get("capacity_quota_bridge", "groq-verified-backup")
    assert {item["id"] for item in bridge["body"]["inherited_operation_refs"]} == {
        "old-reservation", "old-rate",
    }
    assert bridge["body"]["cutover_quota_ref"]["version"] == before[
        ("capacity_quota", "groq-selected")]["version"]
    current = CapacityScheduler(store, policy(backup=True))
    queue(current, "new-backup", "groq-verified-backup")
    assert current.admit("host", "model", eligible_operation_ids=frozenset({"new-backup"})) is None
    with pytest.raises(StateConflict, match="reload"):
        old.dispatched("old-reservation", "host", reserved["generation"])
    old.yield_admission("old-reservation", "host", reserved["generation"])
    assert current.admit("host", "model", eligible_operation_ids=frozenset({"new-backup"})) is None
    assert current.get("new-backup")["wait_reason"] == "provider"
    clock.advance(21)
    admit(current, "new-backup")
    queue(current, "new-primary")
    admit(current, "new-primary")
    assert current.get("new-backup")["quota_scopes"] == ["groq-verified-backup"]
    assert current.get("new-primary")["quota_scopes"] == ["groq-selected"]
    with pytest.raises(StateConflict, match="reload"):
        old.admit("stale", "model", eligible_operation_ids=frozenset({"old-reservation"}))


def test_later_inherited_429_is_carried_but_new_primary_429_is_not(tmp_path):
    clock = Clock()
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, policy(requests=5))
    queue(old, "in-flight")
    inherited = admit(old, "in-flight")
    old.dispatched("in-flight", "host", inherited["generation"])
    CapacityScheduler.reconcile_additive_quota_domain(
        store, policy(backup=True, requests=5), from_scope="groq-selected",
        to_scope="groq-verified-backup",
    )
    current = CapacityScheduler(store, policy(backup=True, requests=5))
    queue(current, "new-backup", "groq-verified-backup")
    assert current.admit("host", "model", eligible_operation_ids=frozenset({"new-backup"})) is None
    old.rate_limited("in-flight", "host", inherited["generation"],
                     scope="groq-selected", retry_after=30)
    bridge = store.get("capacity_quota_bridge", "groq-verified-backup")
    assert len(bridge["body"]["later_quota_events"]) == 1
    clock.advance(31)
    queue(current, "post-cutover-primary")
    primary = admit(current, "post-cutover-primary")
    current.dispatched("post-cutover-primary", "host", primary["generation"])
    current.rate_limited("post-cutover-primary", "host", primary["generation"],
                         scope="groq-selected", retry_after=50)
    assert len(store.get("capacity_quota_bridge", "groq-verified-backup")["body"][
        "later_quota_events"]) == 1
    admit(current, "new-backup")  # The unrelated new Primary wait does not throttle Backup.


def test_reverse_order_primary_429_cannot_extend_inherited_backup_wait(tmp_path):
    clock = Clock()
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, policy(requests=5))
    queue(old, "in-flight")
    inherited = admit(old, "in-flight")
    old.dispatched("in-flight", "host", inherited["generation"])
    CapacityScheduler.reconcile_additive_quota_domain(
        store, policy(backup=True, requests=5), from_scope="groq-selected",
        to_scope="groq-verified-backup",
    )
    current = CapacityScheduler(store, policy(backup=True, requests=5))
    queue(current, "post-cutover-primary")
    primary = admit(current, "post-cutover-primary")
    current.dispatched("post-cutover-primary", "host", primary["generation"])
    current.rate_limited("post-cutover-primary", "host", primary["generation"],
                         scope="groq-selected", retry_after=50)
    old.rate_limited("in-flight", "host", inherited["generation"],
                     scope="groq-selected", retry_after=10)
    assert store.get("capacity_quota", "groq-selected")["body"]["blocked_until"] == 1050
    bridge = store.get("capacity_quota_bridge", "groq-verified-backup")["body"]
    assert bridge["later_quota_events"][0]["constraint"]["blocked_until"] == 1010
    queue(current, "new-backup", "groq-verified-backup")
    clock.advance(11)
    admit(current, "new-backup")


def test_reverse_order_primary_429_cannot_contaminate_inherited_header(tmp_path):
    clock = Clock()
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, policy(requests=5))
    queue(old, "in-flight")
    inherited = admit(old, "in-flight")
    old.dispatched("in-flight", "host", inherited["generation"])
    CapacityScheduler.reconcile_additive_quota_domain(
        store, policy(backup=True, requests=5), from_scope="groq-selected",
        to_scope="groq-verified-backup",
    )
    current = CapacityScheduler(store, policy(backup=True, requests=5))
    queue(current, "post-cutover-primary")
    primary = admit(current, "post-cutover-primary")
    current.dispatched("post-cutover-primary", "host", primary["generation"])
    current.rate_limited("post-cutover-primary", "host", primary["generation"],
                         scope="groq-selected", retry_after=50)
    old.observe_headers(
        "groq-selected", remaining_requests=0, remaining_tokens=0,
        reset_after_requests=10, reset_after_tokens=10,
        source_operation_id="in-flight", observation_id="inherited-after-primary",
    )
    old.settle("in-flight", "host", inherited["generation"],
               used_tokens=10, outcome="completed")
    bridge = store.get("capacity_quota_bridge", "groq-verified-backup")["body"]
    assert bridge["later_quota_events"][0]["constraint"]["blocked_until"] == 0
    queue(current, "new-backup", "groq-verified-backup")
    clock.advance(11)
    admit(current, "new-backup")


def test_later_retry_of_inherited_operation_consumes_saved_headroom(tmp_path):
    clock = Clock()
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, policy(requests=5))
    queue(old, "in-flight")
    first = admit(old, "in-flight")
    old.dispatched("in-flight", "host", first["generation"])
    CapacityScheduler.reconcile_additive_quota_domain(
        store, policy(backup=True, requests=5), from_scope="groq-selected",
        to_scope="groq-verified-backup",
    )
    current = CapacityScheduler(store, policy(backup=True, requests=5))
    old.observe_headers(
        "groq-selected", remaining_requests=4, remaining_tokens=20,
        reset_after_requests=60, reset_after_tokens=60,
        source_operation_id="in-flight", observation_id="before-retry",
    )
    old.transport_failure(
        "in-flight", "host", first["generation"],
        classification="temporary_no_effect", retry_after=2,
    )
    clock.advance(2)
    retry = admit(current, "in-flight")
    current.dispatched("in-flight", "host", retry["generation"])
    current.settle("in-flight", "host", retry["generation"],
                   used_tokens=10, outcome="completed")
    current.enqueue(
        "new-backup", task_id="new-backup", lane="new-backup", kind="model",
        quota_scopes=("groq-verified-backup",), estimated_tokens=15,
    )
    assert current.admit("host", "model", eligible_operation_ids=frozenset({"new-backup"})) is None
    clock.advance(59)
    admit(current, "new-backup")


def test_delayed_inherited_header_remains_in_bridge_after_newer_primary_header(tmp_path):
    clock = Clock()
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, policy(requests=5))
    queue(old, "in-flight")
    inherited = admit(old, "in-flight")
    old.dispatched("in-flight", "host", inherited["generation"])
    CapacityScheduler.reconcile_additive_quota_domain(
        store, policy(backup=True, requests=5), from_scope="groq-selected",
        to_scope="groq-verified-backup",
    )
    current = CapacityScheduler(store, policy(backup=True, requests=5))
    clock.advance(5)
    queue(current, "post-cutover-primary")
    primary = admit(current, "post-cutover-primary")
    current.dispatched("post-cutover-primary", "host", primary["generation"])
    current.observe_headers(
        "groq-selected", remaining_requests=4, remaining_tokens=50,
        reset_after_tokens=60, source_operation_id="post-cutover-primary",
        observation_id="newer-primary", observed_at=clock(),
    )
    old.observe_headers(
        "groq-selected", remaining_requests=0, remaining_tokens=0,
        reset_after_requests=10, reset_after_tokens=10,
        source_operation_id="in-flight", observation_id="late-old-response",
        observed_at=1001,
    )
    old.settle("in-flight", "host", inherited["generation"],
               used_tokens=10, outcome="completed")
    bridge = store.get("capacity_quota_bridge", "groq-verified-backup")["body"]
    assert bridge["later_quota_events"][0]["quota_ref"] is None
    assert bridge["later_quota_events"][0]["observation_id"] == "late-old-response"
    queue(current, "new-backup", "groq-verified-backup")
    assert current.admit("host", "model", eligible_operation_ids=frozenset({"new-backup"})) is None
    clock.advance(7)
    admit(current, "new-backup")


def test_later_inherited_header_restricts_backup_without_relabelling_old_receipt(tmp_path):
    clock = Clock()
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, policy(requests=5))
    queue(old, "in-flight")
    inherited = admit(old, "in-flight")
    old.dispatched("in-flight", "host", inherited["generation"])
    CapacityScheduler.reconcile_additive_quota_domain(
        store, policy(backup=True, requests=5), from_scope="groq-selected",
        to_scope="groq-verified-backup",
    )
    current = CapacityScheduler(store, policy(backup=True, requests=5))
    old.observe_headers(
        "groq-selected", remaining_requests=0, remaining_tokens=0,
        reset_after_requests=40, reset_after_tokens=40,
        source_operation_id="in-flight", observation_id="inherited-response",
    )
    old.settle("in-flight", "host", inherited["generation"],
               used_tokens=10, outcome="completed")
    bridge = store.get("capacity_quota_bridge", "groq-verified-backup")
    assert len(bridge["body"]["later_quota_events"]) == 1
    assert current.get("in-flight")["quota_scopes"] == ["groq-selected"]
    queue(current, "new-backup", "groq-verified-backup")
    assert current.admit("host", "model", eligible_operation_ids=frozenset({"new-backup"})) is None
    clock.advance(41)
    admit(current, "new-backup")


def test_non_additive_migration_rejects_without_ledger_mutation(tmp_path):
    clock = Clock()
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, policy())
    queue(old, "pending")
    original = store.get("capacity_config", "default")
    changed = CapacityPolicy(
        {"model": 2, "retrieval": 1, "compute": 1},
        {"groq-selected": QuotaLimit(60, requests=3, tokens=100),
         "groq-verified-backup": QuotaLimit(60, requests=3, tokens=100)},
    )
    with pytest.raises(StateConflict, match="add one scope"):
        CapacityScheduler.reconcile_additive_quota_domain(
            store, changed, from_scope="groq-selected", to_scope="groq-verified-backup",
        )
    assert store.get("capacity_config", "default") == original
    assert store.get("capacity_operation", "pending")["body"]["phase"] == "queued"
    assert store.get("capacity_quota_bridge", "groq-verified-backup") is None


def test_inherited_headroom_ceiling_fades_after_exact_window_and_reset(tmp_path):
    clock = Clock()
    store = StateStore(tmp_path, clock=clock)
    original = CapacityPolicy(
        {"model": 3, "retrieval": 1, "compute": 1},
        {"groq-selected": QuotaLimit(60, requests=5)},
    )
    expanded = CapacityPolicy(
        original.permits,
        {**original.quotas, "groq-verified-backup": QuotaLimit(60, requests=5, tokens=100)},
    )
    old = CapacityScheduler(store, original)
    queue(old, "old-effect")
    effect = admit(old, "old-effect")
    old.dispatched("old-effect", "host", effect["generation"])
    old.observe_headers(
        "groq-selected", remaining_requests=4, remaining_tokens=1,
        reset_after_tokens=20, token_limit=10,
        source_operation_id="old-effect", observation_id="old-headroom",
    )
    old.settle("old-effect", "host", effect["generation"],
               used_tokens=10, outcome="completed")
    CapacityScheduler.reconcile_additive_quota_domain(
        store, expanded, from_scope="groq-selected", to_scope="groq-verified-backup",
    )
    current = CapacityScheduler(store, expanded)
    current.enqueue(
        "backup-larger", task_id="backup-larger", lane="backup-larger", kind="model",
        quota_scopes=("groq-verified-backup",), estimated_tokens=20,
    )
    clock.advance(21)
    assert current.admit("host", "model", eligible_operation_ids=frozenset({"backup-larger"})) is None
    clock.advance(40)
    admit(current, "backup-larger")  # Old account ceiling is no longer borrowed.


def test_unknown_old_external_effect_keeps_new_domain_closed(tmp_path):
    clock = Clock()
    store = StateStore(tmp_path, clock=clock)
    old = CapacityScheduler(store, policy())
    queue(old, "uncertain-old")
    active = admit(old, "uncertain-old")
    old.dispatched("uncertain-old", "host", active["generation"])
    old.timeout("uncertain-old", "host", active["generation"])
    saved = store.get("capacity_operation", "uncertain-old")
    migrate(store)
    current = CapacityScheduler(store, policy(backup=True))
    queue(current, "new-backup", "groq-verified-backup")
    clock.advance(120)
    assert current.admit("host", "model", eligible_operation_ids=frozenset({"new-backup"})) is None
    assert store.get("capacity_operation", "uncertain-old") == saved
    assert current.get("uncertain-old")["external_active"] is True


@pytest.mark.parametrize("requests,tokens", [(0, None), (None, 5)])
def test_insufficient_current_headroom_without_reset_cannot_grant_a_fresh_call(
    tmp_path, requests, tokens,
):
    clock = Clock()
    store = StateStore(tmp_path, clock=clock)
    current = CapacityScheduler(store, policy())
    current.observe_headers(
        "groq-selected", remaining_requests=requests, remaining_tokens=tokens,
        source_operation_id="saved-response", observation_id="no-reset",
    )
    queue(current, "new-primary")
    assert current.admit("host", "model", eligible_operation_ids=frozenset({"new-primary"})) is None
    assert current.get("new-primary")["phase"] == "queued"
    current.observe_headers(
        "groq-selected", remaining_requests=1, remaining_tokens=20,
        reset_after_requests=60, reset_after_tokens=60,
        source_operation_id="later-response",
        observation_id="positive-headroom",
    )
    admit(current, "new-primary")
