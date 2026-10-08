"""Build a candidate link between a research claim and one saved source passage.

The helper checks that the named passage belongs to the supplied evidence copy
and carries the source file's ID, version, and hash into the result. A reviewer
must still decide whether the passage actually supports or challenges the claim.
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
