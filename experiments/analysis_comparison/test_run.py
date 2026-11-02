"""Synthetic algebra and input integrity tests for the isolated experiment."""

import json
import unittest
from pathlib import Path

import run


class AnalysisComparisonTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((Path(__file__).parent / "fixture.json").read_text())

    def test_reference_matches_pinned_statsmodels_methods(self):
        result = run.compare(self.data, input_sha256="synthetic")
        self.assertTrue(result["numeric_match"], result["absolute_differences"])
        self.assertGreater(result["reference"]["tau2"], 0)
        self.assertEqual(result["study_ids"], [row["study_id"] for row in self.data["studies"]])

    def test_duplicate_study_is_rejected(self):
        self.data["studies"][1]["study_id"] = self.data["studies"][0]["study_id"]
        with self.assertRaisesRegex(ValueError, "unique"):
            run.validate_input(self.data)

    def test_missing_or_invalid_variance_is_rejected(self):
        for bad in (None, 0, -0.01, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                self.data["studies"][0]["variance"] = bad
                with self.assertRaises(ValueError):
                    run.validate_input(self.data)
        del self.data["studies"][0]["variance"]
        with self.assertRaises(ValueError):
            run.validate_input(self.data)

    def test_low_heterogeneity_exposes_negative_untruncated_candidate_tau2(self):
        for row, effect in zip(self.data["studies"], (0.20, 0.21, 0.19, 0.20, 0.19)):
            row["effect"] = effect
            row["variance"] = 0.04
        result = run.compare(self.data, input_sha256="synthetic-low-heterogeneity")
        self.assertLess(result["statsmodels"]["tau2"], 0)
        self.assertEqual(result["reference"]["tau2"], 0)
        self.assertEqual(result["candidate_status"], "negative_tau2_mismatch")
        self.assertFalse(result["numeric_match"])

    def test_identical_effects_record_nonfinite_candidate_without_json_nan(self):
        for row in self.data["studies"]:
            row["effect"] = 0.1
            row["variance"] = 0.1
        result = run.compare(self.data, input_sha256="synthetic-identical")
        self.assertEqual(result["candidate_status"], "nonfinite_candidate")
        self.assertLess(result["statsmodels"]["tau2"], 0)
        self.assertEqual(result["reference"]["tau2"], 0)
        self.assertIsNone(result["statsmodels"]["random_mean"])
        self.assertEqual(result["candidate_special_values"]["random_mean"], "NaN")
        self.assertFalse(result["numeric_match"])
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
