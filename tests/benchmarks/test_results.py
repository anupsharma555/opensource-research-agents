"""Check that a saved comparison result names the evidence it actually used.

The tests reject unknown citations, missing source access, impossible resource
values, and old passes presented as current after an input or build change. They
do not decide whether a cited passage supports the written conclusion.
"""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from ora.benchmarks import (
    BenchmarkError,
    make_worker_packet,
    result_applicability,
    validate_run_record,
)

ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "v1"


def fixture_run() -> dict:
    packet = make_worker_packet(ROOT, "syn-abstract-disagreement", "single_role", "controlled_equal_input")
    record = json.loads((ROOT / "run-record-template.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    record.update({
        "run_id": "synthetic-structural-check-1",
        "scenario_id": packet["scenario_id"],
        "scenario_version": packet["scenario_version"],
        "scenario_sha256": packet["scenario_sha256"],
        "profile_id": packet["profile_id"],
        "arm_id": "same_model_single_agent",
        "condition_id": packet["condition_id"],
        "proof_type": "fixture",
        "build_or_product_version": "offline-structural-test-v1",
        "configuration_sha256": hashlib.sha256(b"synthetic-config-v1").hexdigest(),
        "rubric_sha256": manifest["rubric_sha256"],
        "model_id": "none:fixture-only-no-model-call",
        "source_access": [{"source_id": "A1", "route": "fixed_packet", "access_depth": "abstract_only"}],
        "claims": [{"claim_id": "example-claim", "kind": "source_claim", "text": "A1 reports the fixture numbers.", "citation_span_ids": ["A1-result"]}],
    })
    return record


class ResultTests(unittest.TestCase):
    def test_structural_checker_does_not_pretend_to_judge_support(self) -> None:
        run = fixture_run()
        report = validate_run_record(ROOT, run)
        self.assertEqual(report["locatable_citation_span_count"], 1)
        self.assertEqual(report["unique_fixture_source_count"], 1)
        self.assertEqual(report["scientific_support"], "not_scored_by_structural_checker")
        self.assertEqual(report["reader_usefulness"], "not_scored_by_structural_checker")
        # A real scientific reviewer, not this checker, must reject this false claim.
        run["claims"][0]["text"] = "The fixture proves an unrelated impossible conclusion."
        self.assertEqual(validate_run_record(ROOT, run)["scientific_support"], "not_scored_by_structural_checker")

    def test_unknown_or_unencountered_citation_is_rejected(self) -> None:
        run = fixture_run()
        run["claims"][0]["citation_span_ids"] = ["not-a-span"]
        with self.assertRaisesRegex(BenchmarkError, "locatable"):
            validate_run_record(ROOT, run)
        run = fixture_run()
        run["claims"][0]["citation_span_ids"] = ["A2-result"]
        with self.assertRaisesRegex(BenchmarkError, "absent from its recorded access"):
            validate_run_record(ROOT, run)

    def test_error_and_null_fixture_remain_distinct(self) -> None:
        packet = make_worker_packet(ROOT, "syn-null-versus-error", "single_role", "controlled_equal_input")
        self.assertEqual([source["source_id"] for source in packet["sources"]], ["C1"])
        self.assertIn("imprecise null result", packet["sources"][0]["body"])
        self.assertNotIn("parser exception", json.dumps(packet))

    def test_resource_units_remain_separate_and_unknown_is_not_zero(self) -> None:
        run = fixture_run()
        self.assertIsNone(run["resource_use"]["provider_usage"])
        run["resource_use"]["calls"] = 2
        run["resource_use"]["active_seconds"] = 8.5
        run["resource_use"]["provider_usage"] = [
            {"provider": "fixture-route-a", "unit": "request", "amount": 1},
            {"provider": "fixture-route-b", "unit": "credit", "amount": 3},
        ]
        self.assertEqual(validate_run_record(ROOT, run)["proof_type"], "fixture")
        run["resource_use"]["provider_usage"][1]["amount"] = -1
        with self.assertRaisesRegex(BenchmarkError, "Provider usage amount"):
            validate_run_record(ROOT, run)
        run["resource_use"]["provider_usage"][1]["amount"] = 3
        run["resource_use"]["active_seconds"] = float("nan")
        with self.assertRaisesRegex(BenchmarkError, "nonnegative or unknown"):
            validate_run_record(ROOT, run)

    def test_controlled_model_run_needs_predeclared_test_allowance(self) -> None:
        run = fixture_run()
        run["proof_type"] = "selected_model"
        with self.assertRaisesRegex(BenchmarkError, "predeclared numeric evaluation allowance"):
            validate_run_record(ROOT, run)
        run["evaluation_allowance"].update({
            "comparison_group_id": "synthetic-a-single-role-example",
            "declared_before_run_utc": "2026-10-08T00:00:00Z",
            "max_model_calls": 4,
            "max_model_tokens_total": 20000,
            "max_tool_calls": 0,
            "max_currency_amount": 1.0,
            "currency_code": "USD",
            "fixed_source_ids": ["A1", "A2", "A3", "A4"],
        })
        self.assertEqual(validate_run_record(ROOT, run)["scientific_support"], "not_scored_by_structural_checker")
        run["evaluation_allowance"]["max_model_tokens_total"] = 1.5
        with self.assertRaisesRegex(BenchmarkError, "whole units"):
            validate_run_record(ROOT, run)

    def test_inference_must_name_an_existing_premise(self) -> None:
        run = fixture_run()
        run["claims"].append({"claim_id": "inference-1", "kind": "inference", "text": "A bounded interpretation.", "premise_claim_ids": ["missing"]})
        with self.assertRaisesRegex(BenchmarkError, "unknown premise"):
            validate_run_record(ROOT, run)
        run["claims"][-1]["premise_claim_ids"] = ["example-claim"]
        validate_run_record(ROOT, run)

    def test_changed_packet_build_or_config_does_not_inherit_pass(self) -> None:
        run = fixture_run()
        current = dict(
            scenario_version=run["scenario_version"],
            scenario_sha256=run["scenario_sha256"],
            profile_id=run["profile_id"],
            arm_id=run["arm_id"],
            condition_id=run["condition_id"],
            build_or_product_version=run["build_or_product_version"],
            configuration_sha256=run["configuration_sha256"],
            rubric_sha256=run["rubric_sha256"],
            reference_sha256=None,
        )
        self.assertEqual(result_applicability(run, **current), "current")
        changed = copy.deepcopy(current)
        changed["scenario_sha256"] = "new-packet-digest"
        self.assertEqual(result_applicability(run, **changed), "historical_untested_for_current")
        changed = copy.deepcopy(current)
        changed["configuration_sha256"] = "new-config-digest"
        self.assertEqual(result_applicability(run, **changed), "historical_untested_for_current")
        changed = copy.deepcopy(current)
        changed["profile_id"] = "complete_workflow"
        self.assertEqual(result_applicability(run, **changed), "historical_untested_for_current")
        changed = copy.deepcopy(current)
        changed["rubric_sha256"] = "new-rubric-digest"
        self.assertEqual(result_applicability(run, **changed), "historical_untested_for_current")

    def test_scored_rating_requires_frozen_reference_and_reviewer(self) -> None:
        run = fixture_run()
        run["assessment"]["ratings"] = {"reader_usefulness": 2}
        with self.assertRaisesRegex(BenchmarkError, "reviewer IDs"):
            validate_run_record(ROOT, run)
        run["assessment"]["reviewer_ids"] = ["independent-fixture-reviewer"]
        with self.assertRaisesRegex(BenchmarkError, "reference hash"):
            validate_run_record(ROOT, run)
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        entry = next(x for x in manifest["scenarios"] if x["scenario_id"] == run["scenario_id"])
        run["assessment"]["reference_sha256"] = entry["reference_sha256"]
        validate_run_record(ROOT, run)

    def test_unhashed_configuration_cannot_masquerade_as_pinned(self) -> None:
        run = fixture_run()
        run["configuration_sha256"] = "moving-defaults"
        with self.assertRaisesRegex(BenchmarkError, "lowercase SHA-256"):
            validate_run_record(ROOT, run)


if __name__ == "__main__":
    unittest.main()
