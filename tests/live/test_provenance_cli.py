"""Exercise saved live inspection and guarded copy-first migration offline."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from ora.live import cli
from ora.live.inspection import inspect_provenance, migrate_provenance
from ora.live.provenance_schema import CURRENT_VERSION


def _legacy_database(state_dir: Path) -> Path:
    state_dir.mkdir(mode=0o700, parents=True)
    path = state_dir / "research.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE runs (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
              parent_run_id TEXT
            );
            CREATE TABLE snapshots (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, body TEXT NOT NULL
            );
            CREATE TABLE calls (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
              attempt_id TEXT NOT NULL, agent_id TEXT NOT NULL,
              status TEXT NOT NULL, phase TEXT NOT NULL,
              messages TEXT NOT NULL, response TEXT,
              tool_results TEXT NOT NULL, estimated_max_usd REAL NOT NULL,
              actual_usd REAL, retry_of_call_id TEXT,
              created_at TEXT NOT NULL, completed_at TEXT
            );
            """
        )
        snapshot = {
            "agents": [{"id": "agent-synthesis", "name": "Sage", "role": "synthesis"}],
            "attempts": [{"id": "attempt-1", "taskId": "task-1",
                          "agentId": "agent-synthesis",
                          "skillVersions": ["evidence-linking@1.0.2"]}],
            "sources": [{"id": "source-1", "provider": "user_supplied",
                         "version": 1, "contentHash": "synthetic-sha"}],
        }
        db.execute("INSERT INTO runs VALUES (?,?,?,?)", ("run-1", "task-1", "snapshot-1", None))
        db.execute(
            "INSERT INTO snapshots VALUES (?,?,?)",
            ("snapshot-1", "run-1", json.dumps(snapshot)),
        )
        response = {"model": "synthetic-model", "tool_calls": [{
            "id": "provider-tool-1", "function": {
                "name": "source_read_by_id", "arguments": "PRIVATE_ARGUMENT_SENTINEL"
            }
        }], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        db.execute(
            "INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("call-1", "run-1", "attempt-1", "agent-synthesis", "completed",
             "response", "PRIVATE_MESSAGE_SENTINEL", json.dumps(response),
             json.dumps([{"tool_call_id": "provider-tool-1",
                          "content": json.dumps({"ok": True, "detail": "PRIVATE_RESULT_SENTINEL"})}]),
             0.01, 0.002, None, "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:01+00:00"),
        )
    return path


def test_inspection_requires_explicit_migration_and_no_session(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    db_path = _legacy_database(state_dir)
    before = db_path.read_bytes()

    with pytest.raises(ValueError, match="migrate-provenance"):
        inspect_provenance(db_path)

    assert db_path.read_bytes() == before
    assert not (state_dir / "connection.json").exists()


def test_migration_backs_up_original_and_inspects_safe_columns(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    db_path = _legacy_database(state_dir)
    receipt = migrate_provenance(state_dir)

    assert receipt["status"] == "migrated"
    backup_path = Path(receipt["backup"])
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 0
        assert backup.execute("SELECT count(*) FROM calls").fetchone()[0] == 1
        assert backup.execute("SELECT messages FROM calls").fetchone()[0] == "PRIVATE_MESSAGE_SENTINEL"
        assert backup.execute("SELECT body FROM snapshots").fetchone()[0]
    summary = inspect_provenance(db_path, run_id="run-1")
    assert summary["schemaVersion"] == CURRENT_VERSION
    assert summary["views"]["calls"]["rows"][0]["agent_name"] == "Sage"
    assert summary["views"]["calls"]["rows"][0]["configuration_basis"] == "unknown_historical_configuration"
    tool = summary["views"]["tools"]["rows"][0]
    assert tool["status"] == "completed"
    assert tool["provider_tool_call_id"] == "provider-tool-1"
    assert tool["provenance_basis"] == "recorded"
    assert summary["views"]["skills"]["rows"][0]["skill_version"] == "1.0.2"
    source = summary["views"]["sources"]["rows"][0]
    assert source["source_provider"] == "user_supplied"
    assert source["external_provider_contacted"] == 0
    encoded = json.dumps(summary)
    assert "PRIVATE_ARGUMENT_SENTINEL" not in encoded
    assert "PRIVATE_MESSAGE_SENTINEL" not in encoded
    assert "PRIVATE_RESULT_SENTINEL" not in encoded
    assert migrate_provenance(state_dir) == {"status": "already_current", "schemaVersion": CURRENT_VERSION}


def test_migration_refuses_active_host_before_backup(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    db_path = _legacy_database(state_dir)
    before = db_path.read_bytes()
    lock = cli._open_host_lock(state_dir)
    try:
        with pytest.raises(ValueError, match="already owns"):
            migrate_provenance(state_dir)
    finally:
        lock.close()
    assert db_path.read_bytes() == before
    assert not (state_dir / "provenance-backups").exists()


def test_cli_provenance_does_not_require_connection_or_host(tmp_path: Path, capsys) -> None:
    state_dir = tmp_path / ".local" / "live"
    _legacy_database(state_dir)
    migrate_provenance(state_dir)
    parser = argparse.ArgumentParser()
    cli.configure(parser)
    args = parser.parse_args([
        "provenance", "--state-dir", str(state_dir), "--view", "tools",
        "--run-id", "run-1", "--limit", "1",
    ])

    cli.main(args, tmp_path)

    result = json.loads(capsys.readouterr().out)
    assert result["runId"] == "run-1"
    assert list(result["views"]) == ["tools"]
    assert result["views"]["tools"]["rows"][0]["call_id"] == "call-1"
    assert not (state_dir / "connection.json").exists()


def test_inspection_limit_and_missing_database(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No regular saved"):
        inspect_provenance(tmp_path / "missing.sqlite3")
    with pytest.raises(ValueError, match="between 1 and 500"):
        inspect_provenance(tmp_path / "missing.sqlite3", limit=501)
