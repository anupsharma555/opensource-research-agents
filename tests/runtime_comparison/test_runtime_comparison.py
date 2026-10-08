"""Check whether the runtime candidates complete the same scripted peer work.

The tests compare accepted artifacts, direct requests, parallel progress, and
recovery after interruptions; optional frameworks run through separate Python
interpreters. No model call or live research result is measured here.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "experiments/runtime_comparison/run.py"
PACKET_PROBE = ROOT / "experiments/runtime_comparison/probe_packet.py"


def interpreters():
    result = [("custom", sys.executable)]
    for name, variable in (("langgraph", "ORA_LANGGRAPH_PYTHON"), ("maf", "ORA_MAF_PYTHON")):
        if os.environ.get(variable):
            result.append((name, os.environ[variable]))
    return result


def invoke(python: str, adapter: str, workdir: Path, *extra: str, success: bool = True):
    process = subprocess.run(
        [python, str(RUNNER), "--adapter", adapter, "--workdir", str(workdir), *extra],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if success:
        if process.returncode:
            raise AssertionError(process.stderr)
        return json.loads(process.stdout)
    return process


class RuntimeComparisonTest(unittest.TestCase):
    def assert_finished(self, report):
        snapshot = report["snapshot"]
        self.assertEqual(
            {x["artifact_id"] for x in snapshot["artifacts"]},
            {f"artifact:{name}" for name in ("collect_a", "collect_b", "verify_b", "synthesize")},
        )
        self.assertEqual(len(snapshot["messages"]), 1)
        self.assertEqual(snapshot["messages"][0]["recipient"], "critic")
        self.assertTrue(all(task["status"] == "done" for task in snapshot["tasks"]))
        self.assertEqual(len({x["artifact_id"] for x in snapshot["artifacts"]}), 4)
        self.assertEqual(len({x["message_id"] for x in snapshot["messages"]}), 1)
        self.assertTrue(any(x["kind"] == "rejected_action" for x in snapshot["events"]))
        self.assertTrue(any(x["kind"] == "proposal" for x in snapshot["events"]))
        kinds = [(x["kind"], x["task_id"]) for x in snapshot["events"]]
        self.assertLess(
            kinds.index(("proposal", "synthesize")), kinds.index(("committed", "collect_a"))
        )
        self.assertTrue(
            all(x["model_hash"] == snapshot["model_hash"] for x in snapshot["artifacts"])
        )
        self.assertTrue(
            all(
                x["artifact_version"] == "v1"
                and x["content_hash"] == hashlib.sha256(x["content"].encode()).hexdigest()
                for x in snapshot["artifacts"]
            )
        )

    def test_all_available_adapters(self):
        for adapter, python in interpreters():
            with self.subTest(adapter=adapter), tempfile.TemporaryDirectory() as folder:
                report = invoke(python, adapter, Path(folder))
                self.assert_finished(report)
                self.assertEqual(report["max_active"], 2)
                self.assertIsNotNone(report["first_artifact_seconds"])
                if adapter == "custom":
                    self.assertEqual(report["native_checkpoint_inventory"], {})
                else:
                    self.assertTrue(report["native_checkpoint_inventory"])

    def test_recovery_from_three_boundaries(self):
        for adapter, python in interpreters():
            for fault in ("after-first-commit", "before-commit", "during-step"):
                with (
                    self.subTest(adapter=adapter, fault=fault),
                    tempfile.TemporaryDirectory() as folder,
                ):
                    workdir = Path(folder)
                    interrupted = invoke(python, adapter, workdir, "--fault", fault)
                    self.assertTrue(interrupted["interrupted"])
                    resumed = invoke(python, adapter, workdir, "--resume")
                    self.assert_finished(resumed)
                    self.assertEqual(
                        interrupted["snapshot"]["model_hash"], resumed["snapshot"]["model_hash"]
                    )
                    self.assertTrue(
                        any(x["kind"] == "reclaimed" for x in resumed["snapshot"]["events"])
                    )
                    if adapter != "custom":
                        self.assertTrue(resumed["native_checkpoint_inventory"])

    def test_model_override_is_not_silent(self):
        sys.path.insert(0, str(ROOT / "experiments/runtime_comparison"))
        from common import MODEL

        for adapter, python in interpreters():
            with self.subTest(adapter=adapter), tempfile.TemporaryDirectory() as folder:
                workdir = Path(folder)
                invoke(python, adapter, workdir, "--fault", "before-commit")
                rejected = invoke(
                    python,
                    adapter,
                    workdir,
                    "--resume",
                    "--requested-model",
                    "different-model",
                    success=False,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("differs from the saved selected-model manifest", rejected.stderr)
                changed_settings = {**MODEL, "temperature": 0.5}
                rejected_settings = invoke(
                    python,
                    adapter,
                    workdir,
                    "--resume",
                    "--requested-settings-json",
                    json.dumps(changed_settings),
                    success=False,
                )
                self.assertNotEqual(rejected_settings.returncode, 0)
                self.assertIn("requested settings differ", rejected_settings.stderr)
                self.assert_finished(invoke(python, adapter, workdir, "--resume"))

    def test_duplicate_commit_has_one_effect(self):
        sys.path.insert(0, str(ROOT / "experiments/runtime_comparison"))
        from common import Board, scripted_decision

        with tempfile.TemporaryDirectory() as folder:
            board = Board(Path(folder) / "board.sqlite")
            board.seed()
            board.claim()  # collect_a remains claimed while collect_b finishes.
            task = board.claim()
            self.assertEqual(task.task_id, "collect_b")
            decision = scripted_decision(task.payload())
            self.assertEqual(board.commit(task, decision), "committed")
            before = board.snapshot()
            self.assertEqual(board.commit(task, decision), "duplicate")
            self.assertEqual(before, board.snapshot())
            board.close()

    def test_reclaimed_attempt_rejects_late_worker(self):
        sys.path.insert(0, str(ROOT / "experiments/runtime_comparison"))
        from common import Board, scripted_decision

        with tempfile.TemporaryDirectory() as folder:
            board = Board(Path(folder) / "board.sqlite")
            board.seed()
            stale = board.claim()
            board.reclaim_after_process_restart()
            current = board.claim()
            self.assertEqual((stale.task_id, current.task_id), ("collect_a", "collect_a"))
            self.assertEqual(board.commit(stale, scripted_decision(stale.payload())), "stale")
            self.assertEqual(
                board.commit(current, scripted_decision(current.payload())), "committed"
            )
            self.assertEqual(len(board.snapshot()["artifacts"]), 1)
            board.close()

    def test_conflicting_artifact_identity_does_not_mix_evidence(self):
        sys.path.insert(0, str(ROOT / "experiments/runtime_comparison"))
        from common import Board, scripted_decision

        with tempfile.TemporaryDirectory() as folder:
            board = Board(Path(folder) / "board.sqlite")
            board.seed()
            slow, quick = board.claim(), board.claim()
            self.assertEqual(board.commit(quick, scripted_decision(quick.payload())), "committed")
            conflict = scripted_decision(slow.payload())
            conflict["artifact_id"] = "artifact:collect_b"
            self.assertEqual(board.commit(slow, conflict), "repair")
            current = board.claim()
            self.assertEqual(
                board.commit(current, scripted_decision(current.payload())), "committed"
            )
            snapshot = board.snapshot()
            self.assertEqual(len(snapshot["artifacts"]), 2)
            self.assertEqual(len(snapshot["messages"]), 1)
            self.assertEqual([x["kind"] for x in snapshot["events"]].count("rejected_action"), 1)
            board.close()

    def test_public_benchmark_packet_event_identity(self):
        identities = []
        for adapter, python in interpreters():
            with self.subTest(adapter=adapter):
                process = subprocess.run(
                    [python, str(PACKET_PROBE), "--adapter", adapter],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(process.returncode, 0, process.stderr)
                result = json.loads(process.stdout)
                self.assertEqual(result["scenario_id"], "syn-route-recovery")
                self.assertEqual(
                    [x["delivered_event_id"] for x in result["delivered"]],
                    ["B-E1", "B-E2", "B-E3"],
                )
                self.assertTrue(all(x["initial_source_ids"] == ["B1"] for x in result["delivered"]))
                identities.append((result["scenario_sha256"], result["worker_packet_sha256"]))
        self.assertEqual(len(set(identities)), 1)


if __name__ == "__main__":
    unittest.main()
