"""Read shared proposal discussion or a saved explanation's coverage from Codex.

This local inspector uses the same contribution and trace projections supplied to
the recorded Atlas view. It opens the database read-only and cannot send a peer
message, refresh a model explanation or dispatch research work.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ora.state import StateError, StateStore

from .contributions import ContributionReview
from .traces import TraceJournal


def inspect_contributions(
    state_dir: Path, *, mission_id: str, proposal_id: str | None = None,
    viewer_id: str | None = None, summary_ref: dict | None = None,
    after: int = 0, through: int | None = None, limit: int = 100,
) -> dict:
    """Return one projected discussion page or one exact summary's coverage."""
    store = StateStore(state_dir, read_only=True)
    if summary_ref is not None:
        if proposal_id is not None or viewer_id is not None:
            raise ValueError("Choose a summary or a proposal discussion, not both")
        return TraceJournal(store).summary_coverage(summary_ref, mission_id=mission_id)
    if proposal_id is None or viewer_id is None:
        raise ValueError("A proposal discussion needs its proposal and viewing peer")
    return ContributionReview(store).proposal_context(
        mission_id=mission_id, proposal_id=proposal_id, viewer_id=viewer_id,
        after=after, through=through, limit=limit,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--proposal-id")
    parser.add_argument("--viewer-id")
    parser.add_argument("--summary-id")
    parser.add_argument("--summary-version", type=int, default=1)
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--through", type=int)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    summary_ref = ({"kind": "trace_summary", "id": args.summary_id,
                    "version": args.summary_version} if args.summary_id else None)
    try:
        result = inspect_contributions(
            args.state_dir, mission_id=args.mission_id,
            proposal_id=args.proposal_id, viewer_id=args.viewer_id,
            summary_ref=summary_ref, after=args.after,
            through=args.through, limit=args.limit,
        )
    except (ValueError, OSError, StateError) as error:
        parser.exit(2, f"Unable to inspect saved contributions: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
