"""Accept versioned research records, file references and events together.

SQLite owns accepted state; immutable files own retained bytes. Short local
transactions reject stale ownership and changed retries without holding locks
across provider work. Event delivery is repeatable, while acknowledgments never
imply that a scientific request was answered. Domain modules validate meaning.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .blobs import BlobStore, IntegrityFault, sync_directory
from .schema import CURRENT_VERSION, migrate


class StateError(ValueError):
    """A scoped durable operation could not be accepted."""


class StateConflict(StateError):
    """The saved owner, version or operation input differs from this request."""


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def reference(record: dict) -> tuple[str, str, int]:
    return record["kind"], record["id"], record["version"]


def unpack(row) -> dict | None:
    if row is None:
        return None
    record = dict(row)
    record["body"] = json.loads(record["body"])
    return record


def _record_row(db: sqlite3.Connection, kind: str, id: str, version: int | None):
    if version is None:
        return db.execute(
            "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) WHERE r.kind=? AND r.id=?",
            (kind, id),
        ).fetchone()
    return db.execute(
        "SELECT * FROM records WHERE kind=? AND id=? AND version=?", (kind, id, version),
    ).fetchone()


_DEPENDENCY_RELATIONS = ("depends_on", "supports", "challenges", "input", "cites", "derived_from")


def identifier(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise StateError("Record identity must be nonempty text without control characters")


class Transaction:
    """One short atomic acceptance boundary; do not keep or share its connection."""

    def __init__(self, store: StateStore, db: sqlite3.Connection, operation_id: str | None = None):
        self.store, self.db = store, db
        self.operation_id = operation_id
        self.has_origins = store.schema_version >= 3
        self.phase = "acceptance"
        self.failure_storage_path: str | None = None
        self._verified_blobs: dict[str, int] = {}

    def get(self, kind: str, id: str, version: int | None = None) -> dict | None:
        return unpack(_record_row(self.db, kind, id, version))

    def _verify_blob(self, blob) -> None:
        if not isinstance(blob, dict) or set(blob) != {"sha256", "size"}:
            raise IntegrityFault("Blob reference needs an exact hash and byte count")
        digest = blob["sha256"]
        self.store.blobs.path(digest)  # Validate before using an arbitrary value as a cache key.
        if digest not in self._verified_blobs:
            try:
                self._verified_blobs[digest] = len(self.store.read_blob(digest))
            except IntegrityFault:
                self.failure_storage_path = f"objects/{digest}"
                raise
        if type(blob["size"]) is not int or blob["size"] != self._verified_blobs[digest]:
            raise IntegrityFault("Blob byte count does not match accepted content")

    def put(self, kind: str, id: str, body: dict, *, expected_version: int = 0) -> dict:
        identifier(kind)
        identifier(id)
        if not isinstance(body, dict) or type(expected_version) is not int or expected_version < 0:
            raise StateError("Record body and expected version are invalid")
        current = self.get(kind, id)
        if (current["version"] if current else 0) != expected_version:
            raise StateConflict("Saved record version changed")
        blob = body.get("blob")
        if blob is not None:
            self._verify_blob(blob)
        created = self.store.now()
        encoded = canonical(body)
        version = expected_version + 1
        self.phase = "record_write"
        self.db.execute("INSERT INTO records VALUES (?,?,?,?,?)", (kind, id, version, encoded, created))
        if self.has_origins:
            context = self.store.record_context
            self.db.execute("INSERT INTO record_origins VALUES (?,?,?,?,?,?,?,?)", (
                kind, id, version, "json-object.v1", self.operation_id,
                context.get("proof_basis"), context.get("timing_basis"), context.get("implementation_ref"),
            ))
        self.db.execute(
            "INSERT INTO heads VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET version=excluded.version",
            (kind, id, version),
        )
        return {"kind": kind, "id": id, "version": version, "body": json.loads(encoded), "created_at": created}

    def resolve(self, namespace: str, value: str) -> dict | None:
        row = self.db.execute("SELECT kind,id FROM aliases WHERE namespace=? AND value=?", (namespace, value)).fetchone()
        return self.get(row["kind"], row["id"]) if row else None

    def bind(self, namespace: str, value: str, kind: str, id: str) -> None:
        identifier(namespace)
        identifier(value)
        existing = self.db.execute("SELECT kind,id FROM aliases WHERE namespace=? AND value=?", (namespace, value)).fetchone()
        if existing:
            if tuple(existing) != (kind, id):
                raise StateConflict("Identifier is already bound to a different record")
            return
        self.db.execute("INSERT INTO aliases VALUES (?,?,?,?)", (namespace, value, kind, id))

    def link(self, source: dict, target: dict, relation: str) -> None:
        identifier(relation)
        if relation in _DEPENDENCY_RELATIONS:
            self._verify_dependency(target)
        self.phase = "dependency_write"
        self.db.execute("INSERT OR IGNORE INTO links VALUES (?,?,?,?,?,?,?)", (*reference(source), *reference(target), relation))

    def _verify_dependency(self, target: dict) -> None:
        """Check retained bytes only along this new dependency's existing ancestry.

        Old records stay readable for repair and historical review. A damaged
        dependency rejects this transaction; unrelated branches need no scan or
        approval. Each content object is read once per short transaction.
        """
        self.phase = "dependency_integrity"
        pending, seen = [reference(target)], set()
        placeholders = ",".join("?" for _ in _DEPENDENCY_RELATIONS)
        while pending:
            pointer = pending.pop()
            if pointer in seen:
                continue
            seen.add(pointer)
            record = self.get(*pointer)
            if record is None:
                raise IntegrityFault("Dependent work references a missing record version")
            if record["body"].get("blob") is not None:
                self._verify_blob(record["body"]["blob"])
            rows = self.db.execute(
                "SELECT target_kind,target_id,target_version FROM links "
                f"WHERE source_kind=? AND source_id=? AND source_version=? AND relation IN ({placeholders})",
                (*pointer, *_DEPENDENCY_RELATIONS),
            ).fetchall()
            pending.extend(tuple(row) for row in rows)

    def invalidate(self, record: dict, reason: str) -> list[dict]:
        """Mark exact dependent versions for review, without rewriting old reports."""
        identifier(reason)
        origin = reference(record)
        pending, seen, affected = [origin], {origin}, []
        while pending:
            target = pending.pop()
            rows = self.db.execute(
                "SELECT source_kind,source_id,source_version FROM links WHERE target_kind=? AND target_id=? AND target_version=? AND relation IN ('depends_on','supports','challenges','input','cites','derived_from')",
                target,
            ).fetchall()
            for row in rows:
                dependent = tuple(row)
                if dependent in seen:
                    continue
                seen.add(dependent)
                pending.append(dependent)
                self.db.execute("INSERT OR IGNORE INTO review_requirements VALUES (?,?,?,?,?,?,?)", (*dependent, reason, *origin))
                affected.append(dict(zip(("kind", "id", "version"), dependent)))
        return affected

    def emit(self, mission_id: str, event_type: str, body: dict) -> str:
        identifier(event_type)
        event_id = f"event-{uuid.uuid4().hex}"
        self.phase = "outbox_write"
        self.store.fault("before_outbox_write")
        self.db.execute("INSERT INTO events(id,mission_id,event_type,body,created_at) VALUES (?,?,?,?,?)", (event_id, mission_id, event_type, canonical(body), self.store.now()))
        self.store.fault("after_outbox_write")
        return event_id


class StateStore:
    """A private local store shared by agent, retrieval and evidence components."""

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time, fault=None, schema_version: int = CURRENT_VERSION, read_only: bool = False, database_name: str = "state.sqlite3", record_context: dict | None = None):
        if not isinstance(database_name, str) or not database_name or Path(database_name).name != database_name or database_name in {".", ".."}:
            raise StateError("Database name must be a filename within the state directory")
        self.record_context = dict(record_context or {})
        if set(self.record_context) - {"proof_basis", "timing_basis", "implementation_ref"} or any(not isinstance(value, str) or not value for value in self.record_context.values()):
            raise StateError("Record context needs named proof, timing or implementation references")
        self.root = Path(root).resolve()
        self.read_only = read_only
        if not read_only:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root.chmod(0o700)
        self.db_path = self.root / database_name
        self.clock = clock
        self.fault = fault or (lambda _point: None)
        self.blobs = BlobStore(self.root / "objects", self.fault, read_only=read_only)
        db = self._connect()
        try:
            if read_only:
                saved_version = db.execute("PRAGMA user_version").fetchone()[0]
                if saved_version != schema_version and not (schema_version == CURRENT_VERSION and saved_version == 2):
                    raise StateError("Inspecting this store requires a compatible schema version")
            else:
                migrate(db, schema_version, self.now(), self.fault)
            self.schema_version = db.execute("PRAGMA user_version").fetchone()[0]
        finally:
            db.close()
        if not read_only:
            self.db_path.chmod(0o600)

    def now(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()

    def _connect(self) -> sqlite3.Connection:
        location = f"{self.db_path.as_uri()}?mode=ro" if self.read_only else self.db_path
        db = sqlite3.connect(location, uri=self.read_only, isolation_level=None, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        if not self.read_only:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
        return db

    def get(self, kind: str, id: str, version: int | None = None) -> dict | None:
        db = self._connect()
        try:
            return Transaction(self, db).get(kind, id, version)
        finally:
            db.close()

    def get_measured(self, kind: str, id: str, version: int | None = None) -> dict:
        """Read one exact record with observed connection, query and decode times.

        The indexed-query measurement includes SQLite fetch and disk work. It is
        a diagnostic, not a timeout or a claim about isolated index CPU cost.
        """
        started = time.perf_counter()
        db = self._connect()
        connected = time.perf_counter()
        try:
            row = _record_row(db, kind, id, version)
            fetched = time.perf_counter()
            record = unpack(row)
            decoded = time.perf_counter()
        finally:
            db.close()
        return {
            "record": record,
            "connection_ms": (connected - started) * 1000,
            "index_ms": (fetched - connected) * 1000,
            "decode_ms": (decoded - fetched) * 1000,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
        }

    def resolve(self, namespace: str, value: str) -> dict | None:
        db = self._connect()
        try:
            return Transaction(self, db).resolve(namespace, value)
        finally:
            db.close()

    def records(self, kind: str, *, limit: int = 1000, after_id: str = "") -> list[dict]:
        if type(limit) is not int or limit <= 0:
            raise StateError("Page size must be positive")
        db = self._connect()
        try:
            return [unpack(row) for row in db.execute(
                "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) WHERE r.kind=? AND r.id>? ORDER BY r.id LIMIT ?", (kind, after_id, limit),
            )]
        finally:
            db.close()

    def transact(self, operation_id: str, request: dict, action: Callable[[Transaction], dict], *, mission_id: str = "", fence: dict | None = None) -> dict:
        identifier(operation_id)
        digest = hashlib.sha256(canonical({"request": request, "mission_id": mission_id, "fence": fence}).encode()).hexdigest()
        db = self._connect()
        committed = False
        tx = Transaction(self, db, operation_id)
        try:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if previous:
                if previous["request_hash"] != digest:
                    raise StateConflict("Operation ID was already used with different inputs")
                db.rollback()
                return json.loads(previous["result"])
            self._check_fence(db, fence, mission_id)
            result = action(tx)
            if not isinstance(result, dict):
                raise StateError("Transaction result must be a record object")
            tx.phase = "metadata_commit"
            self.fault("before_metadata_commit")
            self._check_fence(db, fence, mission_id)
            db.execute("INSERT INTO operations VALUES (?,?,?,?)", (operation_id, digest, canonical(result), self.now()))
            db.commit()
            committed = True
            self.fault("after_metadata_commit")
            return result
        except BaseException as exc:
            if not committed:
                db.rollback()
                # The failed request's body may contain restricted content. Save
                # the failure class and operation identity, not arbitrary text.
                try:
                    db.execute("INSERT INTO diagnostics(operation_id,code,detail,created_at) VALUES (?,?,?,?)", (operation_id, type(exc).__name__, canonical({"phase": tx.phase, "storage_path": tx.failure_storage_path or self.db_path.name, "retryable": isinstance(exc, (OSError, sqlite3.OperationalError))}), self.now()))
                except sqlite3.Error:
                    pass  # Shared storage failure also prevents a durable diagnostic.
            raise
        finally:
            db.close()

    def _check_fence(self, db, fence, mission_id):
        if fence is None:
            return
        if not mission_id or fence.get("mission_id") != mission_id:
            raise StateConflict("Worker lease belongs to a different mission")
        lease = db.execute("SELECT * FROM leases WHERE mission_id=? AND task_id=?", (mission_id, fence["task_id"])).fetchone()
        if not lease or lease["owner"] != fence["owner"] or lease["generation"] != fence["generation"] or lease["expires_at"] <= self.clock():
            raise StateConflict("Worker lease expired or was replaced")

    def claim(self, task_id: str, owner: str, *, mission_id: str, operation_id: str, ttl: float, expected_generation: int | None = None) -> dict:
        identifier(task_id)
        identifier(owner)
        identifier(mission_id)
        if not math.isfinite(ttl) or ttl <= 0:
            raise StateError("Lease duration must be finite and positive")
        def action(tx):
            previous = tx.db.execute("SELECT * FROM leases WHERE mission_id=? AND task_id=?", (mission_id, task_id)).fetchone()
            generation = previous["generation"] if previous else 0
            if expected_generation is not None and generation != expected_generation:
                raise StateConflict("Worker ownership generation changed")
            if previous and previous["expires_at"] > self.clock() and previous["owner"] != owner:
                raise StateConflict("Another worker still owns this task")
            lease = {"mission_id": mission_id, "task_id": task_id, "owner": owner, "generation": generation + 1, "expires_at": self.clock() + ttl}
            tx.db.execute("INSERT INTO leases VALUES (:mission_id,:task_id,:owner,:generation,:expires_at) ON CONFLICT(mission_id,task_id) DO UPDATE SET owner=excluded.owner,generation=excluded.generation,expires_at=excluded.expires_at", lease)
            return lease

        return self.transact(operation_id, {"task_id": task_id, "owner": owner, "ttl": ttl, "expected_generation": expected_generation}, action, mission_id=mission_id)

    def put_blob(self, data: bytes) -> dict:
        return self.blobs.put(data)

    def rebuild_indexes(self) -> None:
        """Recreate disposable lookup indexes without rewriting accepted records."""
        from .schema import MIGRATIONS

        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            for statements in MIGRATIONS.values():
                for statement in statements:
                    if statement.startswith("CREATE INDEX "):
                        db.execute(statement.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1))
            db.execute("REINDEX")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def read_blob(self, sha256: str) -> bytes:
        return self.blobs.read(sha256)

    def links(self, record: dict, *, incoming: bool = False) -> list[dict]:
        prefix = "target" if incoming else "source"
        db = self._connect()
        try:
            rows = db.execute(f"SELECT * FROM links WHERE {prefix}_kind=? AND {prefix}_id=? AND {prefix}_version=? ORDER BY relation,source_id,target_id", reference(record))
            return [{"source": {key: row[f"source_{key}"] for key in ("kind", "id", "version")}, "target": {key: row[f"target_{key}"] for key in ("kind", "id", "version")}, "relation": row["relation"]} for row in rows]
        finally:
            db.close()

    def review_status(self, record: dict) -> list[dict]:
        db = self._connect()
        try:
            return [dict(row) for row in db.execute("SELECT reason,upstream_kind,upstream_id,upstream_version FROM review_requirements WHERE kind=? AND id=? AND version=?", reference(record))]
        finally:
            db.close()

    def events(self, mission_id: str, *, after: int = 0, limit: int = 1000) -> list[dict]:
        if type(limit) is not int or limit <= 0 or type(after) is not int or after < 0:
            raise StateError("Event page size must be positive and cursor nonnegative")
        db = self._connect()
        try:
            return [self._event(row) for row in db.execute("SELECT * FROM events WHERE mission_id=? AND sequence>? ORDER BY sequence LIMIT ?", (mission_id, after, limit))]
        finally:
            db.close()

    @staticmethod
    def _event(row) -> dict:
        item = dict(row)
        item["type"] = item.pop("event_type")
        item["body"] = json.loads(item["body"])
        item["delivered"] = bool(item["delivered"])
        return item

    def deliver(self, callback: Callable[[dict], None], limit: int = 100) -> int:
        if type(limit) is not int or limit <= 0:
            raise StateError("Delivery page size must be positive")
        db = self._connect()
        try:
            pending = db.execute("SELECT * FROM events WHERE delivered=0 ORDER BY sequence LIMIT ?", (limit,)).fetchall()
            for row in pending:
                callback(self._event(row))
                self.fault("after_event_delivery")
                db.execute("UPDATE events SET delivered=1 WHERE id=?", (row["id"],))
            return len(pending)
        finally:
            db.close()

    def reconcile(self) -> dict:
        db = self._connect()
        try:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            unresolved = [tuple(row) for row in db.execute("PRAGMA foreign_key_check")]
            blobs = {}
            for row in db.execute("SELECT kind,id,version,body FROM records"):
                blob = json.loads(row["body"]).get("blob")
                if blob:
                    blobs.setdefault(blob["sha256"], []).append({"kind": row["kind"], "id": row["id"], "version": row["version"], "size": blob["size"]})
            faults = []
            for digest, refs in blobs.items():
                try:
                    content = self.read_blob(digest)
                    if any(item["size"] != len(content) for item in refs):
                        raise IntegrityFault("Content length changed")
                except IntegrityFault:
                    faults.append({"sha256": digest, "references": refs})
            files = list(self.blobs.root.iterdir())
            return {"database_integrity": integrity, "unresolved_references": unresolved, "degraded_artifacts": faults, "staged_files": sorted(path.name for path in files if path.name.startswith('.staged-')), "orphan_objects": sorted(path.name for path in files if not path.name.startswith('.') and path.name not in blobs), "pending_events": db.execute("SELECT count(*) FROM events WHERE delivered=0").fetchone()[0]}
        finally:
            db.close()

    def backup(self, destination: Path) -> dict:
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
        source, target = self._connect(), sqlite3.connect(destination / self.db_path.name)
        try:
            source.backup(target)
            target.row_factory = sqlite3.Row
            refs = {}
            for row in target.execute("SELECT body FROM records"):
                blob = json.loads(row["body"]).get("blob")
                if blob:
                    refs[blob["sha256"]] = blob["size"]
            copy_store = BlobStore(destination / "objects", lambda _: None)
            for digest, size in refs.items():
                content = self.read_blob(digest)
                if len(content) != size:
                    raise IntegrityFault("Backup source length differs")
                copy_store.put(content)
            manifest = {"schema_version": target.execute("PRAGMA user_version").fetchone()[0], "database_name": self.db_path.name, "objects": refs, "created_at": self.now()}
            with (destination / "backup.json").open("x") as handle:
                handle.write(canonical(manifest))
                handle.flush()
                os.fsync(handle.fileno())
            sync_directory(destination)
            return manifest
        finally:
            target.close()
            source.close()

    @classmethod
    def restore(cls, backup: Path, destination: Path) -> StateStore:
        backup, destination = Path(backup), Path(destination)
        manifest = json.loads((backup / "backup.json").read_text())
        database_name = manifest.get("database_name", "state.sqlite3")
        if not isinstance(database_name, str) or not database_name or Path(database_name).name != database_name or database_name in {".", ".."}:
            raise IntegrityFault("Backup database name is invalid")
        originals = BlobStore(backup / "objects", lambda _: None, read_only=True)
        for digest, size in manifest["objects"].items():
            if len(originals.read(digest)) != size:
                raise IntegrityFault("Backup object length differs")
        if destination.exists():
            raise FileExistsError("Restore destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.restore-", dir=destination.parent))
        source = sqlite3.connect(f"{(backup / database_name).resolve().as_uri()}?mode=ro", uri=True)
        target = sqlite3.connect(staging / database_name)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
        restored = cls(staging, database_name=database_name)
        for digest in manifest["objects"]:
            restored.put_blob(originals.read(digest))
        report = restored.reconcile()
        if report["database_integrity"] != "ok" or report["unresolved_references"] or report["degraded_artifacts"]:
            raise IntegrityFault("Restored state failed record/content verification")
        sync_directory(staging)
        if destination.exists():
            raise FileExistsError("Restore destination was created by another operation")
        os.rename(staging, destination)
        sync_directory(destination.parent)
        return cls(destination, database_name=database_name)
