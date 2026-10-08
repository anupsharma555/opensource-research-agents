"""Behavioral checks for synthetic packet identity and answer-key separation."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ora.benchmarks import (
    BenchmarkError,
    build_manifest,
    export_worker_packet,
    load_event_script,
    load_reference,
    load_scenario,
    make_worker_packet,
    validate_catalog,
    validate_worker_packet,
)

ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "v1"


class PacketTests(unittest.TestCase):
    def test_catalog_checks_pinned_sources_spans_and_profiles(self) -> None:
        report = validate_catalog(ROOT)
        self.assertEqual(report["scenario_count"], 3)
        self.assertEqual(report["profile_count"], 5)
        self.assertEqual(report["condition_count"], 2)
        scenario = load_scenario(ROOT, "syn-abstract-disagreement")
        a1 = next(source for source in scenario["sources"] if source["source_id"] == "A1")
        result = next(span for span in a1["spans"] if span["span_id"] == "A1-result")
        self.assertEqual(a1["body"][result["start"] : result["end"]], result["quote"])

    def test_worker_receives_initial_sources_but_no_future_events_or_answers(self) -> None:
        packet = make_worker_packet(ROOT, "syn-route-recovery", "peer_exchange", "controlled_equal_input")
        self.assertEqual([source["source_id"] for source in packet["sources"]], ["B1"])
        self.assertEqual(packet["task"]["question"], load_scenario(ROOT, "syn-route-recovery")["task"]["question"])
        self.assertNotIn("scripted_events", packet)
        self.assertNotIn("reference_path", packet)
        self.assertNotIn("expected_partial_outcome", packet)
        reference = load_reference(ROOT, "syn-route-recovery")
        self.assertNotIn(reference["withheld_marker"], json.dumps(packet))
        self.assertEqual([event["event_id"] for event in load_event_script(ROOT, "syn-route-recovery")], ["B-E1", "B-E2", "B-E3"])
        validate_worker_packet(ROOT, packet)

    def test_route_recovery_reference_cites_its_denominators(self) -> None:
        scenario = load_scenario(ROOT, "syn-route-recovery")
        reference = load_reference(ROOT, "syn-route-recovery")
        spans = {span["span_id"]: span for source in scenario["sources"] for span in source["spans"]}
        claims = {claim["claim_id"]: claim for claim in reference["acceptable_claims"]}
        self.assertIn("B2-denominator", claims["B-C1"]["support_span_ids"])
        self.assertIn("B3-denominator", claims["B-C2"]["support_span_ids"])
        self.assertIn("120 youth", spans["B2-denominator"]["quote"])
        self.assertIn("90 youth", spans["B3-denominator"]["quote"])

    def test_result_map_tracks_current_packets_without_claiming_future_proof(self) -> None:
        path = ROOT.parent.parent / "docs/benchmarks/evidence/scenario-result-map.json"
        result_map = json.loads(path.read_text(encoding="utf-8"))
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(result_map["packet_set_version"], manifest["packet_set_version"])
        entries = {entry["scenario_id"]: entry for entry in manifest["scenarios"]}
        for result in result_map["scenario_results"]:
            entry = entries[result["scenario_id"]]
            for field in ("scenario_version", "scenario_sha256", "reference_sha256"):
                self.assertEqual(result[field], entry[field])
            self.assertEqual([item["status"] for item in result["consumers"]],
                             ["passed", "untested", "untested"])

    def test_native_condition_keeps_question_but_not_fixed_source_snapshots(self) -> None:
        controlled = make_worker_packet(ROOT, "syn-abstract-disagreement", "single_role", "controlled_equal_input")
        native = make_worker_packet(ROOT, "syn-abstract-disagreement", "single_role", "native_workflow")
        self.assertEqual(controlled["task"], native["task"])
        self.assertTrue(controlled["sources"])
        self.assertEqual(native["sources"], [])
        self.assertEqual(native["source_access"], "native_authorized_workflow")
        self.assertNotEqual(controlled["worker_packet_sha256"], native["worker_packet_sha256"])

    def test_export_is_a_single_key_free_packet_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.json"
            export_worker_packet(ROOT, "syn-null-versus-error", "single_role", "controlled_equal_input", path)
            self.assertEqual([entry.name for entry in Path(directory).iterdir()], ["worker.json"])
            packet = json.loads(path.read_text(encoding="utf-8"))
            validate_worker_packet(ROOT, packet)
            self.assertNotIn("WITHHELD_REFERENCE", path.read_text(encoding="utf-8"))
            with self.assertRaises(FileExistsError):
                export_worker_packet(ROOT, "syn-null-versus-error", "single_role", "controlled_equal_input", path)

    def test_changed_source_or_reference_fails_frozen_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "packet"
            shutil.copytree(ROOT, root)
            source_path = root / "scenarios" / "syn-abstract-disagreement.json"
            scenario = json.loads(source_path.read_text(encoding="utf-8"))
            scenario["sources"][0]["body"] += " An unversioned change."
            source_path.write_text(json.dumps(scenario), encoding="utf-8")
            with self.assertRaisesRegex(BenchmarkError, "Frozen packet changed"):
                load_scenario(root, scenario["scenario_id"])
            shutil.copyfile(ROOT / "scenarios" / source_path.name, source_path)
            reference_path = root / "withheld" / "syn-abstract-disagreement.reference.json"
            reference = json.loads(reference_path.read_text(encoding="utf-8"))
            reference["acceptable_claims"][0]["text"] += " Changed label."
            reference_path.write_text(json.dumps(reference), encoding="utf-8")
            with self.assertRaisesRegex(BenchmarkError, "Frozen packet changed"):
                load_reference(root, scenario["scenario_id"])

    def test_span_corruption_fails_even_if_someone_tries_to_rehash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "packet"
            shutil.copytree(ROOT, root)
            source_path = root / "scenarios" / "syn-abstract-disagreement.json"
            scenario = json.loads(source_path.read_text(encoding="utf-8"))
            scenario["sources"][0]["spans"][0]["start"] += 1
            source_path.write_text(json.dumps(scenario), encoding="utf-8")
            with self.assertRaisesRegex(BenchmarkError, "Passage locator"):
                build_manifest(root)

    def test_worker_packet_cannot_grow_reference_fields(self) -> None:
        packet = make_worker_packet(ROOT, "syn-null-versus-error", "single_role", "controlled_equal_input")
        altered = copy.deepcopy(packet)
        altered["gold_answer"] = "A hidden label must not be attached to worker input."
        with self.assertRaisesRegex(BenchmarkError, "differs from the frozen"):
            validate_worker_packet(ROOT, altered)


if __name__ == "__main__":
    unittest.main()
