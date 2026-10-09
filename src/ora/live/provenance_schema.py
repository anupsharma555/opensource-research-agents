"""Add a queryable, non-destructive provenance index to the local live database.

The shared StateStore owns ``PRAGMA user_version`` and canonical records.  This
module has a separate version ledger because live operational tables and views
are an additive projection over that same database, not another record store.
Historical call JSON remains byte-for-byte unchanged.  A legacy tool row points
back to its saved JSON instead of copying arguments or results into the index.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone

CURRENT_VERSION = 2
_TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


def _json(value: str | None) -> object:
    try:
        return json.loads(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _array(value: str | None) -> list:
    parsed = _json(value)
    return parsed if isinstance(parsed, list) else []


def _snapshot_attempts(db: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """Use each run's selected snapshot, never a newer repository configuration."""
    attempts: dict[tuple[str, str], dict] = {}
    for run_id, body in db.execute(
        "SELECT r.id, s.body FROM runs r LEFT JOIN snapshots s ON s.id=r.snapshot_id"
    ):
        snapshot = _json(body)
        if not isinstance(snapshot, dict):
            continue
        saved_attempts = snapshot.get("attempts")
        if not isinstance(saved_attempts, list):
            continue
        for attempt in saved_attempts:
            if isinstance(attempt, dict) and isinstance(attempt.get("id"), str):
                attempts[(run_id, attempt["id"])] = attempt
    return attempts


def _result_status(item: dict) -> tuple[str, str]:
    content = _json(item.get("content"))
    if content is None:
        return "uncertain", "unreadable_recorded_result"
    # The former host wrote {"error": ...} for several exception paths. Its
    # payload alone cannot distinguish a rejected input from a failed handler
    # or an uncertain external outcome.
    if isinstance(content, dict) and "error" in content:
        return "uncertain", "recorded_error_outcome_unknown"
    return "completed", "recorded"


def _legacy_result(
    calls: list[sqlite3.Row], source: sqlite3.Row, tool: dict
) -> tuple[str, str | None, str]:
    """Resolve a result only if its saved provenance and content are unambiguous."""
    direct = [
        (index, item)
        for index, item in enumerate(_array(source["tool_results"]))
        if isinstance(item, dict) and item.get("tool_call_id") == tool["id"]
    ]
    if direct:
        if len(direct) == 1:
            if not isinstance(direct[0][1].get("content"), str):
                return "uncertain", None, "invalid_recorded_result"
            status, basis = _result_status(direct[0][1])
            return status, f"calls.tool_results[{direct[0][0]}]", basis
        return "uncertain", None, "ambiguous_recorded_results"

    # Later model requests replay the conversation. Repeated copies of one tool
    # message are a single observation. A different content for the same ID is
    # ambiguous and must never be accepted as a completed operation.
    found: dict[str, tuple[str, int, dict]] = {}
    for later in calls:
        if (
            later["run_id"] != source["run_id"]
            or later["attempt_id"] != source["attempt_id"]
            or (later["created_at"], later["id"])
            <= (source["created_at"], source["id"])
        ):
            continue
        messages = _array(later["messages"])
        originating_assistant_seen = False
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant":
                candidates = message.get("tool_calls")
                originating_assistant_seen = any(
                    candidate == tool
                    for candidate in (candidates if isinstance(candidates, list) else [])
                ) or originating_assistant_seen
            if (
                originating_assistant_seen
                and message.get("role") == "tool"
                and message.get("tool_call_id") == tool["id"]
                and isinstance(message.get("content"), str)
            ):
                found.setdefault(message["content"], (later["id"], index, message))
    if len(found) == 1:
        later_id, index, message = next(iter(found.values()))
        status, basis = _result_status(message)
        if basis == "recorded":
            basis = "reconstructed_later_message"
        else:
            basis = f"reconstructed_later_{basis}"
        return status, f"calls:{later_id}.messages[{index}]", basis
    if found:
        return "uncertain", None, "ambiguous_later_messages"
    return "requested", None, "recorded_request_only"


def _backfill_legacy_tools(db: sqlite3.Connection) -> None:
    calls = db.execute("SELECT * FROM calls ORDER BY created_at, id").fetchall()
    attempts = _snapshot_attempts(db)
    repeated_ids: Counter[tuple[str, str, str]] = Counter()
    for call in calls:
        response = _json(call["response"])
        if isinstance(response, dict) and isinstance(response.get("tool_calls"), list):
            repeated_ids.update(
                (call["run_id"], call["attempt_id"], tool["id"])
                for tool in response["tool_calls"]
                if isinstance(tool, dict) and isinstance(tool.get("id"), str)
            )
    for call in calls:
        response = _json(call["response"])
        if not isinstance(response, dict) or not isinstance(response.get("tool_calls"), list):
            continue
        attempt = attempts.get((call["run_id"], call["attempt_id"]), {})
        task_id = (
            attempt.get("taskId")
            if attempt.get("agentId") == call["agent_id"]
            and isinstance(attempt.get("taskId"), str)
            else None
        )
        seen_ids: set[str] = set()
        for index, tool in enumerate(response["tool_calls"]):
            if not isinstance(tool, dict):
                continue
            tool_id = tool.get("id")
            if not isinstance(tool_id, str) or not tool_id or tool_id in seen_ids:
                # An absent or repeated provider ID cannot be linked safely.
                continue
            seen_ids.add(tool_id)
            function = tool.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
                name = None
            status, output_ref, basis = _legacy_result(calls, call, tool)
            if repeated_ids[(call["run_id"], call["attempt_id"], tool_id)] > 1:
                status, output_ref, basis = "uncertain", None, "ambiguous_reused_provider_id"
            error_ref = output_ref if status == "uncertain" and "error" in basis else None
            if error_ref is not None:
                output_ref = None
            db.execute(
                """INSERT INTO tool_invocations
                   (id,run_id,task_id,attempt_id,agent_instance_id,model_call_id,
                    provider_tool_call_id,tool_name,status,input_ref,output_ref,
                    error_ref,execution_provider,execution_location,requested_at,
                    started_at,completed_at,provenance_basis)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"legacy:{call['id']}:{index}", call["run_id"], task_id,
                    call["attempt_id"], None, call["id"], tool_id, name, status,
                    f"calls.response.tool_calls[{index}].function.arguments",
                    output_ref, error_ref, None, None, call["completed_at"], None,
                    None, basis,
                ),
            )


_DDL = (
    """CREATE TABLE live_provenance_migrations (
         version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)""",
    "ALTER TABLE calls ADD COLUMN agent_instance_id TEXT",
    "ALTER TABLE calls ADD COLUMN config_id TEXT",
    "ALTER TABLE calls ADD COLUMN config_version INTEGER",
    """CREATE TABLE tool_invocations (
         id TEXT PRIMARY KEY,
         run_id TEXT NOT NULL REFERENCES runs(id),
         task_id TEXT,
         attempt_id TEXT NOT NULL,
         agent_instance_id TEXT,
         model_call_id TEXT NOT NULL REFERENCES calls(id),
         provider_tool_call_id TEXT NOT NULL,
         tool_name TEXT,
         status TEXT NOT NULL CHECK(status IN
           ('requested','started','completed','rejected','failed','uncertain')),
         input_ref TEXT NOT NULL,
         output_ref TEXT,
         error_ref TEXT,
         execution_provider TEXT,
         execution_location TEXT,
         requested_at TEXT,
         started_at TEXT,
         completed_at TEXT,
         provenance_basis TEXT NOT NULL,
         UNIQUE(model_call_id, provider_tool_call_id))""",
    "CREATE INDEX tool_invocations_run ON tool_invocations(run_id,model_call_id)",
    "CREATE INDEX tool_invocations_status ON tool_invocations(status)",
    # Views expose metadata only. Payloads, source text, prompts, tool arguments,
    # instructions and endpoint URLs remain in their private source records.
    """CREATE VIEW live_call_overview AS
       SELECT c.id AS call_id, c.run_id, r.parent_run_id, r.task_id AS run_task_id,
         (SELECT json_extract(a.value,'$.taskId') FROM snapshots s,
                 json_each(CASE WHEN json_valid(s.body) THEN s.body ELSE '{}' END,'$.attempts') a
          WHERE s.id=r.snapshot_id AND json_valid(a.value)
            AND json_extract(CASE WHEN json_valid(a.value) THEN a.value END,'$.id')=c.attempt_id
            AND json_extract(CASE WHEN json_valid(a.value) THEN a.value END,'$.agentId')=c.agent_id
          LIMIT 1) AS task_id,
         c.attempt_id, c.agent_id AS legacy_agent_id, c.agent_instance_id,
         COALESCE(json_extract(CASE WHEN json_valid(i.body) THEN i.body END,'$.display_name'),
           (SELECT json_extract(CASE WHEN json_valid(a.value) THEN a.value END,'$.name') FROM snapshots s,
                   json_each(CASE WHEN json_valid(s.body) THEN s.body ELSE '{}' END,'$.agents') a
            WHERE s.id=r.snapshot_id AND json_valid(a.value)
              AND json_extract(CASE WHEN json_valid(a.value) THEN a.value END,'$.id')=c.agent_id
            LIMIT 1)) AS agent_name,
         COALESCE(json_extract(CASE WHEN json_valid(i.body) THEN i.body END,'$.role_id'),
           (SELECT json_extract(CASE WHEN json_valid(a.value) THEN a.value END,'$.role') FROM snapshots s,
                   json_each(CASE WHEN json_valid(s.body) THEN s.body ELSE '{}' END,'$.agents') a
            WHERE s.id=r.snapshot_id AND json_valid(a.value)
              AND json_extract(CASE WHEN json_valid(a.value) THEN a.value END,'$.id')=c.agent_id
            LIMIT 1)) AS agent_role,
         CASE WHEN i.id IS NOT NULL AND json_valid(i.body) THEN 'recorded_agent_instance'
              WHEN i.id IS NOT NULL THEN 'invalid_agent_instance_record'
              WHEN c.agent_instance_id IS NULL AND json_valid(selected.body) THEN 'historical_snapshot'
              WHEN c.agent_instance_id IS NULL THEN 'unknown_historical_identity'
              ELSE 'unresolved_agent_instance' END AS agent_identity_basis,
         c.status, c.phase, c.created_at, c.completed_at,
         CASE WHEN c.completed_at IS NOT NULL THEN
           ROUND((julianday(c.completed_at)-julianday(c.created_at))*86400000,1)
           END AS duration_ms,
         c.config_id, c.config_version,
         json_extract(CASE WHEN json_valid(cfg.body) THEN cfg.body END,'$.provider') AS requested_provider,
         json_extract(CASE WHEN json_valid(cfg.body) THEN cfg.body END,'$.model_id') AS requested_model,
         json_extract(CASE WHEN json_valid(c.response) THEN c.response END,'$.model') AS reported_model,
         json_extract(CASE WHEN json_valid(c.response) THEN c.response END,'$.usage.prompt_tokens') AS prompt_tokens,
         json_extract(CASE WHEN json_valid(c.response) THEN c.response END,'$.usage.completion_tokens') AS completion_tokens,
         json_extract(CASE WHEN json_valid(c.response) THEN c.response END,'$.usage.total_tokens') AS total_tokens,
         c.estimated_max_usd, c.actual_usd, c.retry_of_call_id,
         CASE WHEN c.config_id IS NULL THEN 'unknown_historical_configuration'
              WHEN cfg.id IS NULL THEN 'missing_configuration_record'
              WHEN NOT json_valid(cfg.body) THEN 'invalid_configuration_record'
              ELSE 'recorded_configuration' END AS configuration_basis,
         CASE WHEN c.messages IS NULL THEN 'absent'
              WHEN json_valid(c.messages) THEN 'valid' ELSE 'invalid' END AS messages_json_status,
         CASE WHEN c.response IS NULL THEN 'absent'
              WHEN json_valid(c.response) THEN 'valid' ELSE 'invalid' END AS response_json_status,
         CASE WHEN selected.id IS NULL THEN 'missing'
              WHEN json_valid(selected.body) THEN 'valid' ELSE 'invalid' END AS snapshot_json_status,
         'calls.messages' AS input_ref, 'calls.response' AS output_ref
       FROM calls c JOIN runs r ON r.id=c.run_id
       LEFT JOIN snapshots selected ON selected.id=r.snapshot_id
       LEFT JOIN records i ON i.kind='agent_instance' AND i.id=c.agent_instance_id
         AND i.version=1
       LEFT JOIN records cfg ON cfg.kind='agent_configuration' AND cfg.id=c.config_id
         AND cfg.version=c.config_version""",
    """CREATE VIEW live_tool_overview AS
       SELECT t.id AS invocation_id, t.run_id, r.parent_run_id, t.task_id, t.attempt_id,
         c.agent_id AS legacy_agent_id, t.agent_instance_id,
         t.model_call_id AS call_id, t.provider_tool_call_id, t.tool_name,
         t.status, t.requested_at, t.started_at, t.completed_at,
         t.execution_provider, t.execution_location, t.provenance_basis,
         t.input_ref, t.output_ref, t.error_ref
       FROM tool_invocations t JOIN calls c ON c.id=t.model_call_id
         JOIN runs r ON r.id=t.run_id""",
    """CREATE VIEW live_configuration_overview AS
       SELECT c.run_id, c.id AS call_id, c.attempt_id, c.agent_instance_id,
         c.config_id, c.config_version,
         json_extract(CASE WHEN json_valid(cfg.body) THEN cfg.body END,'$.provider') AS requested_provider,
         json_extract(CASE WHEN json_valid(cfg.body) THEN cfg.body END,'$.model_id') AS requested_model,
         json_extract(CASE WHEN json_valid(c.response) THEN c.response END,'$.model') AS reported_model,
         json_extract(CASE WHEN json_valid(cfg.body) THEN cfg.body END,'$.configuration_sha256') AS configuration_sha256,
         json_extract(CASE WHEN json_valid(cfg.body) THEN cfg.body END,'$.instructions_sha256') AS instructions_sha256,
         CASE WHEN json_valid(manifest.body) THEN
           (SELECT count(*) FROM json_each(manifest.body,'$.skills')) END AS loaded_skill_count,
         CASE WHEN json_valid(manifest.body) THEN
           json_array_length(manifest.body,'$.exposed_tools') END AS available_tool_count,
         CASE WHEN c.config_id IS NULL THEN 'unknown_historical_configuration'
              WHEN cfg.id IS NULL THEN 'missing_configuration_record'
              WHEN NOT json_valid(cfg.body) THEN 'invalid_configuration_record'
              ELSE 'recorded_configuration' END AS provenance_basis,
         CASE WHEN json_valid(cfg.body) THEN 'records.agent_configuration.body.settings'
              END AS settings_ref,
         CASE WHEN json_valid(cfg.body) THEN 'records.agent_configuration.body.reasoning_settings'
              END AS reasoning_settings_ref
       FROM calls c LEFT JOIN records cfg ON cfg.kind='agent_configuration'
         AND cfg.id=c.config_id AND cfg.version=c.config_version
       LEFT JOIN records manifest ON manifest.kind='agent_call'
         AND manifest.id=c.id AND manifest.version=1""",
    """CREATE VIEW live_skill_overview AS
       SELECT c.run_id, c.id AS call_id, c.attempt_id, c.agent_instance_id,
         sk.key AS skill_id,
         json_extract(CASE WHEN json_valid(sk.value) THEN sk.value END,'$.version') AS skill_version,
         json_extract(CASE WHEN json_valid(sk.value) THEN sk.value END,'$.instructions_sha256') AS instructions_sha256,
         'loaded_in_recorded_call' AS provenance_basis,
         'records.agent_call.body.skills' AS instruction_ref
       FROM calls c JOIN records manifest ON manifest.kind='agent_call'
         AND manifest.id=c.id AND manifest.version=1,
         json_each(CASE WHEN json_valid(manifest.body) THEN manifest.body ELSE '{}' END,'$.skills') sk
       UNION ALL
       SELECT c.run_id, c.id AS call_id, c.attempt_id, NULL AS agent_instance_id,
         CASE WHEN instr(sv.value,'@')>0 THEN substr(sv.value,1,instr(sv.value,'@')-1)
              END AS skill_id,
         CASE WHEN instr(sv.value,'@')>0 THEN substr(sv.value,instr(sv.value,'@')+1)
              ELSE sv.value END AS skill_version,
         NULL AS instructions_sha256, 'historical_attempt_snapshot' AS provenance_basis,
         'snapshots.body.attempts.skillVersions' AS instruction_ref
       FROM calls c JOIN runs r ON r.id=c.run_id JOIN snapshots s ON s.id=r.snapshot_id,
         json_each(CASE WHEN json_valid(s.body) THEN s.body ELSE '{}' END,'$.attempts') a,
         json_each(CASE WHEN json_valid(a.value) THEN a.value ELSE '{}' END,'$.skillVersions') sv
       WHERE c.config_id IS NULL
         AND json_extract(CASE WHEN json_valid(a.value) THEN a.value END,'$.id')=c.attempt_id
         AND json_extract(CASE WHEN json_valid(a.value) THEN a.value END,'$.agentId')=c.agent_id""",
    """CREATE VIEW live_source_overview AS
       SELECT r.id AS run_id, r.parent_run_id, r.snapshot_id,
         json_extract(CASE WHEN json_valid(src.value) THEN src.value END,'$.id') AS source_id,
         json_extract(CASE WHEN json_valid(src.value) THEN src.value END,'$.provider') AS source_provider,
         json_extract(CASE WHEN json_valid(src.value) THEN src.value END,'$.version') AS source_version,
         json_extract(CASE WHEN json_valid(src.value) THEN src.value END,'$.contentHash') AS content_sha256,
         json_extract(CASE WHEN json_valid(src.value) THEN src.value END,'$.retrievalStatus') AS retrieval_status,
         CASE WHEN json_extract(CASE WHEN json_valid(src.value) THEN src.value END,'$.provider')='user_supplied' THEN 0
              ELSE NULL END AS external_provider_contacted,
         CASE WHEN json_valid(src.value) THEN 'selected_run_snapshot'
              ELSE 'unknown_invalid_source_entry' END AS provenance_basis,
         'snapshots.body.sources' AS source_ref
       FROM runs r JOIN snapshots s ON s.id=r.snapshot_id,
         json_each(CASE WHEN json_valid(s.body) THEN s.body ELSE '{}' END,'$.sources') src
       UNION ALL
       SELECT r.id, r.parent_run_id, r.snapshot_id,
         NULL, NULL, NULL, NULL, NULL, NULL,
         CASE WHEN s.id IS NULL THEN 'unknown_missing_snapshot'
              ELSE 'unknown_invalid_snapshot_json' END,
         NULL
       FROM runs r LEFT JOIN snapshots s ON s.id=r.snapshot_id
       WHERE s.id IS NULL OR NOT json_valid(s.body)""",
)


_VIEW_NAMES = (
    "live_call_overview", "live_tool_overview", "live_configuration_overview",
    "live_skill_overview", "live_source_overview",
)
_V2_DDL = (
    """CREATE TABLE tool_invocation_events (
         sequence INTEGER PRIMARY KEY AUTOINCREMENT,
         invocation_id TEXT NOT NULL REFERENCES tool_invocations(id),
         status TEXT NOT NULL CHECK(status IN
           ('requested','started','completed','rejected','failed','uncertain')),
         occurred_at TEXT NOT NULL,
         provenance_basis TEXT NOT NULL,
         UNIQUE(invocation_id,status,occurred_at))""",
    "CREATE INDEX tool_invocation_events_invocation ON tool_invocation_events(invocation_id,sequence)",
    *(f"DROP VIEW IF EXISTS {name}" for name in _VIEW_NAMES),
    *_DDL[7:],
)


def _backfill_tool_events(db: sqlite3.Connection) -> None:
    """Index only timestamps saved on v1 rows; do not invent past phases."""
    for invocation_id, status, requested_at, started_at, completed_at in db.execute(
        "SELECT id,status,requested_at,started_at,completed_at FROM tool_invocations"
    ):
        requested_basis = (
            "reconstructed_from_legacy_response_completed_at"
            if invocation_id.startswith("legacy:")
            else "reconstructed_from_saved_requested_at"
        )
        phases = (
            ("requested", requested_at, requested_basis),
            ("started", started_at, "reconstructed_from_saved_started_at"),
            (
                status if status in {"completed", "rejected", "failed", "uncertain"} else None,
                completed_at,
                "reconstructed_from_saved_completed_at",
            ),
        )
        for phase, occurred_at, basis in phases:
            if phase is not None and occurred_at is not None:
                db.execute(
                    "INSERT INTO tool_invocation_events"
                    "(invocation_id,status,occurred_at,provenance_basis) VALUES (?,?,?,?)",
                    (invocation_id, phase, occurred_at, basis),
                )


def migrate_live_provenance(
    db: sqlite3.Connection,
    *,
    now: str | None = None,
    fault: Callable[[str], None] | None = None,
) -> None:
    """Install the current live index and backfill evidence in one transaction.

    The caller owns a verified backup and exclusive host lock for an existing
    database. A failed step rolls back tables, columns, views and backfill.
    """
    if db.in_transaction:
        raise ValueError("Live provenance migration needs an idle connection")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'").fetchone() is None:
        raise ValueError("Initialize the shared StateStore schema before live provenance")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='calls'").fetchone() is None:
        raise ValueError("Live calls table is unavailable")
    fault = fault or (lambda _step: None)
    now = now or datetime.now(timezone.utc).isoformat()
    db.execute("BEGIN IMMEDIATE")
    try:
        existing = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_provenance_migrations'"
        ).fetchone()
        version = (
            db.execute("SELECT COALESCE(MAX(version),0) FROM live_provenance_migrations").fetchone()[0]
            if existing else 0
        )
        if version > CURRENT_VERSION:
            raise ValueError(f"Unsupported live provenance schema: saved={version}")
        if version == CURRENT_VERSION:
            db.commit()
            return
        if version == 0:
            for index, statement in enumerate(_DDL):
                db.execute(statement)
                fault(f"live_provenance_1_step_{index}")
            row_factory = db.row_factory
            db.row_factory = sqlite3.Row
            try:
                _backfill_legacy_tools(db)
            finally:
                db.row_factory = row_factory
            fault("live_provenance_1_after_backfill")
            db.execute(
                "INSERT INTO live_provenance_migrations(version,applied_at) VALUES (1,?)",
                (now,),
            )
        if version <= 1:
            for index, statement in enumerate(_V2_DDL):
                db.execute(statement)
                fault(f"live_provenance_2_step_{index}")
            _backfill_tool_events(db)
            fault("live_provenance_2_after_backfill")
            db.execute(
                "INSERT INTO live_provenance_migrations(version,applied_at) VALUES (2,?)",
                (now,),
            )
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Live provenance migration left unresolved references")
        db.commit()
    except BaseException:
        db.rollback()
        raise
