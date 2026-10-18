"""Planning guidance stays optional, pinned, and separate from helper execution."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ora.agents.capabilities import content_hash
from ora.contracts.models import EvidencePacket
from ora.mission.skill_activation import (
    checked_registry,
    pin_catalog,
    selected_ids,
    selected_tools,
)
from ora.mission.skill_resources import read_skill_resource
from ora.skills import RegistryError, SkillUnavailable, load_registry
from ora.state import StateError
from tests.agents.test_configuration import _config, _fixture

ROOT = Path(__file__).resolve().parents[2]
SKILL = "task-planning"


def _copy_package(tmp_path: Path) -> Path:
    for relative in ("runtime_skills", "roles", "src/ora/skills"):
        shutil.copytree(ROOT / relative, tmp_path / relative)
    return tmp_path


def test_every_role_can_use_instruction_only_planning_without_extra_capabilities():
    registry = load_registry(ROOT, set())
    skill = registry.skills[SKILL]
    assert skill.version == "2.0.0"
    assert skill.helper is None
    assert skill.manifest["instruction_only"] is True
    assert "helper" not in skill.manifest
    assert skill.manifest["required_capabilities"] == []
    attempts = [registry.start_skill_attempt(role, SKILL, role) for role in registry.roles]
    assert len(attempts) == 9
    assert all(attempt.helper_sha256 is None for attempt in attempts)
    attempts[0].state["intent"] = "one task"
    assert attempts[1].state == {}
    assert registry.render_instructions(SKILL)
    assert registry.render_instructions(SKILL, "rich")


def test_activation_selects_plan_tools_and_release_preserves_independent_requests():
    pins = pin_catalog(ROOT)["literature_acquisition"]
    assert pins[SKILL]["optional"] is True
    assert pins[SKILL]["helper_sha256"] is None
    assert pins[SKILL]["tool_ids"] == ["save_plan", "modify_plan", "read_plan"]
    assert SKILL not in selected_ids(pins, {})
    assert "save_plan" not in selected_tools(pins, {})
    active = {"active_skill_ids": [SKILL]}
    assert selected_tools(pins, active) >= {"save_plan", "read_plan"}
    # The skill selection layer owns no independent capability requests.
    released = {"active_skill_ids": [], "requested_tool_ids": ["read_plan"]}
    assert "read_plan" not in selected_tools(pins, released)
    assert released["requested_tool_ids"] == ["read_plan"]
    assert checked_registry(ROOT, "literature_acquisition", pins, [SKILL]).skills[SKILL]


@pytest.mark.parametrize("resource", ["rich_instructions", "example"])
def test_planning_resources_require_activation(resource):
    pins = pin_catalog(ROOT)["analysis"]
    arguments = dict(
        role_id="analysis", pins=pins, skill_id=SKILL, resource=resource,
    )
    with pytest.raises(StateError, match="active"):
        read_skill_resource(ROOT, active_skill_ids=[], **arguments)
    page = read_skill_resource(ROOT, active_skill_ids=[SKILL], **arguments)
    assert page["text"]
    assert page["skill_version"] == "2.0.0"
    with pytest.raises(StateError, match="no helper source"):
        read_skill_resource(
            ROOT, role_id="analysis", pins=pins, active_skill_ids=[SKILL],
            skill_id=SKILL, resource="helper_source",
        )


def test_changed_optional_package_is_scoped_but_saved_selection_rejects_drift(tmp_path):
    root = _copy_package(tmp_path)
    pins = pin_catalog(root)["analysis"]
    concise = root / "runtime_skills/task_planning/CONCISE.md"
    concise.write_text(concise.read_text() + "\nChanged guidance.\n")
    registry = load_registry(root, {"source_read_by_id", "study_appraisal"})
    assert SKILL not in registry.skills
    assert "content hash differs" in registry.unavailable[SKILL]
    registry.start_skill_attempt("analysis", "general-web-discovery", "unaffected")
    with pytest.raises(SkillUnavailable):
        registry.start_skill_attempt("analysis", SKILL, "changed")
    with pytest.raises(StateError, match="changed or is unavailable"):
        checked_registry(root, "analysis", pins, [SKILL])


@pytest.mark.parametrize("marker", [False, "true", None])
def test_helper_absence_requires_explicit_boolean_instruction_only_marker(tmp_path, marker):
    root = _copy_package(tmp_path)
    path = root / "runtime_skills/task_planning/manifest.json"
    manifest = json.loads(path.read_text())
    if marker is None:
        manifest.pop("instruction_only")
    else:
        manifest["instruction_only"] = marker
    path.write_text(json.dumps(manifest))
    registry = load_registry(root, set())
    assert SKILL not in registry.skills
    assert "helper" in registry.unavailable[SKILL] or "boolean" in registry.unavailable[SKILL]


def test_instruction_only_cannot_hide_an_executable_helper(tmp_path):
    root = _copy_package(tmp_path)
    path = root / "runtime_skills/task_planning/manifest.json"
    manifest = json.loads(path.read_text())
    manifest["helper"] = None
    path.write_text(json.dumps(manifest))
    registry = load_registry(root, set())
    assert "cannot declare a helper" in registry.unavailable[SKILL]
    # Existing v1 manifests still require helpers and the canonical EvidencePacket input.
    legacy = root / "runtime_skills/evidence_linking/manifest.json"
    manifest = json.loads(legacy.read_text())
    manifest.pop("helper")
    legacy.write_text(json.dumps(manifest))
    with pytest.raises(RegistryError, match="lacks helper"):
        load_registry(root, set())


def test_configuration_pins_guidance_but_cannot_invoke_it_as_a_helper(tmp_path):
    store, registry, _, agent, _, _ = _fixture(tmp_path)
    example = json.loads((ROOT / "runtime_skills/evidence_linking/example.json").read_text())
    evidence = EvidencePacket.model_validate(example["input"]["evidence"])
    body = evidence.model_dump(mode="json")
    packet = store.transact(
        "planning-evidence", body,
        lambda tx: tx.put("evidence_packet", evidence.id, body, expected_version=0),
        mission_id="mission-one",
    )
    reference = {
        "kind": "evidence_packet", "id": packet["id"], "version": packet["version"],
        "sha256": content_hash(body),
    }
    config = _config(registry, agent, reference, skill_choices={SKILL: "concise"})
    assert config["body"]["skills"][SKILL]["helper_sha256"] is None
    registry.start_call(
        "planning-call", call_id="planning-call", configuration_id=config["id"],
        mission_id="mission-one", task_id="task", attempt_id="attempt",
        loaded_skill_ids=[SKILL], exposed_tool_ids=[], require_common_tools=False,
    )
    receipt = registry.invoke_skill(
        "planning-not-helper", mission_id="mission-one", call_id="planning-call",
        manifest_version=1, skill_id=SKILL, evidence_ref=reference, arguments={},
    )
    assert receipt["body"]["status"] == "rejected"
    assert receipt["body"]["reason"] == "Instruction-only skill has no executable helper"
    assert receipt["body"]["result"] is None


def test_archived_planning_pin_reads_original_resources_and_rejects_drift(tmp_path):
    from ora.mission.skill_activation import _pin, legacy_planning_skill

    root = _copy_package(tmp_path)
    old = legacy_planning_skill(root)
    pins = pin_catalog(root)['analysis']
    pins[SKILL] = _pin(old, True)
    assert pins[SKILL]['tool_ids'] == ['save_plan', 'read_plan']
    checked = checked_registry(root, 'analysis', pins, [SKILL])
    assert checked.render_instructions(SKILL) == old.instruction_paths['concise'].read_text()
    assert 'modify_plan' not in checked.render_instructions(SKILL)
    for resource, path in [('rich_instructions', old.instruction_paths['rich']),
                           ('example', old.example_path)]:
        page = read_skill_resource(root, role_id='analysis', pins=pins,
                                   active_skill_ids=[SKILL], skill_id=SKILL, resource=resource)
        assert page['text'] == path.read_text()[:1800]
        assert page['skill_version'] == '1.0.0'
    old.instruction_paths['rich'].write_text('Unpinned changed guidance')
    with pytest.raises(RegistryError, match='content hash differs'):
        checked_registry(root, 'analysis', pins, [SKILL])
