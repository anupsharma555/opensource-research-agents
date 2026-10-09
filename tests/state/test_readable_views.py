"""Prove shared SQLite overviews preserve versions and honest proof boundaries."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from ora.retrieval import record_delivery
from ora.state import StateStore
from ora.state.demo import build_recorded_workflow
from ora.state.inspect import main as inspect_main
from ora.state.inspect import read_overview
from ora.state.views import VIEW_SQL

ROOT = Path(__file__).resolve().parents[2]


def _rows(db_path: Path, view: str) -> list[dict]:
    assert view in VIEW_SQL
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return [dict(row) for row in db.execute(f"SELECT * FROM {view}")]


def test_recorded_workflow_overviews_match_saved_records_without_payloads(tmp_path: Path) -> None:
    build_recorded_workflow(ROOT, tmp_path)
    store = StateStore(tmp_path, read_only=True)

    calls = _rows(store.db_path, "call_overview")
    assert calls
    for row in calls:
        saved = store.get("agent_call", row["call_id"], row["manifest_revision"])
        assert saved is not None
        assert row["instance_id"] == saved["body"]["instance_id"]
        assert row["configuration_id"] == saved["body"]["configuration_id"]
        assert row["requested_model"] == saved["body"]["model_id"]
        assert row["manifest_state"] == "prepared"
        assert row["execution_status"] == "not_recorded"
        assert row["is_current"] == (store.get("agent_call", row["call_id"])["version"] == row["manifest_revision"])
    assert any(row["display_name"] == "Sage" for row in calls)

    skills = _rows(store.db_path, "call_skills")
    tools = _rows(store.db_path, "call_tools")
    assert skills and tools
    assert any(row["skill_id"] == "evidence-linking" and row["instructions_loaded"] == 1 for row in skills)
    assert all(row["execution_basis"] == "association_only" for row in skills + tools)
    assert all(row["permitted"] == 1 for row in tools)
    assert any(row["exposed"] == 1 for row in tools)
    for row in skills:
        manifest = store.get("agent_call", row["call_id"], row["manifest_revision"])["body"]
        assert row["skill_id"] in manifest["skills"]
        assert row["instructions_loaded"] == int(row["skill_id"] in manifest["loaded_skill_ids"])
    for row in tools:
        manifest = store.get("agent_call", row["call_id"], row["manifest_revision"])["body"]
        assert row["tool_id"] in manifest["permitted_tools"]
        assert row["exposed"] == int(row["tool_id"] in manifest["exposed_tools"])

    deliveries = _rows(store.db_path, "retrieval_overview")
    sources = _rows(store.db_path, "source_overview")
    assert deliveries and sources
    for row in deliveries:
        saved = store.get("retrieval_delivery", row["delivery_id"], row["record_revision"])
        assert row["provider"] == saved["body"]["envelope"]["request"]["provider"]
        assert row["query_sha256"] == saved["body"]["envelope"]["request"]["querySha256"]
        assert row["private_query_record_path"] == "$.envelope.request.query"
    for row in sources:
        saved = store.get("source_copy", row["copy_id"], row["record_revision"])
        assert row["content_sha256"] == saved["body"]["content_sha256"]
        assert row["document_version"] == saved["body"]["source_version"]

    dependencies = _rows(store.db_path, "record_dependencies")
    assert dependencies
    for row in dependencies:
        assert store.get(row["source_kind"], row["source_id"], row["source_version"])
        assert store.get(row["target_kind"], row["target_id"], row["target_version"])

    for row in _rows(store.db_path, "invocation_overview"):
        saved = store.get(row["kind"], row["invocation_id"], row["record_revision"])
        assert row["execution_status"] == saved["body"]["status"]
        assert row["execution_basis"] == "saved_invocation_receipt"

    displayed = json.dumps({name: _rows(store.db_path, name) for name in VIEW_SQL})
    assert "Use inspected evidence and distinguish reported results" not in displayed
    assert "cognitive behavioural therapy depression six weeks" not in displayed
    assert "Synthetic publication abstract." not in displayed
    assert "api.groq.com/openai" not in displayed


def test_read_only_overview_cli_pages_without_exposing_record_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    build_recorded_workflow(ROOT, tmp_path)
    first = read_overview(tmp_path, "call_overview", limit=1)
    assert len(first["rows"]) == 1
    assert first["nextOffset"] == 1
    second = read_overview(tmp_path, "call_overview", limit=1, offset=first["nextOffset"])
    assert second["rows"][0]["call_id"] != first["rows"][0]["call_id"]
    assert first["rows"][0]["manifest_state"] == "prepared"

    monkeypatch.setattr(sys, "argv", [
        "ora.state.inspect", "--state-dir", str(tmp_path), "--overview",
        "call_overview", "--limit", "1", "--offset", "1",
    ])
    inspect_main()
    output = json.loads(capsys.readouterr().out)
    assert output == second
    assert "Use inspected evidence and distinguish reported results" not in json.dumps(output)
    assert "PRIVATE_QUERY_SENTINEL" not in json.dumps(output)


def test_retrieval_usage_view_preserves_observed_vs_documented_basis(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    request = {
        "mission_id": "synthetic-mission", "task_id": "synthetic-task",
        "request_id": "synthetic-request", "attempt_id": "synthetic-attempt",
        "provider": "simulated-count", "route": "synthetic-route",
        "operation": "search", "query": "PRIVATE_QUERY_SENTINEL",
        "requested_at": "2026-10-08T10:00:00Z",
        "requested_window": {"offset": 0, "limit": 1},
    }
    record_delivery(
        store, request,
        {"status": "ok", "count": 0, "offset": 0, "items": [],
         "usage": {"provider_request": 1}},
        delivery_id="synthetic-delivery", adapter="count_offset",
        retrieved_at="2026-10-08T10:01:00Z",
        documented_usage=[{"unit": "estimated_usd", "amount": 0.002}],
    )

    rows = _rows(store.db_path, "retrieval_usage")
    assert [(row["unit"], row["amount"], row["usage_basis"]) for row in rows] == [
        ("provider_request", 1, "observed"), ("estimated_usd", 0.002, "documented")
    ]
    assert "PRIVATE_QUERY_SENTINEL" not in json.dumps(_rows(store.db_path, "retrieval_overview"))


def test_v2_to_v3_migration_rolls_back_then_preserves_revisions_and_named_backup(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    old = StateStore(state_dir, schema_version=2, database_name="research.sqlite3")
    first = old.transact("first", {}, lambda tx: tx.put("claim", "synthetic-claim", {"text": "version one"}))
    second = old.transact("second", {}, lambda tx: tx.put(
        "claim", "synthetic-claim", {"text": "version two"}, expected_version=first["version"]
    ))
    with closing(sqlite3.connect(old.db_path)) as db:
        prior = db.execute("SELECT kind,id,version,body FROM records ORDER BY version").fetchall()

    def interrupt(point: str) -> None:
        if point == "migration_3_step_3":
            raise RuntimeError("synthetic migration interruption")

    with pytest.raises(RuntimeError, match="synthetic migration interruption"):
        StateStore(state_dir, database_name="research.sqlite3", fault=interrupt)
    with closing(sqlite3.connect(old.db_path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT kind,id,version,body FROM records ORDER BY version").fetchall() == prior
        assert db.execute("SELECT name FROM sqlite_master WHERE name='record_origins'").fetchone() is None
        assert db.execute("SELECT name FROM sqlite_master WHERE name='call_overview'").fetchone() is None

    upgraded = StateStore(state_dir, database_name="research.sqlite3", record_context={
        "proof_basis": "offline_test", "timing_basis": "controlled_clock",
        "implementation_ref": "build:synthetic-view-test",
    })
    assert upgraded.get("claim", "synthetic-claim", 1) == first
    assert upgraded.get("claim", "synthetic-claim", 2) == second
    historical = sorted(
        (row for row in _rows(upgraded.db_path, "record_provenance") if row["id"] == "synthetic-claim"),
        key=lambda row: row["record_revision"],
    )
    assert [(row["record_revision"], row["is_current"], row["provenance_basis"]) for row in historical] == [
        (1, 0, "not_recorded"), (2, 1, "not_recorded")
    ]
    assert all(row["proof_basis"] is None and row["timing_basis"] is None for row in historical)
    third = upgraded.transact("third", {}, lambda tx: tx.put(
        "claim", "synthetic-claim", {"text": "version three"}, expected_version=second["version"]
    ))
    current = next(row for row in _rows(upgraded.db_path, "record_provenance")
                   if row["id"] == "synthetic-claim" and row["record_revision"] == 3)
    assert current["is_current"] == 1
    assert (current["proof_basis"], current["timing_basis"], current["implementation_ref"]) == (
        "offline_test", "controlled_clock", "build:synthetic-view-test"
    )
    assert current["operation_id"] == "third"
    assert current["record_format_version"] == "json-object.v1"

    backup = tmp_path / "backup"
    manifest = upgraded.backup(backup)
    assert manifest["database_name"] == "research.sqlite3"
    restored = StateStore.restore(backup, tmp_path / "restored")
    assert restored.db_path.name == "research.sqlite3"
    assert restored.get("claim", "synthetic-claim", 1) == first
    assert restored.get("claim", "synthetic-claim", 3) == third
    assert len([row for row in _rows(restored.db_path, "record_provenance")
                if row["id"] == "synthetic-claim"]) == 3
