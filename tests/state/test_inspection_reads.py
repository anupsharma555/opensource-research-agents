"""Keep saved-view reads efficient without serving stale or mixed evidence."""

import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ora.state import StateError, StateStore, inspection
from ora.state.demo import build_recorded_workflow
from ora.state.store import Transaction

ROOT = Path(__file__).resolve().parents[2]


def reviewable_report(store, snapshot):
    source = snapshot["inspection"]["sources"][0]["copyReference"]
    report = snapshot["inspection"]["acceptedOutputReferences"][0]
    def link(tx):
        tx.link(report, source, "depends_on")
        return report
    store.transact("link-review-source", {}, link)
    return source, report


def detail(view, pointer):
    return next(item["data"] for item in view["inspection"]["records"]
                if item["reference"] == pointer)


def test_view_reuses_record_reads_and_closes_its_connections(tmp_path, monkeypatch):
    saved = build_recorded_workflow(ROOT, tmp_path)["child"]
    store = StateStore(tmp_path, read_only=True)
    connections, reads = [], Counter()
    connect, get = store._connect, Transaction.get

    def counted_connect():
        connection = connect()
        connections.append(connection)
        return connection

    def counted_get(tx, kind, id, version=None):
        # Component reconstruction has its own bounded read session. The
        # repeated event and detail references must not repeat record queries.
        if kind != "snapshot_component":
            reads[kind, id, version] += 1
        return get(tx, kind, id, version)

    monkeypatch.setattr(store, "_connect", counted_connect)
    monkeypatch.setattr(Transaction, "get", counted_get)
    assert inspection.load_snapshot(store, saved["snapshotId"]) == saved
    assert len(saved["inspection"]["steps"]) > 10
    assert len(connections) <= 2
    assert reads and max(reads.values()) == 1
    with pytest.raises(StateError, match="unavailable"):
        inspection.load_snapshot(store, "missing-view")
    # Both successful and failed views release their transaction/connection.
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_view_is_consistent_during_write_and_next_refresh_sees_review(tmp_path, monkeypatch):
    saved = build_recorded_workflow(ROOT, tmp_path)["child"]
    writer = StateStore(tmp_path)
    source, report = reviewable_report(writer, saved)
    reader = StateStore(tmp_path, read_only=True)
    explain = inspection.explain_event
    writes = []

    def invalidate():
        def accept(tx):
            affected = tx.invalidate(source, "Source needs another check")
            event_id = tx.emit(saved["mission"]["id"], "later_review_notice", {})
            return {"affected": affected, "later_event_id": event_id}
        return writer.transact("new-review", {}, accept)

    with ThreadPoolExecutor(max_workers=1) as pool:
        def explain_during_write(store, event):
            if not writes:
                # The first snapshot/event read pins the view. A separate
                # writer can still commit while this view is being assembled.
                writes.append(pool.submit(invalidate).result(timeout=5))
            return explain(store, event)

        monkeypatch.setattr(inspection, "explain_event", explain_during_write)
        first = inspection.load_snapshot(reader, saved["snapshotId"])

    assert report in writes[0]["affected"]
    assert detail(first, report)["reviewRequired"] == []
    second = inspection.load_snapshot(reader, saved["snapshotId"])
    assert any(flag["reason"] == "Source needs another check"
               for flag in detail(second, report)["reviewRequired"])
    assert first["artifacts"] == second["artifacts"] == saved["artifacts"]
    assert first["inspection"]["steps"] == second["inspection"]["steps"]
    assert writes[0]["later_event_id"] not in {
        step["id"] for step in second["inspection"]["steps"]
    }


@pytest.mark.parametrize("damage", ["corrupt_blob", "missing_blob", "missing_record"])
def test_same_reader_refresh_does_not_mask_missing_or_damaged_source(tmp_path, damage):
    saved = build_recorded_workflow(ROOT, tmp_path)["child"]
    writer = StateStore(tmp_path)
    reader = StateStore(tmp_path, read_only=True)
    source = saved["inspection"]["sources"][0]["copyReference"]
    original = writer.get(**source)
    first = inspection.load_snapshot(reader, saved["snapshotId"])
    assert detail(first, source)["contentIntegrity"] == "verified"
    if damage == "missing_record":
        # Simulate damaged storage in this disposable fixture, never live data.
        with sqlite3.connect(writer.db_path) as db:
            db.execute("DELETE FROM records WHERE kind=? AND id=? AND version=?",
                       (source["kind"], source["id"], source["version"]))
    else:
        path = writer.blobs.path(original["body"]["blob"]["sha256"])
        if damage == "missing_blob":
            path.unlink()
        else:
            path.write_bytes(b"damaged fixture source")
    second = inspection.load_snapshot(reader, saved["snapshotId"])
    if damage == "missing_record":
        assert detail(second, source)["availability"] == "missing"
    else:
        assert detail(second, source)["contentIntegrity"] == "unavailable_or_corrupt"
        displayed = next(item for item in second["sources"] if item["copyId"] == source["id"])
        assert displayed["retrievalStatus"] == "content_integrity_fault"
        assert displayed["passages"] == []
    assert second["artifacts"] == saved["artifacts"]


def test_same_snapshot_identity_in_other_workspace_cannot_inherit_review(tmp_path):
    left = build_recorded_workflow(ROOT, tmp_path / "left")["child"]
    right = build_recorded_workflow(ROOT, tmp_path / "right")["child"]
    assert left["snapshotId"] == right["snapshotId"]
    writer = StateStore(tmp_path / "left")
    source, report = reviewable_report(writer, left)
    writer.transact("left-review", {}, lambda tx: {
        "affected": tx.invalidate(source, "Review only in left workspace"),
    })
    left_reader = StateStore(tmp_path / "left", read_only=True)
    right_reader = StateStore(tmp_path / "right", read_only=True)
    left_view = inspection.load_snapshot(left_reader, left["snapshotId"])
    right_view = inspection.load_snapshot(right_reader, right["snapshotId"])
    assert detail(left_view, report)["reviewRequired"]
    assert detail(right_view, report)["reviewRequired"] == []
    assert right_view == right
