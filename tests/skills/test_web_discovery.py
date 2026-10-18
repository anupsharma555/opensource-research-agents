"""General web discovery is shared guidance, not automatic external search."""

from __future__ import annotations

import json
from pathlib import Path

from ora.contracts.models import EvidencePacket
from ora.skills import load_registry, prepare_claim_link

ROOT = Path(__file__).resolve().parents[2]


def test_all_nine_roles_load_short_guidance_without_browser_capability():
    registry = load_registry(ROOT, {"source_read_by_id", "study_appraisal"})
    assert len(registry.roles) == 9
    assert "general-web-discovery" in registry.skills
    for role_id in registry.roles:
        attempt = registry.start_skill_attempt(role_id, "general-web-discovery", role_id)
        assert attempt.skill_version == "2.0.0"
    concise = registry.render_instructions("general-web-discovery")
    rich = registry.render_instructions("general-web-discovery", "rich")
    assert len(concise.split()) <= 170
    assert len(rich.split()) > len(concise.split())
    assert "request `browser_search`" in concise
    assert "Honor no-external-search limits" in concise
    assert "not inspected evidence" in concise
    assert "`fetch_source`" in concise


def test_synthetic_example_links_only_a_separately_inspected_packet():
    example = json.loads((ROOT / "runtime_skills" / "general_web_discovery"
                          / "example.json").read_text(encoding="utf-8"))
    assert example["synthetic"] is True
    assert "metadata only" in example["scenario"]
    values = example["input"]
    packet = EvidencePacket.model_validate(values["evidence"])
    result = prepare_claim_link(
        packet, claim=values["claim"], locator=values["locator"],
        relation=values["relation"],
    )
    assert result == example["expected_helper_output"]
    assert result["semantic_support_status"] == "pending_independent_review"
