"""Lossless component reuse and recovery for saved research views."""

from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ora.state import IntegrityFault, StateStore
from ora.state.demo import build_recorded_workflow
from ora.state.inspection import load_snapshot, save_snapshot
from ora.state.snapshot_storage import FORMAT
from ora.state.store import canonical

ROOT = Path(__file__).resolve().parents[2]


def _variant(saved: dict, name: str, event_count: int) -> dict:
    child = copy.deepcopy(saved["child"])
    child["snapshotId"] = name
    template = child["events"][-1]
    child["events"] = [
        {**template, "id": f"synthetic-progress-{index}", "summary": f"Progress {index}"}
        for index in range(event_count)
    ]
    return child


def _save_child(store: StateStore, saved: dict, child: dict) -> dict:
    inspection = child["inspection"]
    return save_snapshot(
        store, child["snapshotId"], child,
        source_provenance=inspection["sources"],
        attempt_provenance=inspection["attempts"],
        detail_refs=[item["reference"] for item in inspection["records"]],
        accepted_outputs=inspection["acceptedOutputReferences"],
        initial_source_ids=["A1"],
        parent_ref=saved["parent"]["inspection"]["snapshotReference"],
        followup_target=inspection["followupTarget"],
    )


def _manifest(store: StateStore, snapshot_id: str) -> dict:
    record = store.get("research_snapshot", snapshot_id)
    return json.loads(store.read_blob(record["body"]["blob"]["sha256"]))


def test_new_and_legacy_snapshot_ids_return_the_same_exact_view(tmp_path):
    saved = build_recorded_workflow(ROOT, tmp_path)
    store = StateStore(tmp_path)
    assert load_snapshot(StateStore(tmp_path, read_only=True), saved["child"]["snapshotId"]) == saved["child"]
    assert store.get("research_snapshot", saved["child"]["snapshotId"])["body"]["storage_format"] == FORMAT

    parent = saved["parent"]
    old_id = "synthetic-legacy-parent"
    full = {key: value for key, value in parent.items() if key != "inspection"}
    full["snapshotId"] = old_id
    blob = store.put_blob(canonical(full).encode())
    body = copy.deepcopy(store.get("research_snapshot", parent["snapshotId"])["body"])
    body.pop("storage_format")
    body["blob"] = blob
    body["inspection"]["snapshotReference"]["id"] = old_id
    store.transact("synthetic-legacy-record", {}, lambda tx: tx.put("research_snapshot", old_id, body))
    expected = copy.deepcopy(parent)
    expected["snapshotId"] = old_id
    expected["inspection"]["snapshotReference"]["id"] = old_id
    assert load_snapshot(StateStore(tmp_path, read_only=True), old_id) == expected


def test_repeated_progress_reuses_ordered_chunks_and_preserves_old_views(tmp_path):
    saved = build_recorded_workflow(ROOT, tmp_path)
    store = StateStore(tmp_path)
    first, second, third = (
        _variant(saved, f"progress-{count}", count) for count in (191, 192, 193)
    )
    for item in (first, second, third):
        _save_child(store, saved, item)
    manifests = [_manifest(store, item["snapshotId"]) for item in (first, second, third)]
    assert all(manifest["format"] == FORMAT for manifest in manifests)
    chunks = [manifest["arrays"]["events"] for manifest in manifests]
    assert [len(items) for items in chunks] == [3, 3, 4]
    assert chunks[0][:2] == chunks[1][:2] == chunks[2][:2]
    assert chunks[0][2] != chunks[1][2]
    assert chunks[1] == chunks[2][:3]
    assert len({pointer["id"] for group in chunks for pointer in group}) == 5
    for item in (first, second, third):
        actual = load_snapshot(store, item["snapshotId"])
        assert actual["events"] == item["events"]
        assert actual["artifacts"] == item["artifacts"]
        assert actual["inspection"]["sources"] == item["inspection"]["sources"]
    assert load_snapshot(store, saved["parent"]["snapshotId"]) == saved["parent"]


def test_failed_acceptance_leaves_no_partial_component_records_or_snapshot(tmp_path):
    saved = build_recorded_workflow(ROOT, tmp_path)
    initial = StateStore(tmp_path)
    prior_count = len(initial.records("snapshot_component"))
    child = _variant(saved, "failed-component-save", 75)

    def fail(point: str) -> None:
        if point == "before_metadata_commit":
            raise RuntimeError("synthetic interrupted commit")

    failing = StateStore(tmp_path, fault=fail)
    with pytest.raises(RuntimeError, match="interrupted commit"):
        _save_child(failing, saved, child)
    reopened = StateStore(tmp_path)
    assert reopened.get("research_snapshot", child["snapshotId"]) is None
    assert len(reopened.records("snapshot_component")) == prior_count
    assert load_snapshot(reopened, saved["child"]["snapshotId"]) == saved["child"]


def test_concurrent_saves_and_reads_never_expose_partial_views(tmp_path):
    saved = build_recorded_workflow(ROOT, tmp_path)
    store = StateStore(tmp_path)
    variants = [_variant(saved, f"parallel-{index}", 130) for index in range(2)]

    def writer(child: dict) -> dict:
        _save_child(store, saved, child)
        return load_snapshot(store, child["snapshotId"])

    def reader() -> dict:
        return load_snapshot(StateStore(tmp_path, read_only=True), saved["parent"]["snapshotId"])

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(writer, item) for item in variants]
        observed = pool.submit(reader)
        for expected, future in zip(variants, futures):
            assert future.result()["events"] == expected["events"]
        assert observed.result() == saved["parent"]
    assert StateStore(tmp_path).reconcile()["degraded_artifacts"] == []


def test_backup_restore_and_component_corruption_detection(tmp_path):
    saved = build_recorded_workflow(ROOT, tmp_path / "state")
    store = StateStore(tmp_path / "state")
    child = _variant(saved, "backup-component-view", 130)
    _save_child(store, saved, child)
    manifest = store.backup(tmp_path / "backup")
    assert len(manifest["objects"]) >= len(store.records("snapshot_component"))
    restored = StateStore.restore(tmp_path / "backup", tmp_path / "restored")
    assert restored.reconcile()["degraded_artifacts"] == []
    assert load_snapshot(restored, child["snapshotId"])["events"] == child["events"]
    component = restored.get(**_manifest(restored, child["snapshotId"])["arrays"]["events"][-1])
    digest = component["body"]["blob"]["sha256"]
    restored.blobs.path(digest).write_bytes(b"damaged synthetic component")
    assert any(item["sha256"] == digest for item in restored.reconcile()["degraded_artifacts"])
    with pytest.raises(IntegrityFault):
        load_snapshot(restored, child["snapshotId"])
