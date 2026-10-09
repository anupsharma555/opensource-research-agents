"""Upgrade local research tables atomically while preserving record history.

Version 1 holds accepted records, ownership and delivery state. Version 2 adds
review requirements and a lookup index; version 3 adds read-only overviews and
optional provenance for new record writes without rewriting old bodies. This module
contains no scientific routing or automatic destructive migration.
"""

import sqlite3

from .views import migration_statements

CURRENT_VERSION = 3
MIGRATIONS = {
    1: [
        "CREATE TABLE records(kind TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), body TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(kind,id,version))",
        "CREATE TABLE heads(kind TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, PRIMARY KEY(kind,id), FOREIGN KEY(kind,id,version) REFERENCES records(kind,id,version))",
        "CREATE TABLE aliases(namespace TEXT NOT NULL, value TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY(namespace,value), FOREIGN KEY(kind,id) REFERENCES heads(kind,id))",
        "CREATE TABLE links(source_kind TEXT, source_id TEXT, source_version INTEGER, target_kind TEXT, target_id TEXT, target_version INTEGER, relation TEXT NOT NULL, PRIMARY KEY(source_kind,source_id,source_version,target_kind,target_id,target_version,relation), FOREIGN KEY(source_kind,source_id,source_version) REFERENCES records(kind,id,version), FOREIGN KEY(target_kind,target_id,target_version) REFERENCES records(kind,id,version))",
        "CREATE INDEX reverse_links ON links(target_kind,target_id,target_version)",
        "CREATE TABLE operations(id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, mission_id TEXT NOT NULL, event_type TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0)",
        "CREATE INDEX mission_events ON events(mission_id,sequence)",
        "CREATE INDEX pending_events ON events(delivered,sequence)",
        "CREATE TABLE leases(mission_id TEXT NOT NULL, task_id TEXT NOT NULL, owner TEXT NOT NULL, generation INTEGER NOT NULL, expires_at REAL NOT NULL, PRIMARY KEY(mission_id,task_id))",
        "CREATE TABLE diagnostics(sequence INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, code TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
    ],
    2: [
        "CREATE TABLE review_requirements(kind TEXT, id TEXT, version INTEGER, reason TEXT NOT NULL, upstream_kind TEXT, upstream_id TEXT, upstream_version INTEGER, PRIMARY KEY(kind,id,version,reason,upstream_kind,upstream_id,upstream_version), FOREIGN KEY(kind,id,version) REFERENCES records(kind,id,version))",
        "CREATE INDEX record_kind_time ON records(kind,created_at)",
    ],
    3: [
        "CREATE TABLE record_origins(kind TEXT NOT NULL,id TEXT NOT NULL,version INTEGER NOT NULL,record_format_version TEXT,operation_id TEXT,proof_basis TEXT,timing_basis TEXT,implementation_ref TEXT,PRIMARY KEY(kind,id,version),FOREIGN KEY(kind,id,version) REFERENCES records(kind,id,version))",
        *migration_statements(),
    ],
}


def migrate(db: sqlite3.Connection, target: int, now: str, fault) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        current = db.execute("PRAGMA user_version").fetchone()[0]
        if current > target or target not in MIGRATIONS:
            raise ValueError(f"Unsupported store schema: saved={current}, supported={target}")
        for version in range(current + 1, target + 1):
            for index, statement in enumerate(MIGRATIONS[version]):
                db.execute(statement)
                fault(f"migration_{version}_step_{index}")
            db.execute("INSERT INTO migrations VALUES (?,?)", (version, now))
            db.execute(f"PRAGMA user_version={version:d}")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Migration left unresolved record references")
        db.commit()
    except BaseException:
        db.rollback()
        raise
