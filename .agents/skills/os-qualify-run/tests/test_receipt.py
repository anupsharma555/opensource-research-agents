"""Check that saved metadata distinguishes preparation, dispatch and capture."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/receipt.py"
spec = importlib.util.spec_from_file_location("ora_qualification_receipt", SCRIPT)
assert spec and spec.loader
receipt_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receipt_module)


def test_receipt_keeps_unknown_usage_and_continuing_work_distinct(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE records(kind TEXT,id TEXT,version INTEGER,body TEXT,PRIMARY KEY(kind,id,version))")
    writer.execute("CREATE TABLE heads(kind TEXT,id TEXT,version INTEGER,PRIMARY KEY(kind,id))")
    writer.execute("CREATE TABLE links(source_kind TEXT,source_id TEXT,source_version INTEGER,"
                   "target_kind TEXT,target_id TEXT,target_version INTEGER,relation TEXT)")

    def put(kind: str, record_id: str, body: dict) -> None:
        writer.execute("INSERT INTO records VALUES (?,?,?,?)",
                       (kind, record_id, 1, json.dumps(body)))
        writer.execute("INSERT INTO heads VALUES (?,?,?)", (kind, record_id, 1))

    mission = "synthetic-mission"
    run = "synthetic-run"
    def pointer(kind: str, record_id: str) -> dict:
        return {"kind": kind, "id": record_id, "version": 1}
    put("mission_run", run, {"mission_id": mission, "status": "completed",
                             "profile": {"selectedModel": "synthetic-model",
                                         "workflowVersion": "synthetic-workflow-v2"},
                             "workflow_version": "synthetic-workflow-v2"})
    put("mission_run", "second-run", {"mission_id": mission, "status": "active",
                                       "profile": {"workflowVersion": "synthetic-workflow-v2"}})
    put("mission_task_control", "control", {"mission_id": mission, "run_id": run,
                                              "task_id": "continued-task", "scope_status": "continued",
                                              "phase": "retry_at", "report_ref": pointer("report", "report"),
                                              "source_refs": [pointer("source_copy", "copy") ]})
    put("report", "report", {"mission_id": mission, "run_id": run})
    put("report", "unproved-report", {"mission_id": mission, "run_id": run})
    put("mission_task_control", "done-control", {"mission_id": mission, "run_id": run,
                                                  "task_id": "done-task", "phase": "done",
                                                  "report_ref": pointer("report", "report")})
    put("mission_task_control", "unproved-control", {"mission_id": mission, "run_id": run,
                                                      "task_id": "unproved-task", "phase": "done",
                                                      "report_ref": pointer("report", "unproved-report")})
    put("board_proposal", "done-task", {"mission_id": mission,
                                        "outcome_ref": pointer("board_outcome", "outcome")})
    put("board_outcome", "outcome", {"mission_id": mission,
                                    "result_refs": [pointer("report", "report")]})
    put("mission_task_control", "second-control", {"mission_id": mission,
                                                    "run_id": "second-run", "task_id": "second-task",
                                                    "scope_status": "required", "phase": "ready_model"})
    put("source_copy", "copy", {"publication": pointer("publication", "study"),
                                "content_sha256": "a" * 64,
                                "blob": {"sha256": "b" * 64, "size": 12}})
    for number in range(1, 4):
        call_id = f"call-{number}"
        put("agent_attempt", f"attempt-{number}", {"mission_id": mission, "run_id": run,
                                                   "status": "running" if number == 3 else "completed"})
        put("agent_call", call_id, {"mission_id": mission, "attempt_id": f"attempt-{number}",
                                    "input_refs": [pointer("source_copy", "copy")]})
        put("model_request_intent", f"intent-{number}",
            {"mission_id": mission, "call_ref": pointer("agent_call", call_id)})
    for number in (1, 2):
        put("model_dispatch_attempt", f"dispatch-{number}",
            {"mission_id": mission, "call_ref": pointer("agent_call", f"call-{number}")})
    put("model_response_receipt", "response-1", {
        "mission_id": mission, "call_ref": pointer("agent_call", "call-1"),
        "status": "tool_requested", "usage": {"prompt_tokens": 100,
            "completion_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 20}},
    })
    tool_message = json.dumps({"role": "tool", "content": json.dumps({"status": "rejected",
                                                                        "reason": "private reason"})}).encode()
    digest = hashlib.sha256(tool_message).hexdigest()
    (tmp_path / "objects").mkdir()
    (tmp_path / "objects" / digest).write_bytes(tool_message)
    put("context_tool_result", "tool-1", {"mission_id": mission,
                                            "response_ref": pointer("model_response_receipt", "response-1"),
                                            "blob": {"sha256": digest, "size": len(tool_message)}})
    put("model_response_receipt", "response-2", {
        "mission_id": mission, "call_ref": pointer("agent_call", "call-2"),
        "status": "failed", "error_kind": "provider_rate_limit", "http_status": 429,
    })
    put("capacity_operation", "capacity", {
        "mission_id": mission, "run_id": run, "kind": "model",
        "attempts": [{"status": "completed", "usage_known": True},
                     {"status": "dispatched", "usage_known": False}],
    })
    put("mail_message", "peer-message", {"mission_id": mission,
                                         "task_ref": pointer("board_proposal", "continued-task")})
    put("mail_request", "peer-request", {"mission_id": mission,
                                           "message_ref": pointer("mail_message", "peer-message"),
                                           "status": "partly_answered", "answer_refs": [pointer("mail_answer", "answer")],
                                           "reviewer_id": "reviewer", "review": {"reason": "partial evidence"},
                                           "accepted_answer_ref": None})
    put("research_snapshot", f"snapshot-{run}-first", {"mission_id": mission,
                                                       "blob": {"sha256": "c" * 64, "size": 200}})
    put("research_snapshot", "snapshot-second-run-first", {"mission_id": mission,
                                                            "blob": {"sha256": "d" * 64, "size": 300}})
    writer.execute("INSERT INTO records VALUES (?,?,?,?)", ("mission_task_control", "control", 2,
                   json.dumps({"mission_id": mission, "run_id": run, "task_id": "continued-task",
                               "scope_status": "continued", "phase": "retry_at",
                               "report_ref": pointer("report", "report"),
                               "source_refs": [pointer("source_copy", "copy")]})))
    writer.execute("UPDATE heads SET version=2 WHERE kind='mission_task_control' AND id='control'")
    writer.commit()  # Writer stays open so the read must see committed WAL pages.

    reader = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, isolation_level=None)
    reader.row_factory = sqlite3.Row
    try:
        reader.execute("PRAGMA query_only=ON")
        reader.execute("BEGIN")
        receipt = receipt_module.build_receipt(reader, db_path, [run, "second-run"])
        reader.execute("ROLLBACK")
    finally:
        reader.close()
        writer.close()

    result = receipt["runs"][0]
    assert receipt["generator"] == {
        "skill": "os-qualify-run", "skill_version": "0.3.1",
        "helper_sha256": hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
    }
    assert receipt["schema"] == "ora.codex.run_metadata_receipt.v2"
    assert len(result["prepared_call_ids"]) == 3
    assert len(result["predispatch_marked_call_ids"]) == 2
    assert len(result["captured_call_ids"]) == 2
    assert result["usage"]["responses_with_usage"] == 1
    assert result["usage"]["captured_responses_without_usage"] == 1
    assert result["usage"]["known_totals"]["prompt_tokens"] == 100
    assert result["usage"]["known_totals"]["total_tokens"] is None
    assert result["usage"]["total_tokens_known_responses"] == 0
    assert result["usage"]["optional_subset_known_counts"]["cached_tokens"] == 1
    assert result["usage"]["known_totals"]["cached_tokens"] == 20
    assert result["usage"]["known_totals"]["reasoning_tokens"] is None
    assert result["capacity_unknown_usage_attempts"] == 1
    assert result["response_statuses"] == {"failed": 1, "tool_requested": 1}
    assert result["http_statuses"] == {"429": 1}
    assert result["accepted_report_refs"] == [{"id": "report", "version": 1}]
    assert len(result["control_linked_report_refs"]) == 2
    assert result["profile"]["workflow_version"] == "synthetic-workflow-v2"
    assert result["peer_requests"]["count"] == 1
    assert result["peer_requests"]["requests_with_submitted_answers"] == 1
    assert result["peer_requests"]["submitted_answer_ref_count"] == 1
    assert result["peer_requests"]["requests_with_review"] == 1
    assert result["peer_requests"]["requests_with_fully_accepted_answer"] == 0
    assert result["peer_requests"]["historical_review_versions_assessed"] is False
    assert receipt["runs"][1]["peer_requests"]["count"] == 0
    assert all(value is None for value in receipt["runs"][1]["usage"]["known_totals"].values())
    assert result["tool_results"]["status_categories"] == {"rejected": 1}
    assert result["tool_results"]["uninspected_count"] == 0
    assert result["saved_snapshot_storage"]["unique_blob_logical_bytes"] == 200
    assert receipt["runs"][1]["saved_snapshot_storage"]["unique_blob_logical_bytes"] == 300
    assert result["control_revision_growth"]["revision_count"] == 4
    assert result["continuing_task_ids"] == ["continued-task"]
    assert result["distinct_publication_ids"] == ["study"]
    assert result["referenced_blob_storage"]["logical_bytes"] == 12 + len(tool_message)
    assert receipt["storage"]["bytes_by_file"]["wal"] > 0


def test_explicitly_reported_zero_usage_stays_zero(tmp_path: Path) -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE records(kind TEXT,id TEXT,version INTEGER,body TEXT,PRIMARY KEY(kind,id,version))")
    db.execute("CREATE TABLE heads(kind TEXT,id TEXT,version INTEGER,PRIMARY KEY(kind,id))")
    db.execute("CREATE TABLE links(source_kind TEXT,source_id TEXT,source_version INTEGER,"
               "target_kind TEXT,target_id TEXT,target_version INTEGER,relation TEXT)")

    def put(kind: str, record_id: str, body: dict) -> None:
        db.execute("INSERT INTO records VALUES (?,?,1,?)", (kind, record_id, json.dumps(body)))
        db.execute("INSERT INTO heads VALUES (?,?,1)", (kind, record_id))

    put("mission_run", "zero-run", {"mission_id": "zero-mission", "status": "active"})
    put("agent_attempt", "zero-attempt", {"mission_id": "zero-mission", "run_id": "zero-run"})
    put("agent_call", "zero-call", {"mission_id": "zero-mission", "attempt_id": "zero-attempt"})
    put("model_response_receipt", "zero-response", {
        "mission_id": "zero-mission", "call_ref": {"kind": "agent_call", "id": "zero-call", "version": 1},
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                  "prompt_tokens_details": {"cached_tokens": 0},
                  "completion_tokens_details": {"reasoning_tokens": 0}},
    })
    try:
        result = receipt_module.build_receipt(db, tmp_path / "state.sqlite3", ["zero-run"])["runs"][0]
    finally:
        db.close()
    assert result["usage"]["known_totals"] == {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cached_tokens": 0, "reasoning_tokens": 0,
    }
    assert result["usage"]["total_tokens_known_responses"] == 1
    assert result["usage"]["optional_subset_known_counts"] == {
        "cached_tokens": 1, "reasoning_tokens": 1,
    }
    assert result["saved_snapshot_storage"]["logical_reference_bytes"] == 0
    assert result["saved_snapshot_storage"]["unique_blob_logical_bytes"] == 0


def test_snapshot_storage_counts_legacy_and_shared_components_without_blob_reads(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE records(kind TEXT,id TEXT,version INTEGER,body TEXT,PRIMARY KEY(kind,id,version))")
    writer.execute("CREATE TABLE heads(kind TEXT,id TEXT,version INTEGER,PRIMARY KEY(kind,id))")
    writer.execute("CREATE TABLE links(source_kind TEXT,source_id TEXT,source_version INTEGER,"
                   "target_kind TEXT,target_id TEXT,target_version INTEGER,relation TEXT)")

    def put(kind: str, record_id: str, body: dict) -> None:
        writer.execute("INSERT INTO records VALUES (?,?,1,?)", (kind, record_id, json.dumps(body)))
        writer.execute("INSERT INTO heads VALUES (?,?,1)", (kind, record_id))

    def link(snapshot_id: str, component_id: str) -> None:
        writer.execute("INSERT INTO links VALUES ('research_snapshot',?,1,'snapshot_component',?,1,'snapshot_part')",
                       (snapshot_id, component_id))

    run_id = "mixed-run"
    mission_id = "mixed-mission"
    put("mission_run", run_id, {"mission_id": mission_id, "status": "completed"})
    legacy_id = f"snapshot-{run_id}-legacy"
    first_id = f"snapshot-{run_id}-first"
    second_id = f"snapshot-{run_id}-second"
    put("research_snapshot", legacy_id, {"mission_id": mission_id,
                                         "blob": {"sha256": "a" * 64, "size": 100}})
    for snapshot_id, digest, size in ((first_id, "b" * 64, 20), (second_id, "c" * 64, 22)):
        put("research_snapshot", snapshot_id, {"mission_id": mission_id,
                                               "storage_format": "ora.snapshot.components.v1",
                                               "blob": {"sha256": digest, "size": size}})
    shared = f"snapshot-component-{'d' * 64}"
    other = f"snapshot-component-{'e' * 64}"
    put("snapshot_component", shared, {"blob": {"sha256": "d" * 64, "size": 50}})
    put("snapshot_component", other, {"blob": {"sha256": "e" * 64, "size": 30}})
    link(first_id, shared)
    link(first_id, other)
    link(second_id, shared)
    writer.commit()

    def inspect() -> dict:
        reader = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, isolation_level=None)
        reader.row_factory = sqlite3.Row
        try:
            reader.execute("PRAGMA query_only=ON")
            reader.execute("BEGIN")
            packet = receipt_module.build_receipt(reader, db_path, [run_id])
            json.dumps(packet)  # The CLI must be able to save every diagnostic field.
            result = packet["runs"][0]
            reader.execute("ROLLBACK")
            return result["saved_snapshot_storage"]
        finally:
            reader.close()

    first = inspect()
    assert first["count"] == 3
    assert first["legacy_full_snapshot_count"] == 1
    assert first["component_format_snapshot_count"] == 2
    assert first["contributions"]["legacy_full"]["known_logical_reference_bytes"] == 100
    assert first["contributions"]["manifest"]["known_logical_reference_bytes"] == 42
    assert first["contributions"]["component"]["known_logical_reference_bytes"] == 130
    assert first["contributions"]["component"]["unique_object_bytes_from_metadata"] == 80
    assert first["contributions"]["component"]["distinct_sha256"] == ["d" * 64, "e" * 64]
    assert first["logical_reference_bytes"] == 272
    assert first["unique_blob_hashes"] == 5
    assert first["distinct_referenced_object_sha256"] == [value * 64 for value in "abcde"]
    assert first["unique_blob_logical_bytes"] == 222
    assert first["physical_bytes_verified"] is False

    link(second_id, f"snapshot-component-{'f' * 64}")  # Exact link, absent target record.
    conflict = f"snapshot-component-{'g' * 64}"
    put("snapshot_component", conflict, {"blob": {"sha256": "d" * 64, "size": 51}})
    link(second_id, conflict)  # Same content hash with conflicting size and identity.
    writer.commit()
    unresolved = inspect()
    future_id = f"snapshot-{run_id}-future"
    put("research_snapshot", future_id, {"mission_id": mission_id,
                                         "storage_format": "ora.snapshot.future.v2",
                                         "blob": {"sha256": "9" * 64, "size": 0}})
    bad = f"snapshot-component-{'8' * 64}"
    put("snapshot_component", bad, {"blob": {"sha256": "8" * 64, "size": "unknown"}})
    link(first_id, bad)
    writer.commit()
    malformed = inspect()
    writer.close()
    assert unresolved["logical_reference_bytes"] is None
    assert unresolved["unique_blob_logical_bytes"] is None
    assert unresolved["known_logical_reference_bytes"] == 272
    assert unresolved["issues"]["missing_component_record"] == 1
    assert unresolved["issues"]["component_identity_mismatch"] == 1
    assert unresolved["issues"]["conflicting_size_for_hash"] == 1
    assert malformed["unknown_format_snapshot_count"] == 1
    assert malformed["missing_blob_size_count"] == 1
    assert malformed["contributions"]["unclassified"]["known_logical_reference_bytes"] == 0
    assert malformed["logical_reference_bytes"] is None
