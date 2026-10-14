"""Read bounded supporting material for a currently active, pinned skill.

The request names a registered resource, never a filesystem path. Source files
are returned as text only; this module does not invoke a skill helper.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ora.state import StateError

from .skill_activation import checked_registry, selected_ids

RESOURCE_PAGE_SCHEMA = "ora.skill_resource_page.v1"
MAX_SKILL_RESOURCE_CHARS = 1800
RESOURCES = frozenset({"rich_instructions", "example", "helper_source"})


def authorized_skill(root: Path, *, role_id: str, pins: dict,
                     active_skill_ids: list[str], skill_id: str):
    """Recheck this role's saved pin and current active selection before a read.

    Mandatory skills are selected by the existing lifecycle rule. Optional
    skills require their ID in the control's current active list.
    """
    if not isinstance(skill_id, str) or not skill_id:
        raise StateError("Skill ID is required")
    if not isinstance(pins, dict) or not isinstance(active_skill_ids, list):
        raise StateError("Saved skill selection is invalid")
    selected = selected_ids(pins, {"active_skill_ids": active_skill_ids})
    if skill_id not in selected:
        raise StateError("Skill resource requires an active, role-granted skill")
    registry = checked_registry(root, role_id, pins, [skill_id])
    return registry.skills[skill_id]


def _resource_spec(skill, resource: str) -> tuple[dict, dict, str]:
    manifest = skill.manifest
    if resource == "rich_instructions":
        return manifest["instruction_variants"]["rich"], {}, "guidance, not evidence"
    if resource == "example":
        return manifest["example"], {"synthetic": True}, "synthetic example, not fresh source evidence"
    helper = manifest["helper"]
    metadata = {key: helper[key] for key in (
        "module", "function", "dependencies", "helper_version", "origin_type", "side_effects"
    ) if key in helper}
    return helper, metadata, "code for inspection, not execution"


def _verified_text(root: Path, spec: dict) -> tuple[str, str]:
    """Read only the exact manifest-listed file, confined to the project root."""
    relative = spec["path"]
    if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
            or ".." in Path(relative).parts):
        raise StateError("Skill resource manifest path is invalid")
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise StateError("Skill resource path escapes the project or is unavailable")
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StateError("Skill resource is unreadable text") from exc
    digest = hashlib.sha256(data).hexdigest()
    if digest != spec["sha256"]:
        raise StateError("Pinned skill resource content changed")
    return text, digest


def read_skill_resource(
    root: Path, *, role_id: str, pins: dict, active_skill_ids: list[str],
    skill_id: str, resource: str, offset: int = 0,
    content_sha256: str = "", limit: int = MAX_SKILL_RESOURCE_CHARS,
) -> dict:
    """Return one hash-bound character page of registered skill material.

    A continuation rechecks role grant, activation, package pin and source
    bytes. Its content hash rejects changed material instead of mixing pages.
    """
    if not isinstance(resource, str) or resource not in RESOURCES:
        raise StateError("Unknown skill resource identifier")
    if type(offset) is not int or offset < 0:
        raise StateError("Skill resource offset must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= MAX_SKILL_RESOURCE_CHARS:
        raise StateError("Skill resource limit must be between 1 and 1800")
    if not isinstance(content_sha256, str) or (offset and not content_sha256):
        raise StateError("Skill resource continuation requires its content hash")
    skill = authorized_skill(
        root, role_id=role_id, pins=pins,
        active_skill_ids=active_skill_ids, skill_id=skill_id,
    )
    spec, metadata, qualification = _resource_spec(skill, resource)
    text, digest = _verified_text(root, spec)
    if content_sha256 and content_sha256 != digest:
        raise StateError("Skill resource changed; restart from offset zero")
    if offset > len(text):
        raise StateError("Skill resource offset exceeds its content")
    end = min(offset + limit, len(text))
    return {
        "schema": RESOURCE_PAGE_SCHEMA,
        "skill_id": skill_id,
        "skill_version": skill.version,
        "resource": resource,
        "content_sha256": digest,
        "qualification": qualification,
        "metadata": metadata,
        "offset": offset,
        "end_offset": end,
        "total_chars": len(text),
        "text": text[offset:end],
        "next_request": ({
            "tool": "read_skill_resource", "skill_id": skill_id,
            "resource": resource, "offset": end, "content_sha256": digest,
            "limit": limit,
        } if end < len(text) else None),
    }
