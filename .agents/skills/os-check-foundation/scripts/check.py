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


def source_state(root: Path) -> dict:
    """Summarize Git source state without retaining paths or diff contents."""

    def git(*args: str):
        try:
            return subprocess.run(
                ["git", *args], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except OSError:
            return None

    revision = git("rev-parse", "HEAD")
    diff = git("diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z", "--")
    if any(result is None or result.returncode != 0 for result in (revision, diff, untracked)):
        return {
            "available": False,
            "headCommit": None,
            "trackedDiffSha256": None,
            "trackedDiffPresent": None,
            "untrackedPathsPresent": None,
            "untrackedPathCount": None,
            "clean": False,
        }
    tracked_changed = bool(diff.stdout)
    untracked_count = untracked.stdout.count(b"\0")
    return {
        "available": True,
        "headCommit": revision.stdout.decode("ascii").strip(),
        "trackedDiffSha256": hashlib.sha256(diff.stdout).hexdigest(),
        "trackedDiffPresent": tracked_changed,
        "untrackedPathsPresent": untracked_count > 0,
        "untrackedPathCount": untracked_count,
        "clean": not tracked_changed and untracked_count == 0,
    }


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
    before = source_state(ROOT)
    folder = ROOT / ".local/checks"
    folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
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
    after = source_state(ROOT)
    passed = len(results) == len(commands) and all(r["exitCode"] == 0 for r in results)
    commit_only_proof = (
        passed and before["clean"] and after["clean"]
        and before["headCommit"] == after["headCommit"]
    )
    receipt = {"skill": "os-check-foundation", "version": "0.1.1", "mode": args.mode,
               "commit": before["headCommit"] if commit_only_proof else "uncommitted",
               "headCommit": before["headCommit"],
               "sourceStateBefore": before, "sourceStateAfter": after,
               "commitOnlyProofValid": commit_only_proof,
               "skillHash": hashlib.sha256((Path(__file__).parents[1] / "SKILL.md").read_bytes()).hexdigest(),
               "platform": platform.system(), "python": platform.python_version(),
               "proofKind": "offline_development_check", "results": results,
               "passed": passed}
    target = folder / f"{timestamp}-receipt.json"
    target.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"receipt": str(target.relative_to(ROOT)), "passed": receipt["passed"],
                      "commit": receipt["commit"], "commitOnlyProofValid": commit_only_proof}))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
