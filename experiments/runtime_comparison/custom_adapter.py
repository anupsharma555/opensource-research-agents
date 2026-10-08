"""Minimal stdlib asyncio task execution; board persistence lives in common.py."""

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
