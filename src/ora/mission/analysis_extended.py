"""Version 2 numerical tools for explicitly supplied, bounded research data.

These calculations do not extract source values, select a scientific design, or
authorize source use. The mission host owns those boundaries and saved receipts.
"""

from __future__ import annotations

import math
import statistics
import sys
import warnings
from importlib.metadata import version

from ora.contracts.models import (
    AdjustPValuesInput,
    AnalyzeContingencyInput,
    AnalyzePairedValuesInput,
    CompareSummaryStatisticsInput,
    ConvertValuesUnitsInput,
    CorrelateValuesInput,
    DescribeValuesInput,
)
from ora.contracts.tool_schema import tool_schema
from ora.mission.analysis_tools import (
    _NUMERIC_POLICY,
    AnalysisToolError,
    _display,
    _hash_input,
    _unit_registry,
)

_SCOPE = "computed_from_supplied_values_not_source_design_or_causal_validation"
_SOURCE_CAVEAT = "Supplied numbers, pairings and exclusions match the cited source"


def _receipt(request, operation: str, method: str, *, scipy: bool = False,
             pint: bool = False, caveats: list[str]) -> dict:
    packages = {"python": sys.version.split()[0]}
    if scipy:
        packages.update({"scipy": version("scipy"), "numpy": version("numpy")})
    if pint:
        packages["pint"] = version("pint")
    return {
        "operation": operation,
        "method": method,
        "input_sha256": _hash_input(request),
        "numeric_policy": (
            _NUMERIC_POLICY + "; interval endpoints use round-trip floats, or center and "
            "half-width when bounds collapse below floating precision"
        ),
        "software_versions": packages,
        "validation_scope": _SCOPE,
        "assumptions_not_checked": [_SOURCE_CAVEAT, *caveats],
    }


def _interval(low: float, high: float, *, center: float | None = None,
              half_width: float | None = None) -> dict[str, str | None]:
    # Twelve significant digits can turn a narrow, nonzero interval into two
    # identical displayed bounds at a large offset.
    _display(low)
    _display(high)
    if low == high and half_width is not None and half_width > 0:
        if center is None:
            raise AnalysisToolError("Collapsed interval needs its center")
        return {
            "low": None, "high": None, "center": repr(float(center)),
            "half_width": repr(float(half_width)),
            "resolution_status": "bounds_below_floating_precision",
        }
    return {"low": repr(float(low)), "high": repr(float(high))}


def _computed_p(value: float) -> tuple[str | None, str]:
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise AnalysisToolError("The test did not produce a finite probability")
    if value == 0:
        return None, "zero_at_floating_precision_or_boundary"
    return _display(value), "finite"


def compare_summary_statistics(**arguments: object) -> dict:
    """Independent Welch comparison from sample sizes, means and sample SDs."""
    request = CompareSummaryStatisticsInput.model_validate(arguments)
    a, b = request.group_a, request.group_b
    sd_scale = max(a.sample_sd, b.sample_sd)
    if sd_scale == 0:
        raise AnalysisToolError("Sample uncertainty is zero or below numerical resolution")
    # Normalize before forming variances: both SD squared and its later square
    # can underflow even when the final SE is a reliable normal float.
    va = (a.sample_sd / sd_scale) ** 2 / a.n
    vb = (b.sample_sd / sd_scale) ** 2 / b.n
    difference = a.mean - b.mean
    standard_error = sd_scale * math.sqrt(va + vb)
    if not math.isfinite(standard_error) or standard_error < sys.float_info.min:
        raise AnalysisToolError("Sample uncertainty is below supported floating precision")
    normalized_difference = difference / sd_scale
    if (not math.isfinite(normalized_difference)
            or not math.isfinite(normalized_difference / math.sqrt(va + vb))):
        raise AnalysisToolError("The standardized mean difference exceeds numerical resolution")
    df = (va + vb) ** 2 / (va ** 2 / (a.n - 1) + vb ** 2 / (b.n - 1))
    from scipy import stats

    # Welch is invariant to a common positive scale and shift. Normalize the
    # difference and SDs so SciPy's internal variance-square calculation does
    # not underflow and silently substitute a different degrees of freedom.
    test = stats.ttest_ind_from_stats(
        normalized_difference, a.sample_sd / sd_scale, a.n,
        0.0, b.sample_sd / sd_scale, b.n,
        equal_var=False, alternative=request.alternative,
    )
    ci = None
    if request.alternative == "two-sided":
        margin = float(stats.t.ppf(0.975, df)) * standard_error
        ci = _interval(difference - margin, difference + margin,
                       center=difference, half_width=margin)
    result = _receipt(
        request, "compare_summary_statistics",
        "independent Welch t test from sample summaries; unequal variances",
        scipy=True,
        caveats=[
            "Sample SD uses the n-1 denominator; summaries describe independent groups",
            "Sampling, distribution, exclusions and unit comparability support this test",
            "A p-value or interval does not establish a clinical or causal effect",
        ],
    )
    p_value, p_status = _computed_p(float(test.pvalue))
    result.update({
        "group_a": {"n": a.n, "mean": _display(a.mean), "sample_sd": _display(a.sample_sd)},
        "group_b": {"n": b.n, "mean": _display(b.mean), "sample_sd": _display(b.sample_sd)},
        "alternative": request.alternative,
        "units": request.units,
        "formula": (
            "difference=mean_a-mean_b; SE=sqrt(sd_a^2/n_a+sd_b^2/n_b); "
            "df=(va+vb)^2/(va^2/(n_a-1)+vb^2/(n_b-1)), va=sd_a^2/n_a, vb=sd_b^2/n_b"
        ),
        "mean_difference_a_minus_b": _display(difference),
        "standard_error": _display(standard_error),
        "statistic": _display(float(test.statistic)),
        "degrees_of_freedom": _display(df),
        "p_value": p_value,
        "p_value_status": p_status,
        "confidence_interval_95_two_sided": ci,
    })
    return result


def analyze_contingency(**arguments: object) -> dict:
    """Estimate one prespecified 2x2 effect with an explicit zero policy."""
    request = AnalyzeContingencyInput.model_validate(arguments)
    raw = (request.event_a, request.nonevent_a, request.event_b, request.nonevent_b)
    needs_correction = (
        (request.operation == "odds_ratio" and 0 in raw)
        or (request.operation == "risk_ratio" and (request.event_a == 0 or request.event_b == 0))
    )
    if needs_correction and request.zero_cell_policy == "reject":
        raise AnalysisToolError("A ratio with a zero cell needs explicit add_half policy")
    corrected = tuple(float(value) + (0.5 if needs_correction else 0.0) for value in raw)
    ea, na, eb, nb = corrected
    risk_a = ea / (ea + na)
    risk_b = eb / (eb + nb)
    from scipy import stats

    if request.operation == "risk_ratio":
        estimate = risk_a / risk_b
        se = math.sqrt(1 / ea - 1 / (ea + na) + 1 / eb - 1 / (eb + nb))
        null = 1.0
        scale = "log"
        method = "sample risk ratio with log-Wald 95% confidence interval"
        formula = "RR=(event_a/total_a)/(event_b/total_b); SE(log RR)=sqrt(1/ea-1/ta+1/eb-1/tb)"
    elif request.operation == "odds_ratio":
        estimate = ea * nb / (na * eb)
        se = math.sqrt(1 / ea + 1 / na + 1 / eb + 1 / nb)
        null = 1.0
        scale = "log"
        method = "sample odds ratio with log-Wald 95% confidence interval"
        formula = "OR=(event_a*nonevent_b)/(nonevent_a*event_b); SE(log OR)=sqrt(sum(1/cell))"
    else:
        estimate = risk_a - risk_b
        se = math.sqrt(risk_a * (1 - risk_a) / (ea + na)
                       + risk_b * (1 - risk_b) / (eb + nb))
        null = 0.0
        scale = "identity"
        method = "sample risk difference with unpooled Wald 95% confidence interval"
        formula = "RD=event_a/total_a-event_b/total_b; SE=sqrt(pa*(1-pa)/ta+pb*(1-pb)/tb)"
    if not math.isfinite(se) or se == 0:
        raise AnalysisToolError("The selected effect has undefined or zero estimated uncertainty")
    z = (math.log(estimate / null) if scale == "log" else estimate - null) / se
    p_value = float(2 * stats.norm.sf(abs(z)))
    margin = float(stats.norm.ppf(0.975)) * se
    if scale == "log":
        center = math.log(estimate)
        ci = _interval(math.exp(center - margin), math.exp(center + margin))
    else:
        ci = _interval(estimate - margin, estimate + margin)
    result = _receipt(
        request, "analyze_contingency", method, scipy=True,
        caveats=[
            "Rows represent independent groups and columns use the same event definition",
            ("Group totals must be valid at-risk denominators for RR/RD; case-control "
             "sampled counts do not directly establish cohort risks"
             if request.operation != "odds_ratio" else
             "The sample odds ratio does not by itself establish an adjusted or causal effect"),
            "Wald intervals and tests can be inaccurate with sparse or boundary counts",
            "An explicit 0.5 correction changes the estimate when applied; no causal claim follows",
        ],
    )
    p_display, p_status = _computed_p(p_value)
    result.update({
        "effect": request.operation,
        "raw_counts": dict(zip(("event_a", "nonevent_a", "event_b", "nonevent_b"), raw,
                               strict=True)),
        "zero_cell_policy": request.zero_cell_policy,
        "zero_cell_semantics": (
            "Risk difference uses original counts, including zero cells; zero estimated SE is rejected"
            if request.operation == "risk_difference" else
            "Odds ratio needs all cells positive; risk ratio needs event cells positive; "
            "add_half corrects every cell when the selected ratio needs correction"
        ),
        "correction_applied": needs_correction,
        "effective_counts": dict(zip(
            ("event_a", "nonevent_a", "event_b", "nonevent_b"),
            (_display(value) for value in corrected), strict=True,
        )),
        "risk_a": _display(risk_a),
        "risk_b": _display(risk_b),
        "formula": formula,
        "estimate": _display(estimate),
        "standard_error_on_test_scale": _display(se),
        "test_scale": scale,
        "statistic_z": _display(z),
        "p_value_two_sided_wald": p_display,
        "p_value_status": p_status,
        "confidence_interval_95_two_sided": ci,
    })
    return result


def analyze_paired_values(**arguments: object) -> dict:
    """Paired t test on caller-matched complete observation pairs."""
    request = AnalyzePairedValuesInput.model_validate(arguments)
    differences = [pair.value_a - pair.value_b for pair in request.pairs]
    sd = statistics.stdev(differences)
    if sd == 0:
        raise AnalysisToolError("All paired differences are constant; test uncertainty is undefined")
    from scipy import stats

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "error", message="Precision loss occurred.*", category=RuntimeWarning,
            )
            test = stats.ttest_rel(
                [pair.value_a for pair in request.pairs],
                [pair.value_b for pair in request.pairs],
                alternative=request.alternative, nan_policy="raise",
            )
    except RuntimeWarning as exc:
        raise AnalysisToolError("Paired t test loses precision for nearly identical values") from exc
    ci = None
    if request.alternative == "two-sided":
        bounds = test.confidence_interval(confidence_level=0.95)
        center = statistics.fmean(differences)
        margin = float(stats.t.ppf(0.975, len(differences) - 1)) * sd / math.sqrt(len(differences))
        ci = _interval(float(bounds.low), float(bounds.high), center=center, half_width=margin)
    result = _receipt(
        request, "analyze_paired_values", "paired Student t test on a-minus-b differences",
        scipy=True,
        caveats=[
            "Pair IDs identify correct related observations; no pair or outcome is omitted",
            "Distribution and sampling of within-pair differences support a t test",
        ],
    )
    p_value, p_status = _computed_p(float(test.pvalue))
    result.update({
        "n_pairs": len(request.pairs),
        "alternative": request.alternative,
        "units": request.units,
        "formula": "d_i=value_a_i-value_b_i; t=mean(d)/(sample_sd(d)/sqrt(n)); df=n-1",
        "mean_difference_a_minus_b": _display(statistics.fmean(differences)),
        "sample_sd_difference": _display(sd),
        "statistic": _display(float(test.statistic)),
        "degrees_of_freedom": _display(float(test.df)),
        "p_value": p_value,
        "p_value_status": p_status,
        "confidence_interval_95_two_sided": ci,
    })
    return result


def correlate_values(**arguments: object) -> dict:
    """Pearson correlation on complete, explicitly paired supplied values."""
    request = CorrelateValuesInput.model_validate(arguments)
    a = [pair.value_a for pair in request.pairs]
    b = [pair.value_b for pair in request.pairs]
    if min(len(set(a)), len(set(b))) < 2:
        raise AnalysisToolError("Pearson correlation is undefined for a constant input")
    from scipy import stats

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", stats.NearConstantInputWarning)
            test = stats.pearsonr(a, b, alternative=request.alternative)
    except (stats.NearConstantInputWarning, stats.ConstantInputWarning) as exc:
        raise AnalysisToolError("Pearson input is nearly constant; numerical accuracy is uncertain") from exc
    ci = None
    if request.alternative == "two-sided":
        bounds = test.confidence_interval(confidence_level=0.95)
        ci = _interval(float(bounds.low), float(bounds.high))
    result = _receipt(
        request, "correlate_values", "Pearson product-moment correlation; analytic p-value",
        scipy=True,
        caveats=[
            "Pair IDs align observations and observations are independent",
            "Pearson tests a linear association; distribution and sampling support analytic inference",
            "Correlation does not establish causation",
        ],
    )
    p_value, p_status = _computed_p(float(test.pvalue))
    result.update({
        "n_pairs": len(request.pairs),
        "correlation_method": request.method,
        "alternative": request.alternative,
        "coefficient_r": _display(float(test.statistic)),
        "p_value": p_value,
        "p_value_status": p_status,
        "confidence_interval_95_two_sided_fisher": ci,
    })
    return result


def adjust_pvalues(**arguments: object) -> dict:
    """Control FDR over one explicitly supplied testing family."""
    request = AdjustPValuesInput.model_validate(arguments)
    count = len(request.tests)
    multiplier = (sum(1 / rank for rank in range(1, count + 1))
                  if request.method == "benjamini_yekutieli" else 1.0)
    from scipy import stats

    adjusted = stats.false_discovery_control(
        [item.p_value for item in request.tests],
        method="by" if request.method == "benjamini_yekutieli" else "bh",
    )
    result = _receipt(
        request, "adjust_pvalues",
        "scipy.stats.false_discovery_control; Benjamini-Hochberg step-up FDR"
        if multiplier == 1 else
        "scipy.stats.false_discovery_control; Benjamini-Yekutieli step-up FDR",
        scipy=True,
        caveats=[
            "The submitted tests form the intended complete prespecified family",
            "Raw p-values are valid for their tests; adjusted p-values are not posterior probabilities",
            ("Benjamini-Hochberg needs independent or suitable positively dependent tests"
             if multiplier == 1 else "Benjamini-Yekutieli controls FDR under arbitrary dependence"),
        ],
    )
    result.update({
        "numeric_policy": (
            "Round-trip decimal strings for raw and adjusted p-values and alpha; "
            "reject_at_alpha uses unrounded computed values"
        ),
        "family_id": request.family_id,
        "adjustment_method": request.method,
        "alpha": repr(request.alpha),
        "n_tests": count,
        "n_rejected": int(sum(value <= request.alpha for value in adjusted)),
        "adjusted_p_range": [repr(float(min(adjusted))), repr(float(max(adjusted)))],
        "formula": "adjusted_p_i=min(1,min_{j>=rank_i}(p_(j)*m*c_m/j)); c_m=1 for BH, H_m for BY",
        "dependency_multiplier": _display(multiplier),
        "tests": [
            {"test_id": item.test_id, "raw_p_value": repr(item.p_value),
             "adjusted_p_value": repr(float(adjusted[index])),
             "reject_at_alpha": bool(adjusted[index] <= request.alpha)}
            for index, item in enumerate(request.tests)
        ],
    })
    return result


def _linear_quantile(sorted_values: list[float], fraction: float) -> float:
    position = (len(sorted_values) - 1) * fraction
    lower = math.floor(position)
    weight = position - lower
    if weight == 0:
        return sorted_values[lower]
    return sorted_values[lower] * (1 - weight) + sorted_values[lower + 1] * weight


def describe_values(**arguments: object) -> dict:
    """Give descriptive statistics using the stated linear quantile rule."""
    request = DescribeValuesInput.model_validate(arguments)
    values = sorted(request.values)
    q1 = _linear_quantile(values, 0.25)
    median = _linear_quantile(values, 0.5)
    q3 = _linear_quantile(values, 0.75)
    result = _receipt(
        request, "describe_values", "descriptive statistics; linear interpolated quantiles",
        caveats=[
            "Submitted values are the intended complete sample without unreported missing data",
            "Descriptive spread is not a confidence interval or hypothesis test",
        ],
    )
    result.update({
        "n": len(values),
        "units": request.units,
        "quantile_method": request.quantile_method,
        "quantile_formula": "h=(n-1)*q; interpolate between sorted values at floor(h) and ceil(h)",
        "mean": _display(statistics.fmean(values)),
        "sample_sd": _display(statistics.stdev(values)),
        "minimum": _display(values[0]),
        "q1": _display(q1),
        "median": _display(median),
        "q3": _display(q3),
        "iqr": _display(q3 - q1),
        "maximum": _display(values[-1]),
    })
    return result


def convert_values_units(**arguments: object) -> dict:
    """Convert a bounded batch using the same fixed unit allowlist as v1."""
    request = ConvertValuesUnitsInput.model_validate(arguments)
    from pint import DimensionalityError

    registry = _unit_registry()
    try:
        factor = float(registry.Quantity(1, request.from_unit).to(request.to_unit).magnitude)
        converted = [float(registry.Quantity(value, request.from_unit)
                           .to(request.to_unit).magnitude) for value in request.values]
    except DimensionalityError as exc:
        raise AnalysisToolError("Units have incompatible physical dimensions") from exc
    result = _receipt(
        request, "convert_values_units", "Pint physical-unit conversion with fixed units",
        pint=True,
        caveats=[
            "Supplied values and units match the source and intended measurement",
            "Compatible dimensions do not establish clinical or measurement equivalence",
        ],
    )
    result.update({
        "from_unit": request.from_unit,
        "to_unit": request.to_unit,
        "n_values": len(converted),
        "values": [_display(value) for value in request.values],
        "converted_values": [_display(value) for value in converted],
        "conversion_factor": _display(factor),
    })
    return result


EXTENDED_ANALYSIS_TOOL_HANDLERS = {
    "compare_summary_statistics": compare_summary_statistics,
    "analyze_contingency": analyze_contingency,
    "analyze_paired_values": analyze_paired_values,
    "correlate_values": correlate_values,
    "adjust_pvalues": adjust_pvalues,
    "describe_values": describe_values,
    "convert_values_units": convert_values_units,
}
_MODELS = {
    "compare_summary_statistics": CompareSummaryStatisticsInput,
    "analyze_contingency": AnalyzeContingencyInput,
    "analyze_paired_values": AnalyzePairedValuesInput,
    "correlate_values": CorrelateValuesInput,
    "adjust_pvalues": AdjustPValuesInput,
    "describe_values": DescribeValuesInput,
    "convert_values_units": ConvertValuesUnitsInput,
}
EXTENDED_ANALYSIS_TOOL_SCHEMAS = {name: tool_schema(model) for name, model in _MODELS.items()}
EXTENDED_ANALYSIS_TOOL_DESCRIPTIONS = {
    "compare_summary_statistics": (
        "Compute an independent Welch test from two supplied n/mean/sample-SD summaries; "
        "raw observations are unnecessary. Returns A-minus-B difference, standard error, "
        "statistic, degrees of freedom, p-value and a 95% interval for a two-sided alternative. "
        "Use sample SD, not SE; the caller selects the alternative. Does not validate source "
        "values, sampling or independence."
    ),
    "analyze_contingency": (
        "Compute one risk ratio, odds ratio or risk difference with an approximate 95% "
        "confidence interval from integer event/nonevent counts for groups A and B. "
        "Ratios use A relative to B; differences use A minus B. Select the effect and "
        "explicit ratio zero-cell policy; risk difference uses raw counts and permits zeros. "
        "Wald approximations may be unreliable for sparse counts; input design is unverified."
    ),
    "analyze_paired_values": "Paired t test on ID-matched complete values with explicit alternative.",
    "correlate_values": "Pearson correlation on ID-matched complete pairs with explicit alternative.",
    "adjust_pvalues": "BH or BY false-discovery-rate adjustment for one explicit family of supplied p-values.",
    "describe_values": "Descriptive mean, sample SD, median and linear quartiles for supplied finite values.",
    "convert_values_units": "Batch physical-unit conversion using the fixed mass, length, time and volume allowlist.",
}
EXTENDED_ANALYSIS_TOOL_PURPOSES = {
    "compare_summary_statistics": "Welch p-values and intervals from group n/mean/sample-SD summaries",
    "analyze_contingency": "Risk ratio, odds ratio or risk difference with intervals from event counts",
    "analyze_paired_values": "Test a matched before/after or other paired outcome",
    "correlate_values": "Measure linear association in paired observations",
    "adjust_pvalues": "Adjust one specified family of hypothesis-test p-values",
    "describe_values": "Summarize supplied observations and quartiles",
    "convert_values_units": "Convert a batch of supplied physical measurements",
}
