"""Pin role skills and track optional instruction/tool selections across turns.

Core evidence rules stay loaded. Optional skills have explicit activation and
release, with independent tool requests; no elapsed time or research cap expires
an agent's selection. Package drift prevents a new selection from silently
changing the instructions used by an existing mission.
"""

from __future__ import annotations

import hashlib
import json

from ora.agents.capabilities import content_hash
from ora.skills import load_registry
from ora.state import StateError

LIFECYCLE_TOOLS = frozenset({"activate_skill", "release_skill", "release_capability"})
# These are existing host-granted mission tools, not new grants from skill prose.
# Appraisal uses notes to preserve preliminary assessments and disagreements.
SKILL_TOOLS = {"study-appraisal": ("write_note", "read_note", "search_notes")}

CALLABILITY_GUIDANCE = (
    "CALL NOW: Call an attached local function directly; do not request it just to use it. "
    "The catalog lists grants, while attached schemas show what is callable now. "
    "REQUEST FIRST: For a tool in request_first_local_functions, call "
    "request_capability with the exact tool_id. NEXT TURN: Wait for "
    "selected_for_next_turn and the tool's attached schema. "
    "DATA FIRST: Follow the specific explanation in not_ready_local_functions. "
    "For example, inspect_hit needs an eligible saved search result; requesting access "
    "does not create that result. "
    "For browser_search, request_capability selects a native provider turn; never call "
    "browser_search as a local function. "
    "Choose tools for the task's needs; a supported direct answer is valid when no "
    "additional information or action is needed."
)


def _pin(skill, optional: bool) -> dict:
    instructions = skill.instruction_paths["concise"].read_bytes()
    return {
        "version": skill.version,
        "manifest_sha256": content_hash(skill.manifest),
        "instructions_sha256": hashlib.sha256(instructions).hexdigest(),
        "helper_sha256": skill.manifest["helper"]["sha256"],
        "purpose": skill.manifest["purpose"],
        "optional": optional,
        "tool_ids": list(SKILL_TOOLS.get(skill.skill_id, ())),
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


def checked_registry(root, role_id: str, pins: dict, selected: list[str]):
    registry = load_registry(root, {"source_read_by_id", "study_appraisal"})
    allowed = {item["skill_id"]: item["version"] for item in registry.roles[role_id]["skill_refs"]}
    for name in selected:
        pin = pins.get(name)
        skill = registry.skills.get(name)
        if (pin is None or skill is None or allowed.get(name) != pin["version"]
                or _pin(skill, pin["optional"]) != pin):
            raise StateError(f"Pinned skill package changed or is unavailable: {name}")
    return registry


def catalog_prompt(pins: dict, active: list[str], tools: dict[str, str], *, lifecycle: bool) -> str:
    """Name role-granted skills and supported tools without loading their bodies or schemas."""
    skills = {
        name: {"purpose": pin["purpose"], "state": (
            "active" if name in active else "available_to_activate"
        ), **({"tools_on_activation": pin.get("tool_ids", [])} if pin.get("optional") else {})}
        for name, pin in sorted(pins.items())
    }
    guidance = (
        "Skills provide reusable workflow guidance; tools perform individual operations. "
        "Use active guidance when relevant. Activate a listed optional skill when its "
        "purpose would help with the task, not merely to call an already available tool. "
        "Optional skills load next turn via activate_skill with the exact skill_id, "
        "persist across restarts, and "
        "unload via release_skill. Local tool requests persist across skill release until "
        "release_capability. Release optional selections when no longer useful, not "
        "between every step. After selection changes, use the next turn's tool-availability "
        "notice and attached schemas; activation never overrides permission or missing data. "
        if lifecycle else "Listed skills are already active. "
    )
    return (
        "Role capability catalog: "
        + json.dumps({"skills": skills, "tools": dict(sorted(tools.items()))}, separators=(",", ":"))
        + ". " + guidance
        + "discover_capabilities optionally refreshes this catalog."
    )


def selected_tools(pins: dict, body: dict) -> set[str]:
    return {tool for name in selected_ids(pins, body) for tool in pins[name]["tool_ids"]}
