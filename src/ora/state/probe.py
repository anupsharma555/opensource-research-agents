"""Measure repeated local commits, restart and lookup on a controlled mission trace.

The ten-hour label describes simulated event spacing, not wall-clock endurance.
Actual execution time, file growth and lookup latency are measured separately.
No external provider, model, sandbox or disposable cache is required.
"""

import argparse
import json
import sqlite3
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .store import StateStore


def probe(root: Path, *, batches: int = 60, peers: int = 16) -> dict:
    if root.exists():
        raise FileExistsError("Use a new directory so measurements have a known starting state")
    clock = [1791468000.0]
    store = StateStore(root, clock=lambda: clock[0])
    started = time.perf_counter()
    lookup_ms = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for batch in range(batches):
            clock[0] += 36000 / batches

            def accept(peer):
                key = f"record-{batch:04d}-{peer:03d}"
                blob = store.put_blob(f"Retained depression evidence group {peer % 4}".encode())

                def action(tx):
                    source = tx.put("source_copy", key, {"blob": blob, "rights": "project_owned_fixture"})
                    claim = tx.put("claim", key, {"text": "Recorded uncertain finding", "source_id": key})
                    report = tx.put("report", key, {"text": "Partial report remains available", "claim_id": key})
                    tx.put("peer_request", key, {"status": "requested"})
                    tx.link(claim, source, "supports")
                    tx.link(report, claim, "depends_on")
                    tx.emit("mission", "accepted", {"source_id": key})
                    return report

                return store.transact(key, {"blob": blob}, action, mission_id="mission")

            results = list(pool.map(accept, range(peers)))
            if batch in {batches // 2, batches - 1}:
                store = StateStore(root, clock=lambda: clock[0])
                store.rebuild_indexes()
            begin = time.perf_counter()
            for result in results:
                assert store.get("report", result["id"]) == result
                assert store.get("claim", result["id"])
                assert store.get("source_copy", result["id"])
            lookup_ms.append((time.perf_counter() - begin) * 1000 / len(results))
    backup = root.parent / f"{root.name}-backup"
    store.backup(backup)
    restored = StateStore.restore(backup, root.parent / f"{root.name}-restored")
    expected = batches * peers
    with sqlite3.connect(restored.db_path) as db:
        record_count = db.execute("SELECT count(*) FROM records").fetchone()[0]
        pending = db.execute("SELECT count(*) FROM events WHERE delivered=0").fetchone()[0]
    assert record_count == expected * 4 and pending == expected
    integrity = restored.reconcile()
    assert not integrity["degraded_artifacts"] and integrity["database_integrity"] == "ok"
    objects = list((root / "objects").iterdir())
    return {"proof": "local_recorded_workload", "logical_mission_hours": 10, "wall_seconds": time.perf_counter() - started,
            "parallel_collectors": peers, "executor_workers": 8, "accepted_transactions": expected,
            "accepted_records": record_count, "pending_requests": expected, "pending_outbox_events": pending,
            "unique_content_files": len(objects), "blob_bytes": sum(path.stat().st_size for path in objects),
            "database_bytes": store.db_path.stat().st_size,
            "three_record_lookup_ms_median": statistics.median(lookup_ms), "three_record_lookup_ms_max": max(lookup_ms),
            "restarts_and_index_rebuilds": 2, "backup_restore": "verified", "sqlite_version": sqlite3.sqlite_version,
            "unmeasured": ["actual ten-hour uptime", "power-loss behavior", "provider reliability", "scientific validity", "separate scheduler queue time"],
            "provider_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = probe(args.state_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
