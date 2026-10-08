"""Portable Codex-skill helper; uses the repository's shared check entrypoints."""

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["smoke", "full"], default="smoke")
    args = parser.parse_args()
    if not (ROOT / "pyproject.toml").is_file() or not (ROOT / "scripts/dev").is_file():
        parser.exit(2, "Repository files are missing; invoke the committed skill from a complete checkout.\n")
    commands = ([["scripts/dev", "check"]] if args.mode == "full" else [
        [".venv/bin/python", "-m", "ora.cli", "contracts", "--check"],
        [".venv/bin/python", "-m", "ora.cli", "benchmarks"],
        [".venv/bin/python", "-m", "ora.cli", "skills"],
    ])
    folder = ROOT / ".local/checks"
    folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True)
    results = []
    for index, command in enumerate(commands):
        log = folder / f"{timestamp}-{index}.log"
        with log.open("w") as output:
            try:
                result = subprocess.run(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
                code = result.returncode
            except OSError as error:
                output.write(f"Required command could not start: {type(error).__name__}\n")
                code = 2
        results.append({"command": command, "exitCode": code, "log": str(log.relative_to(ROOT))})
        if code:
            break
    receipt = {"skill": "os-check-foundation", "version": "0.1.0", "mode": args.mode,
               "commit": revision.stdout.strip() if revision.returncode == 0 else "uncommitted",
               "skillHash": hashlib.sha256((Path(__file__).parents[1] / "SKILL.md").read_bytes()).hexdigest(),
               "platform": platform.system(), "python": platform.python_version(),
               "proofKind": "offline_development_check", "results": results,
               "passed": len(results) == len(commands) and all(r["exitCode"] == 0 for r in results)}
    target = folder / f"{timestamp}-receipt.json"
    target.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"receipt": str(target.relative_to(ROOT)), "passed": receipt["passed"], "commit":receipt["commit"]}))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
