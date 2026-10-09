"""Read accepted research snapshots or domain records from a local state directory.

This is the Codex/terminal entry point for the same projection Research Atlas
uses. Opening a store here never migrates it, starts a run or invokes a provider.
"""

import argparse
import json
import sqlite3
from pathlib import Path

from .inspection import load_snapshot
from .store import StateStore
from .views import VIEW_SQL


def read_overview(state_dir: Path, overview: str, *, limit: int = 100, offset: int = 0) -> dict:
    """Read a page of a known SQL view without migrating or exposing record bodies."""
    if overview not in VIEW_SQL or type(limit) is not int or limit <= 0 or type(offset) is not int or offset < 0:
        raise ValueError("Choose a known overview, positive page size and nonnegative offset")
    store = StateStore(state_dir, read_only=True)
    db = store._connect()
    try:
        db.execute("PRAGMA query_only=ON")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name=?", (overview,)).fetchone():
            raise ValueError("This historical database has no overview views; migrate a copy or inspect its existing records")
        # Names are fixed by the module, never caller-supplied SQL. Order every
        # projected column to keep an unchanged database's page order stable.
        columns = db.execute(f"PRAGMA table_info({overview})").fetchall()
        order = ",".join(str(index + 1) for index in range(len(columns)))
        rows = [dict(row) for row in db.execute(f"SELECT * FROM {overview} ORDER BY {order} LIMIT ? OFFSET ?", (limit + 1, offset))]
        return {"overview": overview, "recordScope": "historical revisions; is_current identifies current records where applicable",
                "rows": rows[:limit], "nextOffset": offset + limit if len(rows) > limit else None}
    except sqlite3.DatabaseError as exc:
        raise ValueError("Saved overview could not be read; no database change was made") from exc
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--snapshot")
    selection.add_argument("--kind", help="Read current domain records with stable ID pagination")
    selection.add_argument("--overview", choices=sorted(VIEW_SQL), help="Read safe SQL overview columns, including historical revisions")
    parser.add_argument("--view", choices=["all", "findings", "sources", "diagnostics"], default="all")
    parser.add_argument("--after-id", default="")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    if args.overview:
        print(json.dumps(read_overview(args.state_dir, args.overview, limit=args.limit, offset=args.offset), ensure_ascii=False, indent=2))
        return
    store = StateStore(args.state_dir, read_only=True)
    if args.kind:
        records = store.records(args.kind, limit=args.limit, after_id=args.after_id)
        output = {"records": records, "nextAfterId": records[-1]["id"] if len(records) == args.limit else None}
    else:
        snapshot = load_snapshot(store, args.snapshot)
        if args.view == "findings":
            output = {"snapshotId": snapshot["snapshotId"], "question": snapshot["inputQuestion"], "artifacts": snapshot["artifacts"]}
        elif args.view == "sources":
            output = {"snapshotId": snapshot["snapshotId"], "sources": snapshot["sources"], "provenance": snapshot["inspection"]["sources"]}
        elif args.view == "diagnostics":
            output = {"snapshotId": snapshot["snapshotId"], "inspection": snapshot["inspection"], "diagnostics": snapshot["diagnostics"]}
        else:
            output = snapshot
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
