"""Inspect the intended Git file set without reading local credential files."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATHS = {
    ".env",
    "THREADS.md",
    ".local",
    "node_modules",
    ".venv",
    "output",
    "target",
    "secrets",
    ".secrets",
    "credentials",
    ".npmrc",
    ".pypirc",
}
PATTERNS = [
    re.compile(rb"gsk_[A-Za-z0-9]{20,}"),
    re.compile(rb"(?:sk-proj-|sk_live_|ghp_|github_pat_)[A-Za-z0-9_\-]{16,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"https://linear\.app/"),
    re.compile(rb"/Users/[A-Za-z0-9_.-]+/"),
]


def inspect_index(root: Path):
    """Read the exact blobs Git would commit, never a newer working-tree substitute."""
    output = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=root)
    entries = [item for item in output.decode().split("\0") if item]
    failures = []
    for entry in entries:
        header, name = entry.split("\t", 1)
        mode, object_id, stage = header.split()
        basename = Path(name).name
        private_env = basename.startswith(".env.") and basename != ".env.example"
        if FORBIDDEN_PATHS.intersection(Path(name).parts) or private_env:
            failures.append((name, "excluded local material is in the intended file set"))
            continue
        if stage != "0":
            failures.append((name, "unresolved index conflict"))
            continue
        if mode not in {"100644", "100755"}:
            failures.append((name, "review a symbolic link or nested repository before publishing"))
            continue
        content = subprocess.check_output(["git", "cat-file", "blob", object_id], cwd=root)
        if any(pattern.search(content) for pattern in PATTERNS):
            failures.append((name, "potential credential or private workspace reference"))
    return len(entries), failures


def main():
    count, failures = inspect_index(ROOT)
    for name, reason in failures:
        print(f"{name}: {reason}")  # Never emit a matching value.
    print(f"Reviewed {count} exact index blobs; {len(failures)} publication findings.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
