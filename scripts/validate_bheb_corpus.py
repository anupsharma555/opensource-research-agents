#!/usr/bin/env python3
"""Check a private corpus binding or plan article additions without network access.

Default output contains only readiness/counts. Explicit reports stay under this
checkout's ignored .local directory; this command never changes an archive,
policy, corpus release, provider store or model configuration.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ora.retrieval.bheb_maintenance import inspect_readiness, plan_candidates, public_summary

ROOT = Path(__file__).resolve().parents[1]


def configured_path(root: Path = ROOT) -> Path | None:
    value = os.environ.get("OSRA_BHEB_CONFIG")
    if value is None:
        try:
            lines = (root / ".env").read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            lines = []
        for line in lines:
            name, separator, setting = line.partition("=")
            if separator and name.strip().removeprefix("export ") == "OSRA_BHEB_CONFIG":
                value = setting.strip().strip("\"'")
    if not value or not value.strip():
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def save_private(report: dict, destination: Path, root: Path = ROOT) -> None:
    destination = destination.resolve()
    if not destination.is_relative_to((root / ".local").resolve()):
        raise ValueError("Private reports must stay inside the checkout's .local directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Trusted local binding; otherwise OSRA_BHEB_CONFIG")
    parser.add_argument("--candidates", type=Path, help="Local candidate metadata JSON; plan only")
    parser.add_argument("--private-output", type=Path, help="New report under .local; never an archive destination")
    args = parser.parse_args(argv)
    config = args.config or configured_path(ROOT)
    if config is None:
        print(json.dumps({"status": "unavailable", "reason": "config_unavailable", "network_calls": 0}))
        return 0
    try:
        report = plan_candidates(config, args.candidates) if args.candidates else inspect_readiness(config)
        if args.private_output:
            save_private(report, args.private_output, ROOT)
        print(json.dumps(public_summary(report), indent=2))
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"status": "invalid_input_or_output", "network_calls": 0,
                          "action": "Check local input format and choose a new .local report path"}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
