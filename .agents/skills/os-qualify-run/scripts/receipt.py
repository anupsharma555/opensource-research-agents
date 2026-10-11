"""Read saved Stage 4 records into a private, bounded diagnostic receipt.

SQLite metadata and small exact linked tool-result objects are read. Model,
source and snapshot object contents and local connection credentials are not.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SKILL_VERSION = "0.3.1"
SNAPSHOT_COMPONENT_FORMAT = "ora.snapshot.components.v1"
KINDS = (
    "agent_attempt", "agent_call", "board_outcome", "board_proposal",
    "capacity_operation", "context_inclusion",
    "context_tool_result", "mail_answer", "mail_message", "mail_request",
    "mission_task_control", "model_dispatch_attempt", "model_request_intent",
    "model_response_receipt", "report",
)
SAFE_CATEGORY = re.compile(r"^[a-zA-Z0-9_.:-]{1,64}$")
MAX_TOOL_RESULT_BYTES = 1_000_000


def _head(db: sqlite3.Connection, kind: str, record_id: str) -> dict | None:
    row = db.execute(
        "SELECT r.kind,r.id,r.version,r.body FROM records r "
        "JOIN heads h USING(kind,id,version) WHERE r.kind=? AND r.id=?",
        (kind, record_id),
    ).fetchone()
    return dict(row) | {"body": json.loads(row["body"])} if row else None


def _exact(db: sqlite3.Connection, pointer: object) -> dict | None:
    if not isinstance(pointer, dict) or not all(key in pointer for key in ("kind", "id", "version")):
        return None
    if not isinstance(pointer["kind"], str) or not isinstance(pointer["id"], str) or type(pointer["version"]) is not int:
        return None
    row = db.execute("SELECT kind,id,version,body FROM records WHERE kind=? AND id=? AND version=?",
                     (pointer["kind"], pointer["id"], pointer["version"])).fetchone()
    return dict(row) | {"body": json.loads(row["body"])} if row else None


def _mission_records(db: sqlite3.Connection, mission_id: str) -> dict[str, list[dict]]:
    found = {kind: [] for kind in KINDS}
    marks = ",".join("?" for _ in KINDS)
    rows = db.execute(
        "SELECT r.kind,r.id,r.version,r.body FROM records r "
        "JOIN heads h USING(kind,id,version) "
        f"WHERE r.kind IN ({marks}) AND json_extract(r.body,'$.mission_id')=?",
        (*KINDS, mission_id),
    )
    for row in rows:
        found[row["kind"]].append(dict(row) | {"body": json.loads(row["body"])})
    return found


def _pointer(value: object, kind: str) -> tuple[str, int] | None:
    if (isinstance(value, dict) and value.get("kind") == kind
            and isinstance(value.get("id"), str)
            and type(value.get("version")) is int):
        return value["id"], value["version"]
    return None


def _category(value: object) -> str:
    return value if isinstance(value, str) and SAFE_CATEGORY.fullmatch(value) else "unknown"


def _token(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _storage(db_path: Path) -> dict:
    files = {}
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db_path) + suffix)
        files["database" if not suffix else suffix[1:]] = path.stat().st_size if path.exists() else 0
    return {"scope": "whole_state_directory_sqlite_files_at_observation",
            "bytes_by_file": files, "total_bytes": sum(files.values())}


def _snapshot_storage(db: sqlite3.Connection, mission_id: str, run_id: str) -> dict:
    prefix = f"snapshot-{run_id}-"
    rows = db.execute(
        "SELECT r.id,r.version,r.body FROM records r "
        "JOIN heads h USING(kind,id,version) WHERE r.kind='research_snapshot' "
        "AND json_extract(r.body,'$.mission_id')=? AND substr(r.id,1,?)=?",
        (mission_id, len(prefix), prefix),
    ).fetchall()
    categories = {
        name: {"reference_count": 0, "known_logical_reference_bytes": 0,
               "missing_blob_metadata_count": 0, "hash_sizes": {}}
        for name in ("legacy_full", "manifest", "component", "unclassified")
    }
    hash_sizes: dict[str, set[int]] = {}
    issues = Counter()
    known_logical_bytes = 0
    complete = True
    legacy_count = component_format_count = unknown_format_count = 0

    def add_blob(category: str, blob: object, seen_in_snapshot: dict[str, int]) -> None:
        nonlocal complete, known_logical_bytes
        stats = categories[category]
        stats["reference_count"] += 1
        digest = blob.get("sha256") if isinstance(blob, dict) else None
        size = blob.get("size") if isinstance(blob, dict) else None
        if (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or type(size) is not int or size < 0):
            stats["missing_blob_metadata_count"] += 1
            issues["missing_or_invalid_blob_metadata"] += 1
            complete = False
            return
        hash_sizes.setdefault(digest, set()).add(size)
        stats["hash_sizes"].setdefault(digest, set()).add(size)
        if digest in seen_in_snapshot:
            issues["duplicate_hash_within_snapshot"] += 1
            if seen_in_snapshot[digest] != size:
                complete = False
            return
        seen_in_snapshot[digest] = size
        stats["known_logical_reference_bytes"] += size
        known_logical_bytes += size

    for row in rows:
        body = json.loads(row["body"])
        storage_format = body.get("storage_format")
        if storage_format is None:
            category = "legacy_full"
            legacy_count += 1
        elif storage_format == SNAPSHOT_COMPONENT_FORMAT:
            category = "manifest"
            component_format_count += 1
        else:
            category = "unclassified"
            unknown_format_count += 1
            issues["unknown_storage_format"] += 1
            complete = False
        seen_in_snapshot: dict[str, int] = {}
        add_blob(category, body.get("blob"), seen_in_snapshot)
        links = db.execute(
            "SELECT target_kind,target_id,target_version FROM links "
            "WHERE source_kind='research_snapshot' AND source_id=? "
            "AND source_version=? AND relation='snapshot_part'",
            (row["id"], row["version"]),
        ).fetchall()
        if links and storage_format is None:
            issues["legacy_snapshot_with_component_links"] += 1
            complete = False
        for link in links:
            target = {"kind": link["target_kind"], "id": link["target_id"],
                      "version": link["target_version"]}
            if _pointer(target, "snapshot_component") is None:
                issues["invalid_component_link"] += 1
                complete = False
                continue
            component = _exact(db, target)
            if component is None:
                issues["missing_component_record"] += 1
                complete = False
                continue
            blob = component["body"].get("blob")
            digest = blob.get("sha256") if isinstance(blob, dict) else None
            if target["id"] != f"snapshot-component-{digest}" or target["version"] != 1:
                issues["component_identity_mismatch"] += 1
                complete = False
            add_blob("component" if storage_format == SNAPSHOT_COMPONENT_FORMAT
                     else "unclassified", blob, seen_in_snapshot)

    conflicting = {digest for digest, sizes in hash_sizes.items() if len(sizes) > 1}
    if conflicting:
        issues["conflicting_size_for_hash"] = len(conflicting)
        complete = False
    known_unique_bytes = sum(next(iter(sizes)) for digest, sizes in hash_sizes.items()
                             if digest not in conflicting)
    contributions = {}
    for name, stats in categories.items():
        sizes = stats.pop("hash_sizes")
        local_conflicts = any(len(group) > 1 for group in sizes.values())
        contributions[name] = {
            **stats,
            "unique_hashes_with_metadata": len(sizes),
            "distinct_sha256": sorted(sizes),
            "unique_object_bytes_from_metadata": (
                sum(next(iter(group)) for group in sizes.values())
                if not local_conflicts and stats["missing_blob_metadata_count"] == 0 else None
            ),
        }
    return {"scope": "saved_research_snapshot_records_with_selected_run_id_prefix",
            "count": len(rows), "legacy_full_snapshot_count": legacy_count,
            "component_format_snapshot_count": component_format_count,
            "unknown_format_snapshot_count": unknown_format_count,
            "contributions": contributions,
            "known_logical_reference_bytes": known_logical_bytes,
            "logical_reference_bytes": known_logical_bytes if complete else None,
            "unique_blob_hashes": len(hash_sizes),
            "distinct_referenced_object_sha256": sorted(hash_sizes),
            "known_unique_object_bytes_from_metadata": known_unique_bytes,
            "unique_blob_logical_bytes": known_unique_bytes if complete else None,
            "missing_blob_size_count": issues["missing_or_invalid_blob_metadata"],
            "issues": dict(sorted(issues.items())),
            "recorded_link_coverage_only": component_format_count > 0,
            "manifest_contents_or_link_completeness_verified": False,
            "physical_bytes_verified": False}


def _control_revisions(db: sqlite3.Connection, run_id: str) -> dict:
    row = db.execute(
        "SELECT COUNT(*) AS revisions,COUNT(DISTINCT id) AS controls,"
        "COALESCE(SUM(length(CAST(body AS BLOB))),0) AS metadata_json_bytes FROM records "
        "WHERE kind='mission_task_control' AND json_extract(body,'$.run_id')=?",
        (run_id,),
    ).fetchone()
    return {"scope": "all_saved_selected_run_control_versions",
            "revision_count": row["revisions"], "distinct_control_count": row["controls"],
            "metadata_json_bytes": row["metadata_json_bytes"]}


def _tool_status(state_dir: Path, result: dict) -> str:
    """Read one small linked tool message and return only a safe status category."""
    blob = result["body"].get("blob")
    if not isinstance(blob, dict):
        return "uninspected_missing_blob_ref"
    digest, size = blob.get("sha256"), blob.get("size")
    if (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or type(size) is not int or size < 0 or size > MAX_TOOL_RESULT_BYTES):
        return "uninspected_invalid_or_large_blob"
    try:
        descriptor = os.open(state_dir / "objects" / digest, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(MAX_TOOL_RESULT_BYTES + 1)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            return "uninspected_integrity_mismatch"
        message = json.loads(raw)
        content = message.get("content") if isinstance(message, dict) else None
        outcome = json.loads(content) if isinstance(content, str) else None
        if not isinstance(outcome, dict):
            return "uninspected_nonobject_outcome"
        status = outcome.get("status") or outcome.get("outcome")
        return _category(status) if status is not None else "no_structured_status"
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return "uninspected_unreadable_blob"


def _accepted_report(db: sqlite3.Connection, control: dict, mission_id: str) -> bool:
    body = control["body"]
    report_ref = body.get("report_ref")
    if body.get("phase") != "done" or _pointer(report_ref, "report") is None:
        return False
    report = _exact(db, report_ref)
    if not report or report["body"].get("mission_id") != mission_id:
        return False
    proposal = _head(db, "board_proposal", body.get("task_id", ""))
    outcome = _exact(db, proposal["body"].get("outcome_ref")) if proposal else None
    if not outcome or outcome["body"].get("mission_id") != mission_id:
        return False
    for pointer in outcome["body"].get("result_refs", []):
        if pointer == report_ref:
            return True
        if _pointer(pointer, "executor_artifact"):
            artifact = _exact(db, pointer)
            if artifact and artifact["body"].get("mission_id") == mission_id and report_ref in artifact["body"].get("output_refs", []):
                return True
    return False


def build_receipt(db: sqlite3.Connection, db_path: Path, run_ids: list[str]) -> dict:
    """Build a consistent SQLite snapshot; caller owns the read transaction."""
    results = []
    for run_id in run_ids:
        run = _head(db, "mission_run", run_id)
        if run is None:
            raise ValueError(f"Run unavailable: {run_id}")
        body = run["body"]
        mission_id = body["mission_id"]
        records = _mission_records(db, mission_id)
        controls = [r for r in records["mission_task_control"]
                    if r["body"].get("run_id") == run_id]
        attempts = [r for r in records["agent_attempt"]
                    if r["body"].get("run_id") == run_id]
        attempt_ids = {r["id"] for r in attempts}
        calls = [r for r in records["agent_call"]
                 if r["body"].get("attempt_id") in attempt_ids]
        call_refs = {(r["id"], r["version"]) for r in calls}
        intents = [r for r in records["model_request_intent"]
                   if _pointer(r["body"].get("call_ref"), "agent_call") in call_refs]
        markers = [r for r in records["model_dispatch_attempt"]
                   if _pointer(r["body"].get("call_ref"), "agent_call") in call_refs]
        responses = [r for r in records["model_response_receipt"]
                     if _pointer(r["body"].get("call_ref"), "agent_call") in call_refs]
        response_refs = {(r["id"], r["version"]) for r in responses}
        tool_results = [r for r in records["context_tool_result"]
                        if _pointer(r["body"].get("response_ref"), "model_response_receipt")
                        in response_refs]
        inclusions = [r for r in records["context_inclusion"]
                      if _pointer(r["body"].get("call_ref"), "agent_call") in call_refs]
        capacities = [r for r in records["capacity_operation"]
                      if r["body"].get("run_id") == run_id
                      and r["body"].get("kind") == "model"]

        usage_total = {key: 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                                           "cached_tokens", "reasoning_tokens")}
        optional_known = {"cached_tokens": 0, "reasoning_tokens": 0}
        usage_bearing = 0
        total_tokens_known_responses = 0
        for row in responses:
            usage = row["body"].get("usage") or {}
            if not isinstance(usage, dict):
                continue
            prompt = _token(usage.get("prompt_tokens"))
            completion = _token(usage.get("completion_tokens"))
            if prompt is None or completion is None:
                continue
            usage_bearing += 1
            usage_total["prompt_tokens"] += prompt
            usage_total["completion_tokens"] += completion
            total = _token(usage.get("total_tokens"))
            if total is not None:
                total_tokens_known_responses += 1
                usage_total["total_tokens"] += total
            for key, section, detail in (
                ("cached_tokens", "prompt_tokens_details", "cached_tokens"),
                ("reasoning_tokens", "completion_tokens_details", "reasoning_tokens"),
            ):
                group = usage.get(section)
                value = _token(group.get(detail)) if isinstance(group, dict) else None
                if value is not None:
                    optional_known[key] += 1
                    usage_total[key] += value
        if usage_bearing == 0:
            usage_total["prompt_tokens"] = None
            usage_total["completion_tokens"] = None
        if total_tokens_known_responses == 0:
            usage_total["total_tokens"] = None
        for key, count in optional_known.items():
            if count == 0:
                usage_total[key] = None

        source_refs: set[tuple[str, int]] = set()
        passage_refs: set[tuple[str, int]] = set()
        def add_sources(values: object) -> None:
            if isinstance(values, list):
                for value in values:
                    pointer = _pointer(value, "source_copy")
                    if pointer:
                        source_refs.add(pointer)

        add_sources(body.get("source_refs"))
        for row in controls:
            add_sources(row["body"].get("source_refs"))
        for row in calls:
            add_sources(row["body"].get("input_refs"))
        for row in intents:
            add_sources(row["body"].get("source_refs"))
            for decision in row["body"].get("source_decisions", []):
                pointer = _pointer(decision.get("copy_ref"), "source_copy")
                if pointer:
                    source_refs.add(pointer)
                for value in decision.get("passage_refs", []):
                    passage = _pointer(value, "source_passage")
                    if passage:
                        passage_refs.add(passage)
        copies = []
        missing_copy_refs = []
        for copy_id, version in sorted(source_refs):
            row = db.execute("SELECT body FROM records WHERE kind='source_copy' AND id=? AND version=?",
                             (copy_id, version)).fetchone()
            if row:
                copies.append({"id": copy_id, "version": version, "body": json.loads(row["body"])})
            else:
                missing_copy_refs.append({"id": copy_id, "version": version})
        publications = sorted({p["id"] for item in copies
                               if (p := item["body"].get("publication")) and isinstance(p, dict)
                               and isinstance(p.get("id"), str)})
        hashes = sorted({digest for item in copies
                         if isinstance((digest := item["body"].get("content_sha256")), str)})
        reports = {(r["id"], r["version"]): r for r in records["report"]}
        control_linked = sorted({pointer for row in controls
                                 if (pointer := _pointer(row["body"].get("report_ref"), "report"))
                                 and pointer in reports})
        accepted = sorted({pointer for row in controls
                           if (pointer := _pointer(row["body"].get("report_ref"), "report"))
                           and pointer in reports and _accepted_report(db, row, mission_id)})
        task_ids = {r["body"]["task_id"] for r in controls
                    if isinstance(r["body"].get("task_id"), str)}
        messages = {(r["id"], r["version"]): r for r in records["mail_message"]}
        requests = []
        unattributed_in_mission = 0
        for request in records["mail_request"]:
            message_ref = _pointer(request["body"].get("message_ref"), "mail_message")
            message = messages.get(message_ref)
            task_ref = message["body"].get("task_ref") if message else None
            if not isinstance(task_ref, dict) or not isinstance(task_ref.get("id"), str):
                task_ref = request["body"].get("cause_ref")
            if isinstance(task_ref, dict) and isinstance(task_ref.get("id"), str):
                if task_ref["id"] in task_ids:
                    requests.append(request)
            else:
                unattributed_in_mission += 1
        peer_statuses = Counter(_category(r["body"].get("status")) for r in requests)
        continuing = sorted({r["body"]["task_id"] for r in controls
                             if r["body"].get("scope_status") == "continued"
                             and r["body"].get("phase") not in {"done", "deferred"}
                             and isinstance(r["body"].get("task_id"), str)})

        # Count referenced bytes only. Never open or enumerate object files.
        selected_rows = [run, *controls, *attempts, *calls, *intents, *markers,
                         *responses, *capacities, *inclusions, *tool_results, *copies,
                         *(reports[pointer] for pointer in control_linked), *requests]
        blob_sizes: dict[str, int] = {}
        for row in selected_rows:
            blob = row["body"].get("blob")
            if (isinstance(blob, dict) and isinstance(blob.get("sha256"), str)
                    and type(blob.get("size")) is int):
                blob_sizes[blob["sha256"]] = blob["size"]
        tool_statuses = Counter(_tool_status(db_path.parent, row) for row in tool_results)
        uninspected_tools = sum(count for status, count in tool_statuses.items()
                                if status.startswith("uninspected_"))
        results.append({
            "run_id": run_id, "mission_id": mission_id, "run_status": body.get("status"),
            "parent_run_id": body.get("parent_run_id"),
            "profile": {"selected_model": body.get("profile", {}).get("selectedModel"),
                        "reasoning_settings": body.get("profile", {}).get("reasoningSettings"),
                        "workflow_version": body.get("workflow_version"),
                        "profile_workflow_version": body.get("profile", {}).get("workflowVersion")},
            "prepared_call_ids": sorted(r["id"] for r in calls),
            "prepared_request_intent_count": len(intents),
            "predispatch_marked_call_ids": sorted({r["body"]["call_ref"]["id"] for r in markers}),
            "captured_call_ids": sorted({r["body"]["call_ref"]["id"] for r in responses}),
            "saved_tool_result_count": len(tool_results),
            "tool_results": {"scope": "saved_results_linked_to_captured_model_responses",
                             "status_categories": dict(sorted(tool_statuses.items())),
                             "uninspected_count": uninspected_tools,
                             "outcome_unassessed_count": uninspected_tools
                             + tool_statuses.get("no_structured_status", 0),
                             "with_effect_ref_count": sum(bool(r["body"].get("effect_ref"))
                                                          for r in tool_results),
                             "all_attempted_tool_actions_proven": False},
            "capacity_dispatched_attempts": sum(len(r["body"].get("attempts", [])) for r in capacities),
            "capacity_unknown_usage_attempts": sum(
                1 for r in capacities for a in r["body"].get("attempts", [])
                if a.get("usage_known") is False),
            "response_statuses": dict(sorted(Counter(_category(r["body"].get("status"))
                                                     for r in responses).items())),
            "error_categories": dict(sorted(Counter(_category(r["body"].get("error_kind"))
                                                      for r in responses if r["body"].get("error_kind")).items())),
            "http_statuses": dict(sorted(Counter(str(r["body"]["http_status"])
                                                for r in responses if type(r["body"].get("http_status")) is int).items())),
            "usage": {"responses_with_usage": usage_bearing,
                      "captured_responses_without_usage": len(responses) - usage_bearing,
                      "known_totals": usage_total,
                      "total_tokens_known_responses": total_tokens_known_responses,
                      "optional_subset_known_counts": optional_known},
            "source_copies": [{"id": r["id"], "version": r["version"],
                               "content_sha256": r["body"].get("content_sha256")}
                              for r in copies],
            "source_copy_scope": "copies_referenced_by_selected_run_controls_calls_or_intents",
            "missing_source_copy_refs": missing_copy_refs,
            "distinct_publication_ids": publications,
            "distinct_content_sha256": hashes,
            "model_intent_passage_ref_count": len(passage_refs),
            "all_discovered_or_inspected_copies_proven": False,
            "control_linked_report_refs": [{"id": i, "version": v} for i, v in control_linked],
            "accepted_report_refs": [{"id": i, "version": v} for i, v in accepted],
            "attempt_statuses": dict(sorted(Counter(_category(r["body"].get("status"))
                                                    for r in attempts).items())),
            "peer_requests": {"count": len(requests),
                              "scope": "selected_run_attributed_current_request_heads",
                              "attribution_basis": "exact_message_task_or_board_cause_ref_to_run_control",
                              "unattributed_in_mission_count": unattributed_in_mission,
                              "statuses": dict(sorted(peer_statuses.items())),
                              "requests_with_submitted_answers": sum(
                                  bool(r["body"].get("answer_refs")) for r in requests),
                              "submitted_answer_ref_count": sum(
                                  len(r["body"].get("answer_refs") or []) for r in requests),
                              "requests_with_review": sum(
                                  isinstance(r["body"].get("reviewer_id"), str)
                                  and bool(r["body"].get("review"))
                                  and r["body"].get("status") in {"answered", "partly_answered"}
                                  for r in requests),
                              "requests_with_fully_accepted_answer": sum(
                                  bool(r["body"].get("accepted_answer_ref"))
                                  and r["body"].get("status") == "answered" for r in requests),
                              "historical_review_versions_assessed": False},
            "continuing_task_ids": continuing,
            "unfinished_control_phases": dict(sorted(Counter(_category(r["body"].get("phase"))
                                                             for r in controls if r["body"].get("phase")
                                                             not in {"done", "deferred"}).items())),
            "referenced_blob_storage": {"scope": "selected_run_record_and_source_copy_blob_references",
                                        "unique_hashes": len(blob_sizes),
                                        "logical_bytes": sum(blob_sizes.values()),
                                        "physical_bytes_verified": False},
            "saved_snapshot_storage": _snapshot_storage(db, mission_id, run_id),
            "control_revision_growth": _control_revisions(db, run_id),
        })
    return {"schema": "ora.codex.run_metadata_receipt.v2",
            "generator": {"skill": "os-qualify-run", "skill_version": SKILL_VERSION,
                          "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "basis": "one_read_only_sqlite_transaction_over_current_record_heads; small_exact_tool_blobs_hash_verified_separately",
            "note": "Saved dispatch marker is a pre-provider boundary, not billing proof. Unknown usage stays unknown.",
            "runs": results, "storage": _storage(db_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True, action="append")
    args = parser.parse_args()
    db_path = args.state_dir.resolve() / "state.sqlite3"
    if not db_path.is_file():
        parser.error("Selected state directory has no state.sqlite3")
    db = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        receipt = build_receipt(db, db_path, list(dict.fromkeys(args.run_id)))
        db.execute("ROLLBACK")
    finally:
        db.close()
    output_dir = ROOT / ".local/checks"
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = output_dir / f"run-qualification-{uuid.uuid4().hex}.json"
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(receipt, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
