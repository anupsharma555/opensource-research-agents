"""Give each offline task attempt its own temporary workspace and network port.

SQLite tracks the current owner so an older attempt cannot write into or clean up
its replacement's workspace. This example removes only files it created; it does
not supervise processes or enforce a production sandbox.
"""

import hashlib
import json
import secrets
import socket
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Allocation:
    mission_id: str
    logical_id: str
    attempt_id: str
    owner_token: str
    generation: int
    workspace: Path
    listener: socket.socket

    @property
    def port(self) -> int:
        return self.listener.getsockname()[1]


class ResourceRegistry:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        self.db = sqlite3.connect(self.root / "ownership.sqlite")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS owners (mission TEXT, id TEXT, attempt TEXT, token TEXT, generation INTEGER, workspace TEXT, PRIMARY KEY(mission,id))"
        )

    def allocate(self, mission_id: str, logical_id: str, attempt_id: str) -> Allocation:
        self.db.execute("BEGIN IMMEDIATE")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        workspace = None
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            old = self.db.execute(
                "SELECT generation FROM owners WHERE mission=? AND id=?", (mission_id, logical_id)
            ).fetchone()
            generation = old[0] + 1 if old else 1
            prefix = hashlib.sha256(attempt_id.encode()).hexdigest()[:12]
            workspace = Path(tempfile.mkdtemp(prefix=f"attempt-{prefix}-", dir=self.root))
            token = secrets.token_hex(24)
            self.db.execute(
                "INSERT INTO owners VALUES (?,?,?,?,?,?) ON CONFLICT(mission,id) DO UPDATE SET attempt=excluded.attempt,token=excluded.token,generation=excluded.generation,workspace=excluded.workspace",
                (mission_id, logical_id, attempt_id, token, generation, str(workspace)),
            )
            self.db.commit()
            return Allocation(
                mission_id, logical_id, attempt_id, token, generation, workspace, listener
            )
        except BaseException:
            self.db.rollback()
            listener.close()
            if workspace:
                workspace.rmdir()
            raise

    def _assert_owner(self, allocation: Allocation):
        row = self.db.execute(
            "SELECT attempt,token,generation,workspace FROM owners WHERE mission=? AND id=?",
            (allocation.mission_id, allocation.logical_id),
        ).fetchone()
        expected = (
            allocation.attempt_id,
            allocation.owner_token,
            allocation.generation,
            str(allocation.workspace),
        )
        if row != expected or allocation.workspace.resolve().parent != self.root:
            raise PermissionError("stale owner: replacement resources remain untouched")

    def write_result(self, allocation: Allocation, value: dict):
        # The same transaction excludes a concurrent ownership replacement.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._assert_owner(allocation)
            target = allocation.workspace / "result.json"
            if target.is_symlink():
                raise PermissionError("scratch output may not follow a symbolic link")
            target.write_text(json.dumps(value, sort_keys=True))
            self.db.commit()
            return target
        except BaseException:
            self.db.rollback()
            raise

    def cleanup(self, allocation: Allocation):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._assert_owner(allocation)
            allowed = allocation.workspace / "result.json"
            unexpected = [p for p in allocation.workspace.iterdir() if p != allowed]
            if unexpected:
                raise ValueError(
                    "unknown files retained for diagnosis; cleanup is scoped to the known result"
                )
            allowed.unlink(missing_ok=True)
            allocation.workspace.rmdir()
            self.db.execute(
                "DELETE FROM owners WHERE mission=? AND id=?",
                (allocation.mission_id, allocation.logical_id),
            )
            self.db.commit()
            allocation.listener.close()
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()
