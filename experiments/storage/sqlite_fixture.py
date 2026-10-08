"""Small, synthetic SQLite/file-store fixture for the Stage 1 storage decision.

This is evidence for a design choice, not the application's storage layer. All
paths are supplied by the caller; the command-line run uses a temporary folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS source_encounter (
    encounter_id TEXT PRIMARY KEY, provider TEXT NOT NULL, locator TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_copy (
    copy_id TEXT PRIMARY KEY, encounter_id TEXT NOT NULL REFERENCES source_encounter,
    version TEXT NOT NULL, content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message (
    message_id TEXT PRIMARY KEY, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claim (
    claim_id TEXT PRIMARY KEY, copy_id TEXT NOT NULL REFERENCES source_copy,
    message_id TEXT NOT NULL REFERENCES message, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_ref (
    artifact_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES claim,
    content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event (
    event_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, entity_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lookup_cache (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""


def connect(db_path: Path, *, timeout: float = 1.0) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=timeout, isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(SCHEMA)
    return connection


def blob_path(root: Path, digest: str) -> Path:
    return root / "blobs" / digest[:2] / digest


def write_immutable(root: Path, content: bytes) -> str:
    """Stage, sync, and rename bytes before metadata acceptance.

    A second write of the same bytes uses the same path; encounter/copy identity
    stays in SQLite. This fixture assumes one trusted local filesystem.
    """
    digest = hashlib.sha256(content).hexdigest()
    destination = blob_path(root, digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise ValueError("existing blob does not match its hash")
        return digest
    fd, staged = tempfile.mkstemp(prefix=".staged-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
    return digest


def accept_change(
    connection: sqlite3.Connection,
    root: Path,
    *,
    suffix: str,
    digest: str,
    fail_before_commit: bool = False,
) -> None:
    """Accept one synthetic encounter, copy, message, claim and artifact."""
    path = blob_path(root, digest)
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("artifact bytes unavailable or corrupt")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO source_encounter VALUES (?, ?, ?)",
            (f"enc-{suffix}", "synthetic-provider", f"synthetic://{suffix}"),
        )
        connection.execute(
            "INSERT INTO source_copy VALUES (?, ?, ?, ?)",
            (f"copy-{suffix}", f"enc-{suffix}", "v1", digest),
        )
        connection.execute(
            "INSERT INTO message VALUES (?, ?)",
            (f"msg-{suffix}", "Please check the new source."),
        )
        connection.execute(
            "INSERT INTO claim VALUES (?, ?, ?, ?)",
            (f"claim-{suffix}", f"copy-{suffix}", f"msg-{suffix}", "Synthetic finding"),
        )
        connection.execute(
            "INSERT INTO artifact_ref VALUES (?, ?, ?)",
            (f"artifact-{suffix}", f"claim-{suffix}", digest),
        )
        connection.execute(
            "INSERT INTO event(kind, entity_id) VALUES (?, ?)",
            ("accepted_change", f"claim-{suffix}"),
        )
        if fail_before_commit:
            raise RuntimeError("simulated interruption before metadata commit")
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def integrity_state(connection: sqlite3.Connection, root: Path) -> dict[str, list[str]]:
    accepted = {
        row[0] for row in connection.execute(
            "SELECT content_hash FROM artifact_ref UNION SELECT content_hash FROM source_copy"
        )
    }
    missing = sorted(
        digest for digest in accepted
        if not blob_path(root, digest).is_file()
        or hashlib.sha256(blob_path(root, digest).read_bytes()).hexdigest() != digest
    )
    present = {path.name for path in (root / "blobs").glob("*/*") if path.is_file()}
    return {"missing_accepted": missing, "orphan_files": sorted(present - accepted)}


def backup(connection: sqlite3.Connection, source_root: Path, backup_root: Path) -> None:
    """Snapshot SQLite, then verify/copy every referenced immutable blob.

    A real host must coordinate this with concurrent blob creation and retention;
    the fixture takes a quiescent point after accepted writes.
    """
    backup_root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(backup_root / "state.sqlite"))) as destination:
        connection.backup(destination)
        digests = [
            row[0] for row in destination.execute(
                "SELECT content_hash FROM artifact_ref UNION SELECT content_hash FROM source_copy"
            )
        ]
    for digest in digests:
        content = blob_path(source_root, digest).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("cannot back up a missing or corrupt accepted blob")
        write_immutable(backup_root, content)


def run_workload(event_count: int = 300) -> dict[str, object]:
    """Report observed local lookup/growth data; not a service-level target."""
    if event_count < 1:
        raise ValueError("event_count must be positive")
    with tempfile.TemporaryDirectory(prefix="ora-storage-fixture-") as temporary:
        root = Path(temporary)
        with closing(connect(root / "state.sqlite")) as connection:
            digest = write_immutable(root, b"synthetic source bytes")
            claim_count = min(1000, max(1, event_count // 100))
            for index in range(claim_count):
                accept_change(connection, root, suffix=str(index), digest=digest)
            start = time.perf_counter()
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "INSERT INTO event(kind, entity_id) VALUES (?, ?)",
                (("progress", f"step-{index}") for index in range(event_count)),
            )
            connection.execute("COMMIT")
            insert_seconds = time.perf_counter() - start
            start = time.perf_counter()
            for index in range(event_count):
                row = connection.execute(
                    "SELECT claim.text, source_copy.content_hash FROM claim "
                    "JOIN source_copy USING(copy_id) WHERE claim.claim_id=?",
                    (f"claim-{index % claim_count}",),
                ).fetchone()
                assert row == ("Synthetic finding", digest)
            lookup_seconds = time.perf_counter() - start
            bytes_on_disk = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
            return {
                "sqlite_version": sqlite3.sqlite_version,
                "events_inserted": event_count,
                "claims_inserted": claim_count,
                "event_insert_seconds": round(insert_seconds, 6),
                "source_claim_lookups": event_count,
                "lookup_seconds": round(lookup_seconds, 6),
                "bytes_on_disk_including_wal": bytes_on_disk,
                "scope": "one local temporary SQLite database and one synthetic immutable blob",
            }


if __name__ == "__main__":
    print(json.dumps(run_workload(), indent=2, sort_keys=True))
