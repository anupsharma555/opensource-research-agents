"""Read paged coordination traces or timing metadata without starting any work."""

import argparse
import json
from pathlib import Path

from ora.state import StateStore

from .traces import TraceJournal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--view", choices=("events", "progress", "usage"), default="events")
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    journal = TraceJournal(StateStore(args.state_dir, read_only=True))
    if args.view == "progress":
        result = journal.progress(args.mission_id)
    elif args.view == "usage":
        result = journal.usage_totals(args.mission_id)
    else:
        result = journal.export(args.mission_id, after=args.after, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
