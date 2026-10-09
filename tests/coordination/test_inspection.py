"""One saved Stage 3 fixture is shared by Atlas JSON and a read-only CLI."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from ora.coordination.demo import PRIVATE_MARKER, ROOT, build_fixture
from ora.coordination.inspection import read_stage3
from ora.state import StateStore


def test_concurrent_fixture_has_exact_records_without_fabricated_report(tmp_path):
    state_dir = tmp_path / "state"
    snapshot = build_fixture(ROOT, state_dir)
    store = StateStore(state_dir, read_only=True)
    assert snapshot["proofKind"] == "offline_test"
    assert snapshot["artifacts"] == []
    assert snapshot["mission"]["reportArtifactId"] is None
    assert snapshot["mission"]["status"] == "active"
    assert {item["id"] for item in snapshot["attempts"]} == {
        "attempt-a-fast", "attempt-b-slow", "attempt-c-dependent"}
    assert all(item["status"] == "completed" and item["selectedModel"] is None
               for item in snapshot["attempts"])
    assert {item["id"]: item["status"] for item in snapshot["tasks"]}["d-evidence-gap"] == "waiting"
    assert snapshot["messages"][0]["status"] == "acknowledged"
    assert store.get("mail_request", "stage3-question")["body"]["status"] == "unanswered"
    assert len(store.records("executor_artifact")) == len(store.records("executor_receipt")) == 3
    assert not store.records("report") and not store.records("agent_call")
    proof = store.get("stage3_fixture_proof", "concurrency")["body"]
    assert proof["independent_overlap_observed"] is True
    assert proof["dependent_started_before_slow_finished"] is True
    assert proof["provider_calls"] == 0
    projected = snapshot["inspection"]["records"]
    proof_view = next(item["data"] for item in projected
                      if item["reference"]["kind"] == "stage3_fixture_proof")
    assert proof_view["intervals_monotonic_ns"] == proof["intervals_monotonic_ns"]
    assert proof_view["provider_calls"] == 0
    partial = store.get("mail_request", "stage3-partial-review")
    assert partial["body"]["status"] == "partial_awaiting_review"
    assert partial["body"]["accepted_answer_ref"] is None
    partial_view = next(item["data"] for item in projected
                        if item["reference"] == {"kind": "mail_request", "id": partial["id"], "version": partial["version"]})
    assert partial_view["status"] == "partial_awaiting_review"
    answer = store.get(**partial["body"]["answer_refs"][0])
    assert answer["body"]["partial"] is True
    critique = store.get(**answer["body"]["artifact_ref"])
    critique_view = next(item["data"] for item in projected
                         if item["reference"]["id"] == critique["id"])
    assert critique_view["critique"] == critique["body"]["critique"]
    assert critique_view["proposal_ref"] == critique["body"]["proposal_ref"]
    assert critique_view["evidence_refs"] == critique["body"]["evidence_refs"]
    assert critique_view["actor_ref"] == critique["body"]["actor_ref"]
    for item in projected:
        if item["reference"]["kind"] == "capacity_operation":
            original = store.get(**item["reference"])["body"]
            assert item["data"]["phase"] == original["phase"]
            assert item["data"]["active_seconds"] == original["active_seconds"]
            assert "owner" not in item["data"] and "attempts" not in item["data"]
    refs = {tuple(item["reference"][key] for key in ("kind", "id", "version"))
            for item in snapshot["inspection"]["records"]}
    for kind in ("stage3_fixture_input", "agent_instance", "agent_configuration",
                 "executor_artifact", "executor_receipt", "board_outcome"):
        assert any(pointer[0] == kind for pointer in refs)
    for attempt in snapshot["inspection"]["attempts"]:
        assert attempt["instanceReference"]["kind"] == "agent_instance"
        assert attempt["configurationReference"]["kind"] == "agent_configuration"
        assert attempt["inputReferences"][0]["kind"] == "stage3_fixture_input"
        assert attempt["acceptedOutputReferences"] == []
    trace_steps = [step for step in snapshot["inspection"]["steps"]
                   if step["action"] == "Execute peer-selected work"]
    assert len(trace_steps) == 3
    for step in trace_steps:
        assert any(pointer["kind"] == "executor_artifact"
                   for pointer in step["outputReferences"])
        assert all(tuple(pointer[key] for key in ("kind", "id", "version")) in refs
                   for pointer in [*step["inputReferences"], *step["outputReferences"]])
    assert all("payload" not in item["data"]
               for item in snapshot["inspection"]["records"]
               if item["reference"]["kind"] == "executor_artifact")
    assert PRIVATE_MARKER not in json.dumps(snapshot)


def test_client_export_cli_pages_and_refresh_have_one_saved_basis(tmp_path):
    state_dir = tmp_path / "state"
    client_json = build_fixture(ROOT, state_dir)
    snapshot_id = client_json["snapshotId"]
    database = state_dir / "state.sqlite3"
    before = database.read_bytes()
    events_before = StateStore(state_dir, read_only=True).events(client_json["mission"]["id"])

    cli = subprocess.check_output([
        sys.executable, "-m", "ora.coordination.inspection",
        "--state-dir", str(state_dir), "--snapshot-id", snapshot_id,
        "--view", "all",
    ], cwd=ROOT, text=True)
    assert json.loads(cli) == client_json
    shared_cli = subprocess.check_output([
        sys.executable, "-m", "ora.state.inspect",
        "--state-dir", str(state_dir), "--snapshot", snapshot_id,
        "--view", "all",
    ], cwd=ROOT, text=True)
    assert json.loads(shared_cli) == client_json
    assert read_stage3(state_dir, snapshot_id) == client_json
    assert read_stage3(state_dir, snapshot_id, view="findings")["artifacts"] == []
    diagnostics = read_stage3(state_dir, snapshot_id, view="diagnostics")
    assert any(item["kind"] == "deferred_work" for item in diagnostics["diagnostics"])

    cursor, pages = 0, []
    while True:
        page = read_stage3(state_dir, snapshot_id, view="steps", after=cursor, limit=5)
        pages.extend(page["steps"])
        cursor = page["nextCursor"]
        if not page["hasMore"]:
            break
    assert pages == client_json["inspection"]["steps"]
    assert len(pages) > 5
    cli_page = subprocess.check_output([
        sys.executable, "-m", "ora.coordination.inspection",
        "--state-dir", str(state_dir), "--snapshot-id", snapshot_id,
        "--view", "steps", "--after", "0", "--limit", "5",
    ], cwd=ROOT, text=True)
    assert json.loads(cli_page)["steps"] == pages[:5]
    assert database.read_bytes() == before
    assert StateStore(state_dir, read_only=True).events(client_json["mission"]["id"]) == events_before
    assert len(StateStore(state_dir, read_only=True).records("executor_artifact")) == 3


def test_demo_needs_a_fresh_store(tmp_path):
    state_dir = tmp_path / "state"
    build_fixture(ROOT, state_dir)
    with pytest.raises(FileExistsError):
        build_fixture(ROOT, state_dir)
