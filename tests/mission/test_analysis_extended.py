"""V2 analysis tools calculate only from bounded, explicitly supplied numbers."""

import json
import math

import pytest
from pydantic import ValidationError

from ora.mission.analysis_extended import (
    EXTENDED_ANALYSIS_TOOL_DESCRIPTIONS,
    EXTENDED_ANALYSIS_TOOL_HANDLERS,
    EXTENDED_ANALYSIS_TOOL_PURPOSES,
    EXTENDED_ANALYSIS_TOOL_SCHEMAS,
    adjust_pvalues,
    analyze_contingency,
    analyze_paired_values,
    compare_summary_statistics,
    convert_values_units,
    correlate_values,
    describe_values,
)
from ora.mission.analysis_tools import AnalysisToolError


def _pairs(first, second):
    return [
        {"pair_id": f"participant-{index}", "value_a": a, "value_b": b}
        for index, (a, b) in enumerate(zip(first, second, strict=True))
    ]


def test_summary_welch_matches_hand_checked_independent_comparison():
    request = {
        "group_a": {"n": 8, "mean": 5, "sample_sd": 2},
        "group_b": {"n": 10, "mean": 3, "sample_sd": 1},
        "alternative": "two-sided", "units": "points",
    }
    result = compare_summary_statistics(**request)
    assert result["mean_difference_a_minus_b"] == "2"
    assert math.isclose(float(result["standard_error"]), math.sqrt(0.6), rel_tol=1e-11)
    assert math.isclose(float(result["degrees_of_freedom"]), 9.77586206897, rel_tol=1e-10)
    assert math.isclose(float(result["p_value"]), 0.027788504374, rel_tol=1e-10)
    assert math.isclose(float(result["confidence_interval_95_two_sided"]["low"]),
                        0.268712195692, rel_tol=1e-10)
    assert "sd_a^2/n_a" in result["formula"]
    assert result["software_versions"]["scipy"]
    assert result == compare_summary_statistics(**request)
    assert result["input_sha256"] != compare_summary_statistics(
        **{**request, "group_b": {"n": 10, "mean": 4, "sample_sd": 1}}
    )["input_sha256"]


def test_summary_welch_one_sided_direction_and_undefined_variance():
    base = {"group_a": {"n": 8, "mean": 5, "sample_sd": 2},
            "group_b": {"n": 10, "mean": 3, "sample_sd": 1}}
    greater = compare_summary_statistics(**base, alternative="greater")
    less = compare_summary_statistics(**base, alternative="less")
    assert float(greater["p_value"]) < float(less["p_value"])
    assert greater["confidence_interval_95_two_sided"] is None
    with pytest.raises(AnalysisToolError, match="uncertainty is zero"):
        compare_summary_statistics(
            group_a={"n": 8, "mean": 5, "sample_sd": 0},
            group_b={"n": 10, "mean": 3, "sample_sd": 0}, alternative="two-sided",
        )
    tiny = compare_summary_statistics(
        group_a={"n": 5, "mean": 1e-149, "sample_sd": 1e-150},
        group_b={"n": 6, "mean": 0, "sample_sd": 2e-150},
        alternative="two-sided",
    )
    assert math.isfinite(float(tiny["degrees_of_freedom"]))
    assert math.isfinite(float(tiny["p_value"]))


def test_summary_tail_underflow_and_narrow_large_offset_interval_are_explicit():
    extreme = compare_summary_statistics(
        group_a={"n": 1_000_000, "mean": 1e12, "sample_sd": 1e-12},
        group_b={"n": 1_000_000, "mean": -1e12, "sample_sd": 1e-12},
        alternative="two-sided",
    )
    assert extreme["p_value"] is None
    assert extreme["p_value_status"] == "zero_at_floating_precision_or_boundary"
    assert extreme["confidence_interval_95_two_sided"]["resolution_status"] == (
        "bounds_below_floating_precision"
    )
    narrow = compare_summary_statistics(
        group_a={"n": 1_000_000, "mean": 1e12, "sample_sd": 1},
        group_b={"n": 1_000_000, "mean": 0, "sample_sd": 1},
        alternative="two-sided",
    )
    ci = narrow["confidence_interval_95_two_sided"]
    assert ci["low"] != ci["high"]
    assert float(ci["low"]) < 1e12 < float(ci["high"])
    assert "round-trip" in narrow["numeric_policy"]
    below_ulp = compare_summary_statistics(
        group_a={"n": 1_000_000, "mean": 1e12, "sample_sd": 1e-4},
        group_b={"n": 1_000_000, "mean": 0, "sample_sd": 1e-4},
        alternative="two-sided",
    )["confidence_interval_95_two_sided"]
    assert below_ulp["low"] is None and below_ulp["high"] is None
    assert below_ulp["center"] == "1000000000000.0"
    assert float(below_ulp["half_width"]) > 0


def test_contingency_effects_are_explicit_and_counts_are_preserved():
    base = {"event_a": 20, "nonevent_a": 80, "event_b": 10, "nonevent_b": 90,
            "zero_cell_policy": "reject"}
    rr = analyze_contingency(**base, operation="risk_ratio")
    odds = analyze_contingency(**base, operation="odds_ratio")
    difference = analyze_contingency(**base, operation="risk_difference")
    assert rr["estimate"] == "2"
    assert odds["estimate"] == "2.25"
    assert difference["estimate"] == "0.1"
    assert rr["raw_counts"] == {"event_a": 20, "nonevent_a": 80,
                                "event_b": 10, "nonevent_b": 90}
    assert rr["effective_counts"]["event_a"] == "20"
    assert not rr["correction_applied"]
    assert float(rr["confidence_interval_95_two_sided"]["low"]) < 1
    assert float(rr["confidence_interval_95_two_sided"]["high"]) > 4
    assert any("at-risk denominators" in caveat for caveat in rr["assumptions_not_checked"])
    assert float(difference["confidence_interval_95_two_sided"]["low"]) < 0.1
    assert "Wald" in difference["method"]


def test_contingency_zero_cell_policy_and_degenerate_risk_difference():
    base = {"event_a": 0, "nonevent_a": 100, "event_b": 20, "nonevent_b": 80}
    with pytest.raises(AnalysisToolError, match="zero cell"):
        analyze_contingency(**base, operation="odds_ratio", zero_cell_policy="reject")
    adjusted = analyze_contingency(**base, operation="odds_ratio", zero_cell_policy="add_half")
    assert adjusted["raw_counts"]["event_a"] == 0
    assert adjusted["effective_counts"]["event_a"] == "0.5"
    assert adjusted["correction_applied"]
    assert 0 < float(adjusted["estimate"]) < 1
    rd = analyze_contingency(**base, operation="risk_difference", zero_cell_policy="reject")
    assert rd["estimate"] == "-0.2"
    assert not rd["correction_applied"]
    with pytest.raises(AnalysisToolError, match="uncertainty"):
        analyze_contingency(
            event_a=0, nonevent_a=10, event_b=10, nonevent_b=0,
            operation="risk_difference", zero_cell_policy="reject",
        )


def test_risk_ratio_accepts_valid_zero_nonevent_cell_without_correction():
    base = {"event_a": 100, "nonevent_a": 0, "event_b": 50, "nonevent_b": 50}
    rr = analyze_contingency(**base, operation="risk_ratio", zero_cell_policy="reject")
    assert rr["estimate"] == "2"
    assert rr["standard_error_on_test_scale"] == "0.1"
    assert not rr["correction_applied"]
    assert rr["effective_counts"]["nonevent_a"] == "0"
    with pytest.raises(AnalysisToolError, match="zero cell"):
        analyze_contingency(**base, operation="odds_ratio", zero_cell_policy="reject")
    corrected = analyze_contingency(**base, operation="odds_ratio", zero_cell_policy="add_half")
    assert corrected["correction_applied"]
    assert corrected["effective_counts"]["nonevent_a"] == "0.5"


def test_contingency_extreme_wald_tail_is_not_reported_as_exact_zero():
    result = analyze_contingency(
        event_a=750_000, nonevent_a=250_000,
        event_b=250_000, nonevent_b=750_000,
        operation="risk_difference", zero_cell_policy="reject",
    )
    assert result["estimate"] == "0.5"
    assert result["p_value_two_sided_wald"] is None
    assert result["p_value_status"] == "zero_at_floating_precision_or_boundary"


def test_paired_t_uses_within_pair_differences_and_direction():
    pairs = _pairs([5, 8, 7, 9], [4, 5, 3, 7])
    result = analyze_paired_values(pairs=pairs, alternative="two-sided")
    assert result["mean_difference_a_minus_b"] == "2.5"
    assert math.isclose(float(result["statistic"]), 3.87298334621, rel_tol=1e-10)
    assert result["degrees_of_freedom"] == "3"
    assert math.isclose(float(result["p_value"]), 0.0304662916622, rel_tol=1e-10)
    assert float(result["confidence_interval_95_two_sided"]["low"]) > 0
    less = analyze_paired_values(pairs=pairs, alternative="less")
    assert float(less["p_value"]) > 0.9
    assert less["confidence_interval_95_two_sided"] is None
    with pytest.raises(AnalysisToolError, match="constant"):
        analyze_paired_values(pairs=_pairs([3, 4], [1, 2]), alternative="two-sided")


def test_paired_t_rejects_scipy_precision_loss_in_nearly_identical_differences():
    almost_same = [1e12, 1e12 - 2**-13, 1e12 - 2**-12, 1e12 - 3 * 2**-13]
    with pytest.raises(AnalysisToolError, match="loses precision"):
        analyze_paired_values(
            pairs=_pairs(almost_same, [0, 0, 0, 0]), alternative="two-sided",
        )


def test_pearson_correlation_uses_id_matched_pairs_and_rejects_constant_input():
    pairs = _pairs([1, 2, 3, 4, 5], [2, 4, 5, 4, 6])
    result = correlate_values(pairs=pairs, method="pearson", alternative="two-sided")
    assert result["n_pairs"] == 5
    # Hand covariance: sum((x-3)*(y-4.2))=8; sums of squares 10 and 8.8.
    assert math.isclose(float(result["coefficient_r"]), 8 / math.sqrt(88), rel_tol=1e-10)
    assert math.isclose(float(result["p_value"]), 0.0662756027415, rel_tol=1e-10)
    assert result["confidence_interval_95_two_sided_fisher"] is not None
    assert float(result["confidence_interval_95_two_sided_fisher"]["low"]) < 0
    greater = correlate_values(pairs=pairs, method="pearson", alternative="greater")
    assert float(greater["p_value"]) < float(result["p_value"])
    assert greater["confidence_interval_95_two_sided_fisher"] is None
    with pytest.raises(AnalysisToolError, match="constant"):
        correlate_values(pairs=_pairs([1, 1, 1, 1], [1, 2, 3, 4]),
                         method="pearson", alternative="two-sided")


def test_bh_and_by_adjustments_preserve_ids_and_monotone_sorted_q_values():
    tests = [{"test_id": name, "p_value": p} for name, p in
             [("a", 0.01), ("b", 0.04), ("c", 0.03), ("d", 0.2)]]
    base = {"tests": tests, "alpha": 0.05, "family_id": "primary_family"}
    bh = adjust_pvalues(**base, method="benjamini_hochberg")
    by = adjust_pvalues(**base, method="benjamini_yekutieli")
    assert [item["test_id"] for item in bh["tests"]] == ["a", "b", "c", "d"]
    assert [item["adjusted_p_value"] for item in bh["tests"]] == [
        "0.04", "0.05333333333333333", "0.05333333333333333", "0.2",
    ]
    assert [item["reject_at_alpha"] for item in bh["tests"]] == [True, False, False, False]
    assert float(by["tests"][0]["adjusted_p_value"]) > float(bh["tests"][0]["adjusted_p_value"])
    assert not any(item["reject_at_alpha"] for item in by["tests"])
    assert bh["dependency_multiplier"] == "1"
    assert math.isclose(float(by["dependency_multiplier"]), 2.08333333333, rel_tol=1e-10)
    json.dumps(bh, allow_nan=False)
    json.dumps(by, allow_nan=False)


def test_fdr_displays_decision_precision_without_rounding_across_alpha():
    result = adjust_pvalues(
        tests=[{"test_id": "borderline", "p_value": 0.02500000000001},
               {"test_id": "other", "p_value": 1}],
        method="benjamini_hochberg", alpha=0.05, family_id="specified_family",
    )
    borderline = result["tests"][0]
    assert result["alpha"] == "0.05"
    assert borderline["raw_p_value"] == "0.02500000000001"
    assert borderline["adjusted_p_value"] == "0.05000000000002"
    assert borderline["reject_at_alpha"] is False
    assert "unrounded" in result["numeric_policy"]


def test_linear_quartiles_and_batch_units_are_finite_and_bounded():
    result = describe_values(values=[1, 2, 3, 4], quantile_method="linear", units="hours")
    assert {key: result[key] for key in ("mean", "sample_sd", "q1", "median", "q3", "iqr")} == {
        "mean": "2.5", "sample_sd": "1.29099444874", "q1": "1.75",
        "median": "2.5", "q3": "3.25", "iqr": "1.5",
    }
    converted = convert_values_units(
        values=[1000, 2500], from_unit="milligram", to_unit="gram",
    )
    assert converted["converted_values"] == ["1", "2.5"]
    assert converted["conversion_factor"] == "0.001"
    assert converted["software_versions"]["pint"]
    with pytest.raises(AnalysisToolError, match="incompatible"):
        convert_values_units(values=[1], from_unit="gram", to_unit="second")


@pytest.mark.parametrize("function,arguments", [
    (compare_summary_statistics, {"group_a": {"n": True, "mean": 1, "sample_sd": 1},
                                  "group_b": {"n": 2, "mean": 1, "sample_sd": 1},
                                  "alternative": "two-sided"}),
    (compare_summary_statistics, {"group_a": {"n": 2, "mean": 1, "sample_sd": -1},
                                  "group_b": {"n": 2, "mean": 1, "sample_sd": 1},
                                  "alternative": "two-sided"}),
    (analyze_contingency, {"event_a": True, "nonevent_a": 2, "event_b": 1,
                           "nonevent_b": 2, "operation": "risk_ratio", "zero_cell_policy": "reject"}),
    (analyze_contingency, {"event_a": 0, "nonevent_a": 0, "event_b": 1,
                           "nonevent_b": 2, "operation": "risk_ratio", "zero_cell_policy": "add_half"}),
    (analyze_contingency, {"event_a": 0, "nonevent_a": 2, "event_b": 1,
                           "nonevent_b": 2, "operation": "risk_difference", "zero_cell_policy": "add_half"}),
    (analyze_paired_values, {"pairs": _pairs([1, 2], [2, 3]), "alternative": "unknown"}),
    (analyze_paired_values, {"pairs": [{"pair_id": "same", "value_a": 1, "value_b": 2},
                                       {"pair_id": "same", "value_a": 2, "value_b": 3}],
                             "alternative": "two-sided"}),
    (correlate_values, {"pairs": _pairs([1, 2, 3, 4], [1, 2, 3, None]),
                        "method": "pearson", "alternative": "two-sided"}),
    (adjust_pvalues, {"tests": [{"test_id": "a", "p_value": float("nan")},
                                 {"test_id": "b", "p_value": 0.1}],
                       "method": "benjamini_hochberg", "alpha": 0.05, "family_id": "f"}),
    (adjust_pvalues, {"tests": [{"test_id": "a", "p_value": 0.1},
                                 {"test_id": "a", "p_value": 0.2}],
                       "method": "benjamini_hochberg", "alpha": 0.05, "family_id": "f"}),
    (adjust_pvalues, {"tests": [{"test_id": "a", "p_value": 0.1},
                                 {"test_id": "b", "p_value": 0.2}],
                       "method": "benjamini_hochberg", "alpha": 0, "family_id": "f"}),
    (adjust_pvalues, {"tests": [{"test_id": "a", "p_value": 0.1},
                                 {"test_id": "b", "p_value": 0.2}],
                       "method": "benjamini_hochberg", "alpha": 1, "family_id": "f"}),
    (describe_values, {"values": [1, "2"], "quantile_method": "linear"}),
    (convert_values_units, {"values": [1, float("inf")],
                            "from_unit": "gram", "to_unit": "milligram"}),
])
def test_invalid_scientific_inputs_are_rejected_before_computation(function, arguments):
    with pytest.raises(ValidationError):
        function(**arguments)


def test_every_tool_has_canonical_schema_and_json_finite_receipt():
    names = set(EXTENDED_ANALYSIS_TOOL_HANDLERS)
    assert len(names) == 7
    assert names == set(EXTENDED_ANALYSIS_TOOL_SCHEMAS)
    assert names == set(EXTENDED_ANALYSIS_TOOL_DESCRIPTIONS)
    assert names == set(EXTENDED_ANALYSIS_TOOL_PURPOSES)
    assert all(schema["additionalProperties"] is False
               for schema in EXTENDED_ANALYSIS_TOOL_SCHEMAS.values())
    example = analyze_contingency(
        event_a=0, nonevent_a=10, event_b=3, nonevent_b=7,
        operation="risk_ratio", zero_cell_policy="add_half",
    )
    json.dumps(example, allow_nan=False)
    assert len(example["input_sha256"]) == 64
    assert "not_source_design_or_causal_validation" in example["validation_scope"]


@pytest.mark.parametrize("scale", [1.0, 1e-70, 1e-90, 1e-100, 1e-160, 1e-161, 1e-300])
def test_summary_welch_is_invariant_to_small_measurement_units(scale):
    # The unscaled t is sqrt(5), df=18. Squaring tiny variances inside SciPy
    # previously changed its p-value to a df=1 result while our receipt said 18.
    result = compare_summary_statistics(
        group_a={"n": 10, "mean": scale, "sample_sd": scale},
        group_b={"n": 10, "mean": 0, "sample_sd": scale},
        alternative="two-sided",
    )
    assert float(result["degrees_of_freedom"]) == 18
    assert float(result["statistic"]) == pytest.approx(math.sqrt(5), rel=1e-11)
    assert float(result["p_value"]) == pytest.approx(0.03824961451611385, rel=1e-10)
    assert float(result["standard_error"]) / scale == pytest.approx(1 / math.sqrt(5), rel=1e-10)
    interval = result["confidence_interval_95_two_sided"]
    assert float(interval["low"]) / scale == pytest.approx(0.0604391005187, rel=1e-10)
    assert float(interval["high"]) / scale == pytest.approx(1.93956089948, rel=1e-10)


def test_fdr_family_summary_uses_all_tests_and_unrounded_decisions():
    result = adjust_pvalues(
        tests=[{"test_id": "a", "p_value": 0.01},
               {"test_id": "b", "p_value": 0.04},
               {"test_id": "c", "p_value": 0.5}],
        method="benjamini_hochberg", alpha=0.05, family_id="complete-family",
    )
    assert result["n_tests"] == 3
    assert result["n_rejected"] == 1
    assert result["adjusted_p_range"] == ["0.03", "0.5"]
    assert result["n_rejected"] == sum(row["reject_at_alpha"] for row in result["tests"])


@pytest.mark.parametrize("scale", [1.0, 1e-160, 1e-161, 1e-300])
def test_summary_welch_preserves_asymmetric_df_and_uncertainty_across_units(scale):
    result = compare_summary_statistics(
        group_a={"n": 8, "mean": 3 * scale, "sample_sd": 2 * scale},
        group_b={"n": 11, "mean": 0.5 * scale, "sample_sd": 0.7 * scale},
        alternative="two-sided",
    )
    assert float(result["degrees_of_freedom"]) == pytest.approx(8.25695713107, rel=1e-10)
    assert float(result["statistic"]) == pytest.approx(3.38784037956, rel=1e-10)
    assert float(result["p_value"]) == pytest.approx(0.00909722588121, rel=1e-10)
    assert float(result["standard_error"]) / scale == pytest.approx(0.737933231767, rel=1e-10)
    ci = result["confidence_interval_95_two_sided"]
    assert float(ci["low"]) / scale == pytest.approx(0.807496756249, rel=1e-10)
    assert float(ci["high"]) / scale == pytest.approx(4.19250324375, rel=1e-10)


@pytest.mark.parametrize("mean,sd", [(0.0, 1e-309), (1e12, 1e-300)])
def test_summary_welch_rejects_unreliable_se_or_overflowed_standardized_difference(mean, sd):
    with pytest.raises(AnalysisToolError, match="precision|resolution"):
        compare_summary_statistics(
            group_a={"n": 10, "mean": mean, "sample_sd": sd},
            group_b={"n": 10, "mean": -mean, "sample_sd": sd},
            alternative="two-sided",
        )
