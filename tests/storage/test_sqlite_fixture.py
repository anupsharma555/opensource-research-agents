"""Behavioral checks for the Stage 1 storage decision fixture."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from experiments.storage.sqlite_fixture import (
    accept_change,
    backup,
    blob_path,
    connect,
    integrity_state,
    run_workload,
    write_immutable,
)


class SQLiteDecisionFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ora-storage-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "state.sqlite"
        self.connection = connect(self.db_path)
        self.addCleanup(self.connection.close)

    def test_interrupted_metadata_commit_leaves_only_detectable_orphan(self) -> None:
        digest = write_immutable(self.root, b"unaccepted bytes")
        with self.assertRaisesRegex(RuntimeError, "interruption"):
            accept_change(self.connection, self.root, suffix="failed", digest=digest,
                          fail_before_commit=True)
        for table in ("source_encounter", "source_copy", "message", "claim", "artifact_ref", "event"):
            self.assertEqual(self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(integrity_state(self.connection, self.root),
                         {"missing_accepted": [], "orphan_files": [digest]})

    def test_same_bytes_keep_distinct_encounters_and_missing_accepted_blob_is_visible(self) -> None:
        digest = write_immutable(self.root, b"shared bytes")
        self.assertEqual(write_immutable(self.root, b"shared bytes"), digest)
        accept_change(self.connection, self.root, suffix="a", digest=digest)
        accept_change(self.connection, self.root, suffix="b", digest=digest)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM source_encounter").fetchone()[0], 2)
        self.assertEqual(self.connection.execute("SELECT count(DISTINCT content_hash) FROM source_copy").fetchone()[0], 1)
        blob_path(self.root, digest).unlink()
        self.assertEqual(integrity_state(self.connection, self.root),
                         {"missing_accepted": [digest], "orphan_files": []})
        self.connection.execute("DELETE FROM artifact_ref")
        self.assertEqual(integrity_state(self.connection, self.root)["missing_accepted"], [digest])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM claim").fetchone()[0], 2)

    def test_wal_snapshot_and_single_writer_contention(self) -> None:
        digest = write_immutable(self.root, b"concurrency bytes")
        reader = connect(self.db_path)
        blocked_writer = connect(self.db_path, timeout=0)
        self.addCleanup(reader.close)
        self.addCleanup(blocked_writer.close)
        reader.execute("BEGIN")
        self.assertEqual(reader.execute("SELECT count(*) FROM claim").fetchone()[0], 0)
        self.connection.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
            blocked_writer.execute("BEGIN IMMEDIATE")
        self.connection.execute("ROLLBACK")
        accept_change(self.connection, self.root, suffix="committed", digest=digest)
        self.assertEqual(reader.execute("SELECT count(*) FROM claim").fetchone()[0], 0)
        reader.execute("COMMIT")
        self.assertEqual(reader.execute("SELECT count(*) FROM claim").fetchone()[0], 1)

    def test_backup_restore_and_cache_loss_keep_authoritative_links(self) -> None:
        digest = write_immutable(self.root, b"retained bytes")
        accept_change(self.connection, self.root, suffix="retained", digest=digest)
        self.connection.execute("INSERT INTO lookup_cache VALUES (?, ?)", ("shortcut", "temporary"))
        self.connection.execute("DELETE FROM lookup_cache")
        destination = self.root / "backup"
        backup(self.connection, self.root, destination)
        with connect(destination / "state.sqlite") as restored:
            row = restored.execute(
                "SELECT message.body, claim.text, source_copy.content_hash FROM claim "
                "JOIN message USING(message_id) JOIN source_copy USING(copy_id)"
            ).fetchone()
            self.assertEqual(row, ("Please check the new source.", "Synthetic finding", digest))
            self.assertEqual(integrity_state(restored, destination),
                             {"missing_accepted": [], "orphan_files": []})
            self.assertEqual(restored.execute("SELECT count(*) FROM lookup_cache").fetchone()[0], 0)

    def test_bounded_growth_and_lookup_smoke(self) -> None:
        result = run_workload(event_count=200)
        self.assertEqual(result["events_inserted"], 200)
        self.assertEqual(result["claims_inserted"], 2)
        self.assertEqual(result["source_claim_lookups"], 200)
        self.assertGreater(result["bytes_on_disk_including_wal"], 0)


if __name__ == "__main__":
    unittest.main()
