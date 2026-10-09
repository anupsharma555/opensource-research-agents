"""Measure durable trace writes while no viewer reads, then read-only page replay.

Only disposable StateStore databases and synthetic records are used. This
measures a saved event backlog, not a network viewer or an in-memory queue.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import statistics
import tempfile
import time
import tracemalloc
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from ora.agents import AgentRegistry
from ora.coordination.traces import TraceContext, TraceJournal
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
MISSION = "stage3-trace-burst-fixture"
RUN = "offline-burst-run"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {"median": round(statistics.median(values), 4),
            "p95": round(ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)], 4),
            "max": round(ordered[-1], 4)}


def _sql_fingerprint(db_path: Path) -> dict:
    """Hash accepted SQL rows, not SQLite file bytes affected by WAL checkpoints."""
    db = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    digest = hashlib.sha256()
    counts = {}
    try:
        for table in ("records", "events", "operations"):
            rows = db.execute(f"SELECT * FROM {table} ORDER BY rowid")
            count = 0
            for row in rows:
                digest.update(json.dumps([table, *row], ensure_ascii=False,
                                         separators=(",", ":")).encode())
                digest.update(b"\n")
                count += 1
            counts[table] = count
        counts["trace_records"] = db.execute(
            "SELECT COUNT(*) FROM records WHERE kind='coordination_trace'").fetchone()[0]
        counts["fixture_outputs"] = db.execute(
            "SELECT COUNT(*) FROM records WHERE kind='stage3_trace_fixture_output'").fetchone()[0]
        counts["pending_trace_events"] = db.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='coordination_trace_recorded' "
            "AND delivered=0").fetchone()[0]
        return {"counts": counts, "rows_sha256": digest.hexdigest()}
    finally:
        db.close()


def measure_one(root: Path, *, count: int, page_size: int,
                trace_python_allocations: bool = False) -> dict:
    if root.exists():
        raise FileExistsError("Use a fresh disposable directory for each trace burst")
    if type(count) is not int or count <= 0 or type(page_size) is not int or page_size <= 0:
        raise ValueError("Trace count and page size must be positive integers")
    if trace_python_allocations:
        tracemalloc.start()
    try:
        store = StateStore(root, record_context={
            "proof_basis": "offline_test", "timing_basis": "observed_local",
            "implementation_ref": "ora.stage3.trace_burst.v1",
        })
        actor = AgentRegistry(store, ROOT).register(
            "burst-register", mission_id=MISSION, run_id=RUN,
            role_id="evidence_preparation_synthesis", display_name="Synthetic peer",
        )
        input_record = store.transact(
            "burst-input", {"fixture": True},
            lambda tx: tx.put("stage3_trace_fixture_input", "synthetic-input", {
                "mission_id": MISSION, "synthetic": True,
                "question": "Can saved trace events replay after a viewer absence?",
            }), mission_id=MISSION,
        )
        input_ref = _ref(input_record)
        journal = TraceJournal(store)
        base = TraceContext(MISSION, RUN, "trace-burst-task", "trace-burst-attempt",
                            actor["id"], "operation-0")
        write_samples_ms = []
        write_start = time.perf_counter_ns()
        # There is deliberately no export/read projection inside this loop.
        for index in range(count):
            context = replace(
                base, operation_id=f"operation-{index}",
                parent_operation_id=f"operation-{index - 1}"
                if index > 0 and index % 10 == 1 else None,
            )

            def accept(tx, index=index, context=context):
                output = tx.put("stage3_trace_fixture_output", f"output-{index:06d}", {
                    "mission_id": MISSION, "synthetic": True, "ordinal": index,
                })
                return journal.append_tx(
                    tx, f"trace-event-{index:06d}", context=context,
                    category="helper", status="recorded",
                    action="Save synthetic fixture output",
                    summary="A local output and its trace event were committed together.",
                    inputs=[input_ref], outputs=[_ref(output)],
                    capability_state="attempted", proof="offline_local_fixture",
                )

            began = time.perf_counter_ns()
            store.transact(f"burst-write-{index:06d}",
                           {"index": index, "input_ref": input_ref,
                            "parent_operation_id": context.parent_operation_id},
                           accept, mission_id=MISSION)
            write_samples_ms.append((time.perf_counter_ns() - began) / 1_000_000)
        write_seconds = (time.perf_counter_ns() - write_start) / 1_000_000_000
        before = _sql_fingerprint(store.db_path)
        if before["counts"]["trace_records"] != count or before["counts"]["fixture_outputs"] != count:
            raise AssertionError("Trace burst did not save every output and trace")
        if before["counts"]["pending_trace_events"] != count:
            raise AssertionError("Trace outbox backlog differs from accepted writes")

        reader = TraceJournal(StateStore(root, read_only=True))
        cursor, page_ms, step_ids, missing, lineage = 0, [], [], [], []
        verified_step_references = 0
        read_start = time.perf_counter_ns()
        while True:
            began = time.perf_counter_ns()
            page = reader.export(MISSION, after=cursor, limit=page_size)
            page_ms.append((time.perf_counter_ns() - began) / 1_000_000)
            step_ids.extend(step["id"] for step in page["steps"])
            for step in page["steps"]:
                pointers = [*step["inputReferences"], *step["outputReferences"]]
                if not any(pointer["kind"] == "stage3_trace_fixture_output"
                           for pointer in step["outputReferences"]):
                    raise AssertionError("A trace step lost its exact fixture output")
                for pointer in pointers:
                    if reader.store.get(**pointer) is None:
                        raise AssertionError("A trace step has an unresolved exact reference")
                    verified_step_references += 1
            missing.extend(page["missingReferences"])
            lineage.extend(item["data"]["lineage_status"] for item in page["records"])
            if page["nextCursor"] < cursor or (page["hasMore"] and page["nextCursor"] == cursor):
                raise AssertionError("Trace cursor did not make forward progress")
            cursor = page["nextCursor"]
            if not page["hasMore"]:
                break
        recovery_seconds = (time.perf_counter_ns() - read_start) / 1_000_000_000
        after = _sql_fingerprint(store.db_path)
        if len(step_ids) != count or len(set(step_ids)) != count or missing:
            raise AssertionError("Read-only replay lost, duplicated or missed an exact trace")
        if before != after:
            raise AssertionError("Diagnostic replay changed accepted SQL rows")
        expected_links = len(range(1, count, 10))
        if lineage.count("resolved") != expected_links:
            raise AssertionError("Saved parent/child links did not resolve")
        peak = tracemalloc.get_traced_memory()[1] if trace_python_allocations else None
        return {
            "requested_traces": count,
            "write_wall_seconds": round(write_seconds, 5),
            "write_throughput_per_second": round(count / write_seconds, 3),
            "write_call_ms": _summary(write_samples_ms),
            "viewer_absent_during_writes": True,
            "backlog_before_reader": before["counts"]["pending_trace_events"],
            "backlog_after_reader": after["counts"]["pending_trace_events"],
            "recovery_wall_seconds": round(recovery_seconds, 5),
            "page_size_events": page_size,
            "page_count": len(page_ms),
            "page_ms": _summary(page_ms),
            "recovered_steps": len(step_ids),
            "unique_step_ids": len(set(step_ids)),
            "verified_exact_step_references": verified_step_references,
            "resolved_parent_links": lineage.count("resolved"),
            "root_events": lineage.count("root_event"),
            "missing_references": len(missing),
            "sql_before_reader": before,
            "sql_after_reader": after,
            "python_tracemalloc_peak_bytes": peak,
            "python_tracemalloc_scope": (
                "combined fixture creation, write burst and replay; Python allocations only, not process RSS"
                if trace_python_allocations else "not_measured"),
        }
    finally:
        if trace_python_allocations:
            tracemalloc.stop()


def measure(sizes: list[int], page_size: int,
            trace_python_allocations: bool = False) -> dict:
    if not sizes or any(type(size) is not int or size <= 0 for size in sizes):
        raise ValueError("Sizes must be positive integers")
    trials = []
    with tempfile.TemporaryDirectory(prefix="ora-stage3-diagnostics-") as directory:
        for size in sizes:
            trials.append(measure_one(Path(directory) / f"burst-{size}",
                                      count=size, page_size=page_size,
                                      trace_python_allocations=trace_python_allocations))
    return {
        "schema": "ora.stage3.diagnostic_burst_measurement.v1",
        "proof": "disposable_offline_trace_burst", "recorded_at": _utc(),
        "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "source_hashes": {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in ("src/ora/coordination/traces.py", "src/ora/state/store.py",
                         "scripts/measure_stage3_diagnostics.py")
        },
        "workload": {"sizes": sizes, "page_size_events": page_size,
                     "one_output_and_one_trace_per_write": True,
                     "parent_link_stride": 10, "provider_calls": 0},
        "trials": trials,
        "limits": [
            "No actual browser, network viewer, provider, model or research mission",
            "No export calls during the measured write burst; disconnect is simulated by absence",
            "Read-only export does not acknowledge or drain StateStore outbox delivery flags",
            "SQL row fingerprint proves no accepted-row mutation, not filesystem byte stability",
            "No writer queue, SLA, production latency, long-duration or power-loss claim",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="100,1000",
                        help="Comma-separated trace counts on fresh disposable stores")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--trace-python-alloc", action="store_true",
                        help="Measure Python allocation peak; changes observed timings")
    parser.add_argument("--output", type=Path,
                        help="New JSON file; defaults to unique ignored .local/stage3/evidence path")
    args = parser.parse_args()
    sizes = [int(part) for part in args.sizes.split(",")]
    result = measure(sizes, args.page_size, args.trace_python_alloc)
    output = args.output or (ROOT / ".local" / "stage3" / "evidence" /
                             f"diagnostic-burst-{uuid.uuid4().hex[:12]}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"output": str(output), "proof": result["proof"],
                      "trials": len(result["trials"])}))


if __name__ == "__main__":
    main()
