"""Read a saved Stage 3 snapshot without dispatching work or changing state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ora.state import StateStore
from ora.state.inspection import load_snapshot


def read_stage3(state_dir: Path, snapshot_id: str, *, view: str = "all",
                after: int = 0, limit: int = 100) -> dict:
    """Return the exact Atlas projection or a stable page of its saved steps."""
    if view not in {"all", "steps", "diagnostics", "findings"}:
        raise ValueError("Choose all, steps, diagnostics or findings")
    if type(after) is not int or after < 0 or type(limit) is not int or limit <= 0:
        raise ValueError("Step cursor must be nonnegative and page size positive")
    store = StateStore(state_dir, read_only=True)
    snapshot = load_snapshot(store, snapshot_id)
    if view == "all":
        return snapshot
    if view == "findings":
        return {"snapshotId": snapshot_id, "proofKind": snapshot["proofKind"],
                "reportArtifactId": snapshot["mission"]["reportArtifactId"],
                "artifacts": snapshot["artifacts"]}
    if view == "diagnostics":
        return {"snapshotId": snapshot_id, "proofKind": snapshot["proofKind"],
                "attempts": snapshot["attempts"], "diagnostics": snapshot["diagnostics"],
                "records": snapshot["inspection"]["records"]}
    steps = [item for item in snapshot["inspection"]["steps"] if item["sequence"] > after]
    page = steps[:limit]
    return {"snapshotId": snapshot_id, "steps": page,
            "nextCursor": page[-1]["sequence"] if page else after,
            "hasMore": len(steps) > len(page)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--view", choices=["all", "steps", "diagnostics", "findings"],
                        default="all")
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(read_stage3(args.state_dir, args.snapshot_id, view=args.view,
                                 after=args.after, limit=args.limit),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
