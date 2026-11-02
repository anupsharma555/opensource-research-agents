"""Bounded analysis of supplied numbers and physical-unit conversion.

The host owns permission checks, tool attachment and durable tool receipts.
These functions calculate from caller-supplied values; they do not validate
source extraction, study design, independence or clinical interpretation.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import sys
from functools import lru_cache
from importlib.metadata import version

from ora.contracts.models import AnalyzeValuesInput, ConvertUnitsInput, WireModel
from ora.contracts.tool_schema import tool_schema


class AnalysisToolError(ValueError):
    """A validly shaped request cannot produce a supported numerical result."""


ANALYSIS_TOOL_SCHEMAS = {
    "analyze_values": tool_schema(AnalyzeValuesInput),
    "convert_units": tool_schema(ConvertUnitsInput),
}
ANALYSIS_TOOL_DESCRIPTIONS = {
    "analyze_values": (
        "Describe 2-200 supplied finite observations or compare two independent groups "
        "with Welch's t-test. Choose the alternative explicitly. Returns numerical results, "
        "input hash and package versions; does not validate source values, independence, "
        "missingness, design or clinical meaning. No code or dataset paths."
    ),
    "convert_units": (
        "Convert one supplied finite value among allowlisted mass, length, time or volume "
        "units using Pint. Rejects incompatible dimensions. Does not validate the source "
        "value or clinical equivalence. No custom unit definitions or code."
    ),
}

_NUMERIC_POLICY = "12 significant decimal digits in the returned display values"
_ANALYSIS_SCOPE = "computed_from_supplied_values_not_source_or_design_validation"
_UNIT_SCOPE = "physical_unit_conversion_not_source_or_clinical_equivalence_validation"


def _hash_input(request: WireModel) -> str:
    normalized = request.model_dump(mode="json", by_alias=False)
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _display(value: float) -> str:
    if not math.isfinite(value):
        raise AnalysisToolError("The operation did not produce a finite result")
    return format(0.0 if value == 0 else value, ".12g")


def _summary(values: list[float]) -> dict:
    return {
        "n": len(values),
        "mean": _display(statistics.fmean(values)),
        "sample_sd": _display(statistics.stdev(values)),
        "minimum": _display(min(values)),
        "maximum": _display(max(values)),
    }


def analyze_values(**arguments: object) -> dict:
    """Describe values or run an explicitly chosen independent Welch comparison."""
    request = AnalyzeValuesInput.model_validate(arguments)
    first = _summary(request.group_a)
    result = {
        "operation": request.operation,
        "input_sha256": _hash_input(request),
        "group_a": first,
        "group_b": None,
        "comparison": None,
        "units": request.units,
        "numeric_policy": _NUMERIC_POLICY,
        "software_versions": {"python": sys.version.split()[0]},
        "validation_scope": _ANALYSIS_SCOPE,
        "assumptions_not_checked": [
            "Supplied numbers match the cited source and intended outcome",
            "Observations represent an appropriate sample with no unreported missing values",
        ],
    }
    if request.operation == "describe":
        result["method"] = "descriptive count, arithmetic mean and sample standard deviation"
        return result

    second_values = request.group_b
    if second_values is None:  # The canonical model also enforces this condition.
        raise AnalysisToolError("Welch comparison requires two groups")
    second = _summary(second_values)
    if first["sample_sd"] == "0" and second["sample_sd"] == "0":
        raise AnalysisToolError("Both groups are constant; the test has undefined uncertainty")

    # Import only for the selected operation; ordinary descriptive work stays light.
    from scipy import stats

    test = stats.ttest_ind(
        request.group_a, second_values, equal_var=False,
        nan_policy="raise", alternative=request.alternative,
    )
    difference = statistics.fmean(request.group_a) - statistics.fmean(second_values)
    interval = None
    if request.alternative == "two-sided":
        bounds = test.confidence_interval(confidence_level=0.95)
        interval = {"low": _display(float(bounds.low)), "high": _display(float(bounds.high))}
    result.update({
        "group_b": second,
        "method": "scipy.stats.ttest_ind; independent Welch test; unequal variance; no omissions",
        "comparison": {
            "alternative": request.alternative,
            "mean_difference_a_minus_b": _display(difference),
            "statistic": _display(float(test.statistic)),
            "p_value": _display(float(test.pvalue)),
            "degrees_of_freedom": _display(float(test.df)),
            "confidence_interval_95_two_sided": interval,
        },
        "software_versions": {
            "python": sys.version.split()[0],
            "scipy": version("scipy"),
            "numpy": version("numpy"),
        },
        "assumptions_not_checked": [
            "Supplied numbers match the cited source and intended outcome",
            "Groups contain independent observations and represent appropriate samples",
            "Outcome distribution, design, exclusions and unreported missing values support this test",
            "A p-value or interval does not by itself establish a clinical effect",
        ],
    })
    return result


@lru_cache(maxsize=1)
def _unit_registry():
    # A fixed built-in registry is shared; requests cannot supply definitions.
    from pint import UnitRegistry

    return UnitRegistry()


def convert_units(**arguments: object) -> dict:
    """Convert one physical quantity using only the canonical unit allowlist."""
    request = ConvertUnitsInput.model_validate(arguments)
    from pint import DimensionalityError

    registry = _unit_registry()
    try:
        quantity = registry.Quantity(request.value, request.from_unit)
        converted = quantity.to(request.to_unit)
        factor = registry.Quantity(1, request.from_unit).to(request.to_unit).magnitude
    except DimensionalityError as exc:
        raise AnalysisToolError("Units have incompatible physical dimensions") from exc
    return {
        "operation": "convert_units",
        "input_sha256": _hash_input(request),
        "value": _display(request.value),
        "from_unit": request.from_unit,
        "to_unit": request.to_unit,
        "result": _display(float(converted.magnitude)),
        "conversion_factor": _display(float(factor)),
        "numeric_policy": _NUMERIC_POLICY,
        "software_versions": {"python": sys.version.split()[0], "pint": version("pint")},
        "validation_scope": _UNIT_SCOPE,
        "assumptions_not_checked": [
            "Supplied value and unit match the cited source and intended measurement",
            "Physical unit compatibility does not imply clinical or measurement equivalence",
        ],
    }
