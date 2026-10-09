"""Measure disposable, offline Stage 3 write shapes; never open saved live state.

The snapshot arm mirrors ResearchHost._mutate_locked's read/validate/serialize/
insert/update sequence with a synthetic ResearchSnapshot. The record arms use
the real StateStore transaction path. These are controlled workload timings,
not production latency or an end-to-end research run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sqlite3
import statistics
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from ora.contracts.models import ResearchSnapshot
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "ui" / "public" / "sample-run.json"
MISSION = "synthetic-measurement"


def _json(value: object) -> str:
    # Matches ora.live.engine._json without importing or constructing a host.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def _summary(values: list[float]) -> dict[str, float]:
    return {"median": round(statistics.median(values), 4),
            "p95": round(_percentile(values, .95), 4),
            "max": round(max(values), 4)}


def _bytes(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _storage(db_path: Path, keeper: sqlite3.Connection) -> dict:
    before = {"database": _bytes(db_path), "wal": _bytes(Path(str(db_path) + "-wal")),
              "shm": _bytes(Path(str(db_path) + "-shm"))}
    page_count = keeper.execute("PRAGMA page_count").fetchone()[0]
    page_size = keeper.execute("PRAGMA page_size").fetchone()[0]
    start = time.perf_counter_ns()
    passive = tuple(keeper.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
    passive_ms = (time.perf_counter_ns() - start) / 1_000_000
    start = time.perf_counter_ns()
    truncate = tuple(keeper.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
    truncate_ms = (time.perf_counter_ns() - start) / 1_000_000
    after = {"database": _bytes(db_path), "wal": _bytes(Path(str(db_path) + "-wal")),
             "shm": _bytes(Path(str(db_path) + "-shm"))}
    return {"before_checkpoint_bytes": before, "after_checkpoint_bytes": after,
            "page_count": page_count, "page_size": page_size,
            "passive_checkpoint": {"busy": passive[0], "log_pages": passive[1],
                                   "checkpointed_pages": passive[2],
                                   "elapsed_ms": round(passive_ms, 4)},
            "truncate_checkpoint": {"busy": truncate[0], "log_pages": truncate[1],
                                    "checkpointed_pages": truncate[2],
                                    "elapsed_ms": round(truncate_ms, 4)}}


def _event(index: int, payload_bytes: int) -> dict:
    return {"id": f"measurement-event-{index}", "time": "2026-10-08T00:00:00Z",
            "kind": "live_research", "taskId": None, "agentId": None,
            "summary": f"Synthetic event {index}: " + "x" * payload_bytes}


def _snapshot_connection(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def _snapshot_trial(path: Path, count: int, workers: int, payload_bytes: int) -> dict:
    seed = ResearchSnapshot.model_validate_json(SAMPLE.read_text()).model_dump(by_alias=True)
    seed["snapshotId"] = "measurement-initial"
    path.parent.mkdir(parents=True, exist_ok=True)
    keeper = _snapshot_connection(path)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("PRAGMA synchronous=FULL")
    keeper.executescript(
        "CREATE TABLE runs(id TEXT PRIMARY KEY,snapshot_id TEXT NOT NULL);"
        "CREATE TABLE snapshots(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,body TEXT NOT NULL);"
    )
    keeper.execute("INSERT INTO runs VALUES (?,?)", (MISSION, seed["snapshotId"]))
    keeper.execute("INSERT INTO snapshots VALUES (?,?,?)",
                   (seed["snapshotId"], MISSION, _json(seed)))
    keeper.commit()
    lock = threading.RLock()

    def write(index: int) -> dict:
        begin = time.perf_counter_ns()
        with lock:
            acquired = time.perf_counter_ns()
            with closing(_snapshot_connection(path)) as db:
                with db:
                    row = db.execute(
                        "SELECT s.body FROM snapshots s JOIN runs r ON r.snapshot_id=s.id "
                        "WHERE r.id=?", (MISSION,),
                    ).fetchone()
                    snapshot = json.loads(row["body"])
                    snapshot["events"].append(_event(index, payload_bytes))
                    snapshot_id = f"measurement-snapshot-{index}"
                    snapshot["snapshotId"] = snapshot_id
                    snapshot["capturedAt"] = _utc()
                    checked = ResearchSnapshot.model_validate(snapshot).model_dump(by_alias=True)
                    encoded = _json(checked)
                    transaction_started = time.perf_counter_ns()
                    db.execute("INSERT INTO snapshots VALUES (?,?,?)",
                               (snapshot_id, MISSION, encoded))
                    db.execute("UPDATE runs SET snapshot_id=? WHERE id=?",
                               (snapshot_id, MISSION))
            committed = time.perf_counter_ns()
        with closing(_snapshot_connection(path)) as reader:
            visible = reader.execute("SELECT 1 FROM snapshots WHERE id=?",
                                     (snapshot_id,)).fetchone() is not None
        observed = time.perf_counter_ns()
        if not visible:
            raise AssertionError("Committed snapshot was not visible to a new reader")
        return {"lock_wait_ms": (acquired - begin) / 1_000_000,
                "critical_ms": (committed - acquired) / 1_000_000,
                "transaction_ms": (committed - transaction_started) / 1_000_000,
                "materialization_ms": (transaction_started - acquired) / 1_000_000,
                "write_call_ms": (committed - begin) / 1_000_000,
                "ack_to_read_ms": (observed - committed) / 1_000_000,
                "serialized_bytes": len(encoded.encode())}

    start = time.perf_counter_ns()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        samples = list(pool.map(write, range(count)))
    elapsed = (time.perf_counter_ns() - start) / 1_000_000_000
    current = keeper.execute("SELECT snapshot_id FROM runs WHERE id=?", (MISSION,)).fetchone()[0]
    final = keeper.execute("SELECT body FROM snapshots WHERE id=?", (current,)).fetchone()[0]
    if len(json.loads(final)["events"]) != len(seed["events"]) + count:
        raise AssertionError("Snapshot updates were lost")
    result = _trial_result("full_snapshot_modeled", count, workers, elapsed, samples,
                           _storage(path, keeper))
    result["final_snapshot_bytes"] = len(final.encode())
    result["snapshot_rows"] = keeper.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    keeper.close()
    return result


def _record_trial(path: Path, count: int, workers: int, payload_bytes: int,
                  shared_lock: bool) -> dict:
    store = StateStore(path.parent, database_name=path.name)
    keeper = store._connect()
    lock = threading.Lock()

    def write(index: int) -> dict:
        event = _event(index, payload_bytes)
        begin = time.perf_counter_ns()
        acquired = begin
        action_started = [0]

        def accept(tx):
            action_started[0] = time.perf_counter_ns()
            saved = tx.put("measurement_event", event["id"],
                           {"mission_id": MISSION, "event": event})
            tx.emit(MISSION, "measurement_event_saved", {"record_id": saved["id"]})
            return {"id": saved["id"], "version": saved["version"]}

        if shared_lock:
            with lock:
                acquired = time.perf_counter_ns()
                result = store.transact(f"measurement-op-{index}", event, accept,
                                        mission_id=MISSION)
                committed = time.perf_counter_ns()
        else:
            result = store.transact(f"measurement-op-{index}", event, accept,
                                    mission_id=MISSION)
            committed = time.perf_counter_ns()
        visible = store.get("measurement_event", result["id"])
        observed = time.perf_counter_ns()
        if visible is None or visible["version"] != 1:
            raise AssertionError("Committed record was not visible to a new reader")
        return {"lock_wait_ms": (acquired - begin) / 1_000_000 if shared_lock else 0.0,
                "admission_ms": (action_started[0] - acquired) / 1_000_000,
                "critical_ms": (committed - acquired) / 1_000_000 if shared_lock
                else (committed - action_started[0]) / 1_000_000,
                "transaction_ms": (committed - action_started[0]) / 1_000_000,
                "write_call_ms": (committed - begin) / 1_000_000,
                "ack_to_read_ms": (observed - committed) / 1_000_000,
                "serialized_bytes": len(_json(event).encode())}

    start = time.perf_counter_ns()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        samples = list(pool.map(write, range(count)))
    elapsed = (time.perf_counter_ns() - start) / 1_000_000_000
    records = keeper.execute("SELECT COUNT(*) FROM records WHERE kind='measurement_event'").fetchone()[0]
    events = keeper.execute("SELECT COUNT(*) FROM events WHERE mission_id=?", (MISSION,)).fetchone()[0]
    if records != count or events != count:
        raise AssertionError("Record or outbox updates were lost")
    mode = "shared_lock_short_records" if shared_lock else "per_worker_short_records"
    result = _trial_result(mode, count, workers, elapsed, samples,
                           _storage(path, keeper))
    result["record_rows"] = records
    result["outbox_events"] = events
    keeper.close()
    return result


def _trial_result(mode: str, count: int, workers: int, elapsed: float,
                  samples: list[dict], storage: dict) -> dict:
    result = {"mode": mode, "updates": count, "workers": workers,
              "wall_seconds": round(elapsed, 5),
              "throughput_updates_per_second": round(count / elapsed, 3),
              "lock_wait_ms": _summary([s["lock_wait_ms"] for s in samples]),
              "critical_ms": _summary([s["critical_ms"] for s in samples]),
              "transaction_ms": _summary([s["transaction_ms"] for s in samples]),
              "write_call_ms": _summary([s["write_call_ms"] for s in samples]),
              "ack_to_read_ms": _summary([s["ack_to_read_ms"] for s in samples]),
              "serialized_bytes": _summary([float(s["serialized_bytes"]) for s in samples]),
              "storage": storage}
    if "admission_ms" in samples[0]:
        result["admission_including_connection_ms"] = _summary(
            [s["admission_ms"] for s in samples])
    if "materialization_ms" in samples[0]:
        result["materialization_ms"] = _summary(
            [s["materialization_ms"] for s in samples])
    return result


def measure(sizes: list[int], workers: int, payload_bytes: int) -> dict:
    if not sizes or any(type(size) is not int or size <= 0 for size in sizes):
        raise ValueError("Sizes must be positive integers")
    if type(workers) is not int or workers <= 0 or workers > 32:
        raise ValueError("Workers must be between 1 and 32")
    if type(payload_bytes) is not int or not 1 <= payload_bytes <= 4096:
        raise ValueError("Payload bytes must be between 1 and 4096")
    results = []
    with tempfile.TemporaryDirectory(prefix="ora-stage3-writes-") as directory:
        root = Path(directory)
        for count in sizes:
            for mode in ("full", "shared", "per_worker"):
                path = root / f"{mode}-{count}" / "state.sqlite3"
                if mode == "full":
                    results.append(_snapshot_trial(path, count, workers, payload_bytes))
                else:
                    results.append(_record_trial(path, count, workers, payload_bytes,
                                                 shared_lock=mode == "shared"))
    return {"schema": "ora.stage3.write_measurement.v1", "proof": "disposable_offline_workload",
            "recorded_at": _utc(), "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version, "platform": platform.platform(),
            "workload": {"sizes": sizes, "workers": workers,
                         "payload_bytes": payload_bytes,
                         "synthetic_snapshot_source": str(SAMPLE.relative_to(ROOT)),
                         "snapshot_seed_bytes": _bytes(SAMPLE),
                         "snapshot_seed_sha256": hashlib.sha256(SAMPLE.read_bytes()).hexdigest(),
                         "measurement_script_sha256": hashlib.sha256(
                             Path(__file__).read_bytes()).hexdigest(),
                         "event_shape": "one synthetic timeline event per update"},
            "trials": results,
            "boundaries": ["No ResearchHost instance, provider, model or network call",
                           "Snapshot arm mirrors mutation shape on a synthetic saved sample",
                           "Record arms use StateStore with one record and one event per update",
                           "Commit-to-reader metric is post-ack exact-version query time, not UI refresh lag",
                           "Per-worker admission includes connection opening and SQLite BEGIN wait",
                           "No isolated SQLite lock wait or production deployment measurement"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="30,100,300",
                        help="Comma-separated update counts on fresh stores")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--payload-bytes", type=int, default=200)
    parser.add_argument("--output", type=Path,
                        help="New JSON path; default is a unique ignored .local/stage3/evidence file")
    args = parser.parse_args()
    sizes = [int(part) for part in args.sizes.split(",")]
    result = measure(sizes, args.workers, args.payload_bytes)
    output = args.output or (ROOT / ".local" / "stage3" / "evidence" /
                             f"write-measurements-{uuid.uuid4().hex[:12]}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(_json({"output": str(output), "trials": len(result["trials"]),
                 "proof": result["proof"]}))


if __name__ == "__main__":
    main()
