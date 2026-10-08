"""Create the same miniature research collaboration for each runtime candidate.

Scripted peers collect two sources, ask for a check, repair one invalid action,
and propose a synthesis while other work continues. Its SQLite board records the
accepted work; a framework checkpoint alone cannot accept an artifact or message.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODEL = {
    "provider": "scripted-offline",
    "model": "fixture-model-v1",
    "temperature": 0,
    "tool_schema_version": "fixture-tools-v1",
    "instructions_version": "fixture-instructions-v1",
}
ALLOWED_TOOLS = {"search_snapshot", "check_source", "write_summary"}
SOURCE_CANDIDATES = {
    "collect_a": ["synthetic-source-a"],
    "collect_b": ["synthetic-source-b"],
    "verify_b": ["synthetic-source-b"],
    "synthesize": [],
}
INITIAL_TASKS = (
    ("collect_a", "collector_a", 0.200, []),
    ("collect_b", "collector_b", 0.020, []),
)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class Task:
    task_id: str
    role: str
    delay: float
    attempt: int
    dependencies: tuple[str, ...]
    model: dict[str, Any]
    feedback: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "role": self.role,
            "delay": self.delay,
            "attempt": self.attempt,
            "dependencies": list(self.dependencies),
            "model": self.model,
            "model_hash": digest(self.model),
            "source_candidates": SOURCE_CANDIDATES[self.task_id],
            "feedback": self.feedback,
        }


def scripted_decision(payload: dict[str, Any]) -> dict[str, Any]:
    """A specialist chooses an action; no scheduler chooses scientific work."""
    task_id = payload["task_id"]
    if task_id.startswith("benchmark-event-"):
        packet = payload["worker_packet"]
        event = payload["delivered_event"]
        return {
            "task_id": task_id,
            "worker_packet_sha256": packet["worker_packet_sha256"],
            "initial_source_ids": [x["source_id"] for x in packet["sources"]],
            "delivered_event_id": event["event_id"],
            "delivered_event_kind": event["kind"],
        }
    tool = {
        "collect_a": "search_snapshot",
        "collect_b": "search_snapshot",
        "verify_b": "check_source" if payload["feedback"] else "unlisted_tool",
        "synthesize": "write_summary",
    }[task_id]
    artifact = f"checked:{task_id}"
    decision = {
        "task_id": task_id,
        "tool": tool,
        "artifact_id": f"artifact:{task_id}",
        "artifact": artifact,
        "artifact_version": "v1",
        "content_hash": hashlib.sha256(artifact.encode()).hexdigest(),
        "model_hash": payload["model_hash"],
        "selected_source": payload["source_candidates"][0]
        if payload["source_candidates"]
        else None,
    }
    if task_id == "collect_b":
        decision["directed_request"] = {
            "message_id": "request:verify_b",
            "recipient": "critic",
            "content": "check source-b",
            "task": {
                "task_id": "verify_b",
                "role": "critic",
                "delay": 0.010,
                "dependencies": ["collect_b"],
            },
        }
    if task_id == "verify_b":
        decision["proposed_task"] = {
            "task_id": "synthesize",
            "role": "synthesizer",
            "delay": 0.010,
            "dependencies": ["collect_a", "collect_b", "verify_b"],
        }
    return decision


class Board:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS manifest (
              key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
              task_id TEXT PRIMARY KEY, role TEXT NOT NULL, delay REAL NOT NULL,
              dependencies TEXT NOT NULL, status TEXT NOT NULL,
              attempt INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS artifacts (
              artifact_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
              artifact_version TEXT NOT NULL, content TEXT NOT NULL,
              content_hash TEXT NOT NULL, model_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
              message_id TEXT PRIMARY KEY, sender TEXT NOT NULL,
              recipient TEXT NOT NULL, content TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
              task_id TEXT, detail TEXT NOT NULL
            );
            """
        )

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self):
        # isolation_level=None needs an explicit BEGIN; `with connection` alone
        # would autocommit each statement and split artifact/message effects.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def seed(self) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO manifest VALUES ('model', ?)",
                (canonical(MODEL),),
            )
            for task_id, role, delay, deps in INITIAL_TASKS:
                self.db.execute(
                    "INSERT OR IGNORE INTO tasks VALUES (?, ?, ?, ?, 'ready', 0)",
                    (task_id, role, delay, canonical(deps)),
                )

    def model(self) -> dict[str, Any]:
        return json.loads(
            self.db.execute("SELECT value FROM manifest WHERE key='model'").fetchone()[0]
        )

    def reclaim_after_process_restart(self) -> None:
        # The comparison invokes this only from a new process after the prior one exits.
        with self.transaction():
            rows = self.db.execute("SELECT task_id FROM tasks WHERE status='claimed'").fetchall()
            for row in rows:
                self.db.execute("UPDATE tasks SET status='ready' WHERE task_id=?", (row[0],))
                self._event("reclaimed", row[0], "prior process exited")

    def claim(self) -> Task | None:
        with self.transaction():
            rows = self.db.execute(
                "SELECT * FROM tasks WHERE status='ready' ORDER BY task_id"
            ).fetchall()
            completed = {
                row[0] for row in self.db.execute("SELECT task_id FROM tasks WHERE status='done'")
            }
            for row in rows:
                deps = tuple(json.loads(row["dependencies"]))
                if not set(deps).issubset(completed):
                    continue
                attempt = row["attempt"] + 1
                self.db.execute(
                    "UPDATE tasks SET status='claimed', attempt=? WHERE task_id=?",
                    (attempt, row["task_id"]),
                )
                self._event("claimed", row["task_id"], f"attempt={attempt}")
                error = self.db.execute(
                    "SELECT detail FROM events WHERE kind='rejected_action' AND task_id=? "
                    "ORDER BY sequence DESC LIMIT 1",
                    (row["task_id"],),
                ).fetchone()
                return Task(
                    row["task_id"],
                    row["role"],
                    row["delay"],
                    attempt,
                    deps,
                    self.model(),
                    error[0] if error else None,
                )
        return None

    def commit(self, task: Task, decision: dict[str, Any]) -> str:
        """One transaction accepts a result and its directed follow-up exactly once."""
        with self.transaction():
            row = self.db.execute(
                "SELECT status, attempt FROM tasks WHERE task_id=?", (task.task_id,)
            ).fetchone()
            if row is None or row["status"] == "done":
                return "duplicate"
            if row["status"] != "claimed" or row["attempt"] != task.attempt:
                return "stale"
            if decision["model_hash"] != digest(self.model()):
                raise ValueError("selected model/settings manifest changed")
            if (
                decision["content_hash"]
                != hashlib.sha256(decision["artifact"].encode()).hexdigest()
            ):
                raise ValueError("artifact content hash mismatch")
            valid_sources = SOURCE_CANDIDATES[task.task_id] or [None]
            if (
                decision["tool"] not in ALLOWED_TOOLS
                or decision["selected_source"] not in valid_sources
            ):
                self.db.execute("UPDATE tasks SET status='ready' WHERE task_id=?", (task.task_id,))
                self._event(
                    "rejected_action",
                    task.task_id,
                    f"tool={decision['tool']};source={decision['selected_source']}",
                )
                return "repair"
            existing_artifact = self.db.execute(
                "SELECT task_id, artifact_version, content_hash FROM artifacts WHERE artifact_id=?",
                (decision["artifact_id"],),
            ).fetchone()
            intended_artifact = (
                task.task_id,
                decision["artifact_version"],
                decision["content_hash"],
            )
            if existing_artifact and tuple(existing_artifact) != intended_artifact:
                self.db.execute("UPDATE tasks SET status='ready' WHERE task_id=?", (task.task_id,))
                self._event("rejected_action", task.task_id, "artifact identity collision")
                return "repair"
            request = decision.get("directed_request")
            if request:
                existing_message = self.db.execute(
                    "SELECT sender, recipient, content FROM messages WHERE message_id=?",
                    (request["message_id"],),
                ).fetchone()
                if existing_message and tuple(existing_message) != (
                    task.role,
                    request["recipient"],
                    request["content"],
                ):
                    self.db.execute(
                        "UPDATE tasks SET status='ready' WHERE task_id=?", (task.task_id,)
                    )
                    self._event("rejected_action", task.task_id, "peer-message identity collision")
                    return "repair"
            self.db.execute(
                "INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?, ?, ?, ?)",
                (
                    decision["artifact_id"],
                    task.task_id,
                    decision["artifact_version"],
                    decision["artifact"],
                    decision["content_hash"],
                    decision["model_hash"],
                ),
            )
            self.db.execute("UPDATE tasks SET status='done' WHERE task_id=?", (task.task_id,))
            self._event("committed", task.task_id, decision["artifact_id"])
            if request:
                self.db.execute(
                    "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?)",
                    (request["message_id"], task.role, request["recipient"], request["content"]),
                )
                proposed = request["task"]
                self.db.execute(
                    "INSERT OR IGNORE INTO tasks VALUES (?, ?, ?, ?, 'ready', 0)",
                    (
                        proposed["task_id"],
                        proposed["role"],
                        proposed["delay"],
                        canonical(proposed["dependencies"]),
                    ),
                )
                self._event(
                    "peer_request", proposed["task_id"], f"{task.role} -> {request['recipient']}"
                )
            if proposed := decision.get("proposed_task"):
                self.db.execute(
                    "INSERT OR IGNORE INTO tasks VALUES (?, ?, ?, ?, 'ready', 0)",
                    (
                        proposed["task_id"],
                        proposed["role"],
                        proposed["delay"],
                        canonical(proposed["dependencies"]),
                    ),
                )
                self._event("proposal", proposed["task_id"], f"{task.role} proposed follow-up")
        return "committed"

    def _event(self, kind: str, task_id: str | None, detail: str) -> None:
        self.db.execute(
            "INSERT INTO events(kind, task_id, detail) VALUES (?, ?, ?)", (kind, task_id, detail)
        )

    def snapshot(self) -> dict[str, Any]:
        def rows(query: str) -> list[dict[str, Any]]:
            return [dict(r) for r in self.db.execute(query)]

        return {
            "tasks": rows("SELECT task_id, role, status, attempt FROM tasks ORDER BY task_id"),
            "artifacts": rows("SELECT * FROM artifacts ORDER BY artifact_id"),
            "messages": rows("SELECT * FROM messages ORDER BY message_id"),
            "events": rows("SELECT kind, task_id, detail FROM events ORDER BY sequence"),
            "model_hash": digest(self.model()),
        }
