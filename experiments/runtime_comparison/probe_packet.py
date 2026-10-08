"""Check that each runtime carries an exact test question and later events intact.

It sends a public synthetic worker packet through one adapter, then delivers its
scheduled events one at a time and compares their IDs on return. The probe never
opens evaluator answers or asks a model to interpret the sources.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
from ora.benchmarks import load_event_script, make_worker_packet  # noqa: E402


async def probe(adapter_name: str, root: Path, scenario_id: str) -> dict:
    packet = make_worker_packet(root, scenario_id, "peer_exchange", "controlled_equal_input")
    events = load_event_script(root, scenario_id)
    adapter = importlib.import_module(f"{adapter_name}_adapter")
    delivered = []
    with tempfile.TemporaryDirectory(prefix=f"ora-packet-{adapter_name}-") as folder:
        for index, event in enumerate(events, start=1):
            payload = {
                "task_id": f"benchmark-event-{index}",
                "role": event["target"],
                "delay": 0.0,
                "attempt": 1,
                "worker_packet": packet,
                "delivered_event": event,
            }
            receipt, count = await adapter.execute(payload, Path(folder))
            if receipt["delivered_event_id"] != event["event_id"]:
                raise RuntimeError("Adapter changed event identity")
            if receipt["worker_packet_sha256"] != packet["worker_packet_sha256"]:
                raise RuntimeError("Adapter changed packet identity")
            delivered.append({**receipt, "native_checkpoint_count": count})
    return {
        "adapter": adapter_name,
        "scenario_id": packet["scenario_id"],
        "scenario_version": packet["scenario_version"],
        "scenario_sha256": packet["scenario_sha256"],
        "worker_packet_sha256": packet["worker_packet_sha256"],
        "delivered": delivered,
        "limitation": "Transport/provenance only; no scientific outcome or withheld reference loaded.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", choices=("custom", "langgraph", "maf"), required=True)
    parser.add_argument("--benchmark-root", type=Path, default=REPO / "benchmarks/v1")
    parser.add_argument("--scenario-id", default="syn-route-recovery")
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(probe(args.adapter, args.benchmark_root, args.scenario_id)), sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
