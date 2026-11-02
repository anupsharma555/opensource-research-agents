"""A provider limit hint crosses isolated stores only with exact saved proof."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ora.models.provider_limits import TTL_SECONDS, ProviderLimitCache
from ora.state.store import StateStore


def _ref(record):
    return {key: record[key] for key in ("kind", "id", "version")}


def _receipt(root, *, at=None, limit=8000, profile="synthetic-profile", quota="synthetic-quota",
             endpoint="https://example.invalid/chat", model="synthetic/model",
             intent_profile=None, request_sha=None, status="failed", http_status=413,
             intent_changes=None, receipt_changes=None, input_limit=None,
             input_requested=None, local_input=6980):
    stamp = time.time() if at is None else at
    store = StateStore(root, clock=lambda: stamp)
    payload = b'{"synthetic_task":"private material must not enter limit cache"}'
    blob = store.put_blob(payload)
    error_blob = store.put_blob(json.dumps({"provider_error": {
        "type": "tokens", "code": "rate_limit_exceeded",
        "message": (f"Request too large for model `{model}` in organization "
                    f"`synthetic-private` on input tokens per minute (ITPM): "
                    f"Limit {input_limit}, Requested {input_requested}, try later."),
    }}).encode()) if input_limit is not None else None
    sha = hashlib.sha256(payload).hexdigest()
    lineage = {"mission_id": "synthetic-mission", "task_id": "synthetic-task",
               "attempt_id": "synthetic-attempt"}
    configuration_ref = {"kind": "agent_configuration", "id": "synthetic-config", "version": 1}

    def save(tx):
        call = tx.put("agent_call", "call-one", {
            **lineage, "model_id": model, "endpoint": endpoint,
        })
        intent_body = {
            **lineage, "configuration_ref": configuration_ref,
            "call_ref": _ref(call), "request_sha256": sha, "blob": blob,
            "credential_profile": profile if intent_profile is None else intent_profile,
            "quota_domain": quota,
            "request_token_estimate": {"tokens": local_input, "basis": "synthetic"},
        }
        intent_body.update(intent_changes or {})
        intent = tx.put("model_request_intent", "intent-one", intent_body)
        receipt_body = {
            **lineage, "configuration_ref": configuration_ref,
            "call_ref": _ref(call), "intent_ref": _ref(intent),
            "request_sha256": sha if request_sha is None else request_sha,
            "credential_profile": profile, "quota_domain": quota,
            "rate_headers": {"x-ratelimit-limit-tokens": str(limit)},
            "http_status": http_status, "status": status,
        }
        if error_blob is not None:
            receipt_body.update({"blob": error_blob, "error_kind": "http_error"})
        receipt_body.update(receipt_changes or {})
        receipt = tx.put("model_response_receipt", "receipt-one", receipt_body)
        if error_blob is not None:
            tx.put("model_dispatch_attempt", "model-dispatch:intent-one", {
                "status": "captured", "intent_ref": _ref(intent),
                "receipt_ref": _ref(receipt), "request_sha256": sha,
            })
        return receipt

    receipt = store.transact("seed-provider-limit", {}, save)
    scope = {"credential_profile": profile, "quota_domain": quota,
             "endpoint": endpoint, "model": model}
    return store, _ref(receipt), scope


def test_verified_limit_crosses_isolated_stores_without_request_material(tmp_path):
    source, receipt, scope = _receipt(tmp_path / "case-one")
    cache = ProviderLimitCache(tmp_path / "private-provider-limits")
    saved = cache.record(source, receipt)
    assert saved["observed_limit_tokens"] == 8000
    assert saved["receipt_ref"] == receipt
    assert cache.latest(scope)["request_sha256"] == saved["request_sha256"]
    assert cache.latest({**scope, "credential_profile": "other-profile"}) is None
    assert cache.latest({**scope, "model": "other/model"}) is None
    assert cache.latest({**scope, "endpoint": "https://other.invalid/chat"}) is None
    assert cache.latest({**scope, "quota_domain": "other-domain"}) is None
    assert b"synthetic_task" not in cache.path.read_bytes()
    assert b"private material" not in cache.path.read_bytes()
    assert cache.path.stat().st_mode & 0o777 == 0o600


def test_newer_higher_limit_wins_and_old_observation_cannot_regress(tmp_path):
    now = time.time()
    old_store, old_ref, scope = _receipt(tmp_path / "old", at=now - 120, limit=8000)
    new_store, new_ref, _ = _receipt(
        tmp_path / "new", at=now - 60, limit=10000,
        status="tool_requested", http_status=None,
    )
    cache = ProviderLimitCache(tmp_path / "cache")
    assert cache.record(old_store, old_ref)["observed_limit_tokens"] == 8000
    assert cache.record(new_store, new_ref)["observed_limit_tokens"] == 10000
    cache.record(old_store, old_ref)
    assert cache.latest(scope)["observed_limit_tokens"] == 10000
    assert cache.latest(scope)["source_state_path"] == str(new_store.root)


def test_itpm_hint_stays_separate_from_combined_header_across_newer_success(tmp_path):
    now = time.time()
    failed, failed_ref, scope = _receipt(
        tmp_path / "failed", at=now - 40, limit=8000,
        input_limit=7000, input_requested=7030,
    )
    success, success_ref, _ = _receipt(
        tmp_path / "success", at=now - 10, limit=8000,
        status="complete", http_status=200,
    )
    cache = ProviderLimitCache(tmp_path / "cache")
    first = cache.record(failed, failed_ref)
    assert first["observed_limit_tokens"] == 8000
    assert first["observed_input_limit_tokens"] == 7000
    assert first["input_admission_discrepancy_tokens"] == 50
    after = cache.record(success, success_ref)
    assert after["observed_limit_tokens"] == 8000
    assert after["observed_input_limit_tokens"] == 7000
    assert after["input_limit_receipt_ref"] == failed_ref
    assert cache.latest({**scope, "credential_profile": "other"}) is None
    assert cache.latest({**scope, "model": "other"}) is None
    assert b"synthetic-private" not in cache.path.read_bytes()


def test_old_header_only_row_revalidates_saved_itpm_without_rewriting_receipt(tmp_path):
    now = time.time()
    source, receipt, scope = _receipt(
        tmp_path / "source", at=now - 20,
        input_limit=7000, input_requested=7200,
    )
    cache = ProviderLimitCache(tmp_path / "cache")
    cache.record(source, receipt)
    with sqlite3.connect(cache.path) as db:
        db.execute("DELETE FROM input_observations")
    before = source.get(**receipt)
    hint = cache.latest(scope)
    assert hint["observed_input_limit_tokens"] == 7000
    assert hint["input_admission_discrepancy_tokens"] == 220
    assert source.get(**receipt) == before
    success, success_ref, _ = _receipt(
        tmp_path / "success", at=now - 10,
        status="complete", http_status=200,
    )
    after = cache.record(success, success_ref)
    assert after["observed_limit_tokens"] == 8000
    assert after["observed_input_limit_tokens"] == 7000
    assert after["input_limit_receipt_ref"] == receipt
    with sqlite3.connect(cache.path) as db:
        assert db.execute("SELECT COUNT(*) FROM input_observations").fetchone()[0] == 1
    assert source.get(**receipt) == before
    other, other_ref, other_scope = _receipt(
        tmp_path / "other", at=now - 5, profile="other-profile",
        status="complete", http_status=200,
    )
    assert "observed_input_limit_tokens" not in cache.record(other, other_ref)
    assert "observed_input_limit_tokens" not in cache.latest(other_scope)


def test_tampered_itpm_row_cannot_downgrade_to_header_only_hint(tmp_path):
    source, receipt, scope = _receipt(
        tmp_path / "source", input_limit=7000, input_requested=7030,
    )
    cache = ProviderLimitCache(tmp_path / "cache")
    assert cache.record(source, receipt)["observed_input_limit_tokens"] == 7000
    with sqlite3.connect(cache.path) as db:
        db.execute("UPDATE input_observations SET observed_input_limit_tokens=7100")
    assert cache.latest(scope) is None


def test_strictest_fresh_input_target_survives_later_smaller_offset(tmp_path):
    now = time.time()
    first, first_ref, scope = _receipt(
        tmp_path / "first", at=now - 30, input_limit=7000,
        input_requested=7030, local_input=6980,
    )
    larger, larger_ref, _ = _receipt(
        tmp_path / "larger", at=now - 20, input_limit=7000,
        input_requested=7250, local_input=6980,
    )
    smaller, smaller_ref, _ = _receipt(
        tmp_path / "smaller", at=now - 10, input_limit=7000,
        input_requested=7030, local_input=6980,
    )
    cache = ProviderLimitCache(tmp_path / "cache")
    cache.record(first, first_ref)
    assert cache.record(larger, larger_ref)["input_admission_discrepancy_tokens"] == 270
    assert cache.record(smaller, smaller_ref)["input_admission_discrepancy_tokens"] == 270
    assert cache.latest(scope)["input_limit_receipt_ref"] == larger_ref


def test_legacy_itpm_is_not_promoted_after_saved_evidence_disappears(tmp_path):
    now = time.time()
    old, old_ref, scope = _receipt(
        tmp_path / "old", at=now - 20,
        input_limit=7000, input_requested=7030,
    )
    cache = ProviderLimitCache(tmp_path / "cache")
    cache.record(old, old_ref)
    with sqlite3.connect(cache.path) as db:
        db.execute("DELETE FROM input_observations")
    old.root.rename(tmp_path / "moved-old")
    success, success_ref, _ = _receipt(
        tmp_path / "success", at=now - 10,
        status="complete", http_status=200,
    )
    updated = cache.record(success, success_ref)
    assert updated["observed_limit_tokens"] == 8000
    assert "observed_input_limit_tokens" not in updated
    assert "observed_input_limit_tokens" not in cache.latest(scope)


def test_explicit_old_413_backfill_keeps_newer_combined_header(tmp_path):
    now = time.time()
    old, old_ref, scope = _receipt(
        tmp_path / "old", at=now - 20,
        input_limit=7000, input_requested=7030,
    )
    success, success_ref, _ = _receipt(
        tmp_path / "success", at=now - 10,
        status="complete", http_status=200,
    )
    cache = ProviderLimitCache(tmp_path / "cache")
    assert "observed_input_limit_tokens" not in cache.record(success, success_ref)
    backfilled = cache.record(old, old_ref)
    assert backfilled["receipt_ref"] == success_ref
    assert backfilled["input_limit_receipt_ref"] == old_ref
    assert backfilled["observed_limit_tokens"] == 8000
    assert backfilled["observed_input_limit_tokens"] == 7000
    assert cache.latest(scope)["input_admission_discrepancy_tokens"] == 50


@pytest.mark.parametrize("kwargs", [
    {"input_limit": 7000, "input_requested": 6990},
    {"input_limit": 7000, "input_requested": 7030,
     "receipt_changes": {"error_kind": "not_http"}},
])
def test_input_hint_rejects_mismatched_or_non_itpm_evidence(tmp_path, kwargs):
    source, receipt, scope = _receipt(tmp_path / "source", **kwargs)
    cache = ProviderLimitCache(tmp_path / "cache")
    hint = cache.record(source, receipt)
    assert hint is not None
    assert "observed_input_limit_tokens" not in hint


def test_stale_future_and_malformed_evidence_are_ignored(tmp_path):
    now = time.time()
    cache = ProviderLimitCache(tmp_path / "cache")
    for name, kwargs in (
        ("stale", {"at": now - TTL_SECONDS - 1}),
        ("future", {"at": now + 301}),
        ("bad-scope", {"intent_profile": "other"}),
        ("bad-sha", {"request_sha": "0" * 64}),
        ("bad-header", {"limit": 0}),
    ):
        source, receipt, _ = _receipt(tmp_path / name, **kwargs)
        assert cache.record(source, receipt) is None
    source, receipt, scope = _receipt(tmp_path / "valid")
    assert cache.record(source, receipt)
    assert cache.latest(scope, now=now + TTL_SECONDS + 1) is None
    with sqlite3.connect(cache.path) as db:
        db.execute("UPDATE observations SET request_sha256=?", ("0" * 64,))
    assert cache.latest(scope) is None


@pytest.mark.parametrize("intent_changes,receipt_changes", [
    ({"mission_id": "foreign-mission"}, None),
    ({"task_id": "foreign-task"}, None),
    ({"attempt_id": "foreign-attempt"}, None),
    ({"call_ref": {"kind": "agent_call", "id": "foreign-call", "version": 1}}, None),
    (None, {"mission_id": "foreign-mission"}),
    (None, {"configuration_ref": {
        "kind": "agent_configuration", "id": "foreign-config", "version": 1,
    }}),
    (None, {"status": "locally_blocked", "http_status": None}),
])
def test_mismatched_receipt_intent_or_call_cannot_seed_hint(
    tmp_path, intent_changes, receipt_changes,
):
    source, receipt, scope = _receipt(
        tmp_path / "source", intent_changes=intent_changes,
        receipt_changes=receipt_changes,
    )
    cache = ProviderLimitCache(tmp_path / "cache")
    assert cache.record(source, receipt) is None
    assert cache.latest(scope) is None


def test_missing_saved_provenance_invalidates_cached_hint(tmp_path):
    source, receipt, scope = _receipt(tmp_path / "source")
    cache = ProviderLimitCache(tmp_path / "cache")
    assert cache.record(source, receipt)
    source.root.rename(tmp_path / "moved-source")
    assert cache.latest(scope) is None


def test_concurrent_records_keep_newest_verified_observation(tmp_path):
    now = time.time()
    old_store, old_ref, scope = _receipt(tmp_path / "old", at=now - 20, limit=8000)
    new_store, new_ref, _ = _receipt(tmp_path / "new", at=now - 10, limit=9000)
    cache = ProviderLimitCache(tmp_path / "cache")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda pair: cache.record(*pair),
                      [(new_store, new_ref), (old_store, old_ref)]))
    assert cache.latest(scope)["observed_limit_tokens"] == 9000
