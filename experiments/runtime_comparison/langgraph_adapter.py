"""Actual LangGraph StateGraph with a disk-backed SQLite checkpoint saver."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, TypedDict

from common import scripted_decision
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph


class State(TypedDict):
    payload: dict[str, Any]
    decision: dict[str, Any]


async def prepare(state: State) -> dict[str, Any]:
    return {"payload": state["payload"]}


async def act(state: State) -> dict[str, Any]:
    payload = state["payload"]
    await asyncio.sleep(payload["delay"])
    return {"decision": scripted_decision(payload)}


def build():
    builder = StateGraph(State)
    builder.add_node("prepare", prepare)
    builder.add_node("act", act)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "act")
    builder.add_edge("act", END)
    return builder


async def execute(payload: dict[str, Any], checkpoint_dir: Path) -> tuple[dict[str, Any], int]:
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    path = checkpoint_dir / f"langgraph-{payload['task_id']}-{payload['attempt']}.sqlite"
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        graph = build().compile(checkpointer=saver)
        config = {"configurable": {"thread_id": f"{payload['task_id']}:{payload['attempt']}"}}
        result = await graph.ainvoke({"payload": payload}, config=config, durability="sync")
        checkpoints = [item async for item in saver.alist(config)]
        return result["decision"], len(checkpoints)


async def inventory(checkpoint_dir: Path, tasks: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for task in tasks:
        for attempt in range(1, task["attempt"] + 1):
            key = f"{task['task_id']}:{attempt}"
            path = checkpoint_dir / f"langgraph-{task['task_id']}-{attempt}.sqlite"
            if not path.exists():
                continue
            async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
                config = {"configurable": {"thread_id": key}}
                count = len([item async for item in saver.alist(config)])
                if count:
                    result[key] = count
    return result
