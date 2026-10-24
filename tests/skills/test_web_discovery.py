"""General web discovery is shared guidance, not automatic external search."""

from __future__ import annotations

import json
from pathlib import Path

from ora.contracts.models import EvidencePacket
from ora.mission.skill_activation import _pin, checked_registry, pin_catalog
from ora.skills import load_registry, prepare_claim_link
from ora.skills.registry import SkillDefinition

ROOT = Path(__file__).resolve().parents[2]


def test_all_nine_roles_load_short_guidance_without_browser_capability():
    registry = load_registry(ROOT, {"source_read_by_id", "study_appraisal"})
    assert len(registry.roles) == 9
    assert "general-web-discovery" in registry.skills
    for role_id in registry.roles:
        attempt = registry.start_skill_attempt(role_id, "general-web-discovery", role_id)
        assert attempt.skill_version == "3.0.0"
    concise = registry.render_instructions("general-web-discovery")
    rich = registry.render_instructions("general-web-discovery", "rich")
    assert len(concise.split()) <= 170
    assert len(rich.split()) > len(concise.split())
    assert "request `browser_search`" in concise
    assert "Honor no-external-search limits" in concise
    assert "not inspected evidence" in concise
    assert "`fetch_source`" in concise
    assert "`read_research_activity`" in concise
    assert "`search_evidence_library`" in concise
    assert "`entry_id`" in concise
    assert "`refresh=true`" in concise


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


def test_saved_v1_and_v2_web_skill_pins_keep_archived_instructions():
    current = load_registry(ROOT, {"source_read_by_id", "study_appraisal"})
    for version, folder in (("1.0.0", "v1"), ("2.0.0", "v2")):
        archived = ROOT / "runtime_skills" / "general_web_discovery" / folder
        manifest = json.loads((archived / "manifest.json").read_text(encoding="utf-8"))
        skill = SkillDefinition(
            "general-web-discovery", version, manifest,
            {variant: archived / filename for variant, filename in (
                ("concise", "CONCISE.md"), ("rich", "RICH.md"),
            )}, archived / "example.json", current.skills["general-web-discovery"].helper,
        )
        pins = pin_catalog(ROOT)["analysis"]
        pins["general-web-discovery"] = _pin(skill, False)
        selected = checked_registry(ROOT, "analysis", pins, ["general-web-discovery"])
        assert selected.skills["general-web-discovery"].version == version
        assert selected.render_instructions("general-web-discovery") == (
            archived / "CONCISE.md"
        ).read_text(encoding="utf-8")
