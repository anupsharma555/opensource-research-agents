"""Serve a disposable Atlas workspace through the real local HTTP boundary.

Only bundled synthetic records are used. Commands are committed through StateStore
with stable request identity; no model, provider, .env or live workspace is opened.
The same connection entry can be used by browser tests and the native Mac viewer.
"""

import argparse
import json
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path

from ora.contracts.models import LiveAgent, LiveReceipt, LiveRunSummary, LiveState, ResearchSnapshot
from ora.live.cli import _write_connection
from ora.live.engine import LiveError
from ora.live.server import LocalResearchServer
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[1]


class AtlasFixture:
    def __init__(self, directory: Path):
        self.store = StateStore(directory)
        self.workspace_id = "atlas-offline-fixture"
        self.sample = json.loads((ROOT / "ui/public/sample-run.json").read_text())
        self.sample["proofKind"] = "offline_test"
        self.sample["mission"]["question"] = "Synthetic research: inspect the supplied comparison."
        self.sample["mission"]["scope"] = "Offline Atlas testing; no model or provider calls."
        self.store.transact("fixture-seed", {}, lambda tx: tx.put("atlas_fixture_run", "fixture-run", {"snapshot": self.sample, "parent": None}))

    def state(self, run_id=None):
        runs = self.store.records("atlas_fixture_run")
        selected = self.store.get("atlas_fixture_run", run_id or "fixture-run")
        if selected is None:
            raise LiveError("Unknown fixture run", 404)
        snapshot = ResearchSnapshot.model_validate(selected["body"]["snapshot"])
        parent = selected["body"].get("parent")
        parent_record = self.store.get("atlas_fixture_run", parent) if parent else None
        return LiveState(
            enabled=True, model="Synthetic offline fixture — no model calls",
            workspace_id=self.workspace_id, workspace_label="Offline Atlas test workspace",
            agents=[LiveAgent(id=a.id, name=a.name, role=a.role, skill_version="fixture-v1") for a in snapshot.agents],
            runs=[LiveRunSummary(id=r["id"], question=r["body"]["snapshot"]["mission"]["question"], status="completed", agent_ids=[a.id for a in snapshot.agents], parent_run_id=r["body"].get("parent")) for r in runs],
            run_id=selected["id"], snapshot=snapshot,
            comparison_snapshot=ResearchSnapshot.model_validate(parent_record["body"]["snapshot"]) if parent_record else None,
        )

    def _submit(self, request, parent=None):
        def save(tx):
            snapshot = json.loads(json.dumps(self.sample))
            snapshot["snapshotId"] = f"snapshot-{request.request_id}"
            snapshot["mission"]["question"] = request.question
            snapshot["mission"]["scope"] = request.scope
            run_id = f"fixture-{request.request_id}"
            tx.put("atlas_fixture_run", run_id, {"snapshot": snapshot, "parent": parent})
            return LiveReceipt(request_id=request.request_id, run_id=run_id, task_id=snapshot["tasks"][0]["id"]).model_dump(by_alias=True)
        return LiveReceipt.model_validate(self.store.transact(request.request_id, request.model_dump(mode="json"), save))

    def start(self, request):
        return self._submit(request)

    def followup(self, request):
        record = self.store.get("atlas_fixture_run", request.parent_run_id)
        if record is None or record["body"]["snapshot"]["snapshotId"] != request.parent_snapshot_id:
            raise LiveError("Fixture parent changed", 409)
        snapshot = record["body"]["snapshot"]
        targets = [*snapshot["artifacts"], *snapshot["claims"], *snapshot["sources"], *snapshot["tasks"]]
        if not any(t["id"] == request.target_id and t.get("version", snapshot["mission"]["version"]) == request.target_version for t in targets):
            raise LiveError("Unknown exact fixture target", 409)
        return self._submit(request, request.parent_run_id)

    def calls(self, *_args):
        return []

    def publish_question(self, question: str):
        """Commit a visible change to the selected synthetic run for browser timing."""
        request_id = f"fixture-update-{datetime.now(timezone.utc).timestamp()}"
        def save(tx):
            record = tx.get("atlas_fixture_run", "fixture-run")
            snapshot = json.loads(json.dumps(record["body"]["snapshot"]))
            snapshot["snapshotId"] = request_id
            snapshot["capturedAt"] = datetime.now(timezone.utc).isoformat()
            snapshot["mission"]["question"] = question
            snapshot["mission"]["version"] += 1
            return tx.put("atlas_fixture_run", "fixture-run", {"snapshot": snapshot, "parent": None}, expected_version=record["version"])
        self.store.transact(request_id, {"question": question}, save)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=4177)
    parser.add_argument("--web-root", type=Path, default=ROOT / "ui/dist")
    parser.add_argument("--publish-question", help="Commit a synthetic selected-run change and exit.")
    args = parser.parse_args()
    directory = args.state_dir.resolve()
    # Never seed an existing research workspace, even if invoked by mistake.
    marker = directory / "atlas-offline-fixture.json"
    if directory.exists() and any(directory.iterdir()) and not marker.is_file():
        parser.error("Choose an empty directory or an existing Atlas offline fixture.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker.write_text('{"synthetic": true}\n')
    host = AtlasFixture(directory)
    if args.publish_question is not None:
        host.publish_question(args.publish_question)
        print("Synthetic selected-run change committed.", flush=True)
        return
    server = LocalResearchServer(host, args.web_root.resolve(), args.port)
    _write_connection(directory, {"origin": server.origin, "token": server.token, "launchUrl": server.launch_url, "workspaceId": host.workspace_id})
    def stop(*_args):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print("Atlas offline fixture ready; connection details remain in the selected local directory.", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
