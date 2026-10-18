"""Verify private Groq accounting without inference, credentials or a live host."""

import hashlib
import importlib.util
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "groq_usage_skill", ROOT / ".agents/skills/os-groq-usage/scripts/usage.py")
assert SPEC and SPEC.loader
usage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(usage)
MODEL = "openai/gpt-oss-120b"


def call(identity="response-one", at="2026-01-15T15:00:00Z", profile="backup", **kwargs):
    body = {"credential_profile": profile,
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            **kwargs}
    return usage.normalize_call(body, at, identity, {"model": MODEL}, None)


def report(calls, at="2026-01-15T17:00:00Z", day=date(2026, 1, 15), limit=None):
    return usage.build_report(calls, as_of=usage.stamp(at), day=day,
                              zone="America/New_York", model=MODEL, daily_limit=limit)


def test_calendar_and_trailing_windows_are_separate_with_half_open_boundaries():
    calls = [call("before", "2026-01-15T04:59:59Z"),
             call("midnight", "2026-01-15T05:00:00Z"),
             call("future", "2026-01-15T17:00:00Z")]
    result = report(calls)
    assert result["periods"]["calendar_day"]["profiles"]["backup"]["captured_calls"] == 1
    assert result["periods"]["trailing_24_hours"]["profiles"]["backup"]["captured_calls"] == 2
    assert result["current_account_wide_remaining_tokens"] is None


@pytest.mark.parametrize(("day", "hours"), [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)])
def test_daylight_savings_days_do_not_masquerade_as_24_hours(day, hours):
    result = report([], at="2026-11-04T17:00:00Z", day=day)
    period = result["periods"]["calendar_day"]
    duration = usage.stamp(period["end_exclusive"]) - usage.stamp(period["start_inclusive"])
    assert duration.total_seconds() == hours * 3600


def test_unknown_usage_is_not_zero_and_optional_subsets_are_not_added():
    known = call(usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                       "prompt_tokens_details": {"cached_tokens": 50},
                       "completion_tokens_details": {"reasoning_tokens": 15}})
    failed = call("error", usage={}, http_status=429)
    row = usage.summarize([known, failed], 200_000)
    assert row["token_totals"]["total_tokens"] == {"known_sum": 120, "reported_calls": 1}
    assert row["token_totals"]["reasoning_tokens"]["known_sum"] == 15
    assert row["calls_with_incomplete_usage"] == 1
    assert row["current_provider_remaining_tokens"] is None
    assert usage.summarize([failed], 200_000)["token_totals"]["total_tokens"]["known_sum"] is None


def test_legacy_attribution_requires_explicit_cutoff_and_remains_identified():
    body = {"usage": {}}
    before = usage.stamp("2026-01-15T16:00:00Z")
    strict = usage.normalize_call(body, "2026-01-15T15:00:00Z", "old", {}, None)
    inferred = usage.normalize_call(body, "2026-01-15T15:00:00Z", "old", {}, before)
    later = usage.normalize_call(body, "2026-01-15T17:00:00Z", "new", {}, before)
    assert strict["profile"] == later["profile"] == "unknown"
    assert inferred["profile"] == "primary"
    assert inferred["profile_basis"] == "inferred_legacy_primary"


def test_profile_conflicts_are_not_silently_assigned_or_given_a_quota():
    conflicting = usage.normalize_call({"credential_profile": "primary"},
        "2026-01-15T15:00:00Z", "conflict", {"profile": "backup", "model": MODEL},
        usage.stamp("2026-01-15T16:00:00Z"))
    assert conflicting["profile_basis"] == "conflicting_or_invalid_profile"
    assert conflicting["profile"] == "unknown"
    result = report([conflicting], limit=200_000)
    assert result["periods"]["calendar_day"]["profiles"]["unknown"][
        "local_planning_headroom_ignoring_cache_and_unobserved_usage"] is None


def test_deduplication_counts_calls_not_repeated_receipts_or_matching_prompts():
    first = call()
    same_prompt_new_call = call("response-two")
    result, stats = usage.deduplicate([first, dict(first), same_prompt_new_call])
    assert len(result) == 2 and stats["repeated_receipts_removed"] == 1
    conflicting = call(usage={"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220})
    result, stats = usage.deduplicate([first, conflicting])
    assert result == [] and stats["conflicting_identities_excluded"] == 1


def test_invalid_or_inconsistent_usage_does_not_create_confident_remaining():
    bad = call(usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 500,
                     "completion_tokens_details": {"reasoning_tokens": 500}})
    row = usage.summarize([bad], 200_000)
    assert row["inconsistent_token_total_calls"] == 1
    assert row["token_totals"]["reasoning_tokens"]["known_sum"] is None
    assert row["local_planning_headroom_ignoring_cache_and_unobserved_usage"] is None
    assert usage.token(True) is None and usage.token(-1) is None
    assert "inconsistent input/completion/total" in usage.markdown(report([bad], limit=200_000))


def test_quota_error_is_exact_and_daily_not_minute_and_never_exposes_raw_text(tmp_path):
    (tmp_path / "objects").mkdir()
    raw = json.dumps({"provider_error": {"message":
        "private-account-label tokens per day (TPD): Limit 200000, Used 199000, Requested 2500."}}).encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = tmp_path / "objects" / digest
    path.write_bytes(raw)
    body = {"http_status": 429, "blob": {"sha256": digest, "size": len(raw)},
            "retry_after_seconds": 10,
            "rate_headers": {"x-ratelimit-remaining-tokens": "8000"}}
    result = usage.quota_observation(tmp_path, body, "2026-01-15T15:00:00Z")
    assert result["headroom_at_observation"] == 1000
    assert result["retry_not_before"] == "2026-01-15T15:00:10+00:00"
    assert result["full_reset_at"] is None and "private-account-label" not in json.dumps(result)
    path.write_bytes(raw.replace(b"TPD", b"TPM"))
    assert usage.quota_observation(tmp_path, body, "2026-01-15T15:00:00Z") is None
    assert usage.TPD.search("tokens per minute (TPM): Limit 8000, Used 7900, Requested 1000") is None


def test_latest_observation_remains_historical_even_after_retry_time():
    older = call()
    older["quota_observation"] = {"observed_at": older["observed_at"], "headroom_at_observation": 1}
    result = report([older], limit=200_000)
    assert result["latest_provider_daily_quota_observations"]["backup"]["headroom_at_observation"] == 1
    assert result["current_account_wide_remaining_tokens"] is None
    assert result["periods"]["calendar_day"]["profiles"]["backup"][
        "local_planning_headroom_ignoring_cache_and_unobserved_usage"] == 199_880


def test_database_revisions_use_first_capture_and_live_wal_without_rewriting(tmp_path):
    db = sqlite3.connect(tmp_path / "state.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("CREATE TABLE records(kind TEXT,id TEXT,version INTEGER,body TEXT,created_at TEXT);"
                     "CREATE TABLE heads(kind TEXT,id TEXT,version INTEGER);")
    config = {"provider": "groq", "model_id": MODEL, "settings": {"credential_profile": "backup"}}
    pointer = {"kind": "agent_configuration", "id": "config", "version": 1}
    body = {"configuration_ref": pointer, "response_id": "response-one",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
    db.execute("INSERT INTO records VALUES(?,?,?,?,?)",
               ("agent_configuration", "config", 1, json.dumps(config), "2026-01-15T14:00:00Z"))
    for version, when in ((1, "2026-01-15T15:00:00Z"), (2, "2026-01-16T15:00:00Z")):
        db.execute("INSERT INTO records VALUES(?,?,?,?,?)",
                   ("model_response_receipt", "receipt", version, json.dumps(body), when))
    db.execute("INSERT INTO heads VALUES('model_response_receipt','receipt',2)")
    db.commit()
    before = db.total_changes
    calls, metadata = usage.read_state(tmp_path, None)
    assert metadata["receipt_records"] == 1
    assert calls[0]["observed_at"] == "2026-01-15T15:00:00+00:00"
    assert calls[0]["profile"] == "backup" and db.total_changes == before
    assert db.execute("SELECT count(*) FROM records").fetchone()[0] == 3
    db.close()


def test_figure_usage_uses_exact_intent_profile_and_stays_in_vision_model(tmp_path):
    db = sqlite3.connect(tmp_path / "state.sqlite3")
    db.executescript("CREATE TABLE records(kind TEXT,id TEXT,version INTEGER,body TEXT,created_at TEXT);"
                     "CREATE TABLE heads(kind TEXT,id TEXT,version INTEGER);")
    scope = {"mission_id": "synthetic-mission", "run_id": "synthetic-run",
             "actor_id": "synthetic-actor", "task_id": "synthetic-task"}
    model = "qwen/qwen3.8-27b"
    intent_ref = {"kind": "figure_request_intent", "id": "figure-intent-one", "version": 1}
    page_ref = {"kind": "public_document_page_image", "id": "page-one", "version": 1}
    ocr_ref = {"kind": "public_document_page_ocr", "id": "ocr-one", "version": 1}
    document_ref = {"kind": "public_document_fetch", "id": "document-one", "version": 1}
    common = {**scope, "document_ref": document_ref, "page_image_ref": page_ref,
              "ocr_ref": ocr_ref, "page_number": 1, "request_sha256": "synthetic-hash"}
    intent = {"schema": "ora.figure_request_intent.v1", **common,
              "model": model, "credential_profile": "backup"}
    response = {"schema": "ora.figure_response_receipt.v1", **common,
                "intent_ref": intent_ref, "model": model, "http_status": 200,
                "usage": {"prompt_tokens": 80, "completion_tokens": 20,
                          "total_tokens": 100}}

    def insert(kind, record_id, body):
        db.execute("INSERT INTO records VALUES(?,?,?,?,?)",
                   (kind, record_id, 1, json.dumps(body), "2026-01-15T15:00:00Z"))
        db.execute("INSERT INTO heads VALUES(?,?,1)", (kind, record_id))

    insert("figure_request_intent", intent_ref["id"], intent)
    insert("figure_response_receipt", "figure-response:figure-intent-one", response)
    insert("figure_dispatch_attempt", "figure-dispatch:figure-intent-one", {})
    insert("figure_dispatch_attempt", "figure-dispatch:figure-intent-unknown", {})
    db.commit()
    calls, coverage = usage.read_state(tmp_path, None)
    assert len(calls) == 1 and calls[0]["profile"] == "backup"
    assert calls[0]["model"] == model
    assert calls[0]["usage"]["total_tokens"] == 100
    assert coverage["uncaptured_figure_dispatch_attempts"] == 1
    assert report(calls)["periods"]["calendar_day"]["profiles"]["backup"]["captured_calls"] == 0
    vision = usage.build_report(calls, as_of=usage.stamp("2026-01-15T17:00:00Z"),
        day=date(2026, 1, 15), zone="America/New_York", model=model, daily_limit=None)
    assert vision["periods"]["calendar_day"]["profiles"]["backup"]["token_totals"][
        "total_tokens"]["known_sum"] == 100
    db.execute("UPDATE records SET body=? WHERE kind='figure_response_receipt'",
               (json.dumps({**response, "intent_ref": {
                   **intent_ref, "version": 2}}),))
    db.commit()
    assert usage.read_state(tmp_path, None)[1]["unverified_figure_receipts"] == 1
    db.close()


def test_supplement_whitelists_fields_and_deduplicates_against_database_call(tmp_path):
    path = tmp_path / "standalone.json"
    path.write_text(json.dumps({"kind": "one_request_backup_credential_check",
        "credential_variable": "GROQ_API_KEY_BACKUP", "observed_at": "2026-01-15T15:00:01Z",
        "request": {"messages": "private prompt"}, "http_status": 200,
        "response": {"id": "response-one", "model": MODEL, "choices": "private reasoning",
                     "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}}))
    calls = usage.read_supplement(path)
    result, _ = usage.deduplicate([call(), *calls])
    assert len(result) == 1 and "private" not in json.dumps(result)
    path.write_text('{"total_tokens": 100000}')
    with pytest.raises(ValueError, match="Unsupported supplement"):
        usage.read_supplement(path)


def test_reports_are_private_and_require_explicit_timezones(tmp_path):
    path = tmp_path / "report.json"
    usage.private_write(path, json.dumps(report([call()])))
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        usage.private_write(path, "replacement")
    with pytest.raises(ValueError, match="timezone"):
        usage.stamp("2026-01-15T12:00:00")
    assert "unknown" in usage.markdown(report([]))


def test_other_models_do_not_consume_the_selected_models_planning_allowance():
    other = call(returned_model="another/model")
    result = report([other], limit=200_000)
    assert result["other_model_receipts_excluded"] == {"another/model": 1}
    assert result["periods"]["calendar_day"]["profiles"]["backup"]["captured_calls"] == 0
    assert result["periods"]["calendar_day"]["profiles"]["backup"][
        "local_planning_headroom_ignoring_cache_and_unobserved_usage"] is None


def logged(calls, sources, at="2026-01-15T17:00:00Z", day=date(2026, 1, 15)):
    result = report(calls, at=at, day=day)
    result.update({"sources": [{"path": str(path)} for path in sources],
                   "deduplication": {}, "legacy_primary_before": None})
    return result


def test_daily_log_repeats_and_older_observations_preserve_newer_history(tmp_path):
    path = tmp_path / "daily.json"
    source = tmp_path / "state"
    fresh = logged([call()], [source], at="2026-01-15T18:00:00Z")
    status, view = usage.update_daily_log(path, fresh)
    assert status == "added" and view["observed_days"] == 1
    assert view["profiles"]["backup"]["token_totals"]["total_tokens"] == {
        "known_sum": 120, "reported_calls": 1}
    assert usage.update_daily_log(path, fresh)[0] == "unchanged_older_or_repeat_observation"
    older = logged([], [source], at="2026-01-15T17:00:00Z")
    assert usage.update_daily_log(path, older)[0] == "unchanged_less_complete_observation"
    assert usage.update_daily_log(path, logged([], [source],
        at="2026-01-15T19:00:00Z"))[0] == "unchanged_less_complete_observation"
    assert len(json.loads(path.read_text())["entries"]) == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_daily_log_separates_coverage_model_timezone_and_keeps_old_days(tmp_path):
    path = tmp_path / "daily.json"
    source_a, source_b = tmp_path / "a", tmp_path / "b"
    narrow = logged([call()], [source_a])
    broad = logged([call()], [source_a, source_b])
    usage.update_daily_log(path, broad)
    status, narrow_view = usage.update_daily_log(path, narrow)
    assert status == "added" and narrow_view["observed_days"] == 1
    earlier = logged([call("prior", "2026-01-14T15:00:00Z")], [source_a],
                     at="2026-01-15T17:00:00Z", day=date(2026, 1, 14))
    usage.update_daily_log(path, earlier)
    later = logged([call("next", "2026-01-16T15:00:00Z")], [source_a],
                   at="2026-01-16T17:00:00Z", day=date(2026, 1, 16))
    _, view = usage.update_daily_log(path, later)
    assert view["observed_days"] == 3
    assert view["profiles"]["backup"]["token_totals"]["total_tokens"]["known_sum"] == 360
    assert len(json.loads(path.read_text())["entries"]) == 4
    assert "Seven local calendar days" in usage.markdown({**later, "rolling_seven_days": view})
    assert "missing" in usage.markdown({**later, "rolling_seven_days": view})


def test_daily_log_preserves_unknown_and_subset_coverage_and_rejects_corruption(tmp_path):
    path = tmp_path / "daily.json"
    incomplete = call(usage={})
    result = logged([incomplete], [tmp_path / "state"])
    _, view = usage.update_daily_log(path, result)
    row = view["profiles"]["backup"]
    assert row["captured_calls"] == 1
    assert row["token_totals"]["total_tokens"]["known_sum"] is None
    assert row["token_totals"]["reasoning_tokens"]["reported_calls"] == 0
    assert view["current_provider_remaining_tokens"] is None
    path.write_text('{"schema":"wrong","entries":{}}')
    with pytest.raises(ValueError, match="Unsupported daily log"):
        usage.update_daily_log(path, result)
    assert '"schema":"wrong"' in path.read_text()


def test_daily_log_keeps_dated_quota_evidence_separate_from_current_capacity(tmp_path):
    path = tmp_path / "daily.json"
    prior = call("prior", "2026-01-14T15:00:00Z")
    prior["quota_observation"] = {"observed_at": prior["observed_at"], "limit": 200,
        "used": 180, "requested": 30, "headroom_at_observation": 20}
    today = call("today", "2026-01-15T15:00:00Z")
    today["quota_observation"] = {"observed_at": today["observed_at"], "limit": 200,
        "used": 195, "requested": 30, "headroom_at_observation": 5}
    source = tmp_path / "state"
    older = logged([prior, today], [source], at="2026-01-15T17:00:00Z",
                   day=date(2026, 1, 14))
    usage.update_daily_log(path, older)
    current = logged([prior, today], [source])
    _, view = usage.update_daily_log(path, current)
    assert current["latest_provider_daily_quota_observations"]["backup"][
        "headroom_at_observation"] == 5
    assert view["days"][-2]["provider_daily_quota_observations"]["backup"][
        "headroom_at_observation"] == 20
    assert view["days"][-1]["provider_daily_quota_observations"]["backup"][
        "headroom_at_observation"] == 5
    assert view["current_provider_remaining_tokens"] is None
    assert all(entry["current_provider_remaining_tokens"] is None for entry in
               json.loads(path.read_text())["entries"].values())
    assert "historical, not current capacity" in usage.markdown(
        {**current, "rolling_seven_days": view})


def test_newer_same_call_count_cannot_erase_known_usage_or_quota(tmp_path):
    path = tmp_path / "daily.json"
    source = tmp_path / "state"
    complete = call()
    complete["quota_observation"] = {"observed_at": complete["observed_at"],
                                     "headroom_at_observation": 10}
    usage.update_daily_log(path, logged([complete], [source]))
    lost = call(usage={})
    status, view = usage.update_daily_log(path, logged(
        [lost], [source], at="2026-01-15T18:00:00Z"))
    assert status == "unchanged_less_complete_observation"
    assert view["profiles"]["backup"]["token_totals"]["total_tokens"]["known_sum"] == 120
    assert view["days"][-1]["provider_daily_quota_observations"]["backup"][
        "headroom_at_observation"] == 10


def test_main_backfills_seven_days_from_selected_saved_calls(tmp_path, monkeypatch, capsys):
    supplement = tmp_path / "calls.json"
    supplement.write_text(json.dumps({"schema": "ora.groq.usage.calls.v1", "calls": [
        {"observed_at": "2026-01-13T15:00:00Z", "credential_profile": "backup",
         "model": MODEL, "response_id": "one",
         "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}},
        {"observed_at": "2026-01-15T15:00:00Z", "credential_profile": "backup",
         "model": MODEL, "response_id": "two",
         "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}]}))
    monkeypatch.setattr(usage, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["usage.py", "--supplement", str(supplement),
                                     "--as-of", "2026-01-15T17:00:00Z"])
    usage.main()
    paths = json.loads(capsys.readouterr().out)
    result = json.loads(Path(paths["receipt"]).read_text())
    assert result["rolling_seven_days"]["observed_days"] == 7
    assert result["rolling_seven_days"]["profiles"]["backup"]["token_totals"][
        "total_tokens"]["known_sum"] == 240
    assert len(json.loads(Path(result["daily_log"]["path"]).read_text())["entries"]) == 7
    usage.main()
    repeated = json.loads(capsys.readouterr().out)
    assert json.loads(Path(repeated["receipt"]).read_text())["daily_log"]["update_status"] == (
        "unchanged_older_or_repeat_observation")
