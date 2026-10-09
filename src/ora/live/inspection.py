"""Read saved live provenance without launching a host or contacting a provider.

The explicit upgrade takes a private SQLite backup, proves the migration on a
temporary copy, then upgrades the selected database while holding host.lock.
The normal inspector opens SQLite in read-only/query-only mode and reads only
the deliberately small overview views.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .provenance_schema import CURRENT_VERSION

VIEWS = {
    "calls": "live_call_overview",
    "tools": "live_tool_overview",
    "configurations": "live_configuration_overview",
    "skills": "live_skill_overview",
    "sources": "live_source_overview",
}

# Explicit projections prevent a future view addition from accidentally printing
# prompts, arguments, response bodies, source content or a credential-bearing URL.
COLUMNS = {
    "calls": (
        "call_id", "run_id", "parent_run_id", "run_task_id", "task_id",
        "attempt_id", "legacy_agent_id", "agent_instance_id", "agent_name",
        "agent_role", "agent_identity_basis", "status", "phase", "created_at", "completed_at",
        "duration_ms", "config_id", "config_version", "requested_provider",
        "requested_model", "reported_model", "prompt_tokens",
        "completion_tokens", "total_tokens", "estimated_max_usd",
        "actual_usd", "retry_of_call_id", "configuration_basis",
        "messages_json_status", "response_json_status", "snapshot_json_status",
        "input_ref", "output_ref",
    ),
    "tools": (
        "invocation_id", "run_id", "parent_run_id", "task_id", "attempt_id",
        "legacy_agent_id", "agent_instance_id", "call_id",
        "provider_tool_call_id", "tool_name", "status", "requested_at",
        "started_at", "completed_at", "execution_provider",
        "execution_location", "provenance_basis", "input_ref", "output_ref",
        "error_ref",
    ),
    "configurations": (
        "run_id", "call_id", "attempt_id", "agent_instance_id", "config_id",
        "config_version", "requested_provider", "requested_model",
        "reported_model", "configuration_sha256", "instructions_sha256",
        "loaded_skill_count", "available_tool_count", "provenance_basis",
        "settings_ref", "reasoning_settings_ref",
    ),
    "skills": (
        "run_id", "call_id", "attempt_id", "agent_instance_id", "skill_id",
        "skill_version", "instructions_sha256", "provenance_basis",
        "instruction_ref",
    ),
    "sources": (
        "run_id", "parent_run_id", "snapshot_id", "source_id",
        "source_provider", "source_version", "content_sha256",
        "retrieval_status", "external_provider_contacted", "provenance_basis",
        "source_ref",
    ),
}


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file() or db_path.is_symlink():
        raise ValueError("No regular saved live database exists at the selected state directory.")
    try:
        db = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        raise ValueError("Selected live database cannot be opened read-only.") from None
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
    except sqlite3.DatabaseError:
        db.close()
        raise ValueError("Selected live database cannot be read safely.") from None
    return db


def _migration_version(db: sqlite3.Connection) -> int:
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='live_provenance_migrations'"
    ).fetchone()
    if not exists:
        return 0
    row = db.execute("SELECT max(version) FROM live_provenance_migrations").fetchone()
    return int(row[0] or 0)


def inspect_provenance(
    db_path: Path, *, view: str = "all", run_id: str | None = None,
    limit: int = 100,
) -> dict:
    """Return bounded columns from saved overview views without a live session."""
    if view not in (*VIEWS, "all"):
        raise ValueError("Unknown provenance view.")
    if not 1 <= limit <= 500:
        raise ValueError("--limit must be between 1 and 500.")
    if run_id is not None and not run_id:
        raise ValueError("--run-id must be nonempty.")
    with closing(_connect_read_only(Path(db_path))) as db:
        version = _migration_version(db)
        if version < CURRENT_VERSION:
            raise ValueError(
                "Saved live provenance is older than this inspector; run the explicit "
                "live migrate-provenance command."
            )
        if version > CURRENT_VERSION:
            raise ValueError("Saved live provenance is newer than this inspector.")
        result = {"proofKind": "saved_live_provenance", "schemaVersion": version,
                  "runId": run_id, "views": {}}
        selected = VIEWS if view == "all" else {view: VIEWS[view]}
        for label, name in selected.items():
            present = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='view' AND name=?", (name,)
            ).fetchone()
            if not present:
                raise ValueError(f"Saved live provenance view {name} is missing.")
            # The view name comes from VIEWS, and selected columns from the
            # fixed allowlist, so neither is interpolated from caller input.
            columns = {row["name"] for row in db.execute(f"PRAGMA table_info({name})")}
            selected_columns = COLUMNS[label]
            if not set(selected_columns).issubset(columns):
                raise ValueError(f"Saved live provenance view {name} lacks required columns.")
            projection = ", ".join(f'"{column}"' for column in selected_columns)
            query = f"SELECT {projection} FROM {name}"
            params: list[object] = []
            if run_id is not None:
                query += " WHERE run_id=?"
                params.append(run_id)
            order = next((column for column in ("created_at", "requested_at", "call_id", "run_id") if column in selected_columns), "run_id")
            query += f' ORDER BY "{order}", run_id LIMIT ?'
            params.append(limit + 1)
            try:
                rows = db.execute(query, params).fetchall()
            except sqlite3.DatabaseError as error:
                raise ValueError(f"Saved live provenance view {name} could not be read: {error}") from None
            result["views"][label] = {
                "rows": [dict(row) for row in rows[:limit]],
                "truncated": len(rows) > limit,
            }
        return result


def _legacy_digest(db: sqlite3.Connection) -> dict[str, str]:
    """Fingerprint every original column so appended provenance cannot rewrite history."""
    digests = {}
    for name in ("runs", "snapshots", "calls"):
        columns = [row["name"] for row in db.execute(f"PRAGMA table_info({name})")]
        if not columns:
            raise ValueError(f"Live database is missing the {name} table.")
        selected = ", ".join(f'"{column}"' for column in columns)
        digest = hashlib.sha256()
        for row in db.execute(f"SELECT {selected} FROM {name} ORDER BY rowid"):
            encoded = json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":")).encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        digests[name] = digest.hexdigest()
    return digests


def _verify_upgraded(db_path: Path) -> None:
    with closing(_connect_read_only(db_path)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Migrated live database failed SQLite integrity_check.")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Migrated live database has unresolved foreign keys.")
        if _migration_version(db) != CURRENT_VERSION:
            raise ValueError("Current live provenance migration was not recorded.")
        for name in VIEWS.values():
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='view' AND name=?", (name,)
            ).fetchone():
                raise ValueError(f"Migration did not create {name}.")
            db.execute(f"SELECT run_id FROM {name} LIMIT 1").fetchone()


def _upgrade_database(state_dir: Path) -> None:
    from ora.state.store import StateStore

    from .provenance_schema import migrate_live_provenance

    # This initializes the shared Stage 2 schema in the same live database.
    # No host is created and no provider request is made.
    StateStore(state_dir, database_name="research.sqlite3")
    db = sqlite3.connect(state_dir / "research.sqlite3", isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        migrate_live_provenance(db)
    finally:
        db.close()


def migrate_provenance(state_dir: Path) -> dict:
    """Explicitly upgrade an idle live DB after backup and disposable-copy proof."""
    from .cli import _open_host_lock

    state_dir = Path(state_dir).resolve()
    db_path = state_dir / "research.sqlite3"
    if not state_dir.is_dir() or not db_path.is_file() or db_path.is_symlink():
        raise ValueError("Select an existing live state directory with a regular research.sqlite3.")
    lock = _open_host_lock(state_dir)
    try:
        with closing(_connect_read_only(db_path)) as source:
            version = _migration_version(source)
            if version == CURRENT_VERSION:
                _verify_upgraded(db_path)
                return {"status": "already_current", "schemaVersion": version}
            if version > CURRENT_VERSION:
                raise ValueError("Saved live provenance version is newer than this migrator.")
            legacy = _legacy_digest(source)
            backup_dir = state_dir / "provenance-backups"
            if backup_dir.is_symlink():
                raise ValueError("The provenance backup directory must not be a symlink.")
            backup_dir.mkdir(mode=0o700, exist_ok=True)
            fd, backup_name = tempfile.mkstemp(
                prefix="research-before-provenance-", suffix=".sqlite3", dir=backup_dir
            )
            os.close(fd)
            backup_path = Path(backup_name)
            try:
                with closing(sqlite3.connect(backup_path)) as backup:
                    source.backup(backup)
            except BaseException:
                backup_path.unlink(missing_ok=True)
                raise
        with closing(_connect_read_only(backup_path)) as backup:
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("The private pre-migration backup failed SQLite integrity_check.")
            if _legacy_digest(backup) != legacy:
                raise ValueError("The private pre-migration backup differs from the selected live records.")
        try:
            with tempfile.TemporaryDirectory(prefix="ora-live-provenance-") as temporary:
                copy_dir = Path(temporary)
                copy_path = copy_dir / "research.sqlite3"
                shutil.copyfile(backup_path, copy_path)
                _upgrade_database(copy_dir)
                _verify_upgraded(copy_path)
                with closing(_connect_read_only(copy_path)) as copy:
                    for name, old_hash in legacy.items():
                        if _digest_columns(copy, backup_path, name) != old_hash:
                            raise ValueError(f"Copy migration changed historical {name} records.")
            with closing(_connect_read_only(db_path)) as current:
                if _legacy_digest(current) != legacy:
                    raise ValueError("Live records changed during copy validation; upgrade stopped.")
            _upgrade_database(state_dir)
            _verify_upgraded(db_path)
            with closing(_connect_read_only(db_path)) as migrated:
                for name, old_hash in legacy.items():
                    if _digest_columns(migrated, backup_path, name) != old_hash:
                        raise ValueError(f"Migration changed historical {name} records.")
        except BaseException as error:
            raise ValueError(
                f"Provenance migration did not verify; private backup retained at {backup_path}. "
                f"{error}"
            ) from error
        return {"status": "migrated", "schemaVersion": CURRENT_VERSION,
                "backup": str(backup_path), "historicalRecordsPreserved": True}
    finally:
        lock.close()


def _digest_columns(db: sqlite3.Connection, original_path: Path, table: str) -> str:
    """Hash original columns only, excluding columns added by migration."""
    with closing(_connect_read_only(original_path)) as original:
        columns = [row["name"] for row in original.execute(f"PRAGMA table_info({table})")]
    selected = ", ".join(f'"{column}"' for column in columns)
    digest = hashlib.sha256()
    for row in db.execute(f"SELECT {selected} FROM {table} ORDER BY rowid"):
        encoded = json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":")).encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()
