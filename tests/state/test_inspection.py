"""Verify persisted parent/child views and read-only Codex inspection agree."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ora.state import IntegrityFault, StateError, StateStore
from ora.state.demo import build_recorded_workflow
from ora.state.inspection import load_snapshot, save_snapshot

ROOT = Path(__file__).resolve().parents[2]


def test_saved_workflow_replays_exactly_and_preserves_parent(tmp_path):
    initial = build_recorded_workflow(ROOT, tmp_path)
    repeated = build_recorded_workflow(ROOT, tmp_path)
    assert repeated == initial
    parent, child = initial["parent"], initial["child"]
    assert child["parentSnapshotId"] == parent["snapshotId"]
    assert parent["artifacts"][0]["version"] == 1 and child["artifacts"][0]["version"] == 2
    assert child["artifacts"][0]["id"] == parent["artifacts"][0]["id"]
    assert child["inspection"]["sources"][0]["copyReference"] == parent["inspection"]["sources"][0]["copyReference"]
    assert child["inspection"]["initialSourceIds"] == ["A1"]
    assert parent["inspection"]["initialSourceIds"] == []
    parent_deliveries = [step for step in parent["inspection"]["steps"] if step["action"] == "Record a retrieval delivery"]
    assert len(parent_deliveries) == 1  # Later acquisitions do not leak into the original review.
    assert all(step["recordedAt"] <= parent["capturedAt"] for step in parent["inspection"]["steps"])
    request = next(message for message in child["messages"] if message["status"] == "requested")
    reply = next(message for message in child["messages"] if message["status"] == "fulfilled")
    assert request["fromAgentId"] == reply["toAgentId"]
    assert request["toAgentId"] == reply["fromAgentId"]
    assert any(attempt["status"] == "failed" for attempt in child["attempts"])
    assert any(attempt["status"] == "waiting" for attempt in child["attempts"])
    store = StateStore(tmp_path)
    assert len(store.records("agent_instance")) == 9
    for manifest in store.records("agent_call"):
        assert manifest["body"]["model_id"] == "openai/gpt-oss-120b"
    failed = next(item for item in child["inspection"]["attempts"] if item["attemptId"] == "attempt-a2-failed")
    assert failed["inputReferences"] == []
    assert all(item["returnedReasoning"] == {"availability": "absent", "text": None} for item in child["attempts"])
    included = [item["reference"] for item in child["inspection"]["records"]]
    assert all(pointer in included for step in child["inspection"]["steps"] for pointer in [*step["inputReferences"], *step["outputReferences"]])
    assert not store.records("model_response")  # Prepared configurations are not model execution.
    assert load_snapshot(StateStore(tmp_path, read_only=True), parent["snapshotId"]) == parent


def test_projection_rejects_wrong_passage_without_affecting_saved_views(tmp_path):
    saved = build_recorded_workflow(ROOT, tmp_path)
    store = StateStore(tmp_path)
    broken = copy.deepcopy(saved["child"])
    broken["snapshotId"] = "bad-projection"
    broken["sources"][0]["passages"][0]["text"] = "Unsupported replacement text"
    inspection = broken["inspection"]
    with pytest.raises(StateError, match="Passage differs"):
        save_snapshot(store, "bad", broken, source_provenance=inspection["sources"],
                      attempt_provenance=inspection["attempts"], detail_refs=[item["reference"] for item in inspection["records"]],
                      accepted_outputs=inspection["acceptedOutputReferences"], initial_source_ids=["A1"],
                      parent_ref=saved["parent"]["inspection"]["snapshotReference"], followup_target=inspection["followupTarget"])
    assert store.get("research_snapshot", "bad-projection") is None
    assert load_snapshot(store, saved["parent"]["snapshotId"]) == saved["parent"]


def test_codex_reader_uses_same_sources_without_writes_or_calls(tmp_path):
    snapshots = build_recorded_workflow(ROOT, tmp_path)
    db_before = (tmp_path / "state.sqlite3").read_bytes()
    output = subprocess.check_output([sys.executable, "-m", "ora.state.inspect", "--state-dir", str(tmp_path), "--snapshot", snapshots["child"]["snapshotId"], "--view", "sources"], text=True)
    assert json.loads(output)["sources"] == snapshots["child"]["sources"]
    assert (tmp_path / "state.sqlite3").read_bytes() == db_before
    with pytest.raises(PermissionError):
        StateStore(tmp_path, read_only=True).put_blob(b"not permitted")


def save_child_variant(store, snapshots, child):
    inspection = child["inspection"]
    return save_snapshot(store, child["snapshotId"], child,
                         source_provenance=inspection["sources"], attempt_provenance=inspection["attempts"],
                         detail_refs=[item["reference"] for item in inspection["records"]],
                         accepted_outputs=inspection["acceptedOutputReferences"], initial_source_ids=["A1"],
                         parent_ref=snapshots["parent"]["inspection"]["snapshotReference"],
                         followup_target=inspection["followupTarget"])


@pytest.mark.parametrize("defect, message", [
    ("missing_source", "Every displayed source"),
    ("wrong_encounter", "encounter that supplied"),
    ("extra_wrong_encounter", "different publication"),
    ("missing_attempt", "Every displayed attempt"),
    ("duplicate_attempt", "Every displayed attempt"),
    ("wrong_inputs", "Displayed attempt inputs"),
    ("wrong_parent_target", "not an accepted parent output"),
])
def test_rejects_misleading_lineage_without_changing_accepted_snapshots(tmp_path, defect, message):
    snapshots = build_recorded_workflow(ROOT, tmp_path)
    store = StateStore(tmp_path)
    child = copy.deepcopy(snapshots["child"])
    child["snapshotId"] = f"invalid-{defect}"
    inspection = child["inspection"]
    if defect == "missing_source":
        inspection["sources"].pop()
    elif defect == "wrong_encounter":
        inspection["sources"][0]["encounterReferences"] = inspection["sources"][1]["encounterReferences"]
    elif defect == "extra_wrong_encounter":
        inspection["sources"][0]["encounterReferences"] += inspection["sources"][1]["encounterReferences"]
    elif defect == "missing_attempt":
        inspection["attempts"].pop()
    elif defect == "duplicate_attempt":
        inspection["attempts"].append(inspection["attempts"][0])
    elif defect == "wrong_inputs":
        attempt = next(item for item in inspection["attempts"] if item["inputReferences"])
        attempt["inputReferences"] = []
    elif defect == "wrong_parent_target":
        inspection["followupTarget"] = inspection["acceptedOutputReferences"][0]
    with pytest.raises(StateError, match=message):
        save_child_variant(store, snapshots, child)
    assert store.get("research_snapshot", child["snapshotId"]) is None
    assert load_snapshot(store, snapshots["parent"]["snapshotId"]) == snapshots["parent"]
    assert load_snapshot(store, snapshots["child"]["snapshotId"]) == snapshots["child"]


def test_reopen_rejects_corrupt_dependent_bytes_but_keeps_other_work_available(tmp_path):
    snapshots = build_recorded_workflow(ROOT, tmp_path)
    store = StateStore(tmp_path)
    source_ref = snapshots["child"]["inspection"]["sources"][1]["copyReference"]
    damaged_copy = store.get(**source_ref)
    digest = damaged_copy["body"]["blob"]["sha256"]
    store.blobs.path(digest).write_bytes(b"interrupted or damaged retained content")

    reopened = StateStore(tmp_path)
    assert any(item["sha256"] == digest for item in reopened.reconcile()["degraded_artifacts"])
    child = copy.deepcopy(snapshots["child"])
    child["snapshotId"] = "dependent-after-restart"
    with pytest.raises(IntegrityFault):
        save_child_variant(reopened, snapshots, child)
    assert reopened.get("research_snapshot", child["snapshotId"]) is None
    historical = load_snapshot(reopened, snapshots["child"]["snapshotId"])
    affected = next(source for source in historical["sources"] if source["copyId"] == source_ref["id"])
    assert affected["retrievalStatus"] == "content_integrity_fault" and affected["passages"] == []
    assert historical["artifacts"] == snapshots["child"]["artifacts"]
    assert load_snapshot(reopened, snapshots["parent"]["snapshotId"]) == snapshots["parent"]
    accepted = reopened.transact("unrelated-after-restart", {}, lambda tx: tx.put("task", "unrelated", {"status": "ready"}))
    assert accepted["body"]["status"] == "ready"


def test_inspection_hides_private_instruction_and_reasoning_text(tmp_path):
    snapshots = build_recorded_workflow(ROOT, tmp_path)
    records = snapshots["child"]["inspection"]["records"]
    configurations = [item["data"] for item in records if item["reference"]["kind"] == "agent_configuration"]
    assert configurations and all("instructions" not in item for item in configurations)
    assert all("instructions" not in skill for item in configurations for skill in item["skills"].values())
    assert all(item["returnedReasoning"]["text"] is None for item in snapshots["child"]["attempts"])
