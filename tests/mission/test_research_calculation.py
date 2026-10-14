"""Behavioral checks for explicitly supplied, bounded research arithmetic."""

from __future__ import annotations

import math

import pytest

from ora.agents.json_shapes import validate_schema, validate_value
from ora.mission.calculation import (
    CALCULATION_TOOL_SCHEMA,
    CalculationError,
    calculate,
)


def test_percentage_receipt_is_reproducible_and_does_not_claim_source_validation():
    request = {
        "expression": "responders / enrolled * 100",
        "inputs": [
            {"name": "responders", "value": 43},
            {"name": "enrolled", "value": 60},
        ],
        "units": "%",
    }
    validate_schema(CALCULATION_TOOL_SCHEMA)
    validate_value(CALCULATION_TOOL_SCHEMA, request)
    receipt = calculate(**request)
    assert receipt == {
        "expression": "responders / enrolled * 100",
        "inputs": [
            {"name": "responders", "value": "43"},
            {"name": "enrolled", "value": "60"},
        ],
        "result": "71.66666666666666666666666667",
        "rounding_policy": "28 significant decimal digits; round half even",
        "units": "%",
        "validation_scope": "calculated_from_supplied_values_not_source_validation",
    }


def test_difference_ratio_mean_sum_and_decimal_values():
    inputs = [
        {"name": "treated", "value": "0.3"},
        {"name": "control", "value": "0.1"},
    ]
    assert calculate("treated - control", inputs)["result"] == "0.2"
    assert calculate("treated / control", inputs)["result"] == "3"
    assert calculate("mean(0.1, 0.2, 0.3)", [])["result"] == "0.2"
    assert calculate("sum(0.1, 0.2, 0.3)", [])["result"] == "0.6"
    assert calculate("-(treated - control)", inputs)["result"] == "-0.2"
    assert calculate("0.10000000000000001 - 0.1", [])["result"] == "0.00000000000000001"
    assert calculate("x", [{"name": "x", "value": "1.2300"}])["inputs"] == [
        {"name": "x", "value": "1.23"}
    ]


@pytest.mark.parametrize(
    ("expression", "inputs"),
    [
        ("1 / 0", []),
        ("1 / zero", [{"name": "zero", "value": 0}]),
        ("1e20", []),
        ("1e-20", []),
        ("0.12345678901234567890123456789", []),
        ("1_000", []),
        ("10 ** 3", []),
        ("2 ** 1000000", []),
        ("__import__('os').system('echo bad')", []),
        ("(1).__class__", []),
        ("[1, 2][0]", []),
        ("mean()", []),
        ("sum(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21)", []),
        ("unknown + 1", []),
    ],
)
def test_rejects_unsafe_or_unbounded_expressions(expression, inputs):
    with pytest.raises(CalculationError):
        calculate(expression, inputs)


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan, True, "NaN", "1e10000"])
def test_rejects_nonfinite_and_non_decimal_inputs(value):
    with pytest.raises(CalculationError):
        calculate("x + 1", [{"name": "x", "value": value}])


def test_rejects_oversized_and_ambiguous_inputs():
    with pytest.raises(CalculationError):
        calculate("sum(1)", [{"name": f"x{i}", "value": i} for i in range(21)])
    with pytest.raises(CalculationError):
        calculate("x", [{"name": "x", "value": 1}, {"name": "x", "value": 2}])
    with pytest.raises(CalculationError):
        calculate("x", [{"name": "x", "value": 1, "source": "unverified"}])
    with pytest.raises(CalculationError):
        calculate("1 + " * 81 + "1", [])


def test_arithmetic_does_not_infer_confidence_interval_or_p_value():
    with pytest.raises(CalculationError, match="Unsupported expression"):
        calculate("confidence_interval(43, 60)", [])
    with pytest.raises(CalculationError, match="Unsupported expression"):
        calculate("p_value(43, 60)", [])
