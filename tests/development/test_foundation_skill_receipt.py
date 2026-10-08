"""The foundation receipt must not call a dirty working tree a tested commit."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".agents/skills/os-check-foundation"


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def run_skill(root: Path, *, mutate: bool = False) -> dict:
    env = os.environ.copy()
    env.pop("SYNTHETIC_MUTATE_TRACKED", None)
    if mutate:
        env["SYNTHETIC_MUTATE_TRACKED"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            str(root / ".agents/skills/os-check-foundation/scripts/check.py"),
            "--mode",
            "full",
        ],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    summary = json.loads(result.stdout)
    return json.loads((root / summary["receipt"]).read_text())


def test_receipt_separates_clean_commit_from_tracked_and_untracked_work(tmp_path):
    helper = tmp_path / ".agents/skills/os-check-foundation/scripts/check.py"
    helper.parent.mkdir(parents=True)
    shutil.copyfile(SKILL / "scripts/check.py", helper)
    shutil.copyfile(SKILL / "SKILL.md", helper.parent.parent / "SKILL.md")
    wrapper = tmp_path / "scripts/dev"
    wrapper.parent.mkdir()
    wrapper.write_text(
        "#!/bin/sh\n"
        'if [ "${SYNTHETIC_MUTATE_TRACKED:-}" = 1 ]; then '
        "printf '# changed during check\\n' >> pyproject.toml; fi\n"
        "printf 'synthetic offline check\\n'\n"
    )
    wrapper.chmod(0o755)
    original_project = '[project]\nname = "synthetic-foundation"\nversion = "0.1.0"\n'
    project = tmp_path / "pyproject.toml"
    project.write_text(original_project)
    (tmp_path / ".gitignore").write_text(".local/\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".gitignore", "pyproject.toml", "scripts/dev", ".agents")
    git(
        tmp_path,
        "-c",
        "user.name=Development Fixture",
        "-c",
        "user.email=development-fixture@example.invalid",
        "commit",
        "-qm",
        "Synthetic fixture",
    )
    head = git(tmp_path, "rev-parse", "HEAD")

    clean = run_skill(tmp_path)
    assert clean["version"] == "0.1.1" and clean["passed"]
    assert clean["commitOnlyProofValid"] and clean["commit"] == head
    assert clean["sourceStateBefore"]["clean"] and clean["sourceStateAfter"]["clean"]
    assert clean["sourceStateBefore"]["trackedDiffSha256"] == hashlib.sha256(b"").hexdigest()

    project.write_text(original_project + "# local tracked edit\n")
    tracked = run_skill(tmp_path)
    assert tracked["passed"] and not tracked["commitOnlyProofValid"]
    assert tracked["commit"] == "uncommitted" and tracked["headCommit"] == head
    assert tracked["sourceStateBefore"]["trackedDiffPresent"]
    assert not tracked["sourceStateBefore"]["untrackedPathsPresent"]
    assert (
        tracked["sourceStateBefore"]["trackedDiffSha256"]
        != clean["sourceStateBefore"]["trackedDiffSha256"]
    )
    assert "local tracked edit" not in json.dumps(tracked)

    git(tmp_path, "add", "pyproject.toml")
    staged = run_skill(tmp_path)
    assert staged["passed"] and not staged["commitOnlyProofValid"]
    assert staged["sourceStateBefore"]["trackedDiffPresent"]
    assert staged["sourceStateBefore"]["trackedDiffSha256"] == tracked["sourceStateBefore"]["trackedDiffSha256"]

    project.write_text(original_project)
    git(tmp_path, "add", "pyproject.toml")
    (tmp_path / "unrelated-note.txt").write_text("Synthetic unrelated work.\n")
    untracked = run_skill(tmp_path)
    assert untracked["passed"] and not untracked["commitOnlyProofValid"]
    assert untracked["commit"] == "uncommitted" and untracked["headCommit"] == head
    assert not untracked["sourceStateBefore"]["trackedDiffPresent"]
    assert untracked["sourceStateBefore"]["untrackedPathsPresent"]
    assert untracked["sourceStateBefore"]["untrackedPathCount"] == 1
    assert "unrelated-note.txt" not in json.dumps(untracked)

    (tmp_path / "unrelated-note.txt").unlink()
    during_check = run_skill(tmp_path, mutate=True)
    assert during_check["sourceStateBefore"]["clean"]
    assert not during_check["sourceStateAfter"]["clean"]
    assert not during_check["commitOnlyProofValid"]
    assert during_check["commit"] == "uncommitted"
