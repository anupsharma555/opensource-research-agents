"""Summarize saved Groq usage by credential profile without contacting a provider.

Read canonical receipt metadata and narrowly scoped quota errors. Save private,
deduplicated calendar-day and trailing-day reports; account-wide remaining quota
and provider reset semantics are not inferred from local token totals.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[4]
PROFILES = ("primary", "backup", "unknown")
SAFE_MODEL = re.compile(r"[A-Za-z0-9_./:-]{1,128}\Z")
TPD = re.compile(
    r"(?:tokens per day|\bTPD\b).*?Limit\s*:?\s*([\d,]+)"
    r"\s*,?\s*Used\s*:?\s*([\d,]+)\s*,?\s*Requested\s*:?\s*([\d,]+)", re.I
)


def stamp(value: str) -> datetime:
    """Require explicit timezone information instead of guessing local timestamps."""
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def token(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def normalize_usage(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    result = {key: token(raw.get(key)) for key in
              ("prompt_tokens", "completion_tokens", "total_tokens")}
    for key, parent in (("cached_tokens", "prompt"), ("reasoning_tokens", "completion")):
        details = raw.get(f"{parent}_tokens_details")
        count = token(details.get(key)) if isinstance(details, dict) else None
        total = result[f"{parent}_tokens"]
        result[key] = count if count is not None and total is not None and count <= total else None
    return result


def quota_observation(state: Path, body: dict, observed: str) -> dict | None:
    """Extract numbers from one exact small 429 object, never return its text."""
    if body.get("http_status") != 429:
        return None
    blob = body.get("blob") or {}
    digest, size = blob.get("sha256"), blob.get("size")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        return None
    if type(size) is not int or not 0 < size <= 64_000:
        return None
    path = state / "objects" / digest
    try:
        # Disallow symlink escapes and cap the read even when metadata is stale.
        if path.is_symlink() or path.stat().st_size != size:
            return None
        with path.open("rb") as stream:
            data = stream.read(64_001)
        if hashlib.sha256(data).hexdigest() != digest:
            return None
        error = json.loads(data).get("provider_error", {})
        match = TPD.search(str(error.get("message", "")))
    except (OSError, ValueError, AttributeError):
        return None
    if not match:
        return None
    limit, used, requested = (int(part.replace(",", "")) for part in match.groups())
    retry = body.get("retry_after_seconds")
    retry = retry if type(retry) in (int, float) and 0 <= retry < 31_536_000 else None
    return {"observed_at": observed, "limit": limit, "used": used,
            "requested": requested, "headroom_at_observation": max(0, limit - used),
            "retry_after_seconds": retry,
            "retry_not_before": (stamp(observed) + timedelta(seconds=retry)).isoformat()
            if retry is not None else None, "full_reset_at": None}


def exact_config(db: sqlite3.Connection, pointer: object) -> dict:
    if not isinstance(pointer, dict) or pointer.get("kind") != "agent_configuration":
        return {}
    row = db.execute(
        "SELECT json_extract(body,'$.provider'),json_extract(body,'$.model_id'),"
        "json_extract(body,'$.settings.credential_profile'),"
        "json_extract(body,'$.credential_profile') FROM records "
        "WHERE kind='agent_configuration' AND id=? AND version=?",
        (pointer.get("id"), pointer.get("version")),
    ).fetchone()
    return dict(zip(("provider", "model", "settings_profile", "profile"), row)) if row else {}


def exact_figure_intent(db: sqlite3.Connection, pointer: object) -> dict:
    """Read only a response's exact vision intent, never its request blob."""
    if (not isinstance(pointer, dict) or pointer.get("kind") != "figure_request_intent"
            or type(pointer.get("version")) is not int):
        return {}
    row = db.execute(
        "SELECT body FROM records WHERE kind='figure_request_intent' AND id=? AND version=?",
        (pointer.get("id"), pointer.get("version")),
    ).fetchone()
    return json.loads(row[0]) if row else {}


def normalize_call(body: dict, observed: str, identity: str, config: dict,
                   legacy_before: datetime | None) -> dict:
    recorded = [value for value in (body.get("credential_profile"), config.get("profile"),
                                   config.get("settings_profile")) if value is not None]
    profile = recorded[0] if recorded else None
    basis = "recorded"
    if recorded and (any(value not in PROFILES[:2] for value in recorded)
                     or len(set(recorded)) > 1):
        profile, basis = "unknown", "conflicting_or_invalid_profile"
    elif profile not in PROFILES[:2]:
        profile, basis = "unknown", "unattributed"
        if legacy_before and stamp(observed) < legacy_before:
            profile, basis = "primary", "inferred_legacy_primary"
    model = body.get("returned_model") or config.get("model") or "unknown"
    model = model if isinstance(model, str) and SAFE_MODEL.fullmatch(model) else "unknown"
    return {"identity": identity, "observed_at": stamp(observed).isoformat(),
            "profile": profile, "profile_basis": basis, "model": model,
            "usage": normalize_usage(body.get("usage")),
            "http_status": token(body.get("http_status")),
            "quota_observation": None}


def read_state(state: Path, legacy_before: datetime | None) -> tuple[list[dict], dict]:
    path = (state / "state.sqlite3").resolve(strict=True)
    calls = []
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        rows = db.execute(
            "SELECT r.kind,r.id,r.body,(SELECT MIN(v.created_at) FROM records v "
            "WHERE v.kind=r.kind AND v.id=r.id) FROM records r "
            "JOIN heads h USING(kind,id,version) WHERE r.kind IN "
            "('model_response_receipt','figure_response_receipt')"
        ).fetchall()
        excluded_provider = 0
        unverified_figure = 0
        figure_receipts = 0
        for kind, record_id, raw, observed in rows:
            body = json.loads(raw)
            if kind == "figure_response_receipt":
                figure_receipts += 1
                intent = exact_figure_intent(db, body.get("intent_ref"))
                if (body.get("schema") != "ora.figure_response_receipt.v1"
                        or not intent or intent.get("schema") != "ora.figure_request_intent.v1"
                        or intent.get("request_sha256") != body.get("request_sha256")
                        or intent.get("model") != body.get("model")
                        or any(intent.get(key) != body.get(key) for key in (
                            "mission_id", "run_id", "actor_id", "task_id",
                            "document_ref", "page_image_ref", "ocr_ref", "page_number",
                        ))):
                    unverified_figure += 1
                    continue
                call = normalize_call(
                    body, observed, f"figure:{record_id}",
                    {"model": intent["model"], "profile": intent.get("credential_profile")},
                    legacy_before,
                )
                calls.append(call)
                continue
            config = exact_config(db, body.get("configuration_ref"))
            if config.get("provider") != "groq":
                excluded_provider += 1
                continue
            identity = body.get("response_id") or body.get("provider_request_id") or record_id
            call = normalize_call(body, observed, str(identity), config, legacy_before)
            call["quota_observation"] = quota_observation(state, body, observed)
            calls.append(call)
        uncaptured_figure_dispatches = db.execute(
            "SELECT COUNT(*) FROM records d JOIN heads h USING(kind,id,version) "
            "WHERE d.kind='figure_dispatch_attempt' AND NOT EXISTS ("
            "SELECT 1 FROM records r JOIN heads rh USING(kind,id,version) "
            "WHERE r.kind='figure_response_receipt' "
            "AND r.id='figure-response:' || substr(d.id,length('figure-dispatch:')+1))"
        ).fetchone()[0]
    return calls, {"path": str(path), "receipt_records": len(rows),
                   "non_groq_or_unverified_provider_records": excluded_provider,
                   "figure_receipt_records": figure_receipts,
                   "unverified_figure_receipts": unverified_figure,
                   "uncaptured_figure_dispatch_attempts": uncaptured_figure_dispatches}


def read_supplement(path: Path) -> list[dict]:
    if path.stat().st_size > 2_000_000:
        raise ValueError("Supplement exceeds the 2 MB call-ledger boundary")
    raw = json.loads(path.read_text())
    if raw.get("kind") == "one_request_backup_credential_check":
        response = raw.get("response", {})
        profile = {"GROQ_API_KEY": "primary", "GROQ_API_KEY_BACKUP": "backup"}.get(
            raw.get("credential_variable"), "unknown")
        rows = [{"observed_at": raw.get("observed_at"), "credential_profile": profile,
                 "model": response.get("model"), "response_id": response.get("id"),
                 "usage": response.get("usage"), "http_status": raw.get("http_status")}]
    elif raw.get("schema") == "ora.groq.usage.calls.v1" and isinstance(raw.get("calls"), list):
        rows = raw["calls"]
    else:
        raise ValueError("Unsupported supplement; use an exact call ledger, not aggregate totals")
    result = []
    for row in rows:
        identity = row.get("response_id") or row.get("provider_request_id")
        if not isinstance(identity, str) or not identity:
            raise ValueError("Each supplemental call needs a retained provider response/request ID")
        result.append(normalize_call(row, row["observed_at"], identity,
                                     {"model": row.get("model")}, None))
    return result


def deduplicate(calls: list[dict]) -> tuple[list[dict], dict]:
    unique, conflicted = {}, set()
    repeats = 0
    for call in calls:
        key = call["identity"]
        if key not in unique:
            unique[key] = call
            continue
        repeats += 1
        prior = unique[key]
        # Supplemental timestamps may follow capture by seconds. They must still
        # agree on provider quantities, attribution, and the model.
        if any(prior[field] != call[field] for field in ("usage", "profile", "model")):
            conflicted.add(key)
        elif stamp(call["observed_at"]) < stamp(prior["observed_at"]):
            unique[key] = call
    return [v for k, v in unique.items() if k not in conflicted], {
        "repeated_receipts_removed": repeats, "conflicting_identities_excluded": len(conflicted)}


def summarize(calls: list[dict], daily_limit: int | None) -> dict:
    usage = [c["usage"] for c in calls]
    totals = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens",
                 "reasoning_tokens", "cached_tokens"):
        known = [u[name] for u in usage if u[name] is not None]
        totals[name] = {"known_sum": sum(known) if known else None, "reported_calls": len(known)}
    complete = [u for u in usage if all(u[n] is not None for n in
                                      ("prompt_tokens", "completion_tokens", "total_tokens"))]
    mismatched = sum(u["prompt_tokens"] + u["completion_tokens"] != u["total_tokens"]
                     for u in complete)
    total = totals["total_tokens"]["known_sum"]
    return {"captured_calls": len(calls), "complete_usage_calls": len(complete),
            "calls_with_incomplete_usage": len(calls) - len(complete),
            "token_totals": totals, "inconsistent_token_total_calls": mismatched,
            "http_errors": dict(sorted(Counter(str(c["http_status"]) for c in calls
                                if c["http_status"] and c["http_status"] >= 400).items())),
            "profile_attribution": dict(Counter(c["profile_basis"] for c in calls)),
            "local_planning_headroom_ignoring_cache_and_unobserved_usage":
                max(0, daily_limit - total) if daily_limit is not None and total is not None
                and not mismatched else None,
            "current_provider_remaining_tokens": None}


def build_report(calls: list[dict], *, as_of: datetime, day: date, zone: str,
                 model: str, daily_limit: int | None) -> dict:
    if daily_limit is not None and daily_limit <= 0:
        raise ValueError("Daily planning allowance must be positive")
    tz = ZoneInfo(zone)
    start = datetime.combine(day, time.min, tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(timezone.utc)
    if start > as_of:
        raise ValueError("The selected date is after the observation time")
    ranges = {"calendar_day": (start, min(end, as_of)),
              "trailing_24_hours": (as_of - timedelta(hours=24), as_of)}
    selected = [c for c in calls if c["model"] == model and stamp(c["observed_at"]) < as_of]
    periods = {}
    for name, (lo, hi) in ranges.items():
        included = [c for c in selected if lo <= stamp(c["observed_at"]) < hi]
        profiles = {p: summarize([c for c in included if c["profile"] == p],
                                 daily_limit if p != "unknown" else None) for p in PROFILES}
        periods[name] = {"start_inclusive": lo.isoformat(), "end_exclusive": hi.isoformat(),
                         "profiles": profiles}
    latest = {}
    daily_latest = {}
    day_start, day_end = ranges["calendar_day"]
    for profile in PROFILES:
        observed = [c["quota_observation"] for c in selected
                    if c["profile"] == profile and c["quota_observation"]]
        latest[profile] = max(observed, key=lambda o: stamp(o["observed_at"])) if observed else None
        dated = [value for value in observed
                 if day_start <= stamp(value["observed_at"]) < day_end]
        daily_latest[profile] = (max(dated, key=lambda o: stamp(o["observed_at"]))
                                 if dated else None)
    assumed_resets = {}
    for profile, observation in latest.items():
        assumed_resets[profile] = None
        if profile == "unknown" or not observation:
            continue
        limit, used = token(observation.get("limit")), token(observation.get("used"))
        if not limit or used is None or used * 100 < limit * 95:
            continue
        reset = stamp(observation["observed_at"]) + timedelta(hours=24)
        assumed_resets[profile] = {
            "basis": "user_requested_24h_after_latest_near_limit_observation",
            "near_limit_threshold_fraction": 0.95,
            "anchor_observed_at": observation["observed_at"],
            "observed_used": used, "observed_daily_limit": limit,
            "assumed_full_reset_at": reset.isoformat(),
            "status": "elapsed_unverified" if reset <= as_of else "future_assumption",
            "provider_confirmed": False,
        }
    return {"schema": "ora.groq.usage.summary.v1", "skill_version": "0.2.4",
            "observed_at": as_of.isoformat(), "timezone": zone, "date": day.isoformat(),
            "model": model, "planning_daily_allowance": daily_limit, "periods": periods,
            "latest_provider_daily_quota_observations": latest,
            "calendar_day_provider_daily_quota_observations": daily_latest,
            "assumed_full_reset_estimates": assumed_resets,
            "other_model_receipts_excluded": dict(Counter(c["model"] for c in calls
                                                        if c["model"] != model)),
            "coverage": "Only explicitly selected local databases and standalone call receipts",
            "provider_daily_window_semantics": "unverified; no midnight or rolling-reset assumption",
            "quota_independence_between_profiles": "not established by this report",
            "current_account_wide_remaining_tokens": None,
            "limitations": ["Missing usage is unknown consumption, not zero.",
                "Reasoning and cache values are subsets, not additional tokens.",
                "Local planning headroom is not confirmed capacity or a bound.",
                "Uncaptured responses and other applications may consume unobserved tokens.",
                "Retry-after is not a prediction of full daily-quota reset."]}


def markdown(report: dict) -> str:
    lines = ["# Groq token usage", "", f"Observed: {report['observed_at']}",
             f"Local date: {report['date']} ({report['timezone']})",
             f"Model: {report['model']}", "",
             "These are saved local call totals. Current organization-wide remaining tokens are unknown."]
    for name, period in report["periods"].items():
        notes = []
        lines += ["", f"## {name.replace('_', ' ').capitalize()}", "",
                  f"{period['start_inclusive']} through {period['end_exclusive']} (end excluded).", "",
                  "| Profile | Captured calls | Input | Completion | Total | Incomplete usage | Planning headroom* |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for p, row in period["profiles"].items():
            values = [row["token_totals"][k]["known_sum"] for k in
                      ("prompt_tokens", "completion_tokens", "total_tokens")]
            values += [row["calls_with_incomplete_usage"],
                       row["local_planning_headroom_ignoring_cache_and_unobserved_usage"]]
            lines.append(f"| {p} | {row['captured_calls']} | " + " | ".join(
                f"{v:,}" if v is not None else "unknown" for v in values) + " |")
            if row["captured_calls"]:
                notes.append(f"{p}: attribution {row['profile_attribution']}; "
                             f"HTTP errors {row['http_errors'] or 'none recorded'}.")
                if row["inconsistent_token_total_calls"]:
                    notes.append(f"{p}: {row['inconsistent_token_total_calls']} call(s) have "
                                 "inconsistent input/completion/total quantities; investigate before "
                                 "relying on these totals.")
        lines += ["", *notes]
    if rolling := report.get("rolling_seven_days"):
        lines += ["", "## Seven local calendar days", "",
                  f"{rolling['start_date']} through {rolling['end_date']}; "
                  f"{rolling['observed_days']}/7 days have saved observations for this exact scope.", "",
                  "| Date | Primary total | Backup total | Unknown total | Observation |",
                  "| --- | ---: | ---: | ---: | --- |"]
        for item in rolling["days"]:
            period = item["calendar_day"]
            if period is None:
                values = ["missing"] * 3
            else:
                values = [str(period["profiles"][profile]["token_totals"]["total_tokens"]
                              ["known_sum"])
                          if period["profiles"][profile]["token_totals"]["total_tokens"]
                          ["known_sum"] is not None else "unknown" for profile in PROFILES]
            lines.append(f"| {item['date']} | {' | '.join(values)} | "
                         f"{item['observed_at'] or 'not observed'} |")
        lines += ["", "Known token sums across observed days (input / completion / total; "
                  "reasoning and cache remain subsets):"]
        for profile, row in rolling["profiles"].items():
            counts = row["token_totals"]
            lines.append(f"- {profile}: " + " / ".join(
                str(counts[name]["known_sum"]) if counts[name]["known_sum"] is not None
                else "unknown" for name in
                ("prompt_tokens", "completion_tokens", "total_tokens")) +
                f"; {row['captured_calls']} calls, "
                f"{row['calls_with_incomplete_usage']} with incomplete usage.")
        quota_days = []
        for item in rolling["days"]:
            for profile, observation in (item["provider_daily_quota_observations"] or {}).items():
                if observation:
                    quota_days.append(f"- {item['date']} {profile}: "
                                      f"{observation['headroom_at_observation']:,} "
                                      f"headroom observed at {observation['observed_at']}.")
        if quota_days:
            lines += ["", "Dated provider quota observations (historical, not current capacity):",
                      *quota_days]
        lines += ["", rolling["coverage"]]
    lines += ["", "*Planning headroom = supplied allowance minus known local total tokens. "
              "It ignores cache deductions, unobserved calls and uncertain provider window semantics; "
              "it is not a balance or permission to dispatch.", "", "## Latest daily-quota evidence", ""]
    def local_time(value: str | None) -> str:
        if value is None:
            return "Unknown — not reported"
        return stamp(value).astimezone(ZoneInfo(report["timezone"])).strftime(
            "%b %d, %Y %I:%M:%S %p %Z"
        )

    lines += [
        f"Times shown in {report['timezone']}; historical observations, not current capacity.", "",
        "| API-key profile | Observed at | Used / daily limit | Earliest retry for that request | Full daily allowance available at |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for profile, observation in report["latest_provider_daily_quota_observations"].items():
        if observation:
            reset_text = local_time(observation.get("full_reset_at"))
            estimate = report.get("assumed_full_reset_estimates", {}).get(profile)
            if observation.get("full_reset_at") is None and estimate:
                label = ("assumed; elapsed, unverified" if estimate["status"] == "elapsed_unverified"
                         else "assumed")
                reset_text = f"{local_time(estimate['assumed_full_reset_at'])} ({label})"
            lines.append(
                f"| {profile} | {local_time(observation['observed_at'])} | "
                f"{observation['used']:,} / {observation['limit']:,} | "
                f"{local_time(observation.get('retry_not_before'))} | "
                f"{reset_text} |"
            )
        else:
            lines.append(f"| {profile} | Unknown — no observation | Unknown | Unknown | Unknown — no observation |")
    lines += ["", "Full daily allowance means the entire daily limit shown, not enough tokens "
              "for one retry. Assumed times use the requested planning rule: 24 hours after the "
              "latest provider observation at or above 95% of its daily limit (190K of 200K). "
              "This is not a confirmed Groq reset rule. No qualifying observation means no estimate. "
              "Later usage can change availability; an elapsed estimate does not establish a full balance."]
    lines += ["", report["coverage"], "", *[f"- {x}" for x in report["limitations"]], ""]
    for source in report.get("sources", []):
        lines.append(f"Source: {source['path']}")
    if report.get("legacy_primary_before"):
        lines += ["", f"Legacy primary attribution is inferred before {report['legacy_primary_before']}."]
    if report.get("deduplication", {}).get("conflicting_identities_excluded"):
        lines += ["", "Conflicting duplicate call identities were excluded; inspect the JSON coverage before relying on totals."]
    return "\n".join(lines)


def combined_markdown(reports: dict[str, dict]) -> str:
    """Put model and profile on every row; never imply a shared token balance."""
    first = next(iter(reports.values()))
    selected_zone = ZoneInfo(first["timezone"])

    def local_time(value: str | None) -> str:
        return (stamp(value).astimezone(selected_zone).strftime("%b %d, %Y %I:%M:%S %p %Z")
                if value else "Unknown")

    lines = ["# Groq token usage by model and profile", "",
             f"Observed: {first['observed_at']}",
             f"Local date: {first['date']} ({first['timezone']})", "",
             "Saved local calls only. Quota and reset evidence stays in its model/profile "
             "lane; profiles do not prove separate organizations. "
             "Current organization-wide remaining capacity is unknown."]
    for period_name in ("calendar_day", "trailing_24_hours"):
        lines += ["", f"## {period_name.replace('_', ' ').capitalize()}", "",
                  "| Model | Profile | Calls | Input | Completion | Total | Incomplete usage | Planning headroom* |",
                  "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for model, report in reports.items():
            for profile, row in report["periods"][period_name]["profiles"].items():
                counts = row["token_totals"]
                values = [row["captured_calls"], counts["prompt_tokens"]["known_sum"],
                          counts["completion_tokens"]["known_sum"],
                          counts["total_tokens"]["known_sum"],
                          row["calls_with_incomplete_usage"],
                          row["local_planning_headroom_ignoring_cache_and_unobserved_usage"]]
                lines.append(f"| {model} | {profile} | " + " | ".join(
                    f"{value:,}" if value is not None else "unknown" for value in values) + " |")
    lines += ["", "## Seven local calendar days", "",
              "| Date | Model | Primary total | Backup total | Unknown total | Observation |",
              "| --- | --- | ---: | ---: | ---: | --- |"]
    for model, report in reports.items():
        for item in report["rolling_seven_days"]["days"]:
            period = item["calendar_day"]
            values = ["missing"] * len(PROFILES) if period is None else [
                str(period["profiles"][profile]["token_totals"]["total_tokens"]["known_sum"])
                if period["profiles"][profile]["token_totals"]["total_tokens"]["known_sum"]
                is not None else "unknown" for profile in PROFILES]
            lines.append(f"| {item['date']} | {model} | {' | '.join(values)} | "
                         f"{item['observed_at'] or 'not observed'} |")
    lines += ["", "## Latest daily-quota evidence", "",
              f"Times shown in {first['timezone']}; historical observations, not current capacity.", "",
              "| Model | Profile | Observed at | Used / daily limit | Earliest retry | Full allowance at |",
              "| --- | --- | --- | ---: | --- | --- |"]
    for model, report in reports.items():
        for profile, observation in report["latest_provider_daily_quota_observations"].items():
            if observation is None:
                lines.append(f"| {model} | {profile} | Unknown | Unknown | Unknown | Unknown |")
                continue
            estimate = report["assumed_full_reset_estimates"].get(profile)
            reset = observation.get("full_reset_at")
            if reset is None and estimate:
                label = ("assumed; elapsed, unverified" if
                         estimate["status"] == "elapsed_unverified" else "assumed")
                reset = f"{local_time(estimate['assumed_full_reset_at'])} ({label})"
            else:
                reset = local_time(reset)
            lines.append(f"| {model} | {profile} | {local_time(observation['observed_at'])} | "
                         f"{observation['used']:,} / {observation['limit']:,} | "
                         f"{local_time(observation.get('retry_not_before'))} | {reset} |")
    lines += ["", "*Planning headroom is per model and profile arithmetic from an explicit "
              "allowance. It is not confirmed capacity. A nominal sum across model "
              "allowances needs explicit quota scope and assumptions; it is not a shared, "
              "per-call, or currently available balance. Profile headroom needs verified "
              "independent organization scopes before summing. Retry timing does not "
              "establish a full reset. "
              "Missing usage and uncaptured calls remain unknown.", ""]
    return "\n".join(lines)


def private_write(path: Path, content: str) -> None:
    with open(path, "x", encoding="utf-8", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        stream.write(content)


def daily_scope(report: dict) -> dict:
    """Keep unlike coverage or attribution assumptions in separate history lanes."""
    return {"model": report["model"], "timezone": report["timezone"],
            "sources": sorted({source["path"] for source in report["sources"]}),
            "legacy_primary_before": report["legacy_primary_before"],
            "planning_daily_allowance": report["planning_daily_allowance"]}


def seven_day_view(entries: dict, scope: dict, day: date) -> dict:
    days = []
    scope_key = json.dumps(scope, sort_keys=True)
    for offset in range(6, -1, -1):
        label = (day - timedelta(days=offset)).isoformat()
        key = hashlib.sha256(f"{scope_key}\n{label}".encode()).hexdigest()
        entry = entries.get(key)
        days.append({"date": label, "observed_at": entry["observed_at"] if entry else None,
                     "calendar_day": entry["calendar_day"] if entry else None,
                     "provider_daily_quota_observations":
                         entry.get("provider_daily_quota_observations") if entry else None})
    totals = {}
    for profile in PROFILES:
        rows = [item["calendar_day"]["profiles"][profile] for item in days
                if item["calendar_day"]]
        tokens = {}
        for name in ("prompt_tokens", "completion_tokens", "total_tokens",
                     "reasoning_tokens", "cached_tokens"):
            values = [row["token_totals"][name] for row in rows]
            reported = sum(value["reported_calls"] for value in values)
            tokens[name] = {"known_sum": sum(value["known_sum"] or 0 for value in values)
                            if reported else None, "reported_calls": reported}
        totals[profile] = {"captured_calls": sum(row["captured_calls"] for row in rows),
                           "calls_with_incomplete_usage": sum(
                               row["calls_with_incomplete_usage"] for row in rows),
                           "token_totals": tokens}
    return {"start_date": days[0]["date"], "end_date": days[-1]["date"],
            "days": days, "observed_days": sum(item["calendar_day"] is not None for item in days),
            "profiles": totals, "current_provider_remaining_tokens": None,
            "coverage": "Saved observations for this exact source/model/timezone scope only; "
                        "missing days and uncaptured calls are unknown. Quota headroom "
                        "is historical at its timestamp, never current remaining capacity."}


def update_daily_log(path: Path, report: dict) -> tuple[str, dict]:
    """Merge one daily observation under a lock; never discard earlier history."""
    scope = daily_scope(report)
    day = report["date"]
    key = hashlib.sha256(f"{json.dumps(scope, sort_keys=True)}\n{day}".encode()).hexdigest()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if path.is_symlink():
            raise ValueError("Daily log path must not be a symlink")
        history = json.loads(path.read_text()) if path.exists() else {
            "schema": "ora.groq.usage.daily-log.v1", "entries": {}}
        if history.get("schema") != "ora.groq.usage.daily-log.v1" or not isinstance(
                history.get("entries"), dict):
            raise ValueError("Unsupported daily log; preserving existing file")
        entries = history["entries"]
        previous = entries.get(key)
        if previous and (previous.get("scope") != scope or previous.get("date") != day):
            raise ValueError("Daily log scope collision; preserving existing file")
        incoming = {"scope": scope, "date": day, "observed_at": report["observed_at"],
                    "calendar_day": report["periods"]["calendar_day"],
                    "provider_daily_quota_observations":
                        report["calendar_day_provider_daily_quota_observations"],
                    "current_provider_remaining_tokens": None,
                    "sources": report["sources"], "deduplication": report["deduplication"]}
        if previous:
            for profile, older in previous.get("provider_daily_quota_observations", {}).items():
                newer = incoming["provider_daily_quota_observations"].get(profile)
                if older and (not newer or stamp(older["observed_at"]) >
                              stamp(newer["observed_at"])):
                    incoming["provider_daily_quota_observations"][profile] = older
        old_calls = sum(row["captured_calls"] for row in
                        previous["calendar_day"]["profiles"].values()) if previous else 0
        new_calls = sum(row["captured_calls"] for row in
                        incoming["calendar_day"]["profiles"].values())
        lost_coverage = previous and any(
            incoming["calendar_day"]["profiles"][profile]["token_totals"][name]
            ["reported_calls"] < previous["calendar_day"]["profiles"][profile]
            ["token_totals"][name]["reported_calls"]
            for profile in PROFILES for name in
            ("prompt_tokens", "completion_tokens", "total_tokens",
             "reasoning_tokens", "cached_tokens"))
        if previous and (new_calls < old_calls or lost_coverage):
            status = "unchanged_less_complete_observation"
        elif previous and (stamp(previous["observed_at"]) > stamp(incoming["observed_at"])
                           or (stamp(previous["observed_at"]) == stamp(incoming["observed_at"])
                               and new_calls <= old_calls)):
            status = "unchanged_older_or_repeat_observation"
        else:
            entries[key] = incoming
            payload = json.dumps(history, indent=2) + "\n"
            temporary = None
            try:
                with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".groq-usage-",
                                                 delete=False, encoding="utf-8") as stream:
                    temporary = Path(stream.name)
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            status = "added" if previous is None else "updated_newer_observation"
        return status, seven_day_view(entries, scope, date.fromisoformat(day))
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, action="append", default=[])
    parser.add_argument("--supplement", type=Path, action="append", default=[])
    parser.add_argument("--timezone", default="America/New_York")
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--as-of", type=stamp)
    parser.add_argument("--model", action="append", help="Repeat for separate model lanes")
    parser.add_argument("--daily-limit", type=int)
    parser.add_argument("--legacy-primary-before", type=stamp)
    args = parser.parse_args()
    if not args.state_dir and not args.supplement:
        parser.error("Select a saved state directory or standalone receipt")
    models = list(dict.fromkeys(args.model or ["openai/gpt-oss-120b"]))
    if any(not SAFE_MODEL.fullmatch(model) for model in models):
        parser.error("Model IDs must be simple provider/model identifiers")
    as_of = args.as_of or datetime.now(timezone.utc)
    day = args.date or as_of.astimezone(ZoneInfo(args.timezone)).date()
    calls, sources = [], []
    for state in dict.fromkeys(p.resolve() for p in args.state_dir):
        found, source = read_state(state, args.legacy_primary_before)
        calls.extend(found)
        sources.append(source)
    for path in dict.fromkeys(p.resolve() for p in args.supplement):
        found = read_supplement(path)
        calls.extend(found)
        sources.append({"path": str(path), "supplemental_calls": len(found)})
    calls, duplicates = deduplicate(calls)
    folder = ROOT / ".local/checks"
    folder.mkdir(parents=True, exist_ok=True)
    daily_path = folder / "groq-usage-daily-log.json"
    reports, per_model_paths = {}, {}
    for model in models:
        report = build_report(calls, as_of=as_of, day=day, zone=args.timezone,
                              model=model, daily_limit=args.daily_limit)
        report.update({"sources": sources, "deduplication": duplicates,
                       "legacy_primary_before": args.legacy_primary_before.isoformat()
                       if args.legacy_primary_before else None})
        for offset in range(6, 0, -1):
            prior_day = day - timedelta(days=offset)
            prior = build_report(calls, as_of=as_of, day=prior_day, zone=args.timezone,
                                 model=model, daily_limit=args.daily_limit)
            prior.update({"sources": sources, "deduplication": duplicates,
                          "legacy_primary_before": report["legacy_primary_before"]})
            update_daily_log(daily_path, prior)
        status, rolling = update_daily_log(daily_path, report)
        report["daily_log"] = {"path": str(daily_path), "update_status": status,
                               "scope": daily_scope(report)}
        report["rolling_seven_days"] = rolling
        stem = f"groq-usage-{day.isoformat()}-{uuid.uuid4().hex[:12]}"
        paths = [folder / (stem + suffix) for suffix in (".json", ".md")]
        private_write(paths[0], json.dumps(report, indent=2) + "\n")
        private_write(paths[1], markdown(report))
        reports[model] = report
        per_model_paths[model] = {"receipt": str(paths[0]), "summary": str(paths[1])}
    if len(models) == 1:
        print(json.dumps(per_model_paths[models[0]]))
        return
    combined = {"schema": "ora.groq.usage.multi-summary.v1", "skill_version": "0.2.4",
                "observed_at": as_of.isoformat(), "date": day.isoformat(),
                "timezone": args.timezone, "deduplication": duplicates,
                "sources": sources, "models": {model: {
                    **per_model_paths[model], "periods": report["periods"],
                    "rolling_seven_days": report["rolling_seven_days"],
                    "latest_provider_daily_quota_observations":
                        report["latest_provider_daily_quota_observations"],
                    "assumed_full_reset_estimates": report["assumed_full_reset_estimates"],
                    "daily_log": report["daily_log"]} for model, report in reports.items()},
                "current_account_wide_remaining_tokens": None,
                "quota_independence_between_profiles": "not established by this report"}
    stem = f"groq-usage-combined-{day.isoformat()}-{uuid.uuid4().hex[:12]}"
    paths = [folder / (stem + suffix) for suffix in (".json", ".md")]
    private_write(paths[0], json.dumps(combined, indent=2) + "\n")
    private_write(paths[1], combined_markdown(reports))
    print(json.dumps({"receipt": str(paths[0]), "summary": str(paths[1]),
                      "model_reports": per_model_paths}))


if __name__ == "__main__":
    main()
