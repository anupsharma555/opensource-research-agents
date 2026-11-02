"""Pin role skills and track optional instruction/tool selections across turns.

Core evidence rules stay loaded. Optional skills have explicit activation and
release, with independent tool requests; no elapsed time or research cap expires
an agent's selection. Package drift prevents a new selection from silently
changing the instructions used by an existing mission.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ora.agents.capabilities import content_hash
from ora.skills import load_registry
from ora.skills.registry import SkillDefinition, _checked_file, _read
from ora.state import StateError

LIFECYCLE_TOOLS = frozenset({"activate_skill", "release_skill", "release_capability"})
ROLE_CATALOG_ID_SCHEMA = "ora.role_capability_catalog.ids.v1"
ROLE_CATALOG_PURPOSE_SCHEMA = "ora.role_capability_catalog.purposes.v2"
# These are existing host-granted mission tools, not new grants from skill prose.
# Appraisal uses notes to preserve preliminary assessments and disagreements.
SKILL_TOOLS = {
    "study-appraisal": ("write_note", "read_note", "search_notes"),
    "task-planning": ("save_plan", "modify_plan", "read_plan"),
}

CALLABILITY_GUIDANCE = (
    "Call attached local functions directly; do not request them again. "
    "Catalog grants without attached schemas "
    "are not callable: use request_first_local_functions when listed, otherwise "
    "request_first_rule and any unavailable_now exceptions. For an eligible "
    "deferred local tool, call request_capability with exact tool_id, then wait "
    "for selected_for_next_turn and an attached schema. Resolve "
    "not_ready_local_functions data prerequisites first. discover_capabilities "
    "is optional for a known eligible tool ID. Requesting browser_search "
    "selects a native provider turn, never a local function. Answer when sufficient."
)


def _pin(skill, optional: bool) -> dict:
    instructions = skill.instruction_paths["concise"].read_bytes()
    return {
        "version": skill.version,
        "manifest_sha256": content_hash(skill.manifest),
        "instructions_sha256": hashlib.sha256(instructions).hexdigest(),
        "helper_sha256": (skill.manifest["helper"]["sha256"]
                          if skill.helper is not None else None),
        "purpose": skill.manifest["purpose"],
        "optional": optional,
        "tool_ids": (["save_plan", "read_plan"]
                     if skill.skill_id == "task-planning" and skill.version == "1.0.0"
                     else list(SKILL_TOOLS.get(skill.skill_id, ()))),
    }


def pin_catalog(root) -> dict:
    registry = load_registry(root, {"source_read_by_id", "study_appraisal"})
    index = json.loads((root / "runtime_skills" / "registry.json").read_text())
    optional = {item["skill_id"] for item in index["skills"] if item.get("optional")}
    return {
        role_id: {
            item["skill_id"]: _pin(registry.skills[item["skill_id"]], item["skill_id"] in optional)
            for item in role["skill_refs"] if item["skill_id"] in registry.skills
        }
        for role_id, role in registry.roles.items()
    }


def selected_ids(pins: dict, body: dict) -> list[str]:
    active = set(body.get("active_skill_ids", []))
    if active - pins.keys():
        raise StateError("Active skill is not pinned and role-granted")
    return sorted(active | {name for name, pin in pins.items() if not pin["optional"]})


def legacy_planning_skill(root):
    """Retain exact v1 bytes for saved pins; never upgrade an active old run."""
    directory = "runtime_skills/task_planning/v1/"
    manifest = _read(root / directory / "manifest.json")
    if (manifest.get("skill_id") != "task-planning" or manifest.get("version") != "1.0.0"
            or manifest.get("instruction_only") is not True or "helper" in manifest):
        raise StateError("Archived planning package is invalid")

    def archived(spec, filename):
        return _checked_file(root, {**spec, "path": directory + filename}, "Archived planning")

    return SkillDefinition(
        "task-planning", "1.0.0", manifest,
        {name: archived(manifest["instruction_variants"][name], filename)
         for name, filename in (("concise", "CONCISE.md"), ("rich", "RICH.md"))},
        archived(manifest["example"], "example.json"), None,
    )


def checked_registry(root, role_id: str, pins: dict, selected: list[str]):
    registry = load_registry(root, {"source_read_by_id", "study_appraisal"})
    allowed = {item["skill_id"]: item["version"] for item in registry.roles[role_id]["skill_refs"]}
    for name in selected:
        pin = pins.get(name)
        skill = registry.skills.get(name)
        if (name == "general-web-discovery" and name in allowed and pin
                and pin.get("version") in {"1.0.0", "2.0.0"}):
            archived_version = pin["version"]
            directory = ("runtime_skills/general_web_discovery/v1/"
                         if archived_version == "1.0.0"
                         else "runtime_skills/general_web_discovery/v2/")
            manifest = _read(root / directory / "manifest.json")
            if manifest.get("skill_id") != name or manifest.get("version") != archived_version:
                raise StateError("Archived web-discovery package is invalid")
            skill = SkillDefinition(name, archived_version, manifest, {
                variant: _checked_file(root, {**shape, "path": directory + Path(shape["path"]).name}, "Archived web discovery")
                for variant, shape in manifest["instruction_variants"].items()
            }, _checked_file(root, {**manifest["example"], "path": directory + "example.json"}, "Archived web example"), skill.helper)
            registry.skills[name] = skill
            allowed[name] = skill.version
        if (name == "task-planning" and name in allowed and pin
                and pin.get("version") == "1.0.0"):
            skill = legacy_planning_skill(root)
            registry.skills[name] = skill
            allowed[name] = skill.version
        if (pin is None or skill is None or allowed.get(name) != pin["version"]
                or _pin(skill, pin["optional"]) != pin):
            raise StateError(f"Pinned skill package changed or is unavailable: {name}")
    return registry


def catalog_prompt(
    pins: dict, active: list[str], tools: dict[str, str], *, lifecycle: bool,
    projection_version: int | None = None,
) -> str:
    """Name role-granted skills and supported tools without loading their bodies or schemas."""
    skills = {
        name: {"purpose": pin["purpose"], "state": (
            "active" if name in active else "available_to_activate"
        ), **({"tools_on_activation": pin.get("tool_ids", [])} if pin.get("optional") else {})}
        for name, pin in sorted(pins.items())
    }
    guidance = (
        "Skills guide workflows; tools act. activate_skill loads guidance and selected "
        "tools next turn; release_skill unloads them. Separately requested tools persist "
        "until release_capability. Changes apply next turn and grant no new permissions "
        "or data. "
        if lifecycle else "Listed skills are already active. "
    )
    if projection_version not in (None, 1, 2):
        raise StateError("Unsupported role capability catalog projection")
    if projection_version == 2 and (
        not isinstance(tools, dict)
        or any(not isinstance(name, str) or not name
               or not isinstance(purpose, str) or not purpose.strip()
               for name, purpose in tools.items())
    ):
        raise StateError("Purpose catalog needs nonempty tool names and purposes")
    if projection_version == 1:
        catalog = {"schema": ROLE_CATALOG_ID_SCHEMA, "skills": skills,
                   "tool_ids": sorted(tools)}
    elif projection_version == 2:
        catalog = {"schema": ROLE_CATALOG_PURPOSE_SCHEMA, "skills": skills,
                   "tools": dict(sorted(tools.items()))}
    else:
        catalog = {"skills": skills, "tools": dict(sorted(tools.items()))}
    discovery = (
        "discover_capabilities supplies deferred tool purposes and current access status"
        if projection_version == 1 else
        "discover_capabilities refreshes access status"
    )
    return (
        "Role capability catalog: "
        + json.dumps(catalog, separators=(",", ":"))
        + ". " + guidance
        + discovery + "; request read_skill_resource "
        "for paged active-skill detail."
    )


def selected_tools(pins: dict, body: dict) -> set[str]:
    return {tool for name in selected_ids(pins, body) for tool in pins[name]["tool_ids"]}
