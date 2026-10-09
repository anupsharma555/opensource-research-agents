"""Keep new evidence dependencies sound without stopping unrelated research work."""

import json
import sqlite3

import pytest

from ora.state import IntegrityFault, StateStore


def test_corrupt_upstream_copy_rejects_only_new_dependent_work(tmp_path):
    store = StateStore(tmp_path)
    content = b"The study reports an uncertain result."
    blob = store.put_blob(content)

    def seed(tx):
        source = tx.put("source_copy", "source", {"blob": blob})
        passage = tx.put("source_passage", "passage", {"text": content.decode()})
        tx.link(passage, source, "depends_on")
        return passage

    passage = store.transact("seed", {}, seed)
    store.blobs.path(blob["sha256"]).write_bytes(b"changed bytes")
    reopened = StateStore(tmp_path)

    def accept_claim(tx):
        claim = tx.put("claim", "dependent", {"text": "A new interpretation"})
        tx.link(claim, passage, "cites")
        tx.emit("mission", "claim_saved", {"id": claim["id"]})
        return claim

    with pytest.raises(IntegrityFault, match="hash check"):
        reopened.transact("new-claim", {}, accept_claim, mission_id="mission")
    assert reopened.get("claim", "dependent") is None
    assert reopened.events("mission") == []
    assert reopened.get("source_passage", "passage") == passage
    with sqlite3.connect(reopened.db_path) as db:
        detail = json.loads(db.execute(
            "SELECT detail FROM diagnostics WHERE operation_id='new-claim'",
        ).fetchone()[0])
    assert detail["phase"] == "dependency_integrity"
    assert detail["storage_path"] == f"objects/{blob['sha256']}"

    # New ideas and repair notices remain savable while one source needs repair.
    def independent(tx):
        note = tx.put("research_note", "alternative", {
            "hypotheses": [{"weight": 0.75, "inputs": [None, {"value": 1.2}]}],
        })
        notice = tx.put("source_notice", "repair", {"status": "needs_repair"})
        tx.link(notice, reopened.get("source_copy", "source"), "corrects")
        return note

    note = reopened.transact("independent", {}, independent)
    assert note["body"]["hypotheses"][0]["inputs"][1]["value"] == 1.2
    # Explicitly restore the exact bytes; the failed operation can now retry.
    reopened.blobs.path(blob["sha256"]).write_bytes(content)
    assert reopened.transact("new-claim", {}, accept_claim, mission_id="mission")["version"] == 1
    assert len(reopened.events("mission")) == 1


def test_dependency_cycles_are_finite_and_same_blob_is_read_once(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    blob = store.put_blob(b"Shared immutable content")
    reads = []
    original_read = store.read_blob

    def read(digest):
        reads.append(digest)
        return original_read(digest)

    monkeypatch.setattr(store, "read_blob", read)

    def action(tx):
        left = tx.put("artifact", "left", {"blob": blob})
        right = tx.put("artifact", "right", {"blob": blob})
        tx.link(left, right, "depends_on")
        tx.link(right, left, "depends_on")
        report = tx.put("report", "report", {"text": "Uses the shared material"})
        tx.link(report, right, "cites")
        return report

    assert store.transact("cycle", {}, action)["id"] == "report"
    assert reads == [blob["sha256"]]


def test_measured_read_preserves_exact_versions_and_absence(tmp_path):
    store = StateStore(tmp_path)
    first = store.transact("first", {}, lambda tx: tx.put("note", "idea", {"value": [1.25, None]}))
    store.transact("second", {}, lambda tx: tx.put("note", "idea", {"value": [2.5]}, expected_version=1))
    measured = store.get_measured("note", "idea", version=1)
    assert measured["record"] == first
    for key in ("connection_ms", "index_ms", "decode_ms"):
        assert 0 <= measured[key] <= measured["elapsed_ms"]
    assert store.get_measured("note", "missing")["record"] is None
