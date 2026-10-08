"""Measure how three runtimes handle the same small scripted collaboration.

Each repetition gets a fresh workspace, and the report saves elapsed times,
accepted artifacts, checkpoint size, installed packages, and tested source hashes.
These timings describe synthetic waits, not live model or provider performance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "run.py"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def package_inventory(python: str) -> dict[str, str]:
    script = (
        "import importlib.metadata as m, json; "
        "print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}, sort_keys=True))"
    )
    process = subprocess.run([python, "-c", script], capture_output=True, text=True, check=True)
    return json.loads(process.stdout)


def run_one(python: str, adapter: str) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"ora-{adapter}-") as folder:
        workdir = Path(folder)
        start = time.perf_counter()
        process = subprocess.run(
            [python, str(RUNNER), "--adapter", adapter, "--workdir", str(workdir)],
            capture_output=True,
            text=True,
            check=False,
        )
        wall = time.perf_counter() - start
        if process.returncode:
            raise RuntimeError(process.stderr)
        result = json.loads(process.stdout)
        snapshot = result["snapshot"]
        if len(snapshot["artifacts"]) != 4 or len(snapshot["messages"]) != 1:
            raise RuntimeError(f"Incomplete {adapter} result")
        return {
            "process_wall_seconds": round(wall, 6),
            "scheduler_seconds": result["elapsed_seconds"],
            "first_artifact_seconds": result["first_artifact_seconds"],
            "max_active": result["max_active"],
            "artifact_ids": sorted(x["artifact_id"] for x in snapshot["artifacts"]),
            "model_hash": snapshot["model_hash"],
            "board_bytes": tree_bytes(workdir) - tree_bytes(workdir / "checkpoints"),
            "native_checkpoint_bytes": tree_bytes(workdir / "checkpoints"),
            "native_checkpoint_count": sum(result["native_checkpoint_inventory"].values()),
            "event_kinds": [x["kind"] for x in snapshot["events"]],
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--langgraph-python", required=True)
    parser.add_argument("--maf-python", required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 2:
        parser.error("at least two observations are needed")

    candidates = {
        "custom": sys.executable,
        "langgraph": args.langgraph_python,
        "maf": args.maf_python,
    }
    observations = {
        name: [run_one(python, name) for _ in range(args.repeats)]
        for name, python in candidates.items()
    }
    for entries in observations.values():
        if len({tuple(x["artifact_ids"]) for x in entries}) != 1:
            raise RuntimeError("Candidate runs produced different artifacts")
        if len({x["model_hash"] for x in entries}) != 1:
            raise RuntimeError("Candidate runs used different model manifests")

    report = {
        "conditions": {
            "python": platform.python_version(),
            "os": platform.system(),
            "architecture": platform.machine(),
            "repeats": args.repeats,
            "concurrent_task_limit": 2,
            "provider_calls": 0,
            "package_inventory": {
                name: package_inventory(python) for name, python in candidates.items()
            },
            "source_hashes": {p.name: file_hash(p) for p in sorted(HERE.glob("*.py"))},
        },
        "observations": observations,
        "medians": {
            name: {
                key: round(statistics.median(row[key] for row in rows), 6)
                for key in (
                    "process_wall_seconds",
                    "scheduler_seconds",
                    "first_artifact_seconds",
                    "board_bytes",
                    "native_checkpoint_bytes",
                )
            }
            for name, rows in observations.items()
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    print(json.dumps(report["medians"], sort_keys=True))


if __name__ == "__main__":
    main()
