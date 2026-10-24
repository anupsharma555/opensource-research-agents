"""Export provider-returned reasoning for one saved run to private local files.

This command reads the selected SQLite state without starting a mission or model.
Standard output contains only export status, counts, and local file paths.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from ora.models.reasoning_artifacts import export_run_reasoning
from ora.state import StateError, StateStore

_RUN_STATUSES = frozenset({"complete", "partial", "no_receipts"})
_STEP_STATUSES = frozenset({"returned", "truncated", "absent", "unknown",
                            "unsupported", "not_requested", "not_captured"})
_FIDELITIES = frozenset({"provider_exact", "saved_unredacted",
                          "saved_redacted", "unavailable"})


def _local_destination(path: Path) -> Path:
    """Reject URI-like names and links before the exporter creates any files."""
    raw = str(path)
    if "://" in raw or raw.startswith("\\\\"):
        raise ValueError("The output destination must be a local filesystem path")
    absolute = path.absolute()
    for parent in (absolute, *absolute.parents):
        if parent.is_symlink():
            raise ValueError("The output destination cannot contain a symbolic link")
    return absolute


def _safe_path(value: object, base: Path, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, (str, Path)):
        raise ValueError("The export result had invalid path metadata")
    path = Path(value).absolute()
    if not path.is_relative_to(base):
        raise ValueError("The export result pointed outside the selected destination")
    return str(path)


def _summary(result: dict, run_id: str, base: Path) -> dict:
    """Allowlist the command's metadata so a future API field cannot print text."""
    if result.get("run_id") != run_id:
        raise ValueError("The export result did not match the selected run")
    steps = result.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError("The export result had invalid step metadata")
    if result.get("status") not in _RUN_STATUSES:
        raise ValueError("The export result had invalid status metadata")
    for key in ("receipt_count", "dispatch_only_count", "exported_count",
                "unavailable_count", "truncated_count"):
        if type(result.get(key)) is not int or result[key] < 0:
            raise ValueError("The export result had invalid count metadata")
    for step in steps:
        if (not isinstance(step, dict) or step.get("status") not in _STEP_STATUSES
                or step.get("capture_fidelity") not in _FIDELITIES):
            raise ValueError("The export result had invalid step metadata")
    return {
        "schema": "ora.reasoning_export_cli.v1",
        "run_id": run_id,
        "output_dir": _safe_path(result["output_dir"], base),
        "status": result["status"],
        "receipt_count": result["receipt_count"],
        "dispatch_only_count": result["dispatch_only_count"],
        "exported_count": result["exported_count"],
        "unavailable_count": result["unavailable_count"],
        "truncated_count": result["truncated_count"],
        "steps": [
            {
                "step": index,
                "status": step["status"],
                "capture_fidelity": step["capture_fidelity"],
                "text_path": _safe_path(step.get("text_path"), base, optional=True),
                "json_path": _safe_path(step["json_path"], base),
                "markdown_path": _safe_path(step["markdown_path"], base),
            }
            for index, step in enumerate(steps, start=1)
        ],
        "combined_text_path": _safe_path(result.get("combined_text_path"), base, optional=True),
        "combined_json_path": _safe_path(result.get("combined_json_path"), base, optional=True),
        "combined_markdown_path": _safe_path(result.get("combined_markdown_path"), base, optional=True),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True,
                        help="Existing local StateStore directory")
    parser.add_argument("--run-id", required=True, help="Exact saved mission run ID")
    parser.add_argument("--output-dir", type=Path,
                        help="Private local export directory; defaults to STATE_DIR/reasoning")
    args = parser.parse_args(argv)
    if len(args.run_id) > 200 or not args.run_id or any(
        char.isspace() or char in "/\\\x00" for char in args.run_id
    ):
        parser.error("--run-id must be one bounded record ID")
    try:
        if not args.state_dir.is_dir() or not (args.state_dir / "state.sqlite3").is_file():
            raise ValueError("The selected state directory has no saved SQLite database")
        output_dir = _local_destination(args.output_dir) if args.output_dir else None
        store = StateStore(args.state_dir, read_only=True)
        result = export_run_reasoning(store, args.run_id, output_dir=output_dir)
        base = output_dir or (store.root / "reasoning")
        summary = _summary(result, args.run_id, base.absolute())
    except (OSError, sqlite3.Error, StateError, ValueError, KeyError, TypeError):
        parser.exit(2, "reasoning export failed; check the state, run ID, destination, and saved record integrity.\n")
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
