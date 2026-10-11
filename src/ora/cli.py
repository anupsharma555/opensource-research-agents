"""Inspect research records, run offline checks, or submit to a selected local host.

Mission commands use the same authenticated service as Atlas and return saved
receipts; closing the submitting terminal does not own the research lifetime.
Provider execution starts only through an explicitly launched live host.
"""

import argparse
import json
from pathlib import Path

from .benchmarks import validate_catalog
from .contracts.generate import generate
from .contracts.models import ResearchSnapshot
from .skills import load_registry


def inspect_sample(path: Path, view: str):
    sample = ResearchSnapshot.model_validate_json(path.read_text(), strict=True)
    if view == "sources":
        return {
            "proofKind": "synthetic_sample",
            "sources": [s.model_dump(by_alias=True) for s in sample.sources],
        }
    if view == "diagnostics":
        return {
            "proofKind": "synthetic_sample",
            "diagnostics": [d.model_dump(by_alias=True) for d in sample.diagnostics],
            "attempts": [a.model_dump(by_alias=True) for a in sample.attempts],
        }
    return {
        "proofKind": "synthetic_sample",
        "question": sample.mission.question,
        "findings": [
            a.model_dump(by_alias=True)
            for a in sample.artifacts
            if a.kind in {"report", "research_report"}
        ],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Research-agent foundation tools (offline by default)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    contracts = sub.add_parser("contracts", help="Generate or check canonical contracts")
    contracts.add_argument("--check", action="store_true")
    sub.add_parser("benchmarks", help="Verify frozen synthetic benchmark packets")
    sub.add_parser("skills", help="Validate role, skill, helper and provenance references")
    from .live.cli import configure as configure_live
    from .live.cli import configure_mission

    configure_live(sub.add_parser("live", help="Launch or connect to the persistent local research service"))
    configure_mission(sub.add_parser("mission", help="Submit or inspect work on the selected persistent host"))
    sample = sub.add_parser(
        "inspect-sample", help="Inspect the same sample sources as the desktop app"
    )
    sample.add_argument("--path", type=Path, default=Path("ui/public/sample-run.json"))
    sample.add_argument(
        "--view", choices=["findings", "sources", "diagnostics"], default="findings"
    )
    for name in ["source-replay", "store", "export-run"]:
        sub.add_parser(name, help="Planned production capability; unavailable in this foundation")
    args = parser.parse_args()
    root = Path.cwd()
    try:
        if args.command == "contracts":
            if not generate(root, args.check):
                raise SystemExit(1)
            print("Canonical schemas and shared fixtures are current.")
        elif args.command == "benchmarks":
            print(json.dumps(validate_catalog(root / "benchmarks/v1"), indent=2))
        elif args.command == "skills":
            registry = load_registry(root, {"source_read_by_id", "study_appraisal"})
            print(
                json.dumps(
                    {
                        "roles": len(registry.roles),
                        "skills": list(registry.skills),
                        "unavailable": registry.unavailable,
                    },
                    indent=2,
                )
            )
        elif args.command == "inspect-sample":
            print(json.dumps(inspect_sample(args.path, args.view), indent=2))
        elif args.command == "live":
            from .live.cli import main as live_main

            live_main(args, root)
        elif args.command == "mission":
            from .live.cli import mission_main

            mission_main(args, root)
        else:
            parser.exit(
                2,
                f"{args.command}: unavailable in the foundation release; no work was started.\nSee docs/development/modules.md for its implementation owner and interface.\n",
            )
    except (ValueError, OSError) as error:
        parser.exit(2, f"{args.command}: {error}\n")


if __name__ == "__main__":
    main()
