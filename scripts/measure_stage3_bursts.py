"""Measure disposable Stage 3 queued-work bursts with synthetic callbacks.

Each comparison arm runs in a fresh Python process and StateStore. No model,
provider, historical database, or mission service is invoked.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import resource
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from ora.agents import AgentRegistry
from ora.coordination.board import ProposalBoard, ref
from ora.coordination.capacity import CapacityPolicy, CapacityScheduler, QuotaLimit
from ora.coordination.executor import ConcurrentExecutor, WorkResult, WorkSpec
from ora.coordination.mailbox import PeerMailbox
from ora.coordination.traces import TraceJournal
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
MISSION = "synthetic-stage3-burst"
RUN = "synthetic-stage3-burst-run"
HANDLER = ("synthetic-burst", "1")


def _peak_rss_kib() -> int:
    """Return the process high-water RSS; macOS reports bytes, Linux KiB."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / 1024) if sys.platform == "darwin" else peak


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def _bytes(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _source_hashes() -> dict[str, str]:
    paths = (
        "scripts/measure_stage3_bursts.py",
        "src/ora/coordination/executor.py",
        "src/ora/coordination/capacity.py",
        "src/ora/coordination/board.py",
        "src/ora/coordination/mailbox.py",
        "src/ora/state/store.py",
    )
    return {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in paths}


def _names(size: int) -> list[str]:
    if size < 6:
        raise ValueError("A burst fixture needs at least six work items")
    return (["a-fast", "b-slow", "c-retrieval", "d-dependent"]
            + [f"m-{index:04d}" for index in range(size - 5)] + ["zz-cancel"])


async def _trial(state_dir: Path, *, size: int, model_permits: int,
                 slow_seconds: float, commit_delay_seconds: float) -> dict:
    if model_permits < 1 or slow_seconds <= 0 or commit_delay_seconds < 0:
        raise ValueError("Permits and fixture delays must be valid")
    state_dir.mkdir(parents=True, exist_ok=False)
    setup_started = time.perf_counter()
    rss_before_setup = _peak_rss_kib()
    store = StateStore(state_dir)
    registry = AgentRegistry(store, ROOT)
    actors = [registry.register(
        f"burst-peer-{index}", mission_id=MISSION, run_id=RUN,
        role_id="evidence_preparation_synthesis", display_name=f"Synthetic peer {index}",
    )["id"] for index in range(3)]
    configs = []
    for index, actor in enumerate(actors):
        config = store.transact(
            f"burst-config-{index}", {"actor": actor},
            lambda tx, actor=actor, index=index: tx.put(
                "agent_configuration", f"burst-config-{index}",
                {"mission_id": MISSION, "instance_id": actor,
                 "configuration_sha256": f"synthetic-config-{index}"}),
            mission_id=MISSION,
        )
        configs.append(ref(config))
    board = ProposalBoard(store)
    names = _names(size)
    specs = {}
    for index, name in enumerate(names):
        actor, config = actors[index % len(actors)], configs[index % len(configs)]
        proposal = board.propose(
            f"burst-propose-{name}", mission_id=MISSION, proposal_id=name,
            actor_id=actor, objective=f"Synthetic task {name}",
            expected_artifact="Synthetic callback receipt",
            next_action="Run the chosen synthetic callback",
            depends_on=["a-fast"] if name == "d-dependent" else [],
        )["proposal_ref"]
        kind = "retrieval" if name == "c-retrieval" else "model"
        scope = "synthetic-retrieval" if kind == "retrieval" else (
            "synthetic-slow" if name == "b-slow" else "synthetic-healthy")
        specs[name] = WorkSpec(
            work_id=name, mission_id=MISSION, run_id=RUN,
            proposal_ref=proposal, actor_id=actor, configuration_ref=config,
            input_refs=(), handler_id=HANDLER[0], handler_version=HANDLER[1],
            kind=kind, lane=name, quota_scopes=(scope,),
            estimated_tokens=1 if kind == "model" else 0,
            attempt_id=f"synthetic-attempt-{name}",
        )
    policy = CapacityPolicy(
        permits={"model": model_permits, "retrieval": 1, "compute": 0},
        quotas={scope: QuotaLimit(60, requests=size * 2, tokens=size * 2)
                for scope in ("synthetic-slow", "synthetic-healthy", "synthetic-retrieval")},
        review_reserve=0,
    )
    capacity = CapacityScheduler(store, policy)
    # The original host exists only to submit. A fresh host below proves that
    # pending work comes from StateStore after a local restart.
    submitter = ConcurrentExecutor(
        store, board, PeerMailbox(store), capacity, TraceJournal(store),
        {HANDLER: lambda _spec: WorkResult(payload={"unused": True})}, owner="submitter",
    )
    for spec in specs.values():
        submitter.submit(spec)
    ready_at_submission = len(board.ready_work(MISSION))
    rss_after_setup = _peak_rss_kib()
    setup_seconds = time.perf_counter() - setup_started
    # Reconstruct the host's store and helpers from the saved directory before
    # dispatch; the original submitter has no in-memory pending-work queue.
    store = StateStore(state_dir)
    board = ProposalBoard(store)
    capacity = CapacityScheduler(store, policy)

    event_times: dict[str, dict[str, float]] = {}
    callback_count = {name: 0 for name in names}
    active = max_active = 0
    slow_started = asyncio.Event()
    commit_times: dict[str, float] = {}
    visibility_ms: list[float] = []
    commit_durations_ms: list[float] = []
    observer = StateStore(state_dir, read_only=True)

    async def callback(work: WorkSpec) -> WorkResult:
        nonlocal active, max_active
        callback_count[work.work_id] += 1
        active += 1
        max_active = max(max_active, active)
        event_times.setdefault(work.work_id, {})["start"] = time.perf_counter()
        if work.work_id == "b-slow":
            slow_started.set()
        try:
            await asyncio.sleep(slow_seconds if work.work_id == "b-slow" else 0.004)
            return WorkResult(
                payload={"synthetic": True, "work_id": work.work_id,
                         "configuration_ref": work.configuration_ref},
                used_tokens=1 if work.kind == "model" else 0,
            )
        finally:
            event_times[work.work_id]["callback_end"] = time.perf_counter()
            active -= 1

    # Slow only the accepted-result transaction, while its SQLite write lock is
    # held. This models a delayed writer without changing production source.
    original_transact = store.transact

    def measured_transact(operation_id, request, action, **kwargs):
        if not operation_id.startswith("executor-accept:"):
            return original_transact(operation_id, request, action, **kwargs)

        def delayed_action(tx):
            result = action(tx)
            if commit_delay_seconds:
                time.sleep(commit_delay_seconds)
            return result

        began = time.perf_counter()
        result = original_transact(operation_id, request, delayed_action, **kwargs)
        committed = time.perf_counter()
        work_id = request["related_request"]["work_id"]
        commit_times[work_id] = committed
        commit_durations_ms.append((committed - began) * 1000)
        visible_began = time.perf_counter()
        visible = observer.get("board_proposal", work_id)
        visibility_ms.append((time.perf_counter() - visible_began) * 1000)
        if visible is None or visible["body"]["status"] != "completed":
            raise AssertionError("A committed result was invisible to a separate reader")
        return result

    store.transact = measured_transact
    # Retain a connection so WAL file size can be sampled before its final
    # connection-close checkpoint removes or shrinks it.
    keeper = sqlite3.connect(state_dir / "state.sqlite3")
    keeper.execute("PRAGMA journal_mode").fetchone()
    wal_path = state_dir / "state.sqlite3-wal"
    wal_before = _bytes(wal_path)
    wal_samples = [wal_before]
    stop_sample = threading.Event()

    def sample_wal() -> None:
        while not stop_sample.wait(0.02):
            wal_samples.append(_bytes(wal_path))

    sampler = threading.Thread(target=sample_wal, name="burst-wal-sampler", daemon=True)
    sampler.start()
    executor = ConcurrentExecutor(
        store, board, PeerMailbox(store), capacity, TraceJournal(store),
        {HANDLER: callback}, owner="restarted-host",
    )
    run_started = time.perf_counter()
    main_task = asyncio.create_task(executor.run_ready(MISSION))
    await asyncio.wait_for(slow_started.wait(), timeout=max(10, slow_seconds * 20))
    backlog_while_slow = len(board.ready_work(MISSION))
    canceled = capacity.cancel(executor._capacity_id(specs["zz-cancel"]),
                               reason="branch_cancel")
    if canceled["phase"] != "canceled":
        raise AssertionError("Queued fixture work was not canceled")
    # A second readiness notification races the first, but must not repeat an
    # already dispatched or accepted callback.
    duplicate = ConcurrentExecutor(
        store, board, PeerMailbox(store), CapacityScheduler(store, policy),
        TraceJournal(store), {HANDLER: callback}, owner="duplicate-notification",
    )
    duplicate_task = asyncio.create_task(duplicate.run_ready(MISSION))
    try:
        main_result, duplicate_result = await asyncio.gather(main_task, duplicate_task)
    finally:
        stop_sample.set()
        sampler.join(timeout=2)
        wal_samples.append(_bytes(wal_path))
        keeper.close()
    elapsed_seconds = time.perf_counter() - run_started
    rss_after_run = _peak_rss_kib()
    db_bytes = _bytes(state_dir / "state.sqlite3")
    artifacts = store.records("executor_artifact", limit=size + 5)
    receipts = store.records("executor_receipt", limit=size + 5)
    expected_completed = size - 1
    if (len(artifacts) != expected_completed or len(receipts) != expected_completed
            or set(commit_times) != set(names) - {"zz-cancel"}
            or any(count != (0 if name == "zz-cancel" else 1)
                   for name, count in callback_count.items())):
        raise AssertionError("Burst lost work or repeated a callback/accepted effect")
    for artifact in artifacts:
        body = artifact["body"]
        if body["configuration_ref"] != specs[body["work_id"]].configuration_ref:
            raise AssertionError("Accepted artifact changed its pinned configuration")
    if not (event_times["c-retrieval"]["callback_end"] < event_times["b-slow"]["callback_end"]):
        raise AssertionError("Healthy retrieval failed to progress during slow model work")
    if not (commit_times["c-retrieval"] < event_times["b-slow"]["callback_end"]):
        raise AssertionError("Healthy retrieval result was not accepted during slow model work")

    # Reopen after completion and deliver the same notification. Saved results
    # and cancellation remain; no callback may repeat.
    after_store = StateStore(state_dir)
    after_capacity = CapacityScheduler(after_store, policy)
    after_restart = ConcurrentExecutor(
        after_store, ProposalBoard(after_store), PeerMailbox(after_store),
        after_capacity, TraceJournal(after_store), {HANDLER: callback},
        owner="after-restart",
    )
    again = await after_restart.run_ready(MISSION)
    if any(count != (0 if name == "zz-cancel" else 1)
           for name, count in callback_count.items()):
        raise AssertionError("Restart redispatched saved work")
    if after_capacity.get(executor._capacity_id(specs["zz-cancel"]))["phase"] != "canceled":
        raise AssertionError("Cancellation was not retained on restart")
    board_open_after = len(after_restart.board.open_work(MISSION))
    board_ready_after = len(after_restart.board.ready_work(MISSION))

    starts_ms = [(event_times[name]["start"] - run_started) * 1000
                 for name in names if name != "zz-cancel"]
    timings = [receipt["body"]["timing"] for receipt in receipts]
    capacity_records = [record["body"] for record in store.records("capacity_operation")
                        if record["body"]["mission_id"] == MISSION]
    admission_delays_ms = [max(0, item["attempts"][0]["at"] - item["queued_at"]) * 1000
                           for item in capacity_records if item["attempts"]]
    child_before_slow_end = (event_times["d-dependent"]["start"]
                             < event_times["b-slow"]["callback_end"])
    first_name = min(commit_times, key=commit_times.get)
    return {
        "input": {"size": size, "model_permits": model_permits,
                  "retrieval_permits": 1, "slow_seconds": slow_seconds,
                  "commit_delay_seconds": commit_delay_seconds,
                  "configuration_refs": configs,
                  "synthetic_quota_scopes": ["synthetic-slow", "synthetic-healthy",
                                             "synthetic-retrieval"]},
        "observed": {
            "setup_seconds": setup_seconds,
            "ready_backlog_at_submission": ready_at_submission,
            "ready_backlog_while_slow": backlog_while_slow,
            "max_active_callbacks": max_active,
            "highwater_rss_kib_before_setup": rss_before_setup,
            "highwater_rss_kib_after_setup": rss_after_setup,
            "highwater_rss_kib_after_run": rss_after_run,
            "peak_process_rss_kib": _peak_rss_kib(),
            "capacity_queue_to_admission_p50_ms": _percentile(admission_delays_ms, 0.5),
            "capacity_queue_to_admission_p95_ms": _percentile(admission_delays_ms, 0.95),
            "run_start_to_callback_start_p50_ms": _percentile(starts_ms, 0.5),
            "run_start_to_callback_start_p95_ms": _percentile(starts_ms, 0.95),
            "first_accepted_artifact_ms": (commit_times[first_name] - run_started) * 1000,
            "first_accepted_work_id": first_name,
            "run_elapsed_ms": elapsed_seconds * 1000,
            "sum_receipt_elapsed_seconds": sum(t["elapsed_seconds"] for t in timings),
            "sum_receipt_active_seconds": sum(t["active_seconds"] for t in timings),
            "sum_receipt_queue_wait_seconds": sum(t["queue_wait_seconds"] for t in timings),
            "sum_receipt_quota_wait_seconds": sum(t["quota_wait_seconds"] for t in timings),
            "sum_receipt_provider_wait_seconds": sum(t["provider_wait_seconds"] for t in timings),
            "sum_receipt_retry_wait_seconds": sum(t["retry_wait_seconds"] for t in timings),
            "commit_duration_p95_ms": _percentile(commit_durations_ms, 0.95),
            "immediate_reader_query_p95_ms": _percentile(visibility_ms, 0.95),
            "db_bytes_after_run": db_bytes,
            "wal_bytes_before_run": wal_before,
            "wal_bytes_sampled_peak": max(wal_samples),
            "wal_bytes_after_run": wal_samples[-1],
            "accepted_artifacts": len(artifacts),
            "accepted_receipts": len(receipts),
            "board_open_proposals_after_run": board_open_after,
            "board_ready_proposals_after_run": board_ready_after,
            "capacity_runnable_after_run": sum(
                phase in {"queued", "waiting"} for phase in again["work"].values()),
            "canceled_work": ["zz-cancel"],
            "duplicate_notification_observed": duplicate_result["observed"],
            "restart_observed": again["observed"],
            "healthy_retrieval_finished_during_slow_model": True,
            "dependent_started_before_slow_model_end": child_before_slow_end,
            "work_phases_after_restart": again["work"],
        },
        "timeline_ms": {
            name: {key: (value - run_started) * 1000 for key, value in times.items()}
            for name, times in event_times.items()
        } | {"accepted": {name: (stamp - run_started) * 1000
                           for name, stamp in commit_times.items()}},
        "main_notification_observed": main_result["observed"],
    }


def measure_trial(state_dir: Path, *, size: int, model_permits: int,
                  slow_seconds: float = 0.5,
                  commit_delay_seconds: float = 0.001) -> dict:
    """One real-component workload in a fresh, retained disposable store."""
    return asyncio.run(_trial(
        state_dir, size=size, model_permits=model_permits,
        slow_seconds=slow_seconds, commit_delay_seconds=commit_delay_seconds,
    ))


def _worker(args: argparse.Namespace) -> None:
    result = measure_trial(
        args.state_dir, size=args.size, model_permits=args.model_permits,
        slow_seconds=args.slow_seconds,
        commit_delay_seconds=args.commit_delay_seconds,
    )
    print(json.dumps(result, sort_keys=True))


def _orchestrate(args: argparse.Namespace) -> None:
    sizes = [int(part.strip()) for part in args.sizes.split(",")]
    if not sizes or any(size < 6 for size in sizes) or args.parallel_permits < 2:
        raise SystemExit("Use sizes >= 6 and parallel permits >= 2")
    output_dir = args.output_dir or (ROOT / ".local" / "stage3" / "extensions"
                                     / "bursts" / f"run-{uuid.uuid4().hex[:12]}")
    output_dir.mkdir(parents=True, exist_ok=False)
    trials = []
    for size in sizes:
        for mode, permits in (("serial", 1), ("eligible_parallel", args.parallel_permits)):
            state_dir = output_dir / f"state-{size}-{mode}"
            command = [sys.executable, str(Path(__file__).resolve()), "--worker",
                       "--state-dir", str(state_dir), "--size", str(size),
                       "--model-permits", str(permits), "--slow-seconds",
                       str(args.slow_seconds), "--commit-delay-seconds",
                       str(args.commit_delay_seconds)]
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            if completed.returncode:
                raise RuntimeError(f"Disposable trial {size}/{mode} failed: {completed.stderr[-2000:]}")
            trial = json.loads(completed.stdout)
            trial["mode"] = mode
            trials.append(trial)
    receipt = {
        "schema": "ora.stage3.queued_work_bursts.v1",
        "proof": "disposable_offline_synthetic_callback_workload",
        "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {"platform": platform.platform(), "python": platform.python_version(),
                        "machine": platform.machine(), "processes": "one fresh child per arm"},
        "source_sha256": _source_hashes(),
        "workload": {"sizes": sizes, "serial_model_permits": 1,
                     "parallel_model_permits": args.parallel_permits,
                     "slow_seconds": args.slow_seconds,
                     "commit_delay_seconds": args.commit_delay_seconds,
                     "synthetic_callback_only": True,
                     "duplicate_readiness_race": True,
                     "reopened_store_before_dispatch_and_after_completion": True},
        "trials": trials,
        "limits": [
            "No model/provider request, scientific benchmark or production mission",
            "Accepted result transactions alone receive an artificial precommit delay",
            "Process RSS is the resource.getrusage high-water mark, sampled before setup, after setup and after execution; WAL is sampled every 20 ms",
            "Capacity queue-to-admission uses the saved store clock; callback start from run start is a separate end-to-end timing",
            "Receipt duration sums overlap across concurrent work and are not wall time",
            "StateStore and executor are reopened before dispatch and after completed work; no process kill during an external call",
            "Immediate separate-reader query measures local visibility, not UI refresh latency",
            "No fixed task, research-round, mission-duration or production throughput limit is inferred",
        ],
    }
    receipt_path = output_dir / "measurement.json"
    with receipt_path.open("x") as handle:
        json.dump(receipt, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({"output": str(receipt_path), "trials": len(trials),
                      "proof": receipt["proof"]}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="8,24",
                        help="Comma-separated disposable work counts (each >= 6)")
    parser.add_argument("--parallel-permits", type=int, default=3)
    parser.add_argument("--slow-seconds", type=float, default=0.5)
    parser.add_argument("--commit-delay-seconds", type=float, default=0.001)
    parser.add_argument("--output-dir", type=Path,
                        help="Fresh private directory; default is unique ignored .local path")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--state-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--size", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--model-permits", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        if args.state_dir is None or args.size is None or args.model_permits is None:
            parser.error("Worker requires state directory, size and permits")
        _worker(args)
    else:
        _orchestrate(args)


if __name__ == "__main__":
    main()
