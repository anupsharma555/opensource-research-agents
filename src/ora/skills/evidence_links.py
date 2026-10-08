"""Prepare a source-linked claim for independent semantic support review.

This pure helper validates identity and locator membership. It cannot determine
whether the cited passage entails a claim; a qualified reviewer must do that.
"""

from __future__ import annotations

from typing import Literal

from ora.contracts.models import EvidencePacket

Relation = Literal["supports", "challenges", "context"]


def prepare_claim_link(
    evidence: EvidencePacket, *, claim: str, locator: str, relation: Relation
) -> dict[str, object]:
    if not isinstance(evidence, EvidencePacket):
        raise TypeError("evidence must use the canonical EvidencePacket contract")
    if not claim.strip():
        raise ValueError("claim text is required")
    if locator not in evidence.locators:
        raise ValueError("locator is not present in this evidence packet")
    if relation not in {"supports", "challenges", "context"}:
        raise ValueError("relation must be supports, challenges, or context")
    return {
        "claim": claim.strip(),
        "relation": relation,
        "evidence_packet_id": evidence.id,
        "evidence_packet_version": evidence.version,
        "evidence_kind": evidence.kind,
        "artifact_id": evidence.artifact.id,
        "artifact_version": evidence.artifact.version,
        "artifact_sha256": evidence.artifact.sha256,
        "locator": locator,
        "semantic_support_status": "pending_independent_review",
    }
