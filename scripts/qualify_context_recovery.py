"""Check saved run records for evidence delivery during a context-recovery test.

With --selection, read a JSON file naming exact saved checkpoints, passages and
reports plus the test-case checksum, then write a new private evaluation report.
Without --selection, print only the event chosen by --phase, initially the
opening test event. An optional reviewer file linked by checksums supplies
separate judgments about the answer's meaning. Evaluation reads existing mission
state without changing it, launching a host or calling a provider.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ora.benchmarks.context_recovery import (  # noqa: E402
    digest,
    evaluate_recovery,
    load_case,
    make_worker_packet,
)
from ora.state import StateStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("initial", "pressure", "late"), default="initial",
                        help="Export only this explicitly selected worker event")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.selection is None:
        case = load_case(ROOT)
        print(json.dumps({"case_sha256": digest(case), "worker_packet": make_worker_packet(case, args.phase)}, indent=2))
        return 0
    if args.state_dir is None or args.output is None:
        parser.error("Scoring requires --state-dir and a new private --output file")
    state = args.state_dir.resolve()
    if not (state / "state.sqlite3").is_file():
        parser.error("Existing saved mission state is required; no store will be created")
    output = args.output.resolve()
    if output.is_relative_to(ROOT) and not output.is_relative_to(ROOT / ".local"):
        parser.error("Keep private evaluation receipts under .local or outside the repository")
    selection = json.loads(args.selection.read_text())
    review = json.loads(args.review.read_text()) if args.review else None
    store = StateStore(state, read_only=True)
    result = evaluate_recovery(store, ROOT, selection, review=review)
    # Do not create parents or replace a previous qualification receipt.
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"receipt": str(output), "mechanical_pass": result["mechanical_pass"],
                      "semantic_review": result["semantic_review"],
                      "qualified": result["qualified"]}))
    return 0 if result["mechanical_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
