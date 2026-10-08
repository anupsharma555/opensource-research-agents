"""Run one scripted peer task with Python's built-in asynchronous tools.

It waits for the task's synthetic delay and returns the same proposed action used
by the framework comparisons. The shared SQLite board saves accepted work, so
this small adapter needs no separate checkpoint store.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from common import scripted_decision


async def execute(payload: dict[str, Any], checkpoint_dir: Path) -> tuple[dict[str, Any], int]:
    del checkpoint_dir
    await asyncio.sleep(payload["delay"])
    return scripted_decision(payload), 0


async def inventory(checkpoint_dir: Path, tasks: list[dict[str, Any]]) -> dict[str, int]:
    del checkpoint_dir, tasks
    return {}
