"""Print sanitized, read-only context metrics for one saved mission run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ora.observability import build_context_metrics
from ora.state import StateStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    store = StateStore(args.state_dir, read_only=True)
    result = build_context_metrics(store, args.run_id, include_calls=not args.summary_only)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
