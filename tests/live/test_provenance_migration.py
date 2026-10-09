"""Exercise live provenance migration against synthetic legacy SQLite records."""

import json
import sqlite3
from pathlib import Path

import pytest

from ora.live.provenance_schema import migrate_live_provenance
from ora.state import StateStore

SECRET = "synthetic-private-payload-keep-out-of-views"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _legacy_db(root: Path) -> Path:
    StateStore(root, database_name="research.sqlite3")
    path = root / "research.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE runs (
              id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
              fingerprint TEXT NOT NULL, task_id TEXT NOT NULL,
              parent_run_id TEXT, parent_snapshot_id TEXT,
              question TEXT NOT NULL, scope TEXT NOT NULL,
              agent_ids TEXT NOT NULL, status TEXT NOT NULL,
              snapshot_id TEXT NOT NULL, comparison_snapshot_id TEXT,
              spent_usd REAL NOT NULL DEFAULT 0, reserved_usd REAL NOT NULL DEFAULT 0,
              error TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE snapshots (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
              body TEXT NOT NULL, created_at TEXT NOT NULL,
              FOREIGN KEY(run_id) REFERENCES runs(id)
            );
            CREATE TABLE calls (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
              attempt_id TEXT NOT NULL, agent_id TEXT NOT NULL,
              status TEXT NOT NULL, phase TEXT NOT NULL,
              messages TEXT NOT NULL, response TEXT,
              tool_results TEXT NOT NULL DEFAULT '[]',
              estimated_max_usd REAL NOT NULL, actual_usd REAL,
              retry_of_call_id TEXT, created_at TEXT NOT NULL, completed_at TEXT,
              FOREIGN KEY(run_id) REFERENCES runs(id)
            );
            """
        )
        for run_id, parent_id, snapshot_id in (
            ("parent", None, "snapshot-parent"),
            ("child", "parent", "snapshot-child"),
        ):
            db.execute(
                """INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, f"request-{run_id}", "fingerprint", "root-task",
                    parent_id, "snapshot-parent" if parent_id else None,
                    SECRET, "synthetic", '["agent-synthesis"]', "completed",
                    snapshot_id, None, 0.0, 0.0, None,
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            snapshot = {
                "agents": [{"id": "agent-synthesis", "name": "Sage", "role": "reviewer"}],
                "attempts": [{"id": "attempt-1", "agentId": "agent-synthesis",
                              "taskId": "peer-task", "skillVersions": ["evidence-linking@1.0.2"]}],
                "sources": [{"id": "source-1", "provider": "user_supplied",
                             "version": 1, "contentHash": "abc", "retrievalStatus": "supplied",
                             "title": SECRET, "passages": [{"text": SECRET}]}],
            }
            db.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?)",
                (snapshot_id, run_id, _json(snapshot), "2026-01-01T00:00:01+00:00"),
            )

        def tool(tool_id, name):
            return {"id": tool_id, "type": "function", "function": {
                "name": name, "arguments": _json({"text": SECRET})}}

        tools = [tool("tool-ok", "source_read"), tool("tool-bad", "ask_peer"),
                 tool("tool-later", "publish_result")]

        def call(call_id, number, response, messages=None, results=None, status="returned"):
            db.execute(
                """INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (call_id, "child", "attempt-1", "agent-synthesis", status,
                 "response" if status == "returned" else "provider_error",
                 _json(messages or [{"role": "user", "content": SECRET}]),
                 _json(response) if response is not None else None,
                 _json(results or []), 0.1, 0.01 if response else None,
                 None, f"2026-01-01T00:00:{number:02d}+00:00",
                 f"2026-01-01T00:00:{number:02d}.500000+00:00"),
            )

        def result(tool_id, content):
            return {"role": "tool", "tool_call_id": tool_id, "content": _json(content)}

        call("call-1", 2, {"model": "reported-model", "tool_calls": tools,
                           "usage": {"prompt_tokens": 3, "completion_tokens": 4,
                                     "total_tokens": 7}},
             results=[result("tool-ok", {"ok": True}),
                      result("tool-bad", {"error": "Rejected synthetic input"})])
        call("call-2", 3, {"model": "reported-model", "tool_calls": [tool("tool-no-result", "source_read")]},
             messages=[{"role": "assistant", "tool_calls": tools},
                       result("tool-later", {"ok": True})])
        ambiguous = tool("tool-ambiguous", "source_read")
        call("call-3", 4, {"model": "reported-model", "tool_calls": [ambiguous]})
        call("call-4", 5, {"model": "reported-model", "tool_calls": []},
             messages=[{"role": "assistant", "tool_calls": [ambiguous]},
                       result("tool-ambiguous", {"value": "A"})])
        call("call-5", 6, {"model": "reported-model", "tool_calls": []},
             messages=[{"role": "assistant", "tool_calls": [ambiguous]},
                       result("tool-ambiguous", {"value": "B"})])
        call("call-uncertain", 7, None, status="uncertain")
    return path


def _raw_rows(db: sqlite3.Connection):
    return {
        table: db.execute(
            f"SELECT {('id,run_id,attempt_id,agent_id,status,phase,messages,response,'
                       'tool_results,estimated_max_usd,actual_usd,retry_of_call_id,'
                       'created_at,completed_at') if table == 'calls' else '*'} "
            f"FROM {table} ORDER BY id"
        ).fetchall()
        for table in ("runs", "calls", "snapshots")
    }


def test_historical_rows_are_preserved_and_reconstructed_cautiously(tmp_path):
    path = _legacy_db(tmp_path)
    with sqlite3.connect(path) as db:
        before = _raw_rows(db)
        shared_version = db.execute("PRAGMA user_version").fetchone()[0]
        migrate_live_provenance(db)
        assert _raw_rows(db) == before
        assert db.execute("PRAGMA user_version").fetchone()[0] == shared_version
        assert db.execute("SELECT version FROM live_provenance_migrations").fetchall() == [(1,), (2,)]

        statuses = dict(db.execute(
            "SELECT provider_tool_call_id,status FROM live_tool_overview"
        ))
        assert statuses == {
            "tool-ok": "completed", "tool-bad": "uncertain",
            "tool-later": "completed", "tool-no-result": "requested",
            "tool-ambiguous": "uncertain",
        }
        reconstructed = db.execute(
            "SELECT output_ref,provenance_basis FROM live_tool_overview "
            "WHERE provider_tool_call_id='tool-later'"
        ).fetchone()
        assert reconstructed == ("calls:call-2.messages[1]", "reconstructed_later_message")
        assert db.execute(
            "SELECT output_ref,error_ref,provenance_basis FROM live_tool_overview "
            "WHERE provider_tool_call_id='tool-bad'"
        ).fetchone() == (
            None, "calls.tool_results[1]", "recorded_error_outcome_unknown"
        )
        assert db.execute(
            "SELECT output_ref FROM live_tool_overview WHERE provider_tool_call_id='tool-ambiguous'"
        ).fetchone()[0] is None
        call = db.execute(
            "SELECT parent_run_id,task_id,agent_name,agent_role,requested_provider,"
            "requested_model,reported_model,total_tokens,configuration_basis "
            "FROM live_call_overview WHERE call_id='call-1'"
        ).fetchone()
        assert call == (
            "parent", "peer-task", "Sage", "reviewer", None, None,
            "reported-model", 7, "unknown_historical_configuration",
        )
        assert db.execute(
            "SELECT skill_id,skill_version,provenance_basis FROM live_skill_overview "
            "WHERE call_id='call-1'"
        ).fetchone() == ("evidence-linking", "1.0.2", "historical_attempt_snapshot")
        assert db.execute(
            "SELECT source_provider,external_provider_contacted FROM live_source_overview "
            "WHERE run_id='child'"
        ).fetchone() == ("user_supplied", 0)
        assert db.execute(
            "SELECT DISTINCT status FROM tool_invocation_events ORDER BY status"
        ).fetchall() == [("requested",)]
        assert db.execute("SELECT count(*) FROM tool_invocation_events").fetchone()[0] == 5
        assert db.execute(
            "SELECT DISTINCT provenance_basis FROM tool_invocation_events"
        ).fetchall() == [("reconstructed_from_legacy_response_completed_at",)]
        for view in (
            "live_call_overview", "live_tool_overview", "live_configuration_overview",
            "live_skill_overview", "live_source_overview",
        ):
            assert SECRET not in repr(db.execute(f"SELECT * FROM {view}").fetchall())


def test_interrupted_migration_rolls_back_and_retry_is_idempotent(tmp_path):
    path = _legacy_db(tmp_path)
    with sqlite3.connect(path) as db:
        before = _raw_rows(db)

        def interrupt(point):
            if point == "live_provenance_1_after_backfill":
                raise RuntimeError("synthetic interruption")

        with pytest.raises(RuntimeError, match="synthetic interruption"):
            migrate_live_provenance(db, fault=interrupt)
        assert _raw_rows(db) == before
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='tool_invocations'"
        ).fetchone() is None
        assert "config_id" not in {row[1] for row in db.execute("PRAGMA table_info(calls)")}
        migrate_live_provenance(db)
        assert db.execute("SELECT count(*) FROM tool_invocations").fetchone()[0] == 5
        migrate_live_provenance(db)
        assert db.execute("SELECT count(*) FROM tool_invocations").fetchone()[0] == 5
        assert db.execute("SELECT count(*) FROM tool_invocation_events").fetchone()[0] == 5
        assert _raw_rows(db)["snapshots"] == before["snapshots"]


def test_version_one_copy_upgrades_events_without_inventing_start_times(tmp_path):
    path = _legacy_db(tmp_path)
    with sqlite3.connect(path) as db:
        migrate_live_provenance(db)
        # A disposable fixture of the prior live migration version.
        db.execute("DELETE FROM live_provenance_migrations WHERE version=2")
        db.execute("DROP TABLE tool_invocation_events")
        db.execute(
            "UPDATE tool_invocations SET started_at=?,completed_at=? "
            "WHERE provider_tool_call_id='tool-ok'",
            ("2026-01-01T00:00:02.600000+00:00",
             "2026-01-01T00:00:02.700000+00:00"),
        )
        db.commit()

        def interrupt(point):
            if point == "live_provenance_2_after_backfill":
                raise RuntimeError("synthetic v2 interruption")

        with pytest.raises(RuntimeError, match="synthetic v2 interruption"):
            migrate_live_provenance(db, fault=interrupt)
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='tool_invocation_events'"
        ).fetchone() is None
        assert db.execute("SELECT version FROM live_provenance_migrations").fetchall() == [(1,)]
        migrate_live_provenance(db)
        assert db.execute("SELECT version FROM live_provenance_migrations").fetchall() == [(1,), (2,)]
        assert db.execute(
            "SELECT status,count(*) FROM tool_invocation_events GROUP BY status"
        ).fetchall() == [("completed", 1), ("requested", 5), ("started", 1)]


def test_reused_provider_tool_id_does_not_create_false_reconstruction(tmp_path):
    path = _legacy_db(tmp_path)
    with sqlite3.connect(path) as db:
        response = json.loads(db.execute(
            "SELECT response FROM calls WHERE id='call-1'"
        ).fetchone()[0])
        reused = response["tool_calls"][2]
        db.execute(
            """INSERT INTO calls
               (id,run_id,attempt_id,agent_id,status,phase,messages,response,
                tool_results,estimated_max_usd,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            ("call-reused", "child", "attempt-1", "agent-synthesis", "returned",
             "response", "[]", _json({"tool_calls": [reused]}), "[]", 0.1,
             "2026-01-01T00:00:08+00:00"),
        )
        db.commit()
        migrate_live_provenance(db)
        assert db.execute(
            "SELECT status,output_ref,provenance_basis FROM live_tool_overview "
            "WHERE call_id='call-1' AND provider_tool_call_id='tool-later'"
        ).fetchone() == ("uncertain", None, "ambiguous_reused_provider_id")
        assert db.execute(
            "SELECT status,output_ref,provenance_basis FROM live_tool_overview "
            "WHERE call_id='call-reused' AND provider_tool_call_id='tool-later'"
        ).fetchone() == ("uncertain", None, "ambiguous_reused_provider_id")


def test_later_error_keeps_reconstructed_source_and_unknown_outcome(tmp_path):
    path = _legacy_db(tmp_path)
    with sqlite3.connect(path) as db:
        messages = json.loads(db.execute(
            "SELECT messages FROM calls WHERE id='call-2'"
        ).fetchone()[0])
        messages[1]["content"] = _json({"error": "synthetic tool error"})
        db.execute("UPDATE calls SET messages=? WHERE id='call-2'", (_json(messages),))
        db.commit()
        migrate_live_provenance(db)
        assert db.execute(
            "SELECT status,output_ref,error_ref,provenance_basis FROM live_tool_overview "
            "WHERE call_id='call-1' AND provider_tool_call_id='tool-later'"
        ).fetchone() == (
            "uncertain", None, "calls:call-2.messages[1]",
            "reconstructed_later_recorded_error_outcome_unknown",
        )


def test_malformed_json_and_dangling_config_remain_explicitly_unknown(tmp_path):
    path = _legacy_db(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE snapshots SET body='invalid-json' WHERE id='snapshot-child'")
        db.execute("UPDATE calls SET response='invalid-json' WHERE id='call-5'")
        db.execute("UPDATE calls SET messages='invalid-json' WHERE id='call-4'")
        db.commit()
        migrate_live_provenance(db)
        db.execute(
            "UPDATE calls SET config_id='missing-config',config_version=1 WHERE id='call-1'"
        )
        db.execute(
            "INSERT INTO records(kind,id,version,body,created_at) VALUES (?,?,?,?,?)",
            ("agent_configuration", "broken-config", 1, "invalid-json",
             "2026-01-01T00:00:09+00:00"),
        )
        db.execute(
            "UPDATE calls SET config_id='broken-config',config_version=1 WHERE id='call-2'"
        )
        for view in (
            "live_call_overview", "live_tool_overview", "live_configuration_overview",
            "live_skill_overview", "live_source_overview",
        ):
            db.execute(f"SELECT * FROM {view}").fetchall()
        assert db.execute(
            "SELECT configuration_basis,snapshot_json_status,task_id "
            "FROM live_call_overview WHERE call_id='call-1'"
        ).fetchone() == ("missing_configuration_record", "invalid", None)
        assert db.execute(
            "SELECT provenance_basis,settings_ref FROM live_configuration_overview "
            "WHERE call_id='call-2'"
        ).fetchone() == ("invalid_configuration_record", None)
        assert db.execute(
            "SELECT response_json_status,reported_model FROM live_call_overview "
            "WHERE call_id='call-5'"
        ).fetchone() == ("invalid", None)
        assert db.execute(
            "SELECT messages_json_status FROM live_call_overview WHERE call_id='call-4'"
        ).fetchone() == ("invalid",)
        assert db.execute(
            "SELECT source_id,source_provider,provenance_basis FROM live_source_overview "
            "WHERE run_id='child'"
        ).fetchall() == [(None, None, "unknown_invalid_snapshot_json")]


def test_new_record_refs_and_tool_statuses_remain_readable_without_payloads(tmp_path):
    path = _legacy_db(tmp_path)
    with sqlite3.connect(path) as db:
        migrate_live_provenance(db)
        agent = {"display_name": "New Sage", "role_id": "evidence_preparation_synthesis"}
        config = {
            "provider": "synthetic-model-provider", "model_id": "requested-model",
            "endpoint": f"https://model.invalid/{SECRET}",
            "instructions": SECRET, "instructions_sha256": "instruction-digest",
            "configuration_sha256": "configuration-digest",
            "settings": {"temperature": 0.2}, "reasoning_settings": {},
            "skills": {"evidence-linking": {
                "version": "1.0.2", "instructions_sha256": "skill-digest",
                "instructions": SECRET}},
            "permitted_tools": {"source_read": {"schema": {"description": SECRET}}},
        }
        for kind, record_id, body in (
            ("agent_instance", "instance-1", agent),
            ("agent_configuration", "config-1", config),
            ("agent_call", "call-1", {
                "skills": config["skills"], "exposed_tools": ["source_read"]}),
        ):
            db.execute(
                "INSERT INTO records(kind,id,version,body,created_at) VALUES (?,?,?,?,?)",
                (kind, record_id, 1, _json(body), "2026-01-01T00:00:08+00:00"),
            )
        db.execute(
            """UPDATE calls SET agent_instance_id='instance-1',config_id='config-1',
               config_version=1 WHERE id='call-1'"""
        )
        configuration = db.execute(
            "SELECT requested_provider,requested_model,reported_model,loaded_skill_count,"
            "available_tool_count,provenance_basis FROM live_configuration_overview "
            "WHERE call_id='call-1'"
        ).fetchone()
        assert configuration == (
            "synthetic-model-provider", "requested-model", "reported-model",
            1, 1, "recorded_configuration",
        )
        assert db.execute(
            "SELECT agent_name,agent_role FROM live_call_overview WHERE call_id='call-1'"
        ).fetchone() == ("New Sage", "evidence_preparation_synthesis")
        assert db.execute(
            "SELECT skill_id,skill_version,instructions_sha256,provenance_basis "
            "FROM live_skill_overview WHERE call_id='call-1'"
        ).fetchone() == (
            "evidence-linking", "1.0.2", "skill-digest",
            "loaded_in_recorded_call",
        )
        db.execute(
            "UPDATE tool_invocations SET status='started',started_at='2026-01-01T00:00:09+00:00' "
            "WHERE provider_tool_call_id='tool-no-result'"
        )
        db.execute(
            "UPDATE tool_invocations SET status='uncertain' "
            "WHERE provider_tool_call_id='tool-no-result'"
        )
        assert db.execute(
            "SELECT status FROM live_tool_overview WHERE provider_tool_call_id='tool-no-result'"
        ).fetchone()[0] == "uncertain"
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO tool_invocations
                   (id,run_id,attempt_id,model_call_id,provider_tool_call_id,
                    status,input_ref,provenance_basis)
                   VALUES (?,?,?,?,?,?,?,?)""",
                ("duplicate", "child", "attempt-1", "call-1", "tool-ok",
                 "requested", "calls.response.tool_calls[0]", "recorded"),
            )
        for view in (
            "live_call_overview", "live_tool_overview", "live_configuration_overview",
            "live_skill_overview", "live_source_overview",
        ):
            assert SECRET not in repr(db.execute(f"SELECT * FROM {view}").fetchall())
