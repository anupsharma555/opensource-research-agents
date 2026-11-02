"""The named quantitative tools compute only from bounded supplied values."""

import math

import pytest
from pydantic import ValidationError

from ora.mission.analysis_tools import (
    ANALYSIS_TOOL_SCHEMAS,
    AnalysisToolError,
    analyze_values,
    convert_units,
)


def test_describe_returns_reproducible_receipt_without_statistical_inference():
    request = {"operation": "describe", "group_a": [1, 2, 3], "units": "seconds"}
    first = analyze_values(**request)
    assert first == analyze_values(**request)
    assert first["group_a"] == {
        "n": 3, "mean": "2", "sample_sd": "1", "minimum": "1", "maximum": "3",
    }
    assert first["comparison"] is None
    assert first["software_versions"]["python"]
    assert first["validation_scope"].endswith("not_source_or_design_validation")
    assert len(first["input_sha256"]) == 64
    assert first["input_sha256"] != analyze_values(operation="describe", group_a=[1, 2, 4])["input_sha256"]


def test_welch_comparison_is_explicit_and_returns_finite_test_receipt():
    result = analyze_values(
        operation="welch_ttest", group_a=[1, 2, 3], group_b=[4, 5, 6],
        alternative="two-sided", units="points",
    )
    comparison = result["comparison"]
    assert result["group_a"]["mean"] == "2"
    assert result["group_b"]["mean"] == "5"
    assert comparison["mean_difference_a_minus_b"] == "-3"
    assert float(comparison["statistic"]) < 0
    assert 0 < float(comparison["p_value"]) < 0.05
    assert math.isclose(float(comparison["degrees_of_freedom"]), 4)
    assert float(comparison["confidence_interval_95_two_sided"]["high"]) < 0
    assert result["software_versions"]["scipy"] == "1.18.1"
    assert result["software_versions"]["numpy"]
    assert "independent" in result["method"]


def test_one_sided_alternatives_preserve_direction_without_mislabelled_interval():
    base = {"operation": "welch_ttest", "group_a": [1, 2, 3], "group_b": [4, 5, 6]}
    less = analyze_values(**base, alternative="less")["comparison"]
    greater = analyze_values(**base, alternative="greater")["comparison"]
    assert float(less["p_value"]) < float(greater["p_value"])
    assert less["confidence_interval_95_two_sided"] is None
    assert greater["confidence_interval_95_two_sided"] is None


@pytest.mark.parametrize("group", [
    [1, True], [1, None], [1, "2"], [1, float("nan")],
    [1, float("inf")], [1, [2]], [1], [1] * 201, [1, 1e13],
])
def test_invalid_values_and_sizes_are_rejected(group):
    with pytest.raises(ValidationError):
        analyze_values(operation="describe", group_a=group)


@pytest.mark.parametrize("arguments", [
    {"operation": "describe", "group_a": [1, 2], "group_b": [3, 4]},
    {"operation": "welch_ttest", "group_a": [1, 2], "group_b": [3, 4]},
    {"operation": "welch_ttest", "group_a": [1, 2], "group_b": [3, 4], "alternative": "unknown"},
    {"operation": "regression", "group_a": [1, 2]},
])
def test_unsupported_analysis_design_is_rejected(arguments):
    with pytest.raises(ValidationError):
        analyze_values(**arguments)


def test_both_constant_groups_have_undefined_test_uncertainty():
    with pytest.raises(AnalysisToolError, match="constant"):
        analyze_values(
            operation="welch_ttest", group_a=[1, 1], group_b=[2, 2],
            alternative="two-sided",
        )


def test_unit_conversion_is_dimension_checked_and_versioned():
    result = convert_units(value=1000, from_unit="milligram", to_unit="gram")
    assert result["result"] == "1"
    assert result["conversion_factor"] == "0.001"
    assert result["software_versions"]["pint"] == "0.26.2"
    assert "not_source_or_clinical_equivalence" in result["validation_scope"]
    assert result == convert_units(value=1000, from_unit="milligram", to_unit="gram")
    assert convert_units(value=1, from_unit="liter", to_unit="milliliter")["result"] == "1000"
    with pytest.raises(AnalysisToolError, match="incompatible"):
        convert_units(value=1, from_unit="gram", to_unit="second")


@pytest.mark.parametrize("arguments", [
    {"value": True, "from_unit": "gram", "to_unit": "milligram"},
    {"value": float("nan"), "from_unit": "gram", "to_unit": "milligram"},
    {"value": 1e13, "from_unit": "gram", "to_unit": "milligram"},
    {"value": 1, "from_unit": "made_up_unit", "to_unit": "gram"},
])
def test_conversion_rejects_invalid_inputs(arguments):
    with pytest.raises(ValidationError):
        convert_units(**arguments)


def test_generated_tool_schemas_are_from_canonical_models():
    assert set(ANALYSIS_TOOL_SCHEMAS) == {"analyze_values", "convert_units"}
    assert ANALYSIS_TOOL_SCHEMAS["analyze_values"]["properties"]["operation"]["enum"] == [
        "describe", "welch_ttest",
    ]
    assert ANALYSIS_TOOL_SCHEMAS["convert_units"]["additionalProperties"] is False
