"""Load only optional trace settings; bad monitoring config never blocks research.

Runtime export has its own explicit switch. The read-only trace report does not
load this file, and merely having a LangSmith key does not start an observer.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ora.observability.export import ExportConfig
from ora.state import StateError

TRACE_ENV_KEYS = frozenset({
    "LANGSMITH_API_KEY", "OSRA_TRACE_EXPORT_ENABLED", "OSRA_TRACE_RUNTIME_ENABLED",
    "OSRA_TRACE_OTLP_TRACES_ENDPOINT", "OSRA_TRACE_LANGSMITH_PROJECT",
})


def trace_environment(root: Path) -> dict[str, str]:
    """Read simple scoped dotenv values without interpolation; process env wins."""
    values = {}
    path = root / ".env"
    if path.is_file():
        for line in path.read_text().splitlines():
            key, separator, value = line.strip().removeprefix("export ").partition("=")
            key = key.strip()
            if separator and key in TRACE_ENV_KEYS:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                values[key] = value
    values.update({key: os.environ[key] for key in TRACE_ENV_KEYS if key in os.environ})
    return values


def runtime_export_config(root: Path) -> ExportConfig:
    """Opt in at a live entry point, keeping configuration failure local and safe."""
    try:
        values = trace_environment(root)
        flag = values.get("OSRA_TRACE_RUNTIME_ENABLED", "false").strip().lower()
        if flag not in {"true", "false"}:
            raise StateError("Invalid trace runtime switch")
        return ExportConfig.from_env(values) if flag == "true" else ExportConfig()
    except (OSError, UnicodeError, StateError, ValueError):
        logging.getLogger(__name__).warning(
            "OSRA trace observer disabled: configuration_error; research remains available"
        )
        return ExportConfig()
