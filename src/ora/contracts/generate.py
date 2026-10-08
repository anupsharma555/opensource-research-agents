"""Generate client-consumable schemas; --check never changes tracked output."""

import argparse
import difflib
import json
from pathlib import Path

from .examples import generate_examples
from .models import (
    Command,
    Event,
    EvidencePacket,
    JobRequest,
    OperationResult,
    Query,
    Receipt,
    ResearchSnapshot,
    Subscription,
)

MODELS = {
    "research": ResearchSnapshot,
    "command": Command,
    "query": Query,
    "event": Event,
    "receipt": Receipt,
    "evidence": EvidencePacket,
    "job": JobRequest,
    "operation": OperationResult,
    "subscription": Subscription,
}


def generate(root: Path, check: bool = False) -> bool:
    output = root / "contracts" / "generated"
    valid = True
    for name, model in MODELS.items():
        schema = model.model_json_schema(by_alias=True)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        expected = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        path = output / f"{name}.schema.json"
        if check:
            actual = path.read_text() if path.exists() else ""
            if actual != expected:
                valid = False
                print(f"Stale generated schema: {path.relative_to(root)}; run scripts/dev generate")
                print(
                    "".join(
                        difflib.unified_diff(actual.splitlines(True), expected.splitlines(True))
                    )
                )
        else:
            output.mkdir(parents=True, exist_ok=True)
            path.write_text(expected)
    return generate_examples(root, check) and valid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if generate(Path.cwd(), args.check) else 1)


if __name__ == "__main__":
    main()
