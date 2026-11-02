"""Run explicit advisory checks without upgrading or activating research providers.

The development wrapper supplies pinned runtimes. Reports and advisory caches
stay in .local; manifests and lockfiles remain unchanged. Network errors are
reported as incomplete checks, never as clean dependency results.
"""

from __future__ import annotations

import argparse
import json
import runpy
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def outcome(ecosystem: str, code: int, output: str) -> dict:
    """Separate scanner failures from completed scans that found advisories."""
    try:
        data = json.loads(output)
        if ecosystem == "python":
            dependencies = data["dependencies"]
            count = sum(len(item.get("vulns", [])) for item in dependencies)
            skipped = [item["name"] for item in dependencies if "skip_reason" in item]
        elif ecosystem == "javascript":
            count = data["metadata"]["vulnerabilities"]["total"]
            skipped = []
        else:
            count = data["vulnerabilities"]["count"]
            skipped = []
        if code not in {0, 1} or skipped or (code == 1 and count == 0):
            return {"status": "incomplete", "advisories": count, "skipped": skipped}
        warnings = {key: len(rows) for key, rows in data.get("warnings", {}).items()} if ecosystem == "rust" else {}
        return {"status": "findings" if count else "warnings" if any(warnings.values()) else "clear",
                "advisories": count, **({"warnings": warnings} if warnings else {})}
    except (ValueError, KeyError, TypeError):
        return {"status": "incomplete", "advisories": None}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ecosystem", choices=["python", "javascript", "rust"], action="append")
    args = parser.parse_args(argv)
    wrapper = runpy.run_path(str(ROOT / "scripts/dev"))
    env = wrapper["node_environment"]()
    before = wrapper["locks"]()
    out = ROOT / ".local/audits" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    out.mkdir(parents=True, mode=0o700)
    results = {}

    def capture(label, command):
        result = subprocess.run([str(value) for value in command], cwd=ROOT, env=env,
                                text=True, capture_output=True)
        (out / f"{label}.json").write_text(result.stdout)
        (out / f"{label}.stderr.txt").write_text(result.stderr)
        return result

    for ecosystem in args.ecosystem or ["python", "javascript", "rust"]:
        try:
            if ecosystem == "python":
                requirements = out / "requirements.txt"
                export = capture("python-export", [wrapper["uv_executable"](), "export",
                    "--locked", "--all-extras", "--all-groups", "--no-emit-project",
                    "--no-header", "--no-annotate", "--output-file", requirements])
                if export.returncode:
                    results[ecosystem] = {"status": "incomplete", "reason": "lock export failed"}
                    continue
                command = [wrapper["PYTHON"], "-m", "pip_audit", "-r", requirements,
                    "--require-hashes", "--no-deps", "--disable-pip", "--strict",
                    "--format", "json", "--progress-spinner", "off", "--cache-dir",
                    ROOT / ".local/cache/pip-audit"]
            elif ecosystem == "javascript":
                command = ["npm", "audit", "--package-lock-only", "--json", "--ignore-scripts"]
            else:
                binary = ROOT / ".local/tools/cargo-audit/bin/cargo-audit"
                if not binary.is_file():
                    results[ecosystem] = {"status": "incomplete", "reason": "Run scripts/dev audit-install"}
                    continue
                version = capture("rust-version", [binary, "audit", "--version"])
                if version.returncode or version.stdout.split()[-1:] != [wrapper["CARGO_AUDIT_VERSION"]]:
                    results[ecosystem] = {"status": "incomplete", "reason": "Unexpected cargo-audit version"}
                    continue
                command = [binary, "audit", "--file", ROOT / "desktop/src-tauri/Cargo.lock",
                    "--db", ROOT / ".local/cache/rustsec-advisory-db", "--json"]
            result = capture(ecosystem, command)
            results[ecosystem] = outcome(ecosystem, result.returncode, result.stdout)
        except OSError:
            results[ecosystem] = {"status": "incomplete", "reason": "Scanner unavailable"}
    wrapper["verify_locks"](before)
    summary = {"checkedAt": datetime.now(UTC).isoformat(), "lockHashes": before,
               "scope": "Public advisories; Python current-platform markers, all locked extras/groups",
               "results": results, "reports": str(out.relative_to(ROOT))}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 2 if any(row["status"] == "incomplete" for row in results.values()) else int(
        any(row["status"] in {"findings", "warnings"} for row in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
