"""Check what survives when each scripted runtime stops and starts again.

The script interrupts work at three chosen points, resumes it in a new process,
and records whether artifacts or peer messages were lost or duplicated. It does
not simulate power loss or an uncertain live provider effect.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

RUNNER = Path(__file__).with_name("run.py")


def invoke(python: str, adapter: str, workdir: Path, *options: str) -> dict:
    process = subprocess.run(
        [python, str(RUNNER), "--adapter", adapter, "--workdir", str(workdir), *options],
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode:
        raise RuntimeError(process.stderr)
    return json.loads(process.stdout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--langgraph-python", required=True)
    parser.add_argument("--maf-python", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for adapter, python in (
        ("custom", sys.executable),
        ("langgraph", args.langgraph_python),
        ("maf", args.maf_python),
    ):
        for fault in ("after-first-commit", "before-commit", "during-step"):
            with tempfile.TemporaryDirectory(prefix=f"ora-{adapter}-{fault}-") as folder:
                workdir = Path(folder)
                interrupted = invoke(python, adapter, workdir, "--fault", fault)
                resumed = invoke(python, adapter, workdir, "--resume")
                if not interrupted["interrupted"] or resumed["interrupted"]:
                    raise RuntimeError(
                        f"Interruption/resume did not behave as expected: {adapter}/{fault}"
                    )
                final = resumed["snapshot"]
                if len(final["artifacts"]) != 4 or len(final["messages"]) != 1:
                    raise RuntimeError(f"Lost or duplicated effect: {adapter}/{fault}")
                results.append(
                    {
                        "adapter": adapter,
                        "fault": fault,
                        "before_artifact_count": len(interrupted["snapshot"]["artifacts"]),
                        "before_claimed_count": sum(
                            t["status"] == "claimed" for t in interrupted["snapshot"]["tasks"]
                        ),
                        "before_native_checkpoints": interrupted["native_checkpoint_inventory"],
                        "after_artifact_count": len(final["artifacts"]),
                        "after_message_count": len(final["messages"]),
                        "after_native_checkpoints": resumed["native_checkpoint_inventory"],
                        "reclaimed_count": sum(e["kind"] == "reclaimed" for e in final["events"]),
                        "model_hash_retained": interrupted["snapshot"]["model_hash"]
                        == final["model_hash"],
                        "final_task_attempts": {t["task_id"]: t["attempt"] for t in final["tasks"]},
                    }
                )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, sort_keys=True, indent=2) + "\n")
    print(f"recorded {len(results)} interruption/resume cases")


if __name__ == "__main__":
    main()
