"""Prepare a result-scoped appraisal record without grading science by rule."""

from __future__ import annotations

from collections.abc import Mapping

from ora.contracts.models import EvidencePacket

JUDGMENT_FIELDS = frozenset(
    {"reporting_completeness", "risk_of_bias", "applicability", "precision"}
)


def prepare_result_appraisal(
    evidence: EvidencePacket,
    *,
    locator: str,
    method_id: str,
    study_design: str,
    outcome: str,
    comparison: str,
    time_point: str,
    judgments: Mapping[str, str],
    rationale: str,
) -> dict[str, object]:
    """Bind agent-chosen judgments to the exact result and preserved copy.

    The helper checks presence and traceability, not method suitability or
    scientific correctness. An abstract-only packet can carry unknown values.
    """
    if not isinstance(evidence, EvidencePacket):
        raise TypeError("evidence must use the canonical EvidencePacket contract")
    if locator not in evidence.locators:
        raise ValueError("appraisal locator is not present in the evidence packet")
    required_text = {
        "method_id": method_id,
        "study_design": study_design,
        "outcome": outcome,
        "comparison": comparison,
        "time_point": time_point,
        "rationale": rationale,
    }
    missing = [name for name, value in required_text.items() if not isinstance(value, str) or not value.strip()]
    if missing:
        raise ValueError("appraisal needs " + ", ".join(missing))
    if set(judgments) != JUDGMENT_FIELDS:
        raise ValueError("appraisal needs separate reporting, bias, applicability, and precision judgments")
    if any(not isinstance(value, str) or not value.strip() for value in judgments.values()):
        raise ValueError("appraisal judgments must be stated or explicitly unknown")
    return {
        "evidence_packet_id": evidence.id,
        "evidence_packet_version": evidence.version,
        "artifact_id": evidence.artifact.id,
        "artifact_version": evidence.artifact.version,
        "artifact_sha256": evidence.artifact.sha256,
        "locator": locator,
        "method_id": method_id.strip(),
        "study_design": study_design.strip(),
        "outcome": outcome.strip(),
        "comparison": comparison.strip(),
        "time_point": time_point.strip(),
        "judgments": {key: value.strip() for key, value in judgments.items()},
        "rationale": rationale.strip(),
        "scientific_validity_status": "agent_judgment_pending_independent_check",
    }
