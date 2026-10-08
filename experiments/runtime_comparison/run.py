"""Run one segment of the fixed peer-collaboration test with a chosen runtime.

The script schedules eligible tasks and asks the adapter to return scripted peer
actions; a shared SQLite board saves accepted results. It can stop at a selected
point and resume unfinished work later, without calling a model or provider.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import time
from pathlib import Path
from typing import Any

from common import Board, Task, canonical


async def segment(
    adapter_name: str,
    workdir: Path,
    fault: str,
    resume: bool,
    requested_model: str | None,
    requested_settings_json: str | None,
) -> dict[str, Any]:
    board = Board(workdir / "board.sqlite")
    board.seed()
    if resume:
        board.reclaim_after_process_restart()
    if requested_model and requested_model != board.model()["model"]:
        board.close()
        raise ValueError("requested model differs from the saved selected-model manifest")
    if requested_settings_json and json.loads(requested_settings_json) != board.model():
        board.close()
        raise ValueError("requested settings differ from the saved selected-model manifest")

    adapter = importlib.import_module(f"{adapter_name}_adapter")
    pending: dict[asyncio.Task, Task] = {}
    started = time.monotonic()
    first_artifact_at: float | None = None
    interrupted = False
    max_active = 0
    results: list[str] = []
    completed_this_segment = 0
    fault_deadline = started + 0.050 if fault == "during-step" else None

    try:
        for _ in range(20):  # Fixture-only assertion against an accidental infinite loop.
            while len(pending) < 2:
                task = board.claim()
                if task is None:
                    break
                future = asyncio.create_task(
                    adapter.execute(task.payload(), workdir / "checkpoints")
                )
                pending[future] = task
                max_active = max(max_active, len(pending))
            if not pending:
                break

            timeout = (
                None if fault_deadline is None else max(0.0, fault_deadline - time.monotonic())
            )
            done, _ = await asyncio.wait(
                pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                interrupted = True
                results.append("injected: during-step")
                break
            for future in done:
                task = pending.pop(future)
                decision, checkpoint_count = await future
                if fault == "before-commit" and completed_this_segment == 0:
                    interrupted = True
                    results.append(
                        f"injected: before-commit:{task.task_id}; native_checkpoints={checkpoint_count}"
                    )
                    break
                outcome = board.commit(task, decision)
                results.append(f"{task.task_id}:{outcome}; native_checkpoints={checkpoint_count}")
                if outcome == "committed":
                    completed_this_segment += 1
                    if first_artifact_at is None:
                        first_artifact_at = time.monotonic() - started
                if fault == "after-first-commit" and completed_this_segment == 1:
                    interrupted = True
                    results.append("injected: after-first-commit")
                    break
            if interrupted:
                break
        else:
            raise RuntimeError("fixture did not settle in 20 scheduling rounds")
    finally:
        for future in pending:
            future.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        snapshot = board.snapshot()
        native_checkpoints = await adapter.inventory(workdir / "checkpoints", snapshot["tasks"])
        board.close()

    return {
        "adapter": adapter_name,
        "fault": fault,
        "resume": resume,
        "interrupted": interrupted,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "first_artifact_seconds": None
        if first_artifact_at is None
        else round(first_artifact_at, 6),
        "max_active": max_active,
        "results": results,
        "native_checkpoint_inventory": native_checkpoints,
        "snapshot": snapshot,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", choices=("custom", "langgraph", "maf"), required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument(
        "--fault",
        choices=("none", "after-first-commit", "before-commit", "during-step"),
        default="none",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--requested-model")
    parser.add_argument("--requested-settings-json")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    report = asyncio.run(
        segment(
            args.adapter,
            args.workdir,
            args.fault,
            args.resume,
            args.requested_model,
            args.requested_settings_json,
        )
    )
    output = canonical(report)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(output + "\n")
    print(output)


if __name__ == "__main__":
    main()
