"""Run explicit real-publication cases through the same HTTP commands as Atlas.

The harness saves private receipts, snapshots and actual call records for review.
Its observation window never cancels, completes or caps a research mission.
Additional tests wait for existing work by default; --allow-overlap explicitly
permits a test batch to overlap ongoing missions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ora.live.cli import _connection, _NoRedirect, connect_url  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save(path: Path, value: object) -> None:
    """Write private qualification output without overwriting existing evidence."""
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def _request(connection: dict, route: str, body: dict | None = None) -> dict:
    headers = {"Authorization": "Bearer " + connection["token"]}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        connection["origin"] + route,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
    )
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as response:
        return json.load(response)


def _peers(case: dict, state: dict) -> list[str]:
    selected = []
    for role in case["roles"]:
        candidates = [agent for agent in state["agents"] if agent.get("roleTemplateId") == role
                      and agent.get("availability") in {"available", "enabled"}]
        if len(candidates) != 1:
            raise ValueError(f"Case needs one explicit available configuration for role {role}")
        selected.append(candidates[0]["id"])
    return selected


def _has_accepted_report(snapshot: dict) -> bool:
    """Recognize a saved report by its accepted record, not its prose artifact type."""
    accepted = {
        (reference["id"], reference["version"])
        for reference in snapshot.get("inspection", {}).get("acceptedOutputReferences", [])
        if reference.get("kind") == "report"
    }
    return any((artifact["id"], artifact["version"]) in accepted
               for artifact in snapshot.get("artifacts", []))


def _active_runs(state: dict) -> list[dict]:
    """List work that may still consume resources without reading research text."""
    return [{"run_id": run["id"], "status": run.get("status", "unknown"),
             "continuing_task_ids": run.get("continuingTaskIds", [])}
            for run in state.get("runs", [])
            if run.get("status") not in {"completed", "cancelled"}
            or run.get("continuingTaskIds")]


def _finish(output: Path, summaries: list[dict], *, pending: list[dict],
            submissions_deferred: bool) -> int:
    _save(output / "summary.json", summaries)
    status = {
        "private_receipts": str(output), "cases_observed": len(summaries),
        "active_runs_at_last_observation": pending,
        "ongoing_consumption_possible": bool(pending),
        "additional_test_submissions_deferred": submissions_deferred,
        "missions_cancelled_by_harness": False,
    }
    _save(output / "qualification-status.json", status)
    print(json.dumps(status), flush=True)
    return 2 if submissions_deferred else 0


def _observe(connection: dict, run_id: str, folder: Path, seconds: float,
             state_dir: Path) -> dict:
    from urllib.parse import urlencode

    start = time.monotonic()
    first_output = None
    saved_snapshots = set()
    result: dict = {"status": "not_observed"}
    state: dict = {}
    connection_events = []
    while True:
        try:
            state = _request(connection, "/api/live/state?" + urlencode({"runId": run_id}))
        except (OSError, ValueError) as exc:
            connection_events.append({"at": _now(), "event": "read_unavailable",
                                      "exception_type": type(exc).__name__})
            if time.monotonic() - start >= seconds:
                result.update({"run_id": run_id, "status": "not_currently_observable",
                               "observation_outcome": "host_unavailable_mission_not_stopped",
                               "elapsed_observation_seconds": time.monotonic() - start,
                               "review_status": "pending_source_review"})
                break
            try:
                connect_url(state_dir)
                renewed = _connection(state_dir)
                if renewed["origin"] != connection["origin"]:
                    connection_events.append({"at": _now(), "event": "reconnected_after_restart"})
                connection.update(renewed)
            except (OSError, ValueError):
                pass
            time.sleep(2)
            continue
        run = next(row for row in state["runs"] if row["id"] == run_id)
        snapshot = state.get("snapshot")
        if snapshot:
            snapshot_id = snapshot["snapshotId"]
            if snapshot_id not in saved_snapshots:
                file_key = hashlib.sha256(snapshot_id.encode()).hexdigest()[:24]
                _save(folder / f"snapshot-{file_key}.json", state)
                saved_snapshots.add(snapshot_id)
            if first_output is None and _has_accepted_report(snapshot):
                first_output = time.monotonic() - start
        result = {
            "run_id": run_id, "status": run["status"], "observed_at": _now(),
            "elapsed_observation_seconds": time.monotonic() - start,
            "first_report_observed_seconds": first_output,
            "snapshot_id": snapshot.get("snapshotId") if snapshot else None,
            "saved_snapshots": len(saved_snapshots), "review_status": "pending_source_review",
            "waiting_reason": run.get("waitingReason"),
        }
        if run["status"] in {"completed", "cancelled"}:
            result["observation_outcome"] = "terminal_status_observed"
            break
        if time.monotonic() - start >= seconds:
            result["observation_outcome"] = "observation_window_ended_mission_not_stopped"
            break
        time.sleep(2)
    try:
        calls = _request(connection, "/api/live/calls?" + urlencode({"runId": run_id}))
    except (OSError, ValueError) as exc:
        calls = {"records": [], "inspection_error": type(exc).__name__,
                 "calls_not_proven_absent": True}
    _save(folder / "calls.json", calls)
    _save(folder / "last-state.json", state)
    _save(folder / "connection-events.json", connection_events)
    result["recorded_call_count"] = len(calls.get("records", []))
    _save(folder / "observation.json", result)
    return {**result, "state": state}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".local/stage4/live")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--observe-seconds", type=float, default=600,
                        help="Review window only; queued or waiting missions remain on the host")
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--credential-profile", choices=("primary", "backup"),
                        help="Explicit saved credential profile for every submitted run")
    parser.add_argument("--allow-overlap", action="store_true",
                        help="Explicitly submit more test missions while earlier work remains active")
    args = parser.parse_args()
    suite_file = ROOT / "benchmarks/stage4/cases.json"
    suite = json.loads(suite_file.read_text())
    if not args.case:
        print(json.dumps([{k: case[k] for k in ("id", "difficulty", "variant")}
                          for case in suite["cases"]], indent=2))
        return 0
    if not args.allow_live:
        parser.error("Explicit live qualification requires --allow-live.")
    cases = [case for case in suite["cases"] if case["id"] in args.case]
    if {case["id"] for case in cases} != set(args.case):
        parser.error("Unknown case ID; omit --case to list the versioned cases.")
    if args.repeat < 1 or args.observe_seconds <= 0:
        parser.error("Repetitions and observation seconds must be positive.")
    state_dir = args.state_dir.resolve()
    connect_url(state_dir)  # Deliberately discard the session URL; never print it.
    connection = _connection(state_dir)
    state = _request(connection, "/api/live/state")
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / ".local/stage4/mission-qualifications" / f"{now}-{uuid.uuid4().hex[:8]}"
    output.mkdir(parents=True, mode=0o700)
    try:
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = None
    _save(output / "manifest.json", {
        "schema": "ora.stage4.mission_qualification.v1", "started_at": _now(),
        "suite_version": suite["version"], "suite_sha256": hashlib.sha256(suite_file.read_bytes()).hexdigest(),
        "git_head": revision, "workspace_id": state.get("workspaceId"),
        "host_build_id": connection.get("hostBuildId"),
        "host_build_manifest": (
            str(state_dir / "host-builds" / (connection["hostBuildId"] + ".json"))
            if connection.get("hostBuildId") else None
        ),
        "workflow_version": state.get("workflowVersion"), "declared_model": state["model"],
        "retrieval_routes": state.get("retrievalRoutes", []), "cases": args.case,
        "repetitions": args.repeat, "observe_seconds": args.observe_seconds,
        "allow_overlap": args.allow_overlap,
        "credential_profile": args.credential_profile,
        "active_runs_at_start": _active_runs(state),
        "proof": "actual_commands_and_saved_records; scientific_quality_requires_review",
    })
    summaries = []
    pending = _active_runs(state)
    if pending and not args.allow_overlap:
        return _finish(output, summaries, pending=pending, submissions_deferred=True)
    for case in cases:
        peers = _peers(case, state)
        for repetition in range(1, args.repeat + 1):
            pending = _active_runs(_request(connection, "/api/live/state"))
            if pending and not args.allow_overlap:
                return _finish(output, summaries, pending=pending, submissions_deferred=True)
            folder = output / f"{case['id']}-{repetition}"
            folder.mkdir(mode=0o700)
            command = {"requestId": f"qualification-{uuid.uuid4().hex}",
                       "question": case["question"], "scope": case["scope"], "agentIds": peers}
            if args.credential_profile is not None:
                command["credentialProfile"] = args.credential_profile
            _save(folder / "case.json", {"suite_version": suite["version"], **case})
            _save(folder / "input.json", command)
            receipt = _request(connection, "/api/live/start", command)
            _save(folder / "receipt.json", receipt)
            observed = _observe(connection, receipt["runId"], folder, args.observe_seconds, state_dir)
            summary = {"case_id": case["id"], "difficulty": case["difficulty"],
                       "repetition": repetition, **{k: v for k, v in observed.items() if k != "state"}}
            summaries.append(summary)
            print(json.dumps(summary), flush=True)
            pending = _active_runs(observed["state"])
            if observed["status"] not in {"completed", "cancelled"}:
                if not any(row["run_id"] == receipt["runId"] for row in pending):
                    pending.append({"run_id": receipt["runId"], "status": observed["status"]})
            if pending and not args.allow_overlap:
                return _finish(output, summaries, pending=pending, submissions_deferred=True)
            snapshot = observed["state"].get("snapshot")
            if case.get("followup") and observed["status"] == "completed" and snapshot:
                target = next((a for a in snapshot["artifacts"]
                               if a["id"] == snapshot["mission"].get("reportArtifactId")), None)
                if target is None:
                    _save(folder / "followup-not-run.json", {
                        "reason": "No unambiguous primary report target in this snapshot",
                        "available_artifacts": [{"id": a["id"], "version": a["version"]}
                                                for a in snapshot["artifacts"]],
                    })
                    continue
                follow = {"requestId": f"qualification-follow-{uuid.uuid4().hex}",
                          "parentRunId": receipt["runId"], "parentSnapshotId": snapshot["snapshotId"],
                          "targetId": target["id"], "targetVersion": target["version"],
                          "question": case["followup"], "scope": case["scope"], "agentIds": peers}
                if args.credential_profile is not None:
                    follow["credentialProfile"] = args.credential_profile
                follow_folder = folder / "followup"
                follow_folder.mkdir(mode=0o700)
                _save(follow_folder / "input.json", follow)
                pending = _active_runs(_request(connection, "/api/live/state"))
                if pending and not args.allow_overlap:
                    return _finish(output, summaries, pending=pending, submissions_deferred=True)
                follow_receipt = _request(connection, "/api/live/followups", follow)
                _save(follow_folder / "receipt.json", follow_receipt)
                followed = _observe(connection, follow_receipt["runId"], follow_folder,
                                    args.observe_seconds, state_dir)
                pending = _active_runs(followed["state"])
                if followed["status"] not in {"completed", "cancelled"}:
                    if not any(row["run_id"] == follow_receipt["runId"] for row in pending):
                        pending.append({"run_id": follow_receipt["runId"],
                                        "status": followed["status"]})
                if pending and not args.allow_overlap:
                    return _finish(output, summaries, pending=pending, submissions_deferred=True)
    return _finish(output, summaries, pending=pending, submissions_deferred=False)


if __name__ == "__main__":
    raise SystemExit(main())
