"""Publication must inspect staged bytes even when the working tree is clean-looking."""

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "publication_check", ROOT / "scripts/check-publication.py"
)
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_staged_credential_shape_cannot_hide_behind_clean_worktree(tmp_path):
    git(tmp_path, "init")
    path = tmp_path / "example.txt"
    # Deliberately synthetic: a shape test, never a real credential.
    path.write_text("gsk_" + "SYNTHETIC" * 5)
    git(tmp_path, "add", "example.txt")
    path.write_text("The working copy now looks harmless.")
    count, failures = publication.inspect_index(tmp_path)
    assert count == 1 and failures[0][0] == "example.txt"
    assert "SYNTHETIC" not in str(failures)
    git(tmp_path, "add", "example.txt")
    assert publication.inspect_index(tmp_path) == (1, [])


def test_forced_private_paths_are_rejected_before_content_read(tmp_path):
    git(tmp_path, "init")
    for name in [".env.local", ".npmrc", "secrets/synthetic.txt"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic placeholder")
        git(tmp_path, "add", "-f", name)
    count, failures = publication.inspect_index(tmp_path)
    assert count == 3 and len(failures) == 3
    assert all("excluded local" in reason for _, reason in failures)
