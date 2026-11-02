"""Compare one synthetic meta-analysis with independent fixed and DL algebra."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import statistics
import warnings
from importlib.metadata import version
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = "ora.synthetic_meta_analysis.v1"
METHOD = "statsmodels chi-square/DL random effects; scalar reference truncates negative tau squared at zero"


def _finite(value: object, label: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def validate_input(data: object) -> list[dict]:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError("Synthetic comparison needs its exact schema")
    if data.get("effect_scale") != "unitless_synthetic":
        raise ValueError("Only explicitly synthetic unitless effects are supported")
    studies = data.get("studies")
    if not isinstance(studies, list) or not 2 <= len(studies) <= 200:
        raise ValueError("Comparison needs 2 to 200 study rows")
    ids: set[str] = set()
    checked = []
    for row in studies:
        if not isinstance(row, dict) or set(row) != {"study_id", "effect", "variance"}:
            raise ValueError("Each row needs one study ID, effect and variance")
        study_id = row["study_id"]
        if (not isinstance(study_id, str) or not study_id
                or len(study_id) > 80 or study_id in ids):
            raise ValueError("Study IDs must be unique, nonempty and bounded")
        ids.add(study_id)
        effect = _finite(row["effect"], "effect")
        variance = _finite(row["variance"], "variance")
        if variance <= 0:
            raise ValueError("Every study variance must be positive")
        checked.append({"study_id": study_id, "effect": effect, "variance": variance})
    return checked


def reference(studies: list[dict]) -> dict[str, float]:
    """Independent scalar inverse-variance and DL random-effects algebra."""
    fixed_weights = [1.0 / row["variance"] for row in studies]
    fixed_weight_sum = math.fsum(fixed_weights)
    fixed = math.fsum(weight * row["effect"] for weight, row in zip(fixed_weights, studies)) / fixed_weight_sum
    q = math.fsum(weight * (row["effect"] - fixed) ** 2
                  for weight, row in zip(fixed_weights, studies))
    c = fixed_weight_sum - math.fsum(weight * weight for weight in fixed_weights) / fixed_weight_sum
    if c <= 0:
        raise ValueError("The random-effects moment denominator is undefined")
    tau2 = max(0.0, (q - (len(studies) - 1)) / c)
    random_weights = [1.0 / (row["variance"] + tau2) for row in studies]
    random_weight_sum = math.fsum(random_weights)
    random = math.fsum(weight * row["effect"] for weight, row in zip(random_weights, studies)) / random_weight_sum
    fixed_variance = 1.0 / fixed_weight_sum
    random_variance = 1.0 / random_weight_sum
    z = statistics.NormalDist().inv_cdf(0.975)
    return {"fixed_mean": fixed, "fixed_variance": fixed_variance,
            "fixed_ci_low": fixed - z * math.sqrt(fixed_variance),
            "fixed_ci_high": fixed + z * math.sqrt(fixed_variance),
            "q": q, "tau2": tau2, "random_mean": random,
            "random_variance": random_variance,
            "random_ci_low": random - z * math.sqrt(random_variance),
            "random_ci_high": random + z * math.sqrt(random_variance)}


def compare(data: object, *, input_sha256: str) -> dict:
    studies = validate_input(data)
    import numpy as np
    from statsmodels.stats.meta_analysis import combine_effects

    effects = [row["effect"] for row in studies]
    variances = [row["variance"] for row in studies]
    with warnings.catch_warnings(record=True) as notices:
        warnings.simplefilter("always", RuntimeWarning)
        observed = combine_effects(np.asarray(effects, dtype=float),
                                   np.asarray(variances, dtype=float), method_re="chi2", use_t=False,
                                   row_names=[row["study_id"] for row in studies])
        fixed_ci, random_ci, _fixed_scaled_ci, _random_scaled_ci = observed.conf_int(alpha=0.05)
    expected = reference(studies)
    raw_candidate = {"fixed_mean": float(observed.mean_effect_fe),
                 "fixed_variance": float(observed.var_eff_w_fe),
                 "fixed_ci_low": float(fixed_ci[0]),
                 "fixed_ci_high": float(fixed_ci[1]),
                 "q": float(observed.q), "tau2": float(observed.tau2),
                 "random_mean": float(observed.mean_effect_re),
                 "random_variance": float(observed.var_eff_w_re),
                 "random_ci_low": float(random_ci[0]),
                 "random_ci_high": float(random_ci[1])}
    special = {key: "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
               for key, value in raw_candidate.items() if not math.isfinite(value)}
    candidate = {key: value if math.isfinite(value) else None
                 for key, value in raw_candidate.items()}
    differences = {key: abs(candidate[key] - expected[key]) if candidate[key] is not None else None
                   for key in expected}
    passed = all(candidate[key] is not None and math.isclose(
        candidate[key], expected[key], rel_tol=1e-11, abs_tol=1e-11
    ) for key in expected)
    status = ("nonfinite_candidate" if special else "negative_tau2_mismatch"
              if candidate["tau2"] is not None and candidate["tau2"] < 0 else
              "numeric_match" if passed else "numeric_mismatch")
    return {
        "schema": "ora.analysis_comparison_result.v1",
        "input_sha256": input_sha256,
        "source_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "lock_sha256": hashlib.sha256((HERE / "uv.lock").read_bytes()).hexdigest(),
        "input_scope": "synthetic_unitless_effects_not_source_or_study_validation",
        "study_ids": [row["study_id"] for row in studies],
        "model": "inverse_variance_fixed_and_random_effects",
        "method": METHOD,
        "negative_tau2_policy": "A negative statsmodels tau2 is a mismatch, not an accepted random-effects estimate",
        "reference": expected,
        "statsmodels": candidate,
        "candidate_special_values": special,
        "candidate_warning_classes": sorted({type(item.message).__name__ for item in notices}),
        "absolute_differences": differences,
        "candidate_status": status,
        "numeric_match": passed,
        "versions": {"python": platform.python_version(), **{
            name: version(name) for name in (
                "statsmodels", "numpy", "scipy", "pandas", "patsy", "formulaic")
        }},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=HERE / "fixture.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    raw = args.input.read_bytes()
    result = compare(json.loads(raw), input_sha256=hashlib.sha256(raw).hexdigest())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({"numeric_match": result["numeric_match"],
                      "candidate_status": result["candidate_status"],
                      "study_count": len(result["study_ids"]),
                      "statsmodels_version": result["versions"]["statsmodels"]}))
    return 0 if result["numeric_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
