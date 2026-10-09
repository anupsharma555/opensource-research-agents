"""Exercise crash, replacement and recovery boundaries of accepted research state.

These checks use real SQLite transactions and files with controlled failures.
They distinguish local durability and recorded-data recovery from provider
reliability, scientific validity or survival of an actual machine power failure.
"""

import errno
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from ora.state import IntegrityFault, StateConflict, StateError, StateStore


def accept(store, operation="accept", *, fence=None, content=b"Depression study: uncertain estimate"):
    blob = store.put_blob(content)

    def action(tx):
        artifact = tx.put("artifact", operation, {"blob": blob, "rights": "retain"})
        claim = tx.put("claim", operation, {"text": "The estimate is uncertain"})
        request = tx.put("peer_request", operation, {"status": "requested"})
        tx.link(claim, artifact, "supports")
        tx.emit("mission", "artifact_accepted", {"artifact": artifact["id"], "request": request["id"]})
        return artifact

    return store.transact(operation, {"blob": blob}, action, mission_id="mission", fence=fence)


@pytest.mark.parametrize("point", ["before_blob_write", "during_blob_write", "after_blob_write", "after_blob_install", "before_metadata_commit", "before_outbox_write", "after_outbox_write"])
def test_failed_write_preserves_prior_work_and_recovers(tmp_path, point):
    store = StateStore(tmp_path)
    prior = accept(store, "prior")

    def fail(actual):
        if actual == point:
            raise OSError(errno.ENOSPC, "No capacity on test path")

    failing = StateStore(tmp_path, fault=fail)
    with pytest.raises(OSError):
        accept(failing, "new", content=b"Separate evidence")
    reopened = StateStore(tmp_path)
    assert reopened.get("artifact", "prior") == prior
    assert reopened.get("artifact", "new") is None
    assert reopened.get("claim", "new") is None
    assert len(reopened.events("mission")) == 1
    assert not reopened.reconcile()["degraded_artifacts"]
    accepted = accept(reopened, "new", content=b"Separate evidence")
    assert accepted["version"] == 1
    assert len(reopened.events("mission")) == 2


def test_uncertain_commit_replays_once_after_restart(tmp_path):
    def fail(point):
        if point == "after_metadata_commit":
            raise RuntimeError("Caller lost the committed response")

    store = StateStore(tmp_path, fault=fail)
    with pytest.raises(RuntimeError):
        accept(store)
    resumed = StateStore(tmp_path)
    prior = resumed.get("artifact", "accept")
    assert accept(resumed) == prior
    assert len(resumed.events("mission")) == 1
    with pytest.raises(StateConflict, match="different inputs"):
        accept(resumed, content=b"Changed request")
    assert resumed.get("artifact", "accept") == prior


def test_parallel_commit_deduplicates_bytes_but_not_encounters(tmp_path):
    store = StateStore(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as workers:
        records = list(workers.map(lambda index: accept(store, f"collector-{index}"), range(16)))
    assert len({record["id"] for record in records}) == 16
    assert len(list((tmp_path / "objects").iterdir())) == 1
    assert len(store.events("mission")) == 16
    assert store.reconcile()["database_integrity"] == "ok"


def test_expired_worker_cannot_commit_after_replacement(tmp_path):
    clock = [100.0]
    store = StateStore(tmp_path, clock=lambda: clock[0])
    old = store.claim("task", "worker-a", ttl=10, mission_id="mission", operation_id="lease-a")
    with pytest.raises(StateConflict):
        store.claim("task", "worker-b", ttl=10, mission_id="mission", operation_id="lease-early")
    clock[0] = 111.0
    new = store.claim("task", "worker-b", ttl=10, expected_generation=1, mission_id="mission", operation_id="lease-b")
    assert new["generation"] == 2
    with pytest.raises(StateConflict, match="replaced"):
        accept(store, "old-worker", fence=old)
    assert store.get("artifact", "old-worker") is None
    assert accept(store, "new-worker", fence=new)["version"] == 1


def test_outbox_repeated_delivery_keeps_request_unanswered(tmp_path):
    store = StateStore(tmp_path)
    accept(store)
    seen = set()

    def consumer(event):
        seen.add(event["id"])

    def fail(point):
        if point == "after_event_delivery":
            raise RuntimeError("Delivery acknowledgment lost")

    with pytest.raises(RuntimeError):
        StateStore(tmp_path, fault=fail).deliver(consumer)
    assert not store.events("mission")[0]["delivered"]
    assert StateStore(tmp_path).deliver(consumer) == 1
    assert len(seen) == 1
    assert store.get("peer_request", "accept")["body"]["status"] == "requested"


def test_atomic_aliases_and_exact_version_dependency_review(tmp_path):
    store = StateStore(tmp_path)
    original = accept(store)
    accept(store, "unrelated", content=b"Independent data")

    def correction(tx):
        new = tx.put("artifact", "accept", {"blob": store.put_blob(b"Corrected content")}, expected_version=1)
        tx.link(new, original, "corrects")
        tx.invalidate(original, "Corrected source")
        tx.bind("work", "doi-example", "artifact", "accept")
        return new

    replacement = store.transact("correct", {}, correction)
    assert store.get("artifact", "accept", version=1) == original
    assert store.resolve("work", "doi-example") == replacement
    assert store.review_status(store.get("claim", "accept"))
    assert not store.review_status(replacement)
    assert not store.review_status(store.get("claim", "unrelated"))
    with pytest.raises(StateConflict):
        store.transact("collision", {}, lambda tx: tx.bind("work", "doi-example", "artifact", "unrelated"))
    assert store.resolve("work", "doi-example") == replacement


def test_invalid_reference_rolls_back_and_diagnoses_only_its_operation(tmp_path):
    store = StateStore(tmp_path)
    prior = accept(store)
    with pytest.raises(IntegrityFault):
        store.transact("broken", {}, lambda tx: tx.put("artifact", "broken", {"blob": {"sha256": "a" * 64, "size": 5}}))
    assert store.get("artifact", "broken") is None
    assert store.get("artifact", "accept") == prior
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT code FROM diagnostics WHERE operation_id='broken'").fetchone()[0] == "IntegrityFault"


def test_backup_restore_migration_and_corrupt_blob_detection(tmp_path):
    store = StateStore(tmp_path / "original", schema_version=1)
    original = accept(store)
    checkpoint = tmp_path / "backup"
    store.backup(checkpoint)

    def fail(point):
        if point == "migration_2_step_0":
            raise RuntimeError("Migration interrupted")

    with pytest.raises(RuntimeError):
        StateStore(store.root, fault=fail)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    upgraded = StateStore(store.root)
    assert upgraded.get("artifact", "accept") == original
    restored = StateStore.restore(checkpoint, tmp_path / "restored")
    assert restored.get("artifact", "accept") == original
    assert restored.get("peer_request", "accept")["body"]["status"] == "requested"
    assert len(restored.events("mission")) == 1
    (restored.root / "objects" / original["body"]["blob"]["sha256"]).write_bytes(b"corrupt")
    assert len(restored.reconcile()["degraded_artifacts"]) == 1
    with pytest.raises(IntegrityFault):
        restored.read_blob(original["body"]["blob"]["sha256"])
    # Backup repair is explicit; unrelated accepted records are still inspectable.
    assert restored.get("claim", "accept")["body"]["text"]


def test_actual_sqlite_capacity_failure_rolls_back(tmp_path):
    store = StateStore(tmp_path)
    original = accept(store)

    def fill(tx):
        pages = tx.db.execute("PRAGMA page_count").fetchone()[0]
        tx.db.execute(f"PRAGMA max_page_count={pages}")
        return tx.put("oversized", "candidate", {"content": "x" * 1_000_000})

    with pytest.raises(sqlite3.OperationalError, match="full"):
        store.transact("no-space", {}, fill)
    assert StateStore(tmp_path).get("artifact", "accept") == original
    assert store.get("oversized", "candidate") is None
    assert accept(StateStore(tmp_path), "after-capacity-restored")


@pytest.mark.parametrize("boundary,accepted", [("after_blob_install", False), ("before_metadata_commit", False), ("after_metadata_commit", True)])
def test_process_termination_preserves_commit_boundary(tmp_path, boundary, accepted):
    script = '''
import os,sys
from pathlib import Path
from ora.state import StateStore
def fail(point):
    if point == sys.argv[2]: os._exit(73)
store=StateStore(Path(sys.argv[1]), fault=fail)
blob=store.put_blob(b"Checked source bytes")
def action(tx):
    record=tx.put("artifact","crash",{"blob":blob})
    tx.emit("mission","accepted",{"id":"crash"})
    return record
store.transact("crash",{},action)
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path), boundary], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 73
    store = StateStore(tmp_path)
    assert bool(store.get("artifact", "crash")) is accepted
    assert bool(store.events("mission")) is accepted
    assert not store.reconcile()["degraded_artifacts"]


def test_schema_and_data_cannot_be_silently_downgraded(tmp_path):
    StateStore(tmp_path)
    with pytest.raises(ValueError, match="Unsupported store schema"):
        StateStore(tmp_path, schema_version=1)


def test_blob_hash_paths_cannot_escape_store(tmp_path):
    store = StateStore(tmp_path)
    with pytest.raises(IntegrityFault):
        store.read_blob("../other")
    content = b"test"
    descriptor = store.put_blob(content)
    assert json.loads(json.dumps(descriptor)) == descriptor
    assert store.read_blob(descriptor["sha256"]) == content


def test_lease_replay_and_mission_isolation(tmp_path):
    clock = [100.0]
    store = StateStore(tmp_path, clock=lambda: clock[0])
    lease = store.claim("task", "peer", mission_id="mission", operation_id="claim", ttl=10)
    assert StateStore(tmp_path, clock=lambda: clock[0]).claim("task", "peer", mission_id="mission", operation_id="claim", ttl=10) == lease
    other = store.claim("task", "peer", mission_id="other", operation_id="other-claim", ttl=10)
    assert other["generation"] == lease["generation"] == 1
    with pytest.raises(StateConflict, match="different mission"):
        store.transact("cross-mission", {}, lambda tx: tx.put("output", "cross", {}), mission_id="other", fence=lease)

    def late(tx):
        record = tx.put("output", "late", {})
        clock[0] = 111.0
        return record

    with pytest.raises(StateConflict, match="expired"):
        store.transact("late", {}, late, mission_id="mission", fence=lease)
    assert store.get("output", "late") is None


def test_lost_claim_response_recovers_exact_lease(tmp_path):
    def fail(point):
        if point == "after_metadata_commit":
            raise RuntimeError("Lease response lost")

    with pytest.raises(RuntimeError):
        StateStore(tmp_path, fault=fail).claim("task", "peer", mission_id="mission", operation_id="claim", ttl=60)
    lease = StateStore(tmp_path).claim("task", "peer", mission_id="mission", operation_id="claim", ttl=60)
    assert lease["generation"] == 1


def test_interrupted_restore_never_exposes_partial_destination(tmp_path, monkeypatch):
    from ora.state.blobs import BlobStore

    store = StateStore(tmp_path / "original")
    accept(store, "a", content=b"first")
    accept(store, "b", content=b"second")
    store.backup(tmp_path / "backup")
    original = BlobStore.put
    counter = [0]

    def fail_second(self, content):
        counter[0] += 1
        if counter[0] == 2:
            raise OSError("Interrupted restore")
        return original(self, content)

    monkeypatch.setattr(BlobStore, "put", fail_second)
    with pytest.raises(OSError):
        StateStore.restore(tmp_path / "backup", tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    assert list(tmp_path.glob('.restored.restore-*'))
    monkeypatch.setattr(BlobStore, "put", original)
    restored = StateStore.restore(tmp_path / "backup", tmp_path / "restored")
    assert len(restored.records("artifact")) == 2
    assert not restored.reconcile()["degraded_artifacts"]


@pytest.mark.parametrize("limit", [-1, 0, True])
def test_delivery_and_read_pagination_cannot_become_unbounded(tmp_path, limit):
    store = StateStore(tmp_path)
    with pytest.raises(StateError):
        store.events("mission", limit=limit)
    with pytest.raises(StateError):
        store.deliver(lambda _: None, limit=limit)


def test_private_directories_and_rebuildable_indexes(tmp_path):
    tmp_path.chmod(0o755)
    store = StateStore(tmp_path)
    original = accept(store)
    cache = tmp_path / "disposable-cache.json"
    cache.write_text('{}')
    cache.unlink()
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP INDEX reverse_links")
    store.rebuild_indexes()
    assert tmp_path.stat().st_mode & 0o077 == 0
    assert store.blobs.root.stat().st_mode & 0o077 == 0
    assert StateStore(tmp_path).get("artifact", "accept") == original
    assert store.get("peer_request", "accept")["body"]["status"] == "requested"
