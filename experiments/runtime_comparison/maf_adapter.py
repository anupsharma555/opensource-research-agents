"""Run one scripted peer task through Microsoft Agent Framework.

Two workflow steps pass the task and return its proposed action, saving framework
checkpoints as files. The shared research board still decides whether the action
becomes an accepted artifact or peer message.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Never

from agent_framework import (
    Executor,
    FileCheckpointStorage,
    WorkflowBuilder,
    WorkflowContext,
    handler,
)
from common import scripted_decision


class Prepare(Executor):
    def __init__(self) -> None:
        super().__init__(id="prepare")

    @handler
    async def handle(self, message: str, ctx: WorkflowContext[str]) -> None:
        await ctx.send_message(message)


class Act(Executor):
    def __init__(self) -> None:
        super().__init__(id="act")

    @handler
    async def handle(self, message: str, ctx: WorkflowContext[Never, str]) -> None:
        payload = json.loads(message)
        await asyncio.sleep(payload["delay"])
        await ctx.yield_output(json.dumps(scripted_decision(payload), sort_keys=True))


async def execute(payload: dict[str, Any], checkpoint_dir: Path) -> tuple[dict[str, Any], int]:
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    # Stable name and executor identities are necessary to locate/reload checkpoints.
    name = f"peer-task-{payload['task_id']}-{payload['attempt']}"
    storage = FileCheckpointStorage(checkpoint_dir)
    prepare, act = Prepare(), Act()
    workflow = (
        WorkflowBuilder(start_executor=prepare, name=name, checkpoint_storage=storage)
        .add_edge(prepare, act)
        .build()
    )
    result = await workflow.run(json.dumps(payload, sort_keys=True))
    checkpoints = await storage.list_checkpoints(workflow_name=name)
    return json.loads(result.get_outputs()[-1]), len(checkpoints)


async def inventory(checkpoint_dir: Path, tasks: list[dict[str, Any]]) -> dict[str, int]:
    if not checkpoint_dir.exists():
        return {}
    result: dict[str, int] = {}
    storage = FileCheckpointStorage(checkpoint_dir)
    for task in tasks:
        for attempt in range(1, task["attempt"] + 1):
            name = f"peer-task-{task['task_id']}-{attempt}"
            count = len(await storage.list_checkpoints(workflow_name=name))
            if count:
                result[f"{task['task_id']}:{attempt}"] = count
    return result
