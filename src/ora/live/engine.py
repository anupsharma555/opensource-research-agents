"""Persist and execute bounded local research runs with named model peers.

SQLite records commands, immutable read snapshots, calls and their cost before
and after each external boundary. A call left in flight after a restart is
uncertain and is never repeated automatically.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ora.agents import ConfigurationError
from ora.contracts.models import (
    LiveAgent,
    LiveFollowupRequest,
    LiveReceipt,
    LiveRunSummary,
    LiveStartRequest,
    LiveState,
    ResearchSnapshot,
)
from ora.skills.registry import load_registry
from ora.state.schema import CURRENT_VERSION as STATE_SCHEMA_VERSION

from .provenance import LiveProvenance
from .provenance_schema import CURRENT_VERSION as LIVE_PROVENANCE_VERSION
from .provenance_schema import migrate_live_provenance
from .provider import ConfigurationDrift, CredentialUnavailable, GroqProvider, ProviderError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_EXPLICIT_SOURCE_SPAN = re.compile(
    r"(?<![A-Za-z0-9_.:-])([A-Za-z0-9_.-]+)::([A-Za-z0-9_.-]+)(?![A-Za-z0-9_.:-])"
)
_LIMITATIONS_HEADER = re.compile(
    r"^\s*(?:#{1,6}\s+)?(?:\*\*Limitations?\*\*:|\*\*Limitations?:\*\*|Limitations?:)\s*(.*?)\s*$",
    re.IGNORECASE,
)
_LIMITATIONS_STANDALONE = re.compile(
    r"^\s*(?:#{1,6}\s+)?(?:\*\*Limitations?\*\*|Limitations?)\s*$",
    re.IGNORECASE,
)
_LIMITATIONS_BULLET = re.compile(r"^\s*(?:[-*]\s+|\d+[.)]\s+)(\S.*?)\s*$")
_MARKDOWN_HEADING = re.compile(r"^\s*#{1,6}\s+\S")
_LABELED_HEADING = re.compile(r"^[A-Z][A-Za-z0-9 /-]{1,80}:")
_FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _plain_text_citations(
    text: str, valid: set[tuple[str, str]], read_sources: set[str]
) -> list[dict[str, str]]:
    """Keep only literal saved source::span references from evidence read by this agent."""
    chosen: list[dict[str, str]] = []
    for match in _EXPLICIT_SOURCE_SPAN.finditer(text):
        pair = match.group(1), match.group(2)
        if pair not in valid and pair[1].endswith("."):
            # A sentence period is punctuation only when the shorter exact pair exists.
            trimmed = pair[1]
            while trimmed.endswith("."):
                trimmed = trimmed[:-1]
                if (pair[0], trimmed) in valid:
                    pair = pair[0], trimmed
                    break
        citation = {"sourceId": pair[0], "spanId": pair[1]}
        if pair in valid and pair[0] in read_sources and citation not in chosen:
            chosen.append(citation)
    return chosen


def _plain_text_limitations(text: str) -> list[str]:
    """Copy only text explicitly marked as a limitation; absence remains unknown."""
    limitations: list[str] = []
    paragraph: list[str] = []
    in_section = False
    after_blank = False
    fence: str | None = None

    def finish_paragraph() -> None:
        if paragraph:
            limitations.append(" ".join(paragraph))
            paragraph.clear()

    for line in text.splitlines():
        fence_line = _FENCE_LINE.match(line)
        if fence is not None:
            if (
                fence_line
                and fence_line.group(1)[0] == fence[0]
                and len(fence_line.group(1)) >= len(fence)
                and not fence_line.group(2).strip()
            ):
                fence = None
            continue
        if fence_line:
            finish_paragraph()
            fence = fence_line.group(1)
            continue
        heading = _LIMITATIONS_HEADER.fullmatch(line)
        if heading or _LIMITATIONS_STANDALONE.fullmatch(line):
            finish_paragraph()
            in_section = True
            after_blank = False
        if heading:
            detail = heading.group(1).strip()
            if detail:
                paragraph.append(detail)
            continue
        if _LIMITATIONS_STANDALONE.fullmatch(line):
            continue
        if not in_section:
            continue
        stripped = line.strip()
        if _MARKDOWN_HEADING.match(line) or (
            after_blank and _LABELED_HEADING.match(stripped.replace("**", ""))
        ):
            finish_paragraph()
            in_section = False
            continue
        if not stripped:
            finish_paragraph()
            after_blank = True
            continue
        bullet = _LIMITATIONS_BULLET.fullmatch(line)
        if bullet:
            finish_paragraph()
            limitations.append(bullet.group(1).strip())
        else:
            paragraph.append(stripped)
        after_blank = False
    finish_paragraph()
    return limitations


class LiveError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "source_read",
            "description": "Read an exact saved source and its passage locator. Only supplied evidence is available.",
            "parameters": {
                "type": "object",
                "properties": {"source_id": {"type": "string"}},
                "required": ["source_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_peer",
            "description": "Ask a named research peer to perform a concrete task. Their response is delivered later.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "question": {"type": "string"},
                },
                "required": ["agent_id", "question"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish_result",
            "description": "Publish and finish this agent's current contribution. Queued peers still work and the requester may revise after their replies. Include only relevant source/span IDs actually read; omit unsupported citations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "citations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_id": {"type": "string"},
                                "span_id": {"type": "string"},
                            },
                            "required": ["source_id", "span_id"],
                            "additionalProperties": False,
                        },
                    },
                    "limitations": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
]


class ResearchHost:
    """Single-process local host; independent DB connections guard every write."""

    def __init__(
        self, root: Path, state_dir: Path, provider: Any, run_budget_usd: float = 0.10
    ) -> None:
        if not math.isfinite(run_budget_usd) or run_budget_usd <= 0:
            raise ValueError("run_budget_usd must be finite and positive")
        self.root = Path(root).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
        self.db_path = self.state_dir / "research.sqlite3"
        self.provider = provider
        self.run_budget_usd = run_budget_usd
        self._lock = threading.RLock()
        self._shutdown_event = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ora-live")
        self._closed = False
        registry = load_registry(self.root, {"source_read_by_id", "study_appraisal"})
        self._skills = registry
        self._loaded_skills = {
            skill_id: {
                "instructions": registry.render_instructions(skill_id),
                "manifest_sha256": hashlib.sha256(_json(skill.manifest).encode()).hexdigest(),
            }
            for skill_id, skill in registry.skills.items()
            if skill_id != "task-planning"  # Requires the MissionService plan tools.
        }
        role_map = {
            "agent-synthesis": (
                "Sage",
                "evidence preparation and synthesis",
                "evidence_preparation_synthesis",
            ),
            "agent-critique": (
                "Mira",
                "independent methods reviewer and critic",
                "independent_methods_reviewer_critic",
            ),
        }
        self._role_ids: dict[str, str] = {}
        self.agents: list[LiveAgent] = []
        for agent_id, (name, role, role_id) in role_map.items():
            refs = [item for item in registry.roles[role_id]["skill_refs"]
                    if item["skill_id"] != "task-planning"]
            versions = ", ".join(f"{ref['skill_id']}@{ref['version']}" for ref in refs)
            if any(
                ref["skill_id"] not in registry.skills
                or registry.skills[ref["skill_id"]].version != ref["version"]
                for ref in refs
            ):
                raise LiveError(f"Required skill unavailable for {name}", 503)
            self.agents.append(LiveAgent(
                id=agent_id, name=name, role=role, skill_version=versions,
                model=self.provider.model, availability="available", role_template_id=role_id,
            ))
            self._role_ids[agent_id] = role_id
        self._require_explicit_saved_state_migration()
        self._provenance = LiveProvenance(self.root, self.state_dir, TOOLS)
        self._init_db()
        self._recover_incomplete()

    def _require_explicit_saved_state_migration(self) -> None:
        """Do not upgrade saved live runs during ordinary host startup."""
        if not self.db_path.exists():
            return
        db = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True)
        try:
            tables = {
                row[0]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"runs", "calls"}.issubset(tables):
                return
            if not db.execute("SELECT 1 FROM runs LIMIT 1").fetchone() and not db.execute(
                "SELECT 1 FROM calls LIMIT 1"
            ).fetchone():
                return
            shared_version = db.execute("PRAGMA user_version").fetchone()[0]
            live_version = (
                db.execute(
                    "SELECT COALESCE(MAX(version),0) FROM live_provenance_migrations"
                ).fetchone()[0]
                if "live_provenance_migrations" in tables else 0
            )
            if shared_version != STATE_SCHEMA_VERSION or live_version != LIVE_PROVENANCE_VERSION:
                raise LiveError(
                    "Saved live state requires an explicit backed-up provenance migration "
                    "before host startup; run scripts/dev live migrate-provenance.",
                    503,
                )
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _init_db(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                  fingerprint TEXT NOT NULL, task_id TEXT NOT NULL,
                  parent_run_id TEXT, parent_snapshot_id TEXT,
                  question TEXT NOT NULL, scope TEXT NOT NULL,
                  agent_ids TEXT NOT NULL, status TEXT NOT NULL,
                  snapshot_id TEXT NOT NULL, comparison_snapshot_id TEXT,
                  spent_usd REAL NOT NULL DEFAULT 0,
                  reserved_usd REAL NOT NULL DEFAULT 0,
                  error TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                  id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                  body TEXT NOT NULL, created_at TEXT NOT NULL,
                  FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                CREATE TABLE IF NOT EXISTS calls (
                  id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                  attempt_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                  status TEXT NOT NULL, phase TEXT NOT NULL,
                  messages TEXT NOT NULL, response TEXT,
                  tool_results TEXT NOT NULL DEFAULT '[]',
                  estimated_max_usd REAL NOT NULL, actual_usd REAL,
                  retry_of_call_id TEXT,
                  created_at TEXT NOT NULL, completed_at TEXT,
                  FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                CREATE INDEX IF NOT EXISTS calls_run ON calls(run_id, created_at);
                """
            )
            call_columns = {row["name"] for row in db.execute("PRAGMA table_info(calls)")}
            if "retry_of_call_id" not in call_columns:
                db.execute("ALTER TABLE calls ADD COLUMN retry_of_call_id TEXT")
            if "tool_results" not in call_columns:
                db.execute("ALTER TABLE calls ADD COLUMN tool_results TEXT NOT NULL DEFAULT '[]'")
            migrate_live_provenance(db)
            # Local state contains user-supplied evidence and prompts.
            self.db_path.chmod(0o600)

    def _recover_incomplete(self) -> None:
        safe_queued: list[str] = []
        with self._lock, self._connect() as db:
            runs = db.execute(
                "SELECT id,status FROM runs WHERE status IN ('queued','running')"
            ).fetchall()
            for run in runs:
                run_id = run["id"]
                call_count = db.execute(
                    "SELECT count(*) AS count FROM calls WHERE run_id=?", (run_id,)
                ).fetchone()["count"]
                if run["status"] == "queued" and call_count == 0:
                    safe_queued.append(run_id)
                    continue
                db.execute(
                    "UPDATE calls SET status='uncertain', phase='interrupted', "
                    "completed_at=? WHERE run_id=? AND status='in_flight'",
                    (_now(), run_id),
                )
                interrupted_at = _now()
                started_tools = db.execute(
                    "SELECT id FROM tool_invocations WHERE run_id=? AND status='started'",
                    (run_id,),
                ).fetchall()
                db.execute(
                    "UPDATE tool_invocations SET status='uncertain', completed_at=? "
                    "WHERE run_id=? AND status='started'",
                    (interrupted_at, run_id),
                )
                db.executemany(
                    "INSERT INTO tool_invocation_events(invocation_id,status,occurred_at,"
                    "provenance_basis) VALUES(?,'uncertain',?,'recorded')",
                    [(tool["id"], interrupted_at) for tool in started_tools],
                )
                db.execute(
                    "UPDATE runs SET status='waiting', reserved_usd=0, "
                    "error='Execution interrupted; external call outcome may be uncertain. No automatic retry.' "
                    "WHERE id=?",
                    (run_id,),
                )
                self._mutate_locked(
                    db,
                    run_id,
                    lambda snapshot: self._set_interrupted(snapshot),
                )
        for run_id in safe_queued:
            self._executor.submit(self._execute, run_id)

    @staticmethod
    def _set_interrupted(snapshot: dict) -> None:
        snapshot["mission"]["status"] = "waiting"
        for task in snapshot["tasks"]:
            if task["status"] in ("queued", "running"):
                task["status"] = "waiting"
                task["activeOrWaiting"] = "waiting"
        for attempt in snapshot["attempts"]:
            if attempt["status"] == "running":
                attempt["status"] = "uncertain"
                attempt["endedAt"] = _now()
        snapshot["diagnostics"].append(
            {
                "id": _id("diagnostic"),
                "kind": "restart",
                "status": "uncertain",
                "message": "Local execution stopped; review calls before any new request.",
                "occurredAt": _now(),
            }
        )

    def _snapshot_locked(self, db: sqlite3.Connection, run_id: str) -> dict:
        row = db.execute(
            "SELECT s.body FROM snapshots s JOIN runs r ON r.snapshot_id=s.id WHERE r.id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise LiveError("Unknown run", 404)
        return json.loads(row["body"])

    def _mutate_locked(self, db: sqlite3.Connection, run_id: str, change) -> dict:
        snapshot = self._snapshot_locked(db, run_id)
        change(snapshot)
        snapshot["snapshotId"] = _id("snapshot")
        snapshot["capturedAt"] = _now()
        checked = ResearchSnapshot.model_validate(snapshot).model_dump(by_alias=True)
        db.execute(
            "INSERT INTO snapshots(id,run_id,body,created_at) VALUES(?,?,?,?)",
            (checked["snapshotId"], run_id, _json(checked), checked["capturedAt"]),
        )
        db.execute(
            "UPDATE runs SET snapshot_id=? WHERE id=?",
            (checked["snapshotId"], run_id),
        )
        return checked

    def _mutate(self, run_id: str, change) -> dict:
        with self._lock, self._connect() as db:
            return self._mutate_locked(db, run_id, change)

    def _agent_ids(self, chosen: list[str]) -> list[str]:
        known = {agent.id for agent in self.agents}
        if not chosen or len(set(chosen)) != len(chosen) or set(chosen) - known:
            raise LiveError("Choose one or more distinct available agents")
        return chosen

    @staticmethod
    def _fingerprint(request: Any) -> str:
        body = request.model_dump(by_alias=True)
        # This legacy host predates optional contribution requirements. Empty
        # additions must not change the identity of an unchanged saved request.
        body.pop("requiredAgentIds", None)
        body.pop("credentialProfile", None)
        if not request.allow_vision:
            body.pop("allowVision", None)
        return hashlib.sha256(_json(body).encode()).hexdigest()

    def _duplicate(self, db: sqlite3.Connection, request: Any) -> LiveReceipt | None:
        row = db.execute("SELECT * FROM runs WHERE request_id=?", (request.request_id,)).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != self._fingerprint(request):
            raise LiveError("Request ID already belongs to different input", 409)
        return LiveReceipt(
            request_id=request.request_id,
            run_id=row["id"],
            task_id=row["task_id"],
            status="queued",
            reused=True,
        )

    def start(self, request: LiveStartRequest) -> LiveReceipt:
        if self._closed:
            raise LiveError("Host is closed", 503)
        if not isinstance(request, LiveStartRequest):
            request = LiveStartRequest.model_validate(request)
        if request.allow_vision:
            raise LiveError("Vision calls need the shared mission runtime")
        if request.required_agent_ids:
            raise LiveError("Required contribution choices need the shared mission runtime")
        if request.credential_profile not in {None, "primary"}:
            raise LiveError("Backup credential selection needs the shared mission runtime")
        agents = self._agent_ids(request.agent_ids)
        if not request.evidence_text.strip():
            raise LiveError("Supplied evidence is required")
        with self._lock, self._connect() as db:
            duplicate = self._duplicate(db, request)
            if duplicate:
                return duplicate
            run_id, task_id = _id("run"), _id("task")
            snapshot = self._new_snapshot(run_id, request, agents, task_id)
            db.execute(
                "INSERT INTO runs(id,request_id,fingerprint,task_id,parent_run_id,"
                "parent_snapshot_id,question,scope,agent_ids,status,snapshot_id,"
                "comparison_snapshot_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    request.request_id,
                    self._fingerprint(request),
                    task_id,
                    None,
                    None,
                    request.question,
                    request.scope,
                    _json(agents),
                    "queued",
                    snapshot["snapshotId"],
                    None,
                    _now(),
                ),
            )
            db.execute(
                "INSERT INTO snapshots(id,run_id,body,created_at) VALUES(?,?,?,?)",
                (snapshot["snapshotId"], run_id, _json(snapshot), snapshot["capturedAt"]),
            )
        self._executor.submit(self._execute, run_id)
        return LiveReceipt(
            request_id=request.request_id, run_id=run_id, task_id=task_id, status="queued"
        )

    def followup(self, request: LiveFollowupRequest) -> LiveReceipt:
        if self._closed:
            raise LiveError("Host is closed", 503)
        if not isinstance(request, LiveFollowupRequest):
            request = LiveFollowupRequest.model_validate(request)
        if request.allow_vision:
            raise LiveError("Vision calls need the shared mission runtime")
        if request.required_agent_ids:
            raise LiveError("Required contribution choices need the shared mission runtime")
        if request.credential_profile not in {None, "primary"}:
            raise LiveError("Backup credential selection needs the shared mission runtime")
        agents = self._agent_ids(request.agent_ids)
        with self._lock, self._connect() as db:
            duplicate = self._duplicate(db, request)
            if duplicate:
                return duplicate
            parent = db.execute(
                "SELECT * FROM runs WHERE id=?", (request.parent_run_id,)
            ).fetchone()
            if parent is None:
                raise LiveError("Unknown parent run", 404)
            saved = db.execute(
                "SELECT body FROM snapshots WHERE id=? AND run_id=?",
                (request.parent_snapshot_id, request.parent_run_id),
            ).fetchone()
            if saved is None:
                raise LiveError("Parent snapshot does not belong to that run", 409)
            baseline = json.loads(saved["body"])
            if not self._valid_target(baseline, request.target_id, request.target_version):
                raise LiveError("Target ID/version is absent from the parent snapshot", 409)
            run_id, task_id = _id("run"), _id("task")
            snapshot = deepcopy(baseline)
            snapshot.update(
                {
                    "snapshotId": _id("snapshot"),
                    "capturedAt": _now(),
                    "proofKind": "live_model_on_supplied_evidence",
                    "parentSnapshotId": request.parent_snapshot_id,
                    "inputQuestion": request.question,
                }
            )
            mission = snapshot["mission"]
            mission["version"] += 1
            mission["question"] = request.question
            mission["scope"] = request.scope
            mission["status"] = "active"
            mission["reportArtifactId"] = None
            snapshot["tasks"].append(
                self._task(task_id, request.question, "queued", request.target_id)
            )
            snapshot["tasks"].extend(self._selected_tasks(task_id, request.question, agents))
            snapshot["events"].append(
                self._event(
                    task_id,
                    None,
                    f"Follow-up requested on {request.target_id} version {request.target_version}.",
                )
            )
            checked = ResearchSnapshot.model_validate(snapshot).model_dump(by_alias=True)
            db.execute(
                "INSERT INTO runs(id,request_id,fingerprint,task_id,parent_run_id,"
                "parent_snapshot_id,question,scope,agent_ids,status,snapshot_id,"
                "comparison_snapshot_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    request.request_id,
                    self._fingerprint(request),
                    task_id,
                    request.parent_run_id,
                    request.parent_snapshot_id,
                    request.question,
                    request.scope,
                    _json(agents),
                    "queued",
                    checked["snapshotId"],
                    request.parent_snapshot_id,
                    _now(),
                ),
            )
            db.execute(
                "INSERT INTO snapshots(id,run_id,body,created_at) VALUES(?,?,?,?)",
                (checked["snapshotId"], run_id, _json(checked), checked["capturedAt"]),
            )
        self._executor.submit(self._execute, run_id)
        return LiveReceipt(
            request_id=request.request_id, run_id=run_id, task_id=task_id, status="queued"
        )

    @staticmethod
    def _valid_target(snapshot: dict, target_id: str, version: int) -> bool:
        for group in ("claims", "artifacts", "sources"):
            if any(
                item["id"] == target_id and item["version"] == version for item in snapshot[group]
            ):
                return True
        return any(
            item["id"] == target_id and snapshot["mission"]["version"] == version
            for item in snapshot["tasks"]
        )

    def _new_snapshot(
        self, run_id: str, request: LiveStartRequest, chosen: list[str], task_id: str
    ) -> dict:
        source_id, span_id = _id("source"), _id("span")
        evidence = request.evidence_text
        snapshot = {
            "schemaVersion": "1.0",
            "snapshotId": _id("snapshot"),
            "sampleVersion": "live-1",
            "capturedAt": _now(),
            "proofKind": "live_model_on_supplied_evidence",
            "inputQuestion": request.question,
            "mission": {
                "id": _id("mission"),
                "version": 1,
                "question": request.question,
                "scope": request.scope,
                "status": "active",
                "latencyClass": "long",
                "latencyBasis": "Local model work over supplied evidence.",
                "startedAt": _now(),
                "reportArtifactId": None,
            },
            "agents": [
                {
                    "id": agent.id,
                    "name": agent.name,
                    "role": agent.role,
                    "status": "queued" if agent.id in chosen else "available",
                }
                for agent in self.agents
            ],
            "tasks": [
                self._task(task_id, request.question, "queued"),
                *self._selected_tasks(task_id, request.question, chosen),
            ],
            "attempts": [],
            "messages": [],
            "sources": [
                {
                    "id": source_id,
                    "copyId": _id("copy"),
                    "version": 1,
                    "title": "User-supplied evidence",
                    "accessState": "supplied_text",
                    "retrievalStatus": "supplied by user; fresh retrieval unavailable",
                    "contentHash": hashlib.sha256(evidence.encode()).hexdigest(),
                    "provider": "user_supplied",
                    "passages": [
                        {
                            "spanId": span_id,
                            "locator": "pasted evidence, complete input",
                            "text": evidence,
                        }
                    ],
                }
            ],
            "claims": [],
            "artifacts": [],
            "events": [self._event(task_id, None, "Live run accepted with supplied evidence.")],
            "diagnostics": [],
        }
        return ResearchSnapshot.model_validate(snapshot).model_dump(by_alias=True)

    @staticmethod
    def _task(task_id: str, title: str, status: str, target: str | None = None) -> dict:
        return {
            "id": task_id,
            "targetArtifactId": target,
            "title": title,
            "status": status,
            "latencyClass": "long",
            "latencyBasis": "Model work over saved evidence and peer responses.",
            "activeOrWaiting": status if status in ("queued", "waiting", "completed") else "active",
            "attemptIds": [],
        }

    def _selected_tasks(self, root_task_id: str, question: str, agent_ids: list[str]) -> list[dict]:
        names = {agent.id: agent.name for agent in self.agents}
        tasks = []
        for agent_id in agent_ids:
            task = self._task(
                f"{root_task_id}-{agent_id}", f"{names[agent_id]}: {question}", "queued"
            )
            task["parentTaskId"] = root_task_id
            tasks.append(task)
        return tasks

    @staticmethod
    def _event(task_id: str | None, agent_id: str | None, summary: str) -> dict:
        return {
            "id": _id("event"),
            "time": _now(),
            "kind": "live_research",
            "taskId": task_id,
            "agentId": agent_id,
            "summary": summary,
        }

    def state(self, run_id: str | None = None) -> LiveState:
        with self._lock, self._connect() as db:
            runs = db.execute(
                "SELECT r.id,r.question,r.status,r.parent_run_id,r.agent_ids,r.created_at,r.error,"
                "s.created_at AS last_progress_at,json_extract(s.body,'$.agents') AS saved_agents "
                "FROM runs r LEFT JOIN snapshots s ON s.id=r.snapshot_id "
                "ORDER BY r.created_at DESC, r.rowid DESC"
            ).fetchall()
            if run_id is None and runs:
                run_id = runs[0]["id"]
            snapshot = None
            comparison = None
            error = None
            if run_id is not None:
                row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                if row is None:
                    raise LiveError("Unknown run", 404)
                snapshot = ResearchSnapshot.model_validate(self._snapshot_locked(db, run_id))
                if row["comparison_snapshot_id"]:
                    saved = db.execute(
                        "SELECT body FROM snapshots WHERE id=?",
                        (row["comparison_snapshot_id"],),
                    ).fetchone()
                    comparison = ResearchSnapshot.model_validate_json(saved["body"])
                error = row["error"]
        return LiveState(
            enabled=True,
            model=self.provider.model,
            agents=self.agents,
            runs=[
                LiveRunSummary(
                    id=row["id"],
                    question=row["question"],
                    status=row["status"],
                    parent_run_id=row["parent_run_id"],
                    agent_ids=json.loads(row["agent_ids"]),
                    requested_agent_names={
                        item["id"]: item["name"] for item in json.loads(row["saved_agents"] or "[]")
                        if item["id"] in json.loads(row["agent_ids"])
                    },
                    title=row["question"] if len(row["question"]) <= 88 else row["question"][:85].rstrip() + "…",
                    created_at=row["created_at"],
                    last_progress_at=row["last_progress_at"],
                    completed_at=row["last_progress_at"] if row["status"] == "completed" else None,
                    waiting_reason=row["error"] if row["status"] == "waiting" else None,
                    workspace_label=self.state_dir.name,
                )
                for row in runs
            ],
            snapshot=snapshot,
            comparison_snapshot=comparison,
            run_id=run_id,
            error=error,
            workspace_id="workspace-" + hashlib.sha256(str(self.state_dir).encode()).hexdigest()[:24],
            workspace_label=self.state_dir.name,
            workflow_version="supplied-evidence-legacy-v1",
        )

    def calls(self, run_id: str, attempt_id: str | None = None) -> list[dict]:
        with self._lock, self._connect() as db:
            if db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
                raise LiveError("Unknown run", 404)
            snapshot = self._snapshot_locked(db, run_id)
            source_run_id = run_id
            if attempt_id:
                if not any(item["id"] == attempt_id for item in snapshot["attempts"]):
                    return []
                seen: set[str] = set()
                found = False
                while source_run_id and source_run_id not in seen:
                    seen.add(source_run_id)
                    if db.execute(
                        "SELECT 1 FROM calls WHERE run_id=? AND attempt_id=? LIMIT 1",
                        (source_run_id, attempt_id),
                    ).fetchone():
                        found = True
                        break
                    parent = db.execute(
                        "SELECT parent_run_id FROM runs WHERE id=?", (source_run_id,)
                    ).fetchone()
                    source_run_id = parent["parent_run_id"] if parent else None
                if not found:
                    return []
            query = "SELECT * FROM calls WHERE run_id=?"
            args: list[str] = [source_run_id]
            if attempt_id:
                query += " AND attempt_id=?"
                args.append(attempt_id)
            query += " ORDER BY created_at,rowid"
            rows = db.execute(query, args).fetchall()
        return [
            {
                "id": row["id"],
                "runId": row["run_id"],
                "attemptId": row["attempt_id"],
                "agentId": row["agent_id"],
                "status": row["status"],
                "phase": row["phase"],
                "messages": json.loads(row["messages"]),
                "response": json.loads(row["response"]) if row["response"] else None,
                "toolResults": json.loads(row["tool_results"]),
                "estimatedMaxUsd": row["estimated_max_usd"],
                "actualUsd": row["actual_usd"],
                "retryOfCallId": row["retry_of_call_id"],
                "createdAt": row["created_at"],
                "completedAt": row["completed_at"],
                "trace": self._call_trace(row, snapshot),
            }
            for row in rows
        ]

    @staticmethod
    def _at_or_before(ended_at: str | None, call_at: str) -> bool:
        if not ended_at:
            return False
        try:
            ended = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
            called = datetime.fromisoformat(call_at.replace("Z", "+00:00"))
            return ended <= called
        except (TypeError, ValueError):
            return False

    def _call_trace(self, row: sqlite3.Row, snapshot: dict) -> dict:
        """Derive provenance only from the selected saved snapshot and raw call."""
        agents = {item["id"]: item for item in snapshot["agents"]}
        attempts = {item["id"]: item for item in snapshot["attempts"]}
        agent_id = row["agent_id"]
        agent = agents.get(agent_id)
        attempt = attempts.get(row["attempt_id"])
        author = {
            "agentId": agent_id,
            "name": agent["name"] if agent else None,
            "role": agent["role"] if agent else None,
            "taskId": (attempt["taskId"] if attempt and attempt["agentId"] == agent_id else None),
            "attemptId": row["attempt_id"],
            "runId": row["run_id"],
        }
        response = json.loads(row["response"]) if row["response"] else None
        outputs = []
        if response:
            if response.get("content"):
                outputs.append(
                    {
                        "authoredByAgentId": agent_id,
                        "recordPath": "response.content",
                    }
                )
            if response.get("tool_calls"):
                outputs.append(
                    {
                        "authoredByAgentId": agent_id,
                        "recordPath": "response.tool_calls",
                    }
                )
        peer_inputs = []
        for index, message in enumerate(json.loads(row["messages"])):
            if message.get("role") != "user":
                continue
            try:
                envelope = json.loads(message.get("content", ""))
            except (TypeError, ValueError):
                continue
            if not isinstance(envelope, dict):
                continue
            request_id = envelope.get("peerRequestId")
            peer_response = envelope.get("peerResponse")
            if (
                not isinstance(request_id, str)
                or not request_id
                or not isinstance(peer_response, str)
            ):
                continue
            linked = {
                "inputRecordPath": f"messages[{index}].content",
                "peerRequestId": request_id,
                "peerResponse": peer_response,
                "linkStatus": "unknown",
                "requestMessageId": None,
                "responseMessageId": None,
                "peerAgentId": None,
                "peerAgentName": None,
                "sourceAttemptId": None,
                "sourceAttemptResolution": "unknown",
            }
            requests = [
                item
                for item in snapshot["messages"]
                if item.get("requestId") == request_id
                and item["fromAgentId"] == agent_id
                and item["status"] == "fulfilled"
            ]
            pairs = []
            for request in requests:
                responses = [
                    item
                    for item in snapshot["messages"]
                    if item.get("requestId") == request_id
                    and item["taskId"] == request["taskId"]
                    and item["fromAgentId"] == request["toAgentId"]
                    and item["toAgentId"] == agent_id
                    and item["status"] == "delivered"
                    and item["summary"] == peer_response
                    and self._at_or_before(item.get("createdAt"), row["created_at"])
                ]
                pairs.extend((request, reply) for reply in responses)
            if len(pairs) == 1:
                request, reply = pairs[0]
                peer_id = request["toAgentId"]
                linked.update(
                    {
                        "linkStatus": "linked",
                        "requestMessageId": request["id"],
                        "responseMessageId": reply["id"],
                        "peerAgentId": peer_id,
                        "peerAgentName": agents.get(peer_id, {}).get("name"),
                    }
                )
                candidates = [
                    item
                    for item in snapshot["attempts"]
                    if item["taskId"] == request["taskId"]
                    and item["agentId"] == peer_id
                    and item["status"] == "completed"
                    and item.get("resultSummary") == peer_response
                    and self._at_or_before(item.get("endedAt"), row["created_at"])
                ]
                if len(candidates) == 1:
                    linked["sourceAttemptId"] = candidates[0]["id"]
                    linked["sourceAttemptResolution"] = "unique"
                elif len(candidates) > 1:
                    linked["sourceAttemptResolution"] = "ambiguous"
            elif len(pairs) > 1:
                linked["linkStatus"] = "ambiguous"
                linked["sourceAttemptResolution"] = "ambiguous"
            peer_inputs.append(linked)
        return {
            "basis": "linked_saved_records",
            "sourceSnapshotId": snapshot["snapshotId"],
            "author": author,
            "outputs": outputs,
            "peerInputs": peer_inputs,
        }

    def _reserve_call(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
        agent_id: str,
        messages: list[dict],
        retry_of_call_id: str | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        estimate = float(self.provider.estimate_max_cost(messages, TOOLS))
        if not math.isfinite(estimate) or estimate <= 0:
            raise LiveError("Provider returned an invalid cost estimate", 503)
        with self._lock:
            db = self._connect()
            try:
                row = db.execute(
                    "SELECT spent_usd,reserved_usd FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                if row["spent_usd"] + row["reserved_usd"] + estimate > self.run_budget_usd:
                    return None
                mission_id = self._snapshot_locked(db, run_id)["mission"]["id"]
                root_run_id = run_id
                while True:
                    parent = db.execute(
                        "SELECT parent_run_id FROM runs WHERE id=?", (root_run_id,)
                    ).fetchone()
                    if not parent or not parent["parent_run_id"]:
                        break
                    root_run_id = parent["parent_run_id"]
            finally:
                db.close()
            call_id = _id("call")
            agent = next(item for item in self.agents if item.id == agent_id)
            instance_id, config_id, config_version, effective_config = self._provenance.prepare_call(
                call_id=call_id,
                mission_id=mission_id,
                origin_run_id=root_run_id,
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                agent_id=agent_id,
                display_name=agent.name,
                role_id=self._role_ids[agent_id],
                messages=messages,
                provider=self.provider,
                tools=TOOLS,
                loaded_skills=self._loaded_skills,
            )
            with self._connect() as db:
                db.execute(
                    "INSERT INTO calls(id,run_id,attempt_id,agent_id,status,phase,messages,"
                    "estimated_max_usd,retry_of_call_id,created_at,agent_instance_id,"
                    "config_id,config_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        call_id,
                        run_id,
                        attempt_id,
                        agent_id,
                        "in_flight",
                        "provider",
                        _json(messages),
                        estimate,
                        retry_of_call_id,
                        _now(),
                        instance_id,
                        config_id,
                        config_version,
                    ),
                )
                db.execute(
                    "UPDATE runs SET reserved_usd=reserved_usd+? WHERE id=?",
                    (estimate, run_id),
                )
            return call_id, effective_config

    def _finish_call(self, run_id: str, task_id: str, call_id: str, response: dict) -> None:
        actual = float(response.get("estimated_usd", 0))
        if not math.isfinite(actual) or actual < 0:
            raise LiveError("Provider returned an invalid actual cost", 503)
        public_response = {
            key: response.get(key)
            for key in (
                "content",
                "tool_calls",
                "usage",
                "cache_observation",
                "model",
                "elapsed_seconds",
                "finish_reason",
                "estimated_usd",
            )
        }
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT estimated_max_usd,attempt_id,agent_instance_id FROM calls "
                "WHERE id=? AND run_id=?",
                (call_id, run_id),
            ).fetchone()
            db.execute(
                "UPDATE calls SET status='returned',phase='response',response=?,"
                "actual_usd=?,completed_at=? WHERE id=?",
                (_json(public_response), actual, _now(), call_id),
            )
            db.execute(
                "UPDATE runs SET reserved_usd=reserved_usd-?,spent_usd=spent_usd+? WHERE id=?",
                (row["estimated_max_usd"], actual, run_id),
            )

            # The provider response and its requested invocations commit together.
            # A restart can distinguish returned tool requests from actual starts.
            for index, requested in enumerate(response.get("tool_calls") or []):
                provider_id = requested["id"]
                invocation_id = "invocation-" + hashlib.sha256(
                    f"{call_id}\0{provider_id}".encode()
                ).hexdigest()
                requested_at = _now()
                db.execute(
                    "INSERT INTO tool_invocations(id,run_id,task_id,attempt_id,"
                    "agent_instance_id,model_call_id,provider_tool_call_id,tool_name,"
                    "status,input_ref,execution_provider,execution_location,"
                    "requested_at,provenance_basis) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        invocation_id,
                        run_id,
                        task_id,
                        row["attempt_id"],
                        row["agent_instance_id"],
                        call_id,
                        provider_id,
                        requested["function"]["name"],
                        "requested",
                        f"calls.response.tool_calls[{index}].function.arguments",
                        "local_host",
                        "ora.live.engine.ResearchHost._tool",
                        requested_at,
                        "recorded",
                    ),
                )
                db.execute(
                    "INSERT INTO tool_invocation_events(invocation_id,status,occurred_at,"
                    "provenance_basis) VALUES(?,'requested',?,'recorded')",
                    (invocation_id, requested_at),
                )

    def _start_tool(self, call_id: str, provider_tool_call_id: str) -> None:
        with self._lock, self._connect() as db:
            started_at = _now()
            cursor = db.execute(
                "UPDATE tool_invocations SET status='started',started_at=? "
                "WHERE model_call_id=? AND provider_tool_call_id=? AND status='requested'",
                (started_at, call_id, provider_tool_call_id),
            )
            if cursor.rowcount != 1:
                raise LiveError("Tool request was already handled", 409)
            db.execute(
                "INSERT INTO tool_invocation_events(invocation_id,status,occurred_at,"
                "provenance_basis) SELECT id,'started',?,'recorded' "
                "FROM tool_invocations WHERE model_call_id=? AND provider_tool_call_id=?",
                (started_at, call_id, provider_tool_call_id),
            )

    def _append_tool_result(self, call_id: str, message: dict, status: str) -> None:
        if status not in {"completed", "rejected", "failed", "uncertain"}:
            raise ValueError("Unknown terminal tool status")
        with self._lock, self._connect() as db:
            row = db.execute("SELECT tool_results FROM calls WHERE id=?", (call_id,)).fetchone()
            results = json.loads(row["tool_results"])
            index = len(results)
            results.append(message)
            db.execute(
                "UPDATE calls SET tool_results=? WHERE id=?",
                (_json(results), call_id),
            )
            error_ref = (
                f"calls.tool_results[{index}].content" if status != "completed" else None
            )
            completed_at = _now()
            cursor = db.execute(
                "UPDATE tool_invocations SET status=?,completed_at=?,output_ref=?,error_ref=? "
                "WHERE model_call_id=? AND provider_tool_call_id=? "
                "AND (status='started' OR (status='requested' AND ?='rejected'))",
                (
                    status,
                    completed_at,
                    f"calls.tool_results[{index}].content" if status == "completed" else None,
                    error_ref,
                    call_id,
                    message["tool_call_id"],
                    status,
                ),
            )
            if cursor.rowcount != 1:
                raise LiveError("Tool result was already recorded", 409)
            db.execute(
                "INSERT INTO tool_invocation_events(invocation_id,status,occurred_at,"
                "provenance_basis) SELECT id,?,?, 'recorded' "
                "FROM tool_invocations WHERE model_call_id=? AND provider_tool_call_id=?",
                (status, completed_at, call_id, message["tool_call_id"]),
            )

    def _uncertain_call(self, run_id: str, call_id: str, message: str) -> None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT estimated_max_usd FROM calls WHERE id=?", (call_id,)
            ).fetchone()
            db.execute(
                "UPDATE calls SET status='uncertain',phase='provider_error',"
                "response=?,completed_at=? WHERE id=?",
                (_json({"error": message}), _now(), call_id),
            )
            db.execute(
                "UPDATE runs SET reserved_usd=reserved_usd-?,status='waiting',error=? WHERE id=?",
                (row["estimated_max_usd"], message, run_id),
            )

    def _reject_unsent_call(
        self, run_id: str, call_id: str, message: str, phase: str
    ) -> None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT estimated_max_usd FROM calls WHERE id=? AND run_id=?",
                (call_id, run_id),
            ).fetchone()
            db.execute(
                "UPDATE calls SET status='rejected',phase=?,"
                "response=?,completed_at=? WHERE id=?",
                (phase, _json({"error": message}), _now(), call_id),
            )
            db.execute(
                "UPDATE runs SET reserved_usd=reserved_usd-? WHERE id=?",
                (row["estimated_max_usd"], run_id),
            )

    def _rate_limited_call(
        self, run_id: str, call_id: str, message: str, retry_after: float
    ) -> None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT estimated_max_usd FROM calls WHERE id=? AND run_id=?",
                (call_id, run_id),
            ).fetchone()
            db.execute(
                "UPDATE calls SET status='rate_limited',phase='quota_wait',"
                "response=?,completed_at=? WHERE id=?",
                (_json({"error": message, "retry_after": retry_after}), _now(), call_id),
            )
            db.execute(
                "UPDATE runs SET reserved_usd=reserved_usd-? WHERE id=?",
                (row["estimated_max_usd"], run_id),
            )

    def _set_run_status(
        self,
        run_id: str,
        status: str,
        message: str | None = None,
        diagnostic_kind: str = "execution",
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE runs SET status=?,error=? WHERE id=?",
                (status, message, run_id),
            )

            def change(snapshot: dict) -> None:
                snapshot["mission"]["status"] = (
                    "completed"
                    if status == "completed"
                    else "waiting"
                    if status == "waiting"
                    else "active"
                )
                root_task_id = db.execute(
                    "SELECT task_id FROM runs WHERE id=?", (run_id,)
                ).fetchone()["task_id"]
                root_task = next(item for item in snapshot["tasks"] if item["id"] == root_task_id)
                root_task["status"] = (
                    "completed"
                    if status == "completed"
                    else "waiting"
                    if status == "waiting"
                    else "running"
                )
                root_task["activeOrWaiting"] = (
                    "waiting"
                    if status == "waiting"
                    else "completed"
                    if status == "completed"
                    else "active"
                )
                if message:
                    snapshot["diagnostics"].append(
                        {
                            "id": _id("diagnostic"),
                            "kind": diagnostic_kind,
                            "status": status,
                            "message": message,
                            "occurredAt": _now(),
                        }
                    )
                snapshot["events"].append(self._event(None, None, f"Run {status}."))

            self._mutate_locked(db, run_id, change)

    def _execute(self, run_id: str) -> None:
        try:
            self._set_run_status(run_id, "running")
            with self._lock, self._connect() as db:
                run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                agent_ids = json.loads(run["agent_ids"])
                root_task_id = run["task_id"]
                target_id = None
                if run["parent_snapshot_id"]:
                    target_id = next(
                        (
                            task["targetArtifactId"]
                            for task in self._snapshot_locked(db, run_id)["tasks"]
                            if task["id"] == root_task_id
                        ),
                        None,
                    )
            queue: deque[tuple[str, str, str, str | None, str | None, list[dict] | None]] = deque()
            for agent_id in agent_ids:
                queue.append(
                    (agent_id, f"{root_task_id}-{agent_id}", run["question"], None, None, None)
                )
            while queue:
                agent_id, task_id, question, requester_id, request_id, resume = queue.popleft()
                outcome = self._agent_loop(
                    run_id,
                    agent_id,
                    task_id,
                    question,
                    queue,
                    target_id,
                    resume,
                )
                if outcome is None:
                    return
                if requester_id and request_id and outcome:
                    self._deliver_peer(run_id, request_id, outcome)
                    requester_task_id, original_question, messages = self._resume_after_peer(
                        run_id, requester_id, request_id, outcome
                    )
                    queue.append(
                        (
                            requester_id,
                            requester_task_id,
                            original_question,
                            None,
                            None,
                            messages,
                        )
                    )
            self._set_run_status(run_id, "completed")
        except Exception as exc:
            # A local failure after dispatch or a tool start has an uncertain
            # boundary. Seal it before the run enters its waiting state.
            with self._lock, self._connect() as db:
                in_flight = db.execute(
                    "SELECT 1 FROM calls WHERE run_id=? AND status='in_flight' LIMIT 1",
                    (run_id,),
                ).fetchone() is not None
                started = db.execute(
                    "SELECT 1 FROM tool_invocations WHERE run_id=? AND status='started' LIMIT 1",
                    (run_id,),
                ).fetchone() is not None
                db.execute(
                    "UPDATE calls SET status='uncertain',phase='local_error',completed_at=? "
                    "WHERE run_id=? AND status='in_flight'",
                    (_now(), run_id),
                )
                interrupted_at = _now()
                started_tools = db.execute(
                    "SELECT id FROM tool_invocations WHERE run_id=? AND status='started'",
                    (run_id,),
                ).fetchall()
                db.execute(
                    "UPDATE tool_invocations SET status='uncertain',completed_at=? "
                    "WHERE run_id=? AND status='started'",
                    (interrupted_at, run_id),
                )
                db.executemany(
                    "INSERT INTO tool_invocation_events(invocation_id,status,occurred_at,"
                    "provenance_basis) VALUES(?,'uncertain',?,'recorded')",
                    [(tool["id"], interrupted_at) for tool in started_tools],
                )
                db.execute("UPDATE runs SET reserved_usd=0 WHERE id=?", (run_id,))
                self._mutate_locked(
                    db,
                    run_id,
                    lambda snapshot: self._set_local_failure(
                        snapshot, uncertain=in_flight or started
                    ),
                )
            message = (
                "Live configuration changed before dispatch; no model call was sent."
                if isinstance(exc, ConfigurationError) and not in_flight and not started
                else "Local execution stopped; inspect saved calls."
            )
            self._set_run_status(run_id, "waiting", message)

    @staticmethod
    def _set_local_failure(snapshot: dict, *, uncertain: bool) -> None:
        for attempt in snapshot["attempts"]:
            if attempt["status"] == "running":
                attempt["status"] = "uncertain" if uncertain else "waiting"
                attempt["endedAt"] = _now()
        for task in snapshot["tasks"]:
            if task["status"] == "running":
                task["status"] = "waiting"
                task["activeOrWaiting"] = "waiting"

    def _messages(
        self,
        run_id: str,
        agent_id: str,
        task_id: str,
        question: str,
        target_id: str | None,
    ) -> list[dict]:
        with self._lock, self._connect() as db:
            snapshot = self._snapshot_locked(db, run_id)
            requested_by = next(
                (
                    {
                        "requestId": item["requestId"],
                        "fromAgentId": item["fromAgentId"],
                        "question": item["summary"],
                    }
                    for item in snapshot["messages"]
                    if item["taskId"] == task_id
                    and item["toAgentId"] == agent_id
                    and item["status"] == "requested"
                ),
                None,
            )
            run = db.execute(
                "SELECT parent_run_id,parent_snapshot_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            target = None
            if target_id and run["parent_snapshot_id"]:
                saved = db.execute(
                    "SELECT body FROM snapshots WHERE id=? AND run_id=?",
                    (run["parent_snapshot_id"], run["parent_run_id"]),
                ).fetchone()
                baseline = json.loads(saved["body"])
                for group in ("claims", "artifacts", "sources", "tasks"):
                    record = next(
                        (item for item in baseline[group] if item["id"] == target_id), None
                    )
                    if record is not None:
                        target = {
                            "kind": group[:-1],
                            "id": target_id,
                            "version": record.get("version") or baseline["mission"]["version"],
                            "record": record,
                            "parentRunId": run["parent_run_id"],
                            "parentSnapshotId": run["parent_snapshot_id"],
                        }
                        break
        agent = next(item for item in self.agents if item.id == agent_id)
        role_id = self._role_ids[agent_id]
        skills = [
            self._loaded_skills[ref["skill_id"]]["instructions"]
            for ref in self._skills.roles[role_id]["skill_refs"]
            if ref["skill_id"] != "task-planning"
        ]
        source_list = [
            {
                "id": source["id"],
                "version": source["version"],
                "title": source["title"],
                "passages": [
                    {"spanId": passage["spanId"], "locator": passage["locator"]}
                    for passage in source["passages"]
                ],
            }
            for source in snapshot["sources"]
        ]
        instructions = (
            f"You are {agent.name}, a {agent.role}. Work independently as a scientific peer. "
            "Only saved, user-supplied evidence is available; fresh search is unavailable. "
            "Use source_read for evidence. Ask a peer only when useful. Call publish_result "
            "when your current answer or critique is ready; it finishes this contribution "
            "without cancelling queued peer work. You may instead provide ordinary final text. "
            "Cite only explicit "
            "source/span IDs you actually examined and judged relevant. A citation ID alone "
            "does not prove support. If peerRequest is present, that request is your primary "
            "assignment. The mission's instruction to obtain a focused peer check has already "
            "been fulfilled by this request; retain its evidence and format constraints. "
            "Perform the assigned check yourself when saved evidence suffices. Ask another "
            "peer only for a distinct unresolved question, not to repeat this same check. "
            "Do not reveal private reasoning. " + "\n\n".join(skills)
        )
        user = {
            "question": question,
            "scope": snapshot["mission"]["scope"],
            "target": target,
            "peerRequest": requested_by,
            "assignmentContext": (
                "This is the requested peer check. Apply the mission scope's evidence and "
                "output constraints; its direction to ask for a peer check was fulfilled "
                "when the requester assigned this task to you."
                if requested_by
                else None
            ),
            "sources": source_list,
            "availablePeers": [
                {"id": peer.id, "name": peer.name, "role": peer.role}
                for peer in self.agents
                if peer.id != agent_id
            ],
        }
        return [
            {"role": "system", "content": instructions},
            {"role": "user", "content": _json(user)},
        ]

    def _resume_after_peer(
        self, run_id: str, requester_id: str, request_id: str, answer: str
    ) -> tuple[str, str, list[dict]]:
        with self._lock, self._connect() as db:
            snapshot = self._snapshot_locked(db, run_id)
            request = next(
                item
                for item in snapshot["messages"]
                if item["requestId"] == request_id and item["fromAgentId"] == requester_id
            )
            peer_task = next(item for item in snapshot["tasks"] if item["id"] == request["taskId"])
            requester_task_id = peer_task["parentTaskId"]
            requester_task = next(
                item for item in snapshot["tasks"] if item["id"] == requester_task_id
            )
            attempts = [
                item
                for item in snapshot["attempts"]
                if item["taskId"] == requester_task_id and item["agentId"] == requester_id
            ]
            previous = attempts[-1]
            call = db.execute(
                "SELECT messages,response,tool_results FROM calls WHERE run_id=? AND attempt_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (run_id, previous["id"]),
            ).fetchone()
            if call is None or call["response"] is None:
                raise LiveError("Peer requester has no saved model response", 409)
            prior_claims = [
                item for item in snapshot["claims"] if item["taskId"] == requester_task_id
            ]
        messages = json.loads(call["messages"])
        response = json.loads(call["response"])
        if response.get("tool_calls"):
            messages.append(
                {
                    "role": "assistant",
                    "content": response.get("content"),
                    "tool_calls": response["tool_calls"],
                }
            )
            messages.extend(json.loads(call["tool_results"]))
        elif response.get("content"):
            messages.append({"role": "assistant", "content": response["content"]})
        messages.append(
            {
                "role": "user",
                "content": _json(
                    {
                        "originalQuestion": requester_task["title"],
                        "priorAnswer": prior_claims[-1]["text"] if prior_claims else None,
                        "peerQuestion": request["summary"],
                        "peerResponse": answer,
                        "peerRequestId": request_id,
                        "instruction": "Review this peer response against your prior answer. "
                        "Revise or qualify your result if warranted.",
                    }
                ),
            }
        )
        return requester_task_id, requester_task["title"], messages

    def _agent_loop(
        self,
        run_id: str,
        agent_id: str,
        task_id: str,
        question: str,
        queue: deque,
        target_id: str | None,
        resume_messages: list[dict] | None,
    ) -> str | None:
        attempt_id = _id("attempt")
        skill_versions = next(
            agent.skill_version.split(", ") for agent in self.agents if agent.id == agent_id
        )
        self._mutate(
            run_id,
            lambda snapshot: self._begin_attempt(
                snapshot, task_id, agent_id, attempt_id, skill_versions
            ),
        )
        messages = resume_messages or self._messages(run_id, agent_id, task_id, question, target_id)
        published: str | None = None
        read_sources: set[str] = set()
        if resume_messages:
            for message in resume_messages:
                if message.get("role") != "tool":
                    continue
                try:
                    result = json.loads(message["content"])
                except (KeyError, TypeError, ValueError):
                    continue
                if (
                    isinstance(result, dict)
                    and "passages" in result
                    and isinstance(result.get("id"), str)
                ):
                    read_sources.add(result["id"])
        retry_of_call_id: str | None = None
        while True:
            if self._shutdown_event.is_set():
                self._mutate(
                    run_id,
                    lambda snapshot: self._quota_attempt_state(
                        snapshot, attempt_id, task_id, "waiting"
                    ),
                )
                self._set_run_status(
                    run_id, "waiting", "Local host stopped before the next model call."
                )
                return None
            reservation = self._reserve_call(
                run_id, task_id, attempt_id, agent_id, messages, retry_of_call_id
            )
            retry_of_call_id = None
            if reservation is None:
                self._mutate(
                    run_id,
                    lambda snapshot: self._end_attempt(
                        snapshot, attempt_id, task_id, "waiting", published
                    ),
                )
                with self._lock, self._connect() as db:
                    spent = db.execute(
                        "SELECT spent_usd FROM runs WHERE id=?", (run_id,)
                    ).fetchone()["spent_usd"]
                self._set_run_status(
                    run_id,
                    "waiting",
                    f"Run paused at USD {spent:.4f} spent; the next call exceeds "
                    f"the USD {self.run_budget_usd:.4f} run limit.",
                )
                return None
            call_id, expected_configuration = reservation
            try:
                if isinstance(self.provider, GroqProvider):
                    response = self.provider.complete(
                        messages, TOOLS, expected_configuration=expected_configuration
                    )
                else:
                    response = self.provider.complete(messages, TOOLS)
            except ProviderError as exc:
                if isinstance(exc, (ConfigurationDrift, CredentialUnavailable)):
                    self._reject_unsent_call(
                        run_id,
                        call_id,
                        str(exc),
                        "configuration_drift"
                        if isinstance(exc, ConfigurationDrift)
                        else "credential_preflight",
                    )
                    self._mutate(
                        run_id,
                        lambda snapshot: self._end_attempt(
                            snapshot, attempt_id, task_id, "waiting", published
                        ),
                    )
                    self._set_run_status(run_id, "waiting", str(exc))
                    return None
                if exc.status == 429:
                    delay = exc.retry_after or 60.0
                    if not math.isfinite(delay) or delay <= 0:
                        delay = 60.0
                    self._rate_limited_call(run_id, call_id, str(exc), delay)
                    wake_at = (
                        (datetime.now(timezone.utc) + timedelta(seconds=delay))
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
                    self._mutate(
                        run_id,
                        lambda snapshot: self._quota_attempt_state(
                            snapshot, attempt_id, task_id, "waiting"
                        ),
                    )
                    self._set_run_status(
                        run_id,
                        "waiting",
                        f"Provider rate limit; retrying this call after {delay:g} seconds "
                        f"at {wake_at}.",
                        diagnostic_kind="quota",
                    )
                    if self._shutdown_event.wait(delay):
                        self._set_run_status(
                            run_id,
                            "waiting",
                            "Local host stopped during the quota wait; no retry was sent.",
                            diagnostic_kind="quota",
                        )
                        return None
                    self._mutate(
                        run_id,
                        lambda snapshot: self._quota_attempt_state(
                            snapshot, attempt_id, task_id, "running"
                        ),
                    )
                    self._set_run_status(run_id, "running")
                    retry_of_call_id = call_id
                    continue
                safe = str(exc)
                self._uncertain_call(run_id, call_id, safe)
                self._mutate(
                    run_id,
                    lambda snapshot: self._end_attempt(
                        snapshot, attempt_id, task_id, "uncertain", published
                    ),
                )
                self._set_run_status(run_id, "waiting", safe)
                return None
            except Exception:
                # Unknown exception text is not trusted for display or persistence.
                safe = "Provider call failed; outcome uncertain."
                self._uncertain_call(run_id, call_id, safe)
                self._mutate(
                    run_id,
                    lambda snapshot: self._end_attempt(
                        snapshot, attempt_id, task_id, "uncertain", published
                    ),
                )
                self._set_run_status(run_id, "waiting", safe)
                return None
            self._finish_call(run_id, task_id, call_id, response)
            self._mutate(
                run_id,
                lambda snapshot: self._set_attempt_model(snapshot, attempt_id, response["model"]),
            )
            if response.get("finish_reason") == "length":
                self._mutate(
                    run_id,
                    lambda snapshot: self._end_attempt(
                        snapshot, attempt_id, task_id, "waiting", published
                    ),
                )
                self._set_run_status(
                    run_id,
                    "waiting",
                    "Provider completion was truncated; measured usage was saved.",
                )
                return None
            tool_calls = response.get("tool_calls") or []
            content = response.get("content") or ""
            messages.append(
                {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": tool_calls,
                }
            )
            if not tool_calls:
                if content.strip() and published is None:
                    self._publish(
                        run_id, task_id, attempt_id, agent_id, content, None, None, read_sources
                    )
                    published = content
                if not published:
                    self._mutate(
                        run_id,
                        lambda snapshot: self._end_attempt(
                            snapshot, attempt_id, task_id, "waiting", None
                        ),
                    )
                    self._set_run_status(
                        run_id,
                        "waiting",
                        "Provider returned no final answer or tool call.",
                    )
                    return None
                self._mutate(
                    run_id,
                    lambda snapshot: self._end_attempt(
                        snapshot, attempt_id, task_id, "completed", published
                    ),
                )
                return published or ""
            for tool_call in tool_calls:
                tool_name = (tool_call.get("function") or {}).get("name", "")
                provider_tool_call_id = tool_call["id"]
                try:
                    args = json.loads((tool_call.get("function") or {}).get("arguments") or "{}")
                    if not isinstance(args, dict):
                        raise ValueError
                    self._provenance.validate_tool(tool_name, args)
                except (TypeError, ValueError) as exc:
                    result = {"error": str(exc) or "Invalid tool arguments"}
                    status = "rejected"
                else:
                    self._start_tool(call_id, provider_tool_call_id)
                    try:
                        result, result_text = self._tool(
                            run_id, task_id, attempt_id, agent_id, tool_name, args, queue, read_sources
                        )
                        if result_text:
                            published = result_text
                        status = "completed"
                    except LiveError as exc:
                        result = {"error": str(exc)}
                        status = "rejected"
                    except Exception:
                        # A side effect may have committed before an unexpected
                        # failure. Preserve uncertainty instead of replaying it.
                        status = "failed" if tool_name == "source_read" else "uncertain"
                        result = {"error": "Local tool failed; review the saved invocation."}
                tool_message = {
                    "role": "tool",
                    "tool_call_id": provider_tool_call_id,
                    "content": _json(result),
                }
                self._append_tool_result(call_id, tool_message, status)
                self._mutate(
                    run_id,
                    lambda snapshot, name=tool_name, state=status: self._tool_record(
                        snapshot, attempt_id, name, state
                    ),
                )
                messages.append(tool_message)
                if status == "uncertain":
                    self._mutate(
                        run_id,
                        lambda snapshot: self._end_attempt(
                            snapshot, attempt_id, task_id, "uncertain", published
                        ),
                    )
                    self._set_run_status(
                        run_id, "waiting", "Local tool outcome uncertain; review before continuing."
                    )
                    return None
            if published:
                self._mutate(
                    run_id,
                    lambda snapshot: self._end_attempt(
                        snapshot, attempt_id, task_id, "completed", published
                    ),
                )
                return published

    @staticmethod
    def _begin_attempt(
        snapshot: dict,
        task_id: str,
        agent_id: str,
        attempt_id: str,
        skill_versions: list[str],
    ) -> None:
        task = next(item for item in snapshot["tasks"] if item["id"] == task_id)
        task["status"] = "running"
        task["activeOrWaiting"] = "active"
        task["attemptIds"].append(attempt_id)
        snapshot["attempts"].append(
            {
                "id": attempt_id,
                "taskId": task_id,
                "agentId": agent_id,
                "status": "running",
                "startedAt": _now(),
                "toolCalls": [],
                "selectedModel": None,
                "skillVersions": skill_versions,
                "returnedReasoning": {"availability": "absent"},
            }
        )
        next(item for item in snapshot["agents"] if item["id"] == agent_id)["status"] = "active"
        snapshot["events"].append(ResearchHost._event(task_id, agent_id, "Agent work started."))

    @staticmethod
    def _tool_record(snapshot: dict, attempt_id: str, name: str, status: str) -> None:
        attempt = next(item for item in snapshot["attempts"] if item["id"] == attempt_id)
        attempt["toolCalls"].append({"name": name or "unknown", "status": status})

    @staticmethod
    def _set_attempt_model(snapshot: dict, attempt_id: str, model: str) -> None:
        attempt = next(item for item in snapshot["attempts"] if item["id"] == attempt_id)
        attempt["selectedModel"] = model

    @staticmethod
    def _quota_attempt_state(snapshot: dict, attempt_id: str, task_id: str, status: str) -> None:
        attempt = next(item for item in snapshot["attempts"] if item["id"] == attempt_id)
        attempt["status"] = status
        task = next(item for item in snapshot["tasks"] if item["id"] == task_id)
        task["status"] = status
        task["activeOrWaiting"] = "waiting" if status == "waiting" else "active"
        agent = next(item for item in snapshot["agents"] if item["id"] == attempt["agentId"])
        agent["status"] = status
        snapshot["events"].append(
            ResearchHost._event(
                task_id, attempt["agentId"], f"Agent {status} after quota response."
            )
        )

    @staticmethod
    def _end_attempt(
        snapshot: dict,
        attempt_id: str,
        task_id: str,
        status: str,
        summary: str | None,
    ) -> None:
        attempt = next(item for item in snapshot["attempts"] if item["id"] == attempt_id)
        attempt["status"] = status
        attempt["endedAt"] = _now()
        attempt["resultSummary"] = summary
        task = next(item for item in snapshot["tasks"] if item["id"] == task_id)
        task["status"] = status if status != "uncertain" else "waiting"
        task["activeOrWaiting"] = (
            "waiting"
            if status in ("waiting", "uncertain")
            else "completed"
            if status == "completed"
            else "active"
        )
        next(item for item in snapshot["agents"] if item["id"] == attempt["agentId"])["status"] = (
            status
        )
        snapshot["events"].append(
            ResearchHost._event(task_id, attempt["agentId"], f"Agent work {status}.")
        )

    def _tool(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
        agent_id: str,
        name: str,
        args: dict,
        queue: deque,
        read_sources: set[str],
    ) -> tuple[dict, str | None]:
        if name == "source_read":
            source_id = args.get("source_id")
            with self._lock, self._connect() as db:
                snapshot = self._snapshot_locked(db, run_id)
            source = next((item for item in snapshot["sources"] if item["id"] == source_id), None)
            if source is None:
                raise LiveError("Unknown saved source")
            read_sources.add(source_id)
            return {
                "id": source["id"],
                "version": source["version"],
                "contentHash": source.get("contentHash"),
                "passages": source["passages"],
            }, None
        if name == "ask_peer":
            peer_id, question = args.get("agent_id"), args.get("question")
            if (
                peer_id not in self._role_ids
                or peer_id == agent_id
                or not isinstance(question, str)
                or not question.strip()
            ):
                raise LiveError("Choose another available peer and a nonempty question")
            request_id, peer_task_id = _id("peer-request"), _id("task")

            def change(snapshot: dict) -> None:
                snapshot["tasks"].append(self._task(peer_task_id, question, "queued"))
                snapshot["tasks"][-1]["parentTaskId"] = task_id
                next(item for item in snapshot["agents"] if item["id"] == peer_id)["status"] = (
                    "queued"
                )
                snapshot["messages"].append(
                    {
                        "id": _id("message"),
                        "requestId": request_id,
                        "fromAgentId": agent_id,
                        "toAgentId": peer_id,
                        "taskId": peer_task_id,
                        "status": "requested",
                        "summary": question,
                        "createdAt": _now(),
                    }
                )
                snapshot["events"].append(
                    self._event(peer_task_id, agent_id, f"Peer work requested from {peer_id}.")
                )

            self._mutate(run_id, change)
            queue.append((peer_id, peer_task_id, question, agent_id, request_id, None))
            return {"request_id": request_id, "status": "requested"}, None
        if name == "publish_result":
            text = args.get("text")
            if not isinstance(text, str) or not text.strip():
                raise LiveError("A nonempty result is required")
            citations = args["citations"] if "citations" in args else None
            limits = args["limitations"] if "limitations" in args else None
            if ("citations" in args and not isinstance(citations, list)) or (
                "limitations" in args and not isinstance(limits, list)
            ):
                raise LiveError("Invalid result fields")
            self._publish(
                run_id,
                task_id,
                attempt_id,
                agent_id,
                text.strip(),
                citations,
                [str(item) for item in limits] if limits is not None else None,
                read_sources,
            )
            return {"status": "published"}, text.strip()
        raise LiveError("Unknown tool")

    def _publish(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
        agent_id: str,
        text: str,
        citations: list[dict] | None,
        limitations: list[str] | None,
        read_sources: set[str],
    ) -> None:
        with self._lock, self._connect() as db:
            run = db.execute("SELECT task_id,agent_ids FROM runs WHERE id=?", (run_id,)).fetchone()
        chosen_agents = json.loads(run["agent_ids"])
        report_agent = "agent-synthesis" if "agent-synthesis" in chosen_agents else chosen_agents[0]
        is_report = task_id == f"{run['task_id']}-{agent_id}" and agent_id == report_agent

        def change(snapshot: dict) -> None:
            valid = {
                (source["id"], passage["spanId"])
                for source in snapshot["sources"]
                for passage in source["passages"]
            }
            chosen = []
            provided = (
                citations
                if citations is not None
                else _plain_text_citations(text, valid, read_sources)
            )
            for item in provided:
                if not isinstance(item, dict):
                    raise LiveError("Invalid citation")
                pair = (
                    item.get("source_id", item.get("sourceId")),
                    item.get("span_id", item.get("spanId")),
                )
                if pair not in valid:
                    raise LiveError("Citation is not an exact saved source passage")
                if pair[0] not in read_sources:
                    raise LiveError("Citation source was not read in this attempt")
                citation = {"sourceId": pair[0], "spanId": pair[1]}
                if citation not in chosen:
                    chosen.append(citation)
            claim_id, artifact_id = _id("claim"), _id("artifact")
            snapshot["claims"].append(
                {
                    "id": claim_id,
                    "version": 1,
                    "text": text,
                    "disposition": "agent interpretation; review support",
                    "supports": chosen,
                    "taskId": task_id,
                }
            )
            snapshot["artifacts"].append(
                {
                    "id": artifact_id,
                    "version": 1,
                    "kind": "research answer" if is_report else "peer critique",
                    "title": f"{next(a['name'] for a in snapshot['agents'] if a['id'] == agent_id)} result",
                    "body": text,
                    "citations": chosen,
                    "limitations": (
                        limitations
                        if limitations is not None
                        else _plain_text_limitations(text)
                    ),
                    "producerAttemptId": attempt_id,
                }
            )
            if is_report:
                snapshot["mission"]["reportArtifactId"] = artifact_id
            snapshot["events"].append(
                self._event(task_id, agent_id, "Agent result published for review.")
            )

        self._mutate(run_id, change)

    def _deliver_peer(self, run_id: str, request_id: str, answer: str) -> None:
        def change(snapshot: dict) -> None:
            message = next(item for item in snapshot["messages"] if item["requestId"] == request_id)
            message["status"] = "fulfilled"
            message["updatedAt"] = _now()
            snapshot["messages"].append(
                {
                    "id": _id("message"),
                    "requestId": request_id,
                    "fromAgentId": message["toAgentId"],
                    "toAgentId": message["fromAgentId"],
                    "taskId": message["taskId"],
                    "status": "delivered",
                    "summary": answer,
                    "createdAt": _now(),
                }
            )
            snapshot["events"].append(
                self._event(message["taskId"], message["toAgentId"], "Peer response delivered.")
            )

        self._mutate(run_id, change)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._shutdown_event.set()
            self._executor.shutdown(wait=True, cancel_futures=False)
