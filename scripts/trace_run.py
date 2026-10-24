"""Inspect or explicitly project/export one saved run's metadata-only traces.

The default is a read-only local report. Projection writes only local span
records; export alone loads trace configuration and contacts LangSmith.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
from collections import Counter
from pathlib import Path

from ora.observability import build_context_metrics
from ora.observability.export import (
    EXPORT_KIND,
    MAX_PAGE,
    ExportConfig,
    ToolActivityCache,
    export_pending,
    project_run,
    project_tool_activity,
)
from ora.observability.tool_activity import build_tool_activity
from ora.state import StateError, StateStore

ROOT = Path(__file__).resolve().parents[1]
TRACE_ENV_KEYS = frozenset({
    "OSRA_TRACE_EXPORT_ENABLED", "OSRA_TRACE_OTLP_TRACES_ENDPOINT",
    "OSRA_TRACE_LANGSMITH_PROJECT", "LANGSMITH_API_KEY",
})
_END_CURSOR = "\U0010ffff"
_END_LABEL = "END"
_SAFE_EXPORT_ERRORS = frozenset({
    "timeout", "connection_lost", "http_rejected", "redirect_rejected",
    "configuration_error", "dependency_unavailable", "transport_error",
    "sdk_export_failed", "sender_error",
})
_SAFE_LEGACY_REASONS = frozenset({
    "acknowledgement_unknown", "interrupted_export", "invalid_projection",
})


def _export_env() -> dict[str, str]:
    """Parse only trace keys from this checkout's .env; process env wins."""
    selected: dict[str, str] = {}
    path = ROOT / ".env"
    if path.is_file():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped[7:].lstrip()
            key, separator, value = stripped.partition("=")
            key = key.strip()
            if separator != "=" or key not in TRACE_ENV_KEYS:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            selected[key] = value
    selected.update({key: os.environ[key] for key in TRACE_ENV_KEYS if key in os.environ})
    return selected


def _export_counts(store: StateStore, mission_id: str, run_id: str) -> dict[str, int]:
    db = store._connect()
    try:
        rows = db.execute(
            "SELECT json_extract(r.body,'$.status') AS status, COUNT(*) AS count "
            "FROM records r JOIN heads h USING(kind,id,version) "
            "WHERE r.kind=? AND json_extract(r.body,'$.mission_id')=? "
            "AND json_extract(r.body,'$.run_id')=? GROUP BY status",
            (EXPORT_KIND, mission_id, run_id),
        ).fetchall()
    finally:
        db.close()
    counts = Counter({"pending": 0, "sending": 0, "sent": 0, "uncertain": 0})
    for row in rows:
        counts[row["status"] if row["status"] in counts else "other"] += row["count"]
    return {**counts, "total": sum(row["count"] for row in rows)}


def _export_errors(store: StateStore, mission_id: str, run_id: str) -> dict:
    """Group only sanitized uncertain-send diagnostics, never raw error text."""
    db = store._connect()
    try:
        rows = db.execute(
            "SELECT json_extract(r.body,'$.error_code') AS code, "
            "json_extract(r.body,'$.http_status') AS http_status, "
            "json_extract(r.body,'$.reason') AS reason, COUNT(*) AS count "
            "FROM records r JOIN heads h USING(kind,id,version) "
            "WHERE r.kind=? AND json_extract(r.body,'$.mission_id')=? "
            "AND json_extract(r.body,'$.run_id')=? "
            "AND json_extract(r.body,'$.status')='uncertain' "
            "GROUP BY code,http_status,reason",
            (EXPORT_KIND, mission_id, run_id),
        ).fetchall()
    finally:
        db.close()
    codes: Counter[str] = Counter()
    http: Counter[str] = Counter()
    legacy: Counter[str] = Counter()
    for row in rows:
        count = row["count"]
        code = row["code"]
        if isinstance(code, str) and code in _SAFE_EXPORT_ERRORS:
            codes[code] += count
        elif code is not None:
            codes["other"] += count
        else:
            reason = row["reason"]
            legacy[reason if reason in _SAFE_LEGACY_REASONS else "unclassified"] += count
        status = row["http_status"]
        if type(status) is int and 100 <= status <= 599:
            http[str(status)] += count
    most_common = sorted(http.items(), key=lambda item: (-item[1], item[0]))
    visible_http = dict(most_common[:12])
    return {
        "uncertain_total": sum(row["count"] for row in rows),
        "by_error_code": dict(sorted(codes.items())),
        "by_http_status": visible_http,
        "other_http_status_count": sum(count for _status, count in most_common[12:]),
        "legacy_without_error_code": dict(sorted(legacy.items())),
    }


def _usage(usage: dict) -> dict:
    def field(name: str) -> dict:
        saved = usage[name]
        return {"known_tokens": saved["tokens"] if saved["reported_calls"] else None,
                "reported_calls": saved["reported_calls"],
                "unknown_calls": saved["unknown_calls"],
                "not_reported_calls": saved["not_reported_calls"],
                "invalid_calls": saved["invalid_calls"],
                "legacy_uncertain_calls": saved["legacy_uncertain_calls"]}

    fraction = usage["cache_fraction"]
    return {
        "input": field("prompt_tokens"),
        "output": field("completion_tokens"),
        "total": field("total_tokens"),
        "reasoning_output_subset": field("reasoning"),
        "cache_read_input_subset": field("cache_read"),
        "cache_fraction_reported_subset": {
            "value": fraction["value"],
            "covered_calls": fraction["covered_calls"],
            "total_calls": fraction["total_calls"],
            "covered_input_tokens": fraction["covered_input_tokens"],
            "coverage_status": fraction["coverage_status"],
            "eligible_input_coverage_fraction": fraction["eligible_input_coverage_fraction"],
            "reported_hit_calls": fraction["reported_hit_calls"],
            "reported_miss_calls": fraction["reported_miss_calls"],
        },
        "cache_fraction_confirmed_of_reported_input": usage["cache_fraction_confirmed"],
    }


def _loops(metrics: dict, kind: str = "model") -> list[dict]:
    rows = []
    for item in metrics.get("calls", []):
        if item.get("kind") != kind:
            continue
        usage = item["usage"]
        rows.append({
            "call_id": item.get("call_id", item.get("intent_id")), "status": item["status"],
            "dispatch_recorded": item["dispatch_recorded"],
            "dispatch_created_at": item.get("dispatch_created_at"),
            "response_created_at": item.get("response_created_at"),
            "input": usage["prompt_tokens"],
            "output": usage["completion_tokens"],
            "reasoning_output_subset": usage["reasoning"],
            "cache_read_input_subset": usage["cache_read"],
            "cache_observation": item["cache_observation"],
            "token_measurement": item.get("token_measurement", {}),
        })
    # Preparation order can differ from dispatch order when peers wait for
    # capacity. Number the actual request sequence, leaving unknown times last.
    return sorted(rows, key=lambda row: (row.get("dispatch_created_at")
                                        or row.get("response_created_at") or "~",
                                        row["call_id"] or ""))


def _project(store: StateStore, mission_id: str, run_id: str,
             *, max_spans: int, page_size: int, max_pages: int,
             after_receipt_id: str = "", after_trace_id: str = "",
             after_figure_receipt_id: str = "", after_tool_id: str = "") -> dict:
    receipt_cursor = _END_CURSOR if after_receipt_id == _END_LABEL else after_receipt_id
    trace_cursor = _END_CURSOR if after_trace_id == _END_LABEL else after_trace_id
    figure_cursor = _END_CURSOR if after_figure_receipt_id == _END_LABEL else after_figure_receipt_id
    tool_cursor = _END_CURSOR if after_tool_id == _END_LABEL else after_tool_id
    activity_cache = ToolActivityCache()
    created = scanned = pages = 0
    complete = False
    while pages < max_pages:
        remaining = max_spans - created
        # A page can create one anchor plus one span per row in each stream.
        if remaining < 4:
            break
        limit = min(page_size, (remaining - 1) // 3)
        page = project_run(store, mission_id, run_id=run_id, limit=limit,
                           after_receipt_id=receipt_cursor,
                           after_trace_id=trace_cursor,
                           after_figure_receipt_id=figure_cursor)
        pages += 1
        created += page["created"]
        scanned += page["scanned"]
        receipt_cursor = page["next_after_receipt_id"] or _END_CURSOR
        trace_cursor = page["next_after_trace_id"] or _END_CURSOR
        figure_cursor = page["next_after_figure_receipt_id"] or _END_CURSOR
        tool_room = max_spans - created
        if tool_cursor != _END_CURSOR and tool_room >= 2:
            # Reserve one anchor slot even if another stream already created it.
            tool_page = project_tool_activity(
                store, mission_id, run_id, limit=min(page_size, tool_room - 1),
                after_id=tool_cursor,
                activity_cache=activity_cache,
            )
            created += tool_page["created"]
            scanned += tool_page["scanned"]
            tool_cursor = tool_page["next_after_id"] or _END_CURSOR
        if receipt_cursor == trace_cursor == figure_cursor == tool_cursor == _END_CURSOR:
            complete = True
            break
    return {
        "created": created, "scanned": scanned, "pages": pages,
        "complete": complete,
        "next_after_receipt_id": None if complete else _END_LABEL if receipt_cursor == _END_CURSOR else receipt_cursor,
        "next_after_trace_id": None if complete else _END_LABEL if trace_cursor == _END_CURSOR else trace_cursor,
        "next_after_figure_receipt_id": None if complete else _END_LABEL if figure_cursor == _END_CURSOR else figure_cursor,
        "next_after_tool_id": None if complete else _END_LABEL if tool_cursor == _END_CURSOR else tool_cursor,
        "note": ("Pass the returned cursors to continue. Projection is idempotent; "
                 "no sent or uncertain span is reset."
                 if not complete else None),
    }


def _export(store: StateStore, mission_id: str, run_id: str,
            *, max_spans: int, page_size: int, max_pages: int,
            after_export_id: str = "") -> dict:
    config = ExportConfig.from_env(_export_env())
    if not config.enabled:
        raise ValueError("Explicit export requires OSRA_TRACE_EXPORT_ENABLED=true")
    try:
        importlib.import_module("opentelemetry.sdk.trace")
        importlib.import_module("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    except ImportError as exc:
        raise ValueError("Optional trace packages are missing; run scripts/dev setup --observability") from exc
    cursor = after_export_id
    activity_cache = ToolActivityCache()
    sent = uncertain = pages = 0
    complete = False
    while pages < max_pages and sent + uncertain < max_spans:
        page = export_pending(store, config, mission_id=mission_id,
                              run_id=run_id, limit=min(page_size, max_spans - sent - uncertain),
                              after_id=cursor, activity_cache=activity_cache)
        pages += 1
        sent += page["sent"]
        uncertain += page["uncertain"]
        cursor = page["next_after_id"]
        if cursor is None:
            complete = True
            break
    return {"sent": sent, "uncertain": uncertain, "pages": pages,
            "complete": complete, "next_after_id": cursor,
            "note": ("Uncertain acknowledgements are never resent automatically."
                     if uncertain else None)}


def inspect(args: argparse.Namespace) -> dict:
    store = StateStore(args.state_dir, read_only=not (args.project or args.export))
    run = store.get("mission_run", args.run_id)
    if run is None:
        raise ValueError("Run ID is absent from the selected state directory")
    mission_id = run["body"].get("mission_id")
    if not isinstance(mission_id, str):
        raise ValueError("Saved run lacks a mission ID")
    # Validate and summarize local evidence before any explicit network send.
    metrics = build_context_metrics(store, args.run_id, include_calls=True)
    operation = None
    if args.project:
        operation = _project(store, mission_id, args.run_id,
                             max_spans=args.max_spans, page_size=args.page_size,
                             max_pages=args.max_pages,
                             after_receipt_id=args.after_receipt_id,
                             after_trace_id=args.after_trace_id,
                             after_figure_receipt_id=args.after_figure_receipt_id,
                             after_tool_id=args.after_tool_id)
    elif args.export:
        operation = _export(store, mission_id, args.run_id,
                            max_spans=args.max_spans, page_size=args.page_size,
                            max_pages=args.max_pages,
                            after_export_id=args.after_export_id)
    return {
        "schema": "ora.trace_run_cli.v1", "run_id": args.run_id,
        "mission_status": run["body"].get("status", "unknown"),
        "action": "project" if args.project else "export" if args.export else "inspect",
        "operation": operation,
        "run_usage": metrics["run_usage"],
        "local_prefix_reuse": metrics["local_prefix_reuse"],
        "run_activity": metrics["run_activity"],
        "tool_activity": build_tool_activity(store, args.run_id),
        "evaluation": metrics["evaluation"],
        "retrieval": metrics["retrieval"],
        "model": {"counts": metrics["model"]["counts"],
                  "usage": _usage(metrics["model"]["usage"]),
                  "stage_estimate": metrics["model"]["stage_estimate"]},
        "loops": _loops(metrics),
        "figure": {"counts": metrics["figure"]["counts"],
                   "usage": _usage(metrics["figure"]["usage"]),
                   "loops": _loops(metrics, "figure")},
        "context": metrics["context"], "integrity": metrics["integrity"],
        "local_export": _export_counts(store, mission_id, args.run_id),
        "local_export_errors": _export_errors(store, mission_id, args.run_id),
    }


def _local_input_reuse_markdown(result: dict) -> list[str]:
    """Render the same saved-request comparison used by runtime metric snapshots."""
    reuse = result["local_prefix_reuse"]
    if "structural_coverage_status" not in reuse:
        return []
    def count(value):
        return f"{value:,}" if value is not None else "unknown"
    def percent(value):
        return f"{100 * value:.1f}%" if value is not None else "unavailable"
    state = reuse["structural_coverage_status"]
    lines = ["## Local input reuse across steps", "",
             f"- **Run-level local input reuse:** {percent(reuse['local_structural_input_reuse_fraction'])} "
             f"— {count(reuse['repeated_local_structural_input_tokens'])} repeated / "
             f"{count(reuse['local_structural_input_tokens'])} measured local input tokens.",
             f"- **Coverage:** {state.replace('_', ' ')}; "
             f"{reuse['structural_measured_calls']}/{reuse['structural_candidate_calls']} "
             f"request records measured, {reuse['structural_unavailable_calls']} unavailable, "
             f"{reuse['structural_overlap_skipped_calls']} overlapping comparisons excluded.",
             f"- **Follow-up message-prefix reuse:** "
             f"{percent(reuse['estimated_message_prefix_token_fraction'])}; "
             f"{reuse['measured_pairs']}/{reuse['eligible_pairs']} comparable follow-up pairs.",
             "- **Comparison scope:** Same run, agent, task, provider, model and credential profile. "
             "Compare each request with its preceding completed request in that scope.",
             "- **Method:** Unchanged opening message tokens plus identical whole tool definitions, "
             "divided by locally tokenized messages and tool definitions. The run total includes "
             "each scope's first request at zero local reuse. Missing and overlapping comparisons "
             "are excluded and reduce coverage; they are not zeroes.",
             "- **Interpretation:** Input-structure reuse, not provider cache hits or billing savings. "
             "Unchanged text after the first difference and partially changed tool schemas are not "
             "counted. Request settings and image tokens are outside this measure.", ""]
    if state == "baseline_only":
        lines.extend(["Only first requests in separate scopes were captured. Their baseline rows "
                      "do not establish a 0% provider cache rate or a repeat-request comparison.", ""])
    observed_loops = [loop for loop in result['loops']
                      if loop['dispatch_recorded'] or loop['status'] == 'captured']
    identities = Counter(loop['call_id'] for loop in observed_loops)
    numbered = {row['call_id']: number for number, row in enumerate(observed_loops, 1)
                if identities[row['call_id']] == 1}
    if any(count > 1 for count in identities.values()):
        lines.extend(["Ambiguous duplicate call IDs have no request number (—); "
                      "they are not silently assigned to one occurrence.", ""])
    lines.extend(["| Request | Prior request | Comparison | Repeated prefix | Unchanged tools | "
                  "Measured input | Local reuse |",
                  "| ---: | ---: | --- | ---: | ---: | ---: | ---: |"])
    for row in reuse["structural_input_calls"]:
        status = row['status'].replace('_', ' ')
        if row['status'] in {'unavailable', 'overlap_skipped'}:
            status += ": " + row['reason'].replace('_', ' ')
        number = numbered.get(row['call_id'], '—')
        prior = numbered.get(row['prior_call_id'], '—')
        lines.append(f"| {number} | {prior} | {status} | "
                     f"{count(row['unchanged_leading_message_tokens'])} | "
                     f"{count(row['unchanged_whole_schema_tokens'])} | "
                     f"{count(row['local_structural_input_tokens'])} | "
                     f"{percent(row['repeated_input_fraction'])} |")
    if not reuse['structural_input_calls']:
        lines.append("| — | — | No comparable captured text requests | — | — | — | — |")
    changed_pairs = reuse.get("changed_schema_measured_pairs", 0)
    changed_unavailable = reuse.get("changed_schema_unavailable_pairs", 0)
    if changed_pairs or changed_unavailable:
        lines.extend([
            "", "### Unchanged definitions within changed tool lists", "",
            f"- **Exact tool-definition overlap:** "
            f"{percent(reuse.get('changed_schema_exact_shared_definition_fraction'))} "
            f"— {count(reuse.get('changed_schema_exact_shared_definition_tokens'))} / "
            f"{count(reuse.get('changed_schema_current_individual_definition_tokens'))} "
            f"separately tokenized definition tokens across {changed_pairs} changed-list pairs; "
            f"{changed_unavailable} pairs unavailable.",
            "- **Meaning:** Counts exact individual definitions that survived a tool-list change, "
            "regardless of order. This explains reusable content hidden by the stricter whole-list "
            "measure; it does not change the run-level reuse percentage or establish cache hits.",
            "", "| Request | Prior request | Exact definitions retained | Definition-token overlap |",
            "| ---: | ---: | ---: | ---: |",
        ])
        for row in reuse['structural_input_calls']:
            if row.get("tool_definition_overlap_status") != "measured_changed_schema":
                continue
            lines.append(
                f"| {numbered.get(row['call_id'], '—')} | "
                f"{numbered.get(row['prior_call_id'], '—')} | "
                f"{count(row['exact_shared_tool_definition_count'])} / "
                f"{count(row['current_tool_definition_count'])} | "
                f"{count(row['exact_shared_tool_definition_tokens'])} / "
                f"{count(row['current_individual_tool_definition_tokens'])} |"
            )
    return [*lines, ""]


def _markdown(result: dict) -> str:
    lines = [f"# Saved trace: {result['run_id']}", "",
             f"Mission status: **{result['mission_status']}**. "
             f"Action: **{result['action']}** (local inspection unless export was explicit).", ""]
    if result["operation"] is not None:
        lines.extend([f"Operation: `{json.dumps(result['operation'], sort_keys=True)}`", ""])
    evaluation = result.get("evaluation")
    if evaluation is not None:
        lines.extend([
            f"Evaluation observation ended: **{evaluation['outcome']}**. "
            f"The transport ledger records **{evaluation['model_http_attempts']} model HTTP attempts** "
            f"and **{evaluation['retrieval_http_attempts']} retrieval HTTP attempts**. "
            f"Observed duration: {evaluation['elapsed_seconds']:.2f} seconds. "
            "This terminal evaluation record does not change the saved mission status; "
            "an active mission here does not mean the evaluation host is still running.", "",
        ])
    usage = result["run_usage"]
    activity = result["run_activity"]

    def percent(value):
        return f"{value * 100:.1f}%" if value is not None else "unknown"

    def count(value):
        return f"{value:,}" if value is not None else "unknown"

    mix = usage["token_mix"]
    cache = usage["cache_fraction"]
    confirmed_cache = usage.get("cache_fraction_confirmed")
    local_prefix = result["local_prefix_reuse"]
    local_tokens = local_prefix["estimated_repeated_message_tokens"]
    reasoning = usage["reasoning_fraction"]
    if isinstance(confirmed_cache, dict):
        paired = (f"{confirmed_cache['cache_reported_calls']}/"
                  f"{confirmed_cache['input_reported_calls']} input-reporting calls")
        if confirmed_cache["value"] is None:
            cache_summary = (
                f"Confirmed cached share of all reported input: **unknown**; "
                f"{paired} with cache detail. Missing cache detail is not a reported zero."
            )
        else:
            qualifier = (
                "confirmed minimum; omitted cache detail remains unknown"
                if confirmed_cache["status"] == "minimum"
                else "complete cache detail for reported input"
            )
            cache_summary = (
                f"Confirmed cached share of all reported input: "
                f"**{percent(confirmed_cache['value'])}** "
                f"({count(confirmed_cache['cached_tokens'])} of "
                f"{count(confirmed_cache['reported_input_tokens'])} tokens; "
                f"{paired} with cache detail; {qualifier})."
            )
        without_input = cache['total_calls'] - confirmed_cache['input_reported_calls']
        prepared_only = activity['prepared_only_count']
        cache_records = f"Cache record context: {cache['total_calls']} saved request records; "
        if without_input:
            if 0 <= prepared_only <= without_input:
                other = without_input - prepared_only
                cache_records += (
                    f"{prepared_only} preparation-only and "
                    f"{other} other {'record' if other == 1 else 'records'} "
                    "without reported input."
                )
            else:
                cache_records += f"{without_input} without reported input."
        else:
            cache_records += "all have reported input."
        evaluation = result.get('evaluation')
        if evaluation is not None:
            unreported_http = (evaluation['model_http_attempts']
                               - confirmed_cache['input_reported_calls'])
            if unreported_http > 0:
                cache_records += (
                    f" {unreported_http} ledger-counted model HTTP "
                    f"{'attempt' if unreported_http == 1 else 'attempts'} lacked "
                    "reported input; input usage remains unknown."
                )
        cache_lines = [cache_summary, "", cache_records, ""]
    else:
        cache_lines = [
            f"Cached-input share on reporting calls: **{percent(cache['value'])}** "
            f"({cache['coverage_status']} coverage). "
            f"Covered saved request records: {cache['covered_calls']}/{cache['total_calls']}; "
            f"covered share of reported input tokens: "
            f"{percent(cache['eligible_input_coverage_fraction'])}. "
            "Percentages use summed tokens from matching reporting calls.", "",
        ]
    lines.extend([
        f"Whole-run token split: **{percent(mix['input_fraction'])} input / "
        f"{percent(mix['output_fraction'])} output**, with "
        f"{mix['covered_calls']}/{mix['total_calls']} saved request records with both fields. "
        f"Reasoning accounts for **{percent(reasoning['value'])} of reported output** "
        f"on {reasoning['covered_calls']}/{reasoning['total_calls']} saved request records.", "",
        *cache_lines,
        *_local_input_reuse_markdown(result),
        f"Local exact-prefix reuse proxy: **{local_prefix['status']}**; "
        f"{local_prefix['measured_pairs']}/{local_prefix['eligible_pairs']} comparable "
        "same-run, same-actor/task/provider/model/profile request pairs measured. "
        f"Repeated leading message characters: {local_prefix['repeated_leading_message_characters']}; "
        f"locally estimated repeated message tokens: "
        f"{local_tokens if local_tokens is not None else 'unavailable'}. "
        f"Unchanged tool schemas: {local_prefix['stable_tool_schema_pairs']} pairs. "
        "This is local request structure, "
        "not Groq's rendered prefix, cached tokens, a cache hit, or a billing discount.", "",
        f"Cache-field states across {cache['total_calls']} saved request records: "
        f"{cache['reported_hit_calls']} reported hits; {cache['reported_miss_calls']} reported zeros; "
        f"{usage['cache_read']['not_reported_calls']} provider-omitted; "
        f"{usage['cache_read']['invalid_calls']} invalid; "
        f"{usage['cache_read']['legacy_uncertain_calls']} legacy captures without field-level diagnostics; "
        f"{usage['cache_read']['unknown_calls']} unavailable. "
        "Missing cache data cannot establish either a hit or a miss.", "",
        f"Prepared model intents: {activity['model_call_count']}; recorded dispatches: "
        f"{activity['model_dispatch_recorded_count']}; captured responses: "
        f"{activity['model_captured_response_count']}; figure calls: "
        f"{activity['figure_call_count']}; errors or unresolved calls: "
        f"{activity['error_or_unresolved_count']}. Largest reported input: "
        f"{usage['peak_reported_input_tokens'] if usage['peak_reported_input_tokens'] is not None else 'unknown'} "
        f"tokens. Time through last saved activity: {activity['observed_elapsed_seconds']} seconds.", "",
        "Prepared intents, dispatch markers and unresolved lineage are local request records, "
        "not additional or failed provider HTTP attempts. Use the validation transport ledger "
        "above when available for actual HTTP counts.", "",
    ])
    presence = usage.get("cache_field_presence_counts", {})
    if presence:
        lines.extend([
            "Saved cache-field presence: "
            + ", ".join(f"{name.replace('_', ' ')} {count}"
                        for name, count in sorted(presence.items()))
            + ". These are numeric-field-shape observations at decode, not a provider explanation "
            "for cache behavior. Older receipts without this detail remain unknown.", "",
        ])
    retrieval = result["retrieval"]
    lines.extend([
        f"Saved retrieval attempts: {retrieval['recorded_attempt_count']}; "
        f"deliveries: {retrieval['recorded_delivery_count']}; "
        f"outcomes: {json.dumps(retrieval['outcome_counts'], sort_keys=True)}. "
        "These are normalized retrieval records; complete HTTP-attempt counts are unavailable "
        "from these receipts (one retrieval can make multiple requests). "
        + ("Use the separate evaluation transport counts above for this case."
           if evaluation else ""), "",
    ])
    lines.extend(["| Usage | Model | Figure |", "| --- | ---: | ---: |"])
    for label, key in (("Input tokens", "input"), ("Output tokens", "output"),
                       ("Reasoning output subset", "reasoning_output_subset"),
                       ("Cached input subset", "cache_read_input_subset")):
        values = []
        for bucket in ("model", "figure"):
            field = result[bucket]["usage"][key]
            known = field["known_tokens"]
            value = f"{known:,} reported" if known is not None else "unknown"
            values.append(f"{value} ({field['reported_calls']} calls covered)")
        lines.append(f"| {label} | {values[0]} | {values[1]} |")
    cache = result["model"]["usage"]["cache_fraction_reported_subset"]
    ratio = f"{100 * cache['value']:.1f}%" if cache["value"] is not None else "unknown"
    legacy_cache_line = (
        [f"Model cache fraction: **{ratio}**, based on "
         f"{cache['covered_calls']}/{cache['total_calls']} saved request records "
         "with both fields. Missing provider detail is unknown, not zero.", ""]
        if confirmed_cache is None else []
    )
    lines.extend(["", *legacy_cache_line,
                  "Reasoning is already included in output tokens.", "",
                  "## Model requests with dispatch or response evidence", "",
                  "Rows below have a matching saved dispatch marker or captured response. A dispatch "
                  "marker alone does not prove an HTTP request reached the provider; use the transport "
                  "ledger above when available. Numbering excludes preparation and unmatched records.", "",
                  "| Request | Saved status | Input | Output | Reasoning subset | Cached-input subset | Cache observation |",
                  "| ---: | --- | ---: | ---: | ---: | ---: | --- |"])
    def tokens(field: dict) -> str:
        return (f"{field['value']:,}" if field["status"] == "reported"
                else field["status"].replace("_", " "))

    requests = [loop for loop in result["loops"]
                if loop["dispatch_recorded"] or loop["status"] == "captured"]
    diagnostics = [loop for loop in result["loops"]
                   if not (loop["dispatch_recorded"] or loop["status"] == "captured")]
    for number, loop in enumerate(requests, 1):
        lines.append(f"| {number} | {loop['status']} | {tokens(loop['input'])} | "
                     f"{tokens(loop['output'])} | {tokens(loop['reasoning_output_subset'])} | "
                     f"{tokens(loop['cache_read_input_subset'])} | "
                     f"{loop['cache_observation']['status'].replace('_', ' ')} |")
    if not requests:
        lines.append("| — | No dispatch or captured response recorded | — | — | — | — | — |")
    if diagnostics:
        lines.extend(["", "## Preparation and unmatched call records", "",
                      "These records are excluded from the model request table and provider usage. "
                      "A call manifest without an intent is not evidence of a prepared request.", "",
                      "| Record | Saved status | Evidence |", "| ---: | --- | --- |"])
        descriptions = {
            "manifest_without_intent": "No matching saved request intent",
            "prepared_only": "Request intent saved; no verified dispatch or response",
            "dispatch_lineage_mismatch": "Dispatch marker does not match the request intent",
            "receipt_lineage_mismatch": "Response receipt does not match the request intent",
        }
        for number, loop in enumerate(diagnostics, 1):
            lines.append(f"| {number} | {loop['status']} | "
                         f"{descriptions.get(loop['status'], 'No verified dispatch or response')} |")
    tool_activity = result["tool_activity"]
    lines.extend(["", "## Tool activity by model loop", "",
                  "Selected tools are joined to saved execution records. Selecting a capability "
                  "does not execute that capability. A peer_request completed from a verified "
                  "saved request does not mean a peer answered. Timing below is executor active time, "
                  "separate from model HTTP time and provider waits. Tool-message delivery means a matching "
                  "tool message reached a later request with a captured response; a reference "
                  "or shortened message does not establish full passage delivery or scientific use. "
                  "Source text and references can also arrive through separate context packets; "
                  "absence of a tool message does not establish absence of that source information. "
                  "A dash under later delivery means the next captured request already carried "
                  "the same result; it is not a second delivery.", "",
                  "| Loop | Selected tool | Execution / outcome | New or reused | Active time | Tool message in next model request | Later tool-message delivery |",
                  "| ---: | --- | --- | --- | ---: | --- | --- |"])
    for step in tool_activity["steps"]:
        selected = step["tool_name"]
        if step.get("selected_capability_name"):
            selected += f" → {step['selected_capability_name']}"
        duration = step.get("duration_seconds")
        timing = f"{duration:.3f}s" if duration is not None else "unknown"
        delivery = step["delivery_status"].replace("_", " ")
        if step.get("result_in_later_prepared_request") and not step.get("result_in_later_captured_request"):
            delivery += "; prepared only"
        if step.get("delivery_form") not in (None, "none"):
            delivery += "; " + step["delivery_form"].replace("_", " ")
        next_delivery = step["next_delivery_status"].replace("_", " ")
        if step["next_model_loop_index"] is not None:
            next_delivery = f"Loop {step['next_model_loop_index']}: {next_delivery}"
        if (step["delivery_status"] == "captured_later" and
                (step["next_delivery_status"], step.get("delivery_form")) in {
                    ("captured_with_exact_result", "exact_saved_message"),
                    ("captured_with_projected_or_restricted_result", "host_projected_or_restricted"),
                }):
            delivery = "—"
        lines.append(
            f"| {step['loop_index']} | {selected} | "
            f"{step['execution_status']} / {step['outcome']} | "
            f"{step['reuse_status']} | {timing} | {next_delivery} | {delivery} |"
        )
    if not tool_activity["steps"]:
        lines.append("| — | No matched tool selection recorded | — | — | — | — | — |")
    if result["figure"]["loops"]:
        lines.extend(["", "## Figure calls", "",
                      "| Call | Saved status | Input | Output | Cached input | Cache observation |",
                      "| ---: | --- | ---: | ---: | ---: | --- |"])
        for number, loop in enumerate(result["figure"]["loops"], 1):
            cells = [str(loop[key].get("value", loop[key]["status"].replace("_", " ")))
                     for key in ("input", "output", "cache_read_input_subset")]
            lines.append(f"| {number} | {loop['status']} | {' | '.join(cells)} | "
                         f"{loop['cache_observation']['status'].replace('_', ' ')} |")
    measured = [loop for loop in requests if loop.get("token_measurement")]
    if measured:
        lines.extend(["", "## Token measurement fidelity", "",
                      "Local estimates, provider admission demand and successful-response input usage "
                      "are separate measurements. Admission demand is not billed usage; absent values remain unknown.", "",
                      "| Request | Local input estimate | Provider admission demand | Reported input usage |",
                      "| ---: | ---: | ---: | ---: |"])
        for number, loop in enumerate(requests, 1):
            values = loop.get("token_measurement", {})
            cells = [str(values.get(key, "unknown")) for key in (
                "local_input_estimate", "provider_admission_tokens", "reported_input_tokens",
            )]
            lines.append(f"| {number} | {' | '.join(cells)} |")
    lines.extend(["", "## Context and integrity", "",
                  f"Verified inclusion receipts: {result['context']['verified_inclusion_receipts']}; "
                  f"exact reference reuse occurrences: {result['context']['reused_exact_ref_occurrences']}; "
                  f"missing exact refs: {result['context']['missing_exact_refs']}. "
                  "Prepared inclusion does not prove scientific use.", "",
                  f"Request stages (saved estimates, not billed tokens): "
                  f"{json.dumps(result['model']['stage_estimate']['marginal_tokens_by_kind'], sort_keys=True)}.", "",
                  f"Integrity diagnostics: {json.dumps(result['integrity'], sort_keys=True)}.", "",
                  "## Local trace export", "",
                  f"Saved spans: {json.dumps(result['local_export'], sort_keys=True)}. "
                  "This inspection is local. Cloud sends require an explicit export or opted-in runtime observer.", "",
                  "Uncertain-send diagnostics (safe grouped counts): "
                  f"{json.dumps(result['local_export_errors'], sort_keys=True)}. "
                  "Historical `acknowledgement_unknown` rows have no newer error code; "
                  "raw exceptions and provider responses are never shown.", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--project", action="store_true", help="Save local bounded trace projections")
    action.add_argument("--export", action="store_true", help="Send pending spans using scoped .env configuration")
    parser.add_argument("--max-spans", type=int, default=100)
    parser.add_argument("--page-size", type=int, default=25)
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--after-receipt-id", default="")
    parser.add_argument("--after-trace-id", default="")
    parser.add_argument("--after-figure-receipt-id", default="")
    parser.add_argument("--after-tool-id", default="")
    parser.add_argument("--after-export-id", default="")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args(argv)
    if not 4 <= args.max_spans <= 10_000:
        parser.error("--max-spans must be between 4 and 10000")
    if not 1 <= args.page_size <= MAX_PAGE:
        parser.error(f"--page-size must be between 1 and {MAX_PAGE}")
    if not 1 <= args.max_pages <= 1000:
        parser.error("--max-pages must be between 1 and 1000")
    if (args.after_receipt_id or args.after_trace_id or args.after_figure_receipt_id
            or args.after_tool_id) and not args.project:
        parser.error("Projection cursors require --project")
    if args.after_export_id and not args.export:
        parser.error("Export cursor requires --export")
    if any(len(cursor) > 300 or any(char.isspace() for char in cursor)
           for cursor in (args.after_receipt_id, args.after_trace_id,
                          args.after_figure_receipt_id, args.after_tool_id, args.after_export_id)):
        parser.error("Page cursors must be single bounded record IDs")
    try:
        result = inspect(args)
    except (OSError, StateError, ValueError) as exc:
        parser.exit(2, f"trace_run: {type(exc).__name__}: {exc}\n")
    print(_markdown(result) if args.format == "markdown" else json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
