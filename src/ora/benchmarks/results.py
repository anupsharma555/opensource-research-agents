"""Check whether a saved benchmark run can be traced to its exact test inputs.

The validator checks the question version, recorded source access, cited passage
IDs, and separately measured resources; it also marks old passes as historical
after relevant changes. A reviewer still decides whether a passage supports a claim.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from .catalog import (
    ARM_IDS,
    CONDITION_IDS,
    PROFILE_IDS,
    BenchmarkError,
    _entry,
    _manifest,
    load_scenario,
)

RUN_SCHEMA = "ora.benchmark.run.v1"
PROOF_TYPES = frozenset({"fixture", "recorded_input", "selected_model", "live_provider"})
RESOURCE_KEYS = frozenset(
    {
        "calls", "tokens", "provider_usage", "currency_amount", "currency_code",
        "active_seconds", "queue_provider_wait_seconds", "peer_wait_seconds",
        "retry_seconds", "first_useful_source_seconds",
        "first_supported_partial_answer_seconds", "scoped_completion_seconds",
    }
)
ALLOWANCE_KEYS = frozenset({
    "scope", "comparison_group_id", "declared_before_run_utc", "max_model_calls",
    "max_model_tokens_total", "max_tool_calls", "max_currency_amount", "currency_code",
    "allowed_tool_names", "fixed_source_ids",
})


def _nonnegative_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and value >= 0
        and (not isinstance(value, float) or math.isfinite(value))
    )


def result_applicability(
    record: dict[str, Any], *, scenario_version: str, scenario_sha256: str,
    profile_id: str, arm_id: str, condition_id: str,
    build_or_product_version: str, configuration_sha256: str,
    rubric_sha256: str, reference_sha256: str | None,
) -> str:
    """A changed packet, test arm, rubric, build, or configuration gets no old pass."""
    expected = (
        scenario_version, scenario_sha256, profile_id, arm_id, condition_id,
        build_or_product_version, configuration_sha256, rubric_sha256, reference_sha256,
    )
    observed = tuple(record.get(key) for key in (
        "scenario_version", "scenario_sha256", "profile_id", "arm_id", "condition_id",
        "build_or_product_version", "configuration_sha256", "rubric_sha256"
    )) + ((record.get("assessment") or {}).get("reference_sha256"),)
    return "current" if observed == expected else "historical_untested_for_current"


def validate_run_record(root: Path | str, record: dict[str, Any]) -> dict[str, Any]:
    """Validate a run's lineage and measurable fields, not claim entailment.

    Human/independent scientific reviewers must decide whether cited passages
    actually warrant claims, whether a null is informative, and reader quality.
    """
    if not isinstance(record, dict) or record.get("schema") != RUN_SCHEMA:
        raise BenchmarkError("Run must use the v1 run schema")
    required = {
        "run_id", "scenario_id", "scenario_version", "scenario_sha256", "profile_id",
        "arm_id", "condition_id", "proof_type", "build_or_product_version",
        "configuration_sha256", "rubric_sha256", "model_id", "tool_versions", "source_access",
        "user_interventions", "evaluation_allowance", "resource_use", "artifacts", "claims", "assessment",
    }
    missing = required - record.keys()
    if missing:
        raise BenchmarkError(f"Run is missing {', '.join(sorted(missing))}")
    for key in ("run_id", "scenario_id", "build_or_product_version", "configuration_sha256"):
        if not isinstance(record[key], str) or not record[key]:
            raise BenchmarkError(f"Run {key} must be a nonempty string")
    if re.fullmatch(r"[0-9a-f]{64}", record["configuration_sha256"]) is None:
        raise BenchmarkError("Run configuration_sha256 must be a lowercase SHA-256 digest")
    if record["profile_id"] not in PROFILE_IDS or record["condition_id"] not in CONDITION_IDS:
        raise BenchmarkError("Unknown run profile or comparison condition")
    if record["arm_id"] not in ARM_IDS or record["proof_type"] not in PROOF_TYPES:
        raise BenchmarkError("Unknown comparison arm or proof type")
    if not isinstance(record["tool_versions"], dict) or not isinstance(record["source_access"], list):
        raise BenchmarkError("Tool versions and source access must retain their own structures")
    if not isinstance(record["user_interventions"], list) or not isinstance(record["artifacts"], list):
        raise BenchmarkError("Interventions and artifacts must be lists")
    if record["profile_id"] in {"single_role", "peer_exchange", "complete_workflow"} and not record["model_id"]:
        raise BenchmarkError("Agent profiles require an exact model ID")
    entry = _entry(Path(root), record["scenario_id"])
    manifest = _manifest(Path(root))
    if record["rubric_sha256"] != manifest["rubric_sha256"]:
        raise BenchmarkError("Run uses a different scoring rubric version/hash")
    if (record["scenario_version"], record["scenario_sha256"]) != (
        entry["scenario_version"], entry["scenario_sha256"]
    ):
        raise BenchmarkError("Run references a different packet version/hash; retain it as historical")
    scenario = load_scenario(root, record["scenario_id"])
    sources = {source["source_id"]: source for source in scenario["sources"]}
    allowance = record["evaluation_allowance"]
    if not isinstance(allowance, dict) or set(allowance) != ALLOWANCE_KEYS:
        raise BenchmarkError("Evaluation allowance must retain all separately named budget fields")
    if allowance["scope"] != "benchmark_test_only":
        raise BenchmarkError("Evaluation allowance is only for this benchmark test")
    for key in ("max_model_calls", "max_model_tokens_total", "max_tool_calls", "max_currency_amount"):
        value = allowance[key]
        if value is not None and not _nonnegative_number(value):
            raise BenchmarkError(f"Evaluation allowance {key} must be nonnegative or unknown (null)")
        if key != "max_currency_amount" and value is not None and not isinstance(value, int):
            raise BenchmarkError(f"Evaluation allowance {key} must count whole units")
    for key in ("allowed_tool_names", "fixed_source_ids"):
        values = allowance[key]
        if not isinstance(values, list) or len(values) != len(set(values)) or not all(
            isinstance(item, str) and item for item in values
        ):
            raise BenchmarkError(f"Evaluation allowance {key} needs unique names")
    if not set(allowance["fixed_source_ids"]) <= sources.keys():
        raise BenchmarkError("Evaluation allowance names an unknown source")
    for key in ("comparison_group_id", "declared_before_run_utc", "currency_code"):
        value = allowance[key]
        if value is not None and (not isinstance(value, str) or not value):
            raise BenchmarkError(f"Evaluation allowance {key} must be text or unknown (null)")
    if allowance["max_currency_amount"] is not None and allowance["currency_code"] is None:
        raise BenchmarkError("Evaluation spending allowance needs a currency code")
    if record["condition_id"] == "controlled_equal_input" and record["proof_type"] != "fixture":
        required_values = (
            "comparison_group_id", "declared_before_run_utc", "max_model_calls",
            "max_model_tokens_total", "max_tool_calls", "max_currency_amount", "currency_code",
        )
        if any(allowance[key] is None for key in required_values) or not allowance["fixed_source_ids"]:
            raise BenchmarkError("A measured controlled comparison needs a predeclared numeric evaluation allowance")
    span_to_source = {
        span["span_id"]: source["source_id"]
        for source in scenario["sources"] for span in source["spans"]
    }
    accessed_source_ids: set[str] = set()
    for encountered in record["source_access"]:
        if not isinstance(encountered, dict) or not isinstance(encountered.get("source_id"), str):
            raise BenchmarkError("Each source-access entry needs a source ID")
        accessed_source_ids.add(encountered["source_id"])
        if record["condition_id"] == "controlled_equal_input" and encountered["source_id"] not in sources:
            raise BenchmarkError("Controlled run used a source outside the pinned opportunity")
    if not isinstance(record["claims"], list):
        raise BenchmarkError("Claims must be a list")
    claim_ids: set[str] = set()
    used_spans: set[str] = set()
    for claim in record["claims"]:
        if not isinstance(claim, dict) or not all(k in claim for k in ("claim_id", "text", "kind")):
            raise BenchmarkError("Each claim needs ID, text, and kind")
        claim_id = claim["claim_id"]
        if not isinstance(claim_id, str) or not claim_id or claim_id in claim_ids:
            raise BenchmarkError("Claim IDs must be unique nonempty strings")
        claim_ids.add(claim_id)
        if not isinstance(claim["text"], str) or not claim["text"]:
            raise BenchmarkError("Claim text must be nonempty")
        if claim["kind"] == "source_claim":
            cited = claim.get("citation_span_ids")
            if not isinstance(cited, list) or not cited or not set(cited) <= span_to_source.keys():
                raise BenchmarkError("A source claim needs locatable cited spans")
            if not {span_to_source[span_id] for span_id in cited} <= accessed_source_ids:
                raise BenchmarkError("A claim cites a source absent from its recorded access")
            used_spans.update(cited)
        elif claim["kind"] == "inference":
            if not isinstance(claim.get("premise_claim_ids"), list) or not claim["premise_claim_ids"]:
                raise BenchmarkError("An inference needs explicit premise claim IDs")
        else:
            raise BenchmarkError("Unknown claim kind")
    for claim in record["claims"]:
        if claim["kind"] == "inference" and not set(claim["premise_claim_ids"]) <= claim_ids:
            raise BenchmarkError("Inference names an unknown premise claim")
    usage = record["resource_use"]
    if not isinstance(usage, dict) or set(usage) != RESOURCE_KEYS:
        raise BenchmarkError("Resource use must retain all separately named units")
    for key, value in usage.items():
        if key == "provider_usage":
            if value is not None and not isinstance(value, list):
                raise BenchmarkError("Provider usage must be a list or unknown (null)")
            for charge in value or []:
                if not isinstance(charge, dict) or not all(
                    isinstance(charge.get(field), str) and charge[field]
                    for field in ("provider", "unit")
                ):
                    raise BenchmarkError("Provider usage needs a named provider and unit")
                amount = charge.get("amount")
                if amount is not None and not _nonnegative_number(amount):
                    raise BenchmarkError("Provider usage amount must be nonnegative or unknown (null)")
        elif key == "currency_code":
            if value is not None and (not isinstance(value, str) or not value):
                raise BenchmarkError("Currency code must be a nonempty string or null")
        elif value is not None and not _nonnegative_number(value):
            raise BenchmarkError(f"Resource value {key} must be nonnegative or unknown (null)")
    if usage["currency_amount"] is not None and usage["currency_code"] is None:
        raise BenchmarkError("Measured currency amount needs its currency code")
    assessment = record["assessment"]
    if not isinstance(assessment, dict) or not isinstance(assessment.get("ratings"), dict):
        raise BenchmarkError("Assessment must be a separate rating record")
    if assessment["ratings"] and not assessment.get("reviewer_ids"):
        raise BenchmarkError("Scored ratings need independent reviewer IDs")
    if assessment["ratings"] and assessment.get("reference_sha256") != entry["reference_sha256"]:
        raise BenchmarkError("Scored ratings need the current frozen reference hash")
    return {
        "run_id": record["run_id"],
        "scenario_id": record["scenario_id"],
        "proof_type": record["proof_type"],
        "locatable_citation_span_count": len(used_spans),
        "unique_fixture_source_count": len({span_to_source[x] for x in used_spans}),
        "scientific_support": "not_scored_by_structural_checker",
        "reader_usefulness": "not_scored_by_structural_checker",
    }
