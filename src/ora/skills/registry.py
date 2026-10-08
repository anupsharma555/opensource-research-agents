"""Find which saved skill packages each research-agent role can use.

This loader reads role and skill manifests, checks referenced files and exact
versions, and reports when a required tool capability is unavailable. It creates
separate attempt state for callers but does not start model work.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


class RegistryError(ValueError):
    """A required registry reference or package is malformed."""


class SkillUnavailable(RegistryError):
    """A role's optional or unavailable capability cannot start this skill."""


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RegistryError(f"Expected a JSON object at {path}")
    return value


def _path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise RegistryError("Package path must be a nonempty relative string")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise RegistryError(f"Package path escapes project root: {relative}")
    return path


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RegistryError(f"Cannot read package file {path}: {exc}") from exc


def _checked_file(root: Path, spec: dict[str, Any], label: str) -> Path:
    if not isinstance(spec, dict) or not all(key in spec for key in ("path", "sha256")):
        raise RegistryError(f"{label} needs a path and content hash")
    path = _path(root, spec["path"])
    try:
        digest = _file_sha256(path)
    except RegistryError as exc:
        raise RegistryError(f"{label} is missing or unreadable: {spec['path']}") from exc
    if digest != spec["sha256"]:
        raise RegistryError(f"{label} content hash differs from its manifest: {spec['path']}")
    return path


@dataclass(frozen=True)
class SkillDefinition:
    skill_id: str
    version: str
    manifest: dict[str, Any]
    instruction_paths: dict[str, Path]
    example_path: Path
    helper: Callable[..., dict[str, object]]


@dataclass
class SkillAttempt:
    role_id: str
    skill_id: str
    skill_version: str
    attempt_id: str
    helper_sha256: str
    state: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillRegistry:
    roles: dict[str, dict[str, Any]]
    skills: dict[str, SkillDefinition]
    unavailable: dict[str, str]

    def render_instructions(self, skill_id: str, variant: str = "concise") -> str:
        skill = self.skills.get(skill_id)
        if skill is None:
            raise SkillUnavailable(self.unavailable.get(skill_id, f"Unknown skill: {skill_id}"))
        if variant not in skill.instruction_paths:
            raise RegistryError(f"Unknown instruction variant {variant} for {skill_id}")
        return skill.instruction_paths[variant].read_text(encoding="utf-8")

    def start_skill_attempt(self, role_id: str, skill_id: str, attempt_id: str) -> SkillAttempt:
        role = self.roles.get(role_id)
        if role is None:
            raise RegistryError(f"Unknown role: {role_id}")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise RegistryError("Attempt ID is required")
        refs = {item["skill_id"]: item["version"] for item in role["skill_refs"]}
        if skill_id not in refs:
            raise RegistryError(f"Role {role_id} does not declare skill {skill_id}")
        skill = self.skills.get(skill_id)
        if skill is None:
            raise SkillUnavailable(self.unavailable.get(skill_id, f"Unavailable skill: {skill_id}"))
        if refs[skill_id] != skill.version:
            raise SkillUnavailable(
                f"Role {role_id} requests {skill_id} {refs[skill_id]}, available version is {skill.version}"
            )
        return SkillAttempt(
            role_id=role_id,
            skill_id=skill_id,
            skill_version=skill.version,
            attempt_id=attempt_id,
            helper_sha256=skill.manifest["helper"]["sha256"],
        )


def _load_skill(root: Path, manifest_path: Path) -> SkillDefinition:
    manifest = _read(manifest_path)
    if manifest.get("schema") != "ora.runtime_skill.v1":
        raise RegistryError(f"Unsupported skill manifest schema: {manifest_path}")
    for key in ("skill_id", "version", "purpose", "trigger", "input_contract", "output_contract", "failure_handling", "required_capabilities", "instruction_variants", "example", "helper"):
        if key not in manifest:
            raise RegistryError(f"Skill manifest {manifest_path} lacks {key}")
    if not isinstance(manifest["required_capabilities"], list) or not all(
        isinstance(value, str) and value for value in manifest["required_capabilities"]
    ):
        raise RegistryError(f"{manifest['skill_id']} needs named required capabilities")
    if manifest["input_contract"] != "ora.contracts.models.EvidencePacket":
        raise RegistryError(f"{manifest['skill_id']} must use canonical EvidencePacket input")
    variants = manifest["instruction_variants"]
    if not isinstance(variants, dict) or set(variants) != {"concise", "rich"}:
        raise RegistryError(f"{manifest['skill_id']} needs concise and rich instruction variants")
    instruction_paths = {
        name: _checked_file(root, spec, f"{manifest['skill_id']} {name} instructions")
        for name, spec in variants.items()
    }
    example_path = _checked_file(root, manifest["example"], f"{manifest['skill_id']} example")
    example = _read(example_path)
    if example.get("schema") != "ora.runtime_skill.example.v1" or example.get("synthetic") is not True:
        raise RegistryError(f"{manifest['skill_id']} needs a synthetic versioned example")
    helper = manifest["helper"]
    needed = {"path", "sha256", "module", "function", "origin_type", "license", "dependencies", "local_changes", "upstream_url", "upstream_commit"}
    if not isinstance(helper, dict) or needed - helper.keys():
        raise RegistryError(f"{manifest['skill_id']} has incomplete helper provenance")
    if helper["origin_type"] != "project_owned" or helper["upstream_url"] is not None or helper["upstream_commit"] is not None:
        raise RegistryError(f"{manifest['skill_id']} helper provenance does not match project-owned code")
    if not isinstance(helper["license"], str) or not helper["license"].strip() or not helper["local_changes"]:
        raise RegistryError(f"{manifest['skill_id']} helper needs license and local-change status")
    if not isinstance(helper["dependencies"], list):
        raise RegistryError(f"{manifest['skill_id']} helper dependencies must be a list")
    _checked_file(root, helper, f"{manifest['skill_id']} helper")
    for dependency in helper["dependencies"]:
        if dependency.startswith("pydantic=="):
            wanted = dependency.removeprefix("pydantic==")
            try:
                actual = importlib.metadata.version("pydantic")
            except importlib.metadata.PackageNotFoundError as exc:
                raise RegistryError(f"{manifest['skill_id']} requires pydantic=={wanted}; install the selected core environment") from exc
            if actual != wanted:
                raise RegistryError(f"{manifest['skill_id']} requires pydantic=={wanted}; found {actual}")
        else:
            raise RegistryError(f"Unknown helper dependency declaration: {dependency}")
    try:
        function = getattr(importlib.import_module(helper["module"]), helper["function"])
    except (ImportError, AttributeError) as exc:
        raise RegistryError(f"{manifest['skill_id']} helper is unavailable: {exc}") from exc
    if not callable(function):
        raise RegistryError(f"{manifest['skill_id']} helper target is not callable")
    return SkillDefinition(manifest["skill_id"], manifest["version"], manifest, instruction_paths, example_path, function)


def load_registry(project_root: Path | str, available_capabilities: set[str]) -> SkillRegistry:
    """Load shared skills even when an optional specialist is unavailable."""
    root = Path(project_root).resolve()
    index = _read(root / "runtime_skills" / "registry.json")
    if index.get("schema") != "ora.runtime_skills.registry.v1":
        raise RegistryError("Unsupported runtime-skill index schema")
    roles_spec = index.get("roles")
    roles_path = _checked_file(root, roles_spec, "role manifest")
    roles_doc = _read(roles_path)
    if roles_doc.get("schema") != "ora.roles.v1" or not isinstance(roles_doc.get("roles"), list):
        raise RegistryError("Unsupported role manifest schema")
    roles: dict[str, dict[str, Any]] = {}
    for role in roles_doc["roles"]:
        role_id = role.get("role_id")
        if not isinstance(role_id, str) or not role_id or role_id in roles:
            raise RegistryError(f"Duplicate or invalid role ID: {role_id}")
        if not isinstance(role.get("skill_refs"), list) or not role.get("name"):
            raise RegistryError(f"Invalid role entry: {role_id}")
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("skill_id"), str)
            or not isinstance(item.get("version"), str)
            for item in role["skill_refs"]
        ):
            raise RegistryError(f"Invalid skill reference in role: {role_id}")
        if len({item["skill_id"] for item in role["skill_refs"]}) != len(role["skill_refs"]):
            raise RegistryError(f"Duplicate skill reference in role: {role_id}")
        roles[role_id] = role
    if len(roles) != 9:
        raise RegistryError("The role manifest must preserve the nine plan responsibilities")
    listed = index.get("skills")
    if not isinstance(listed, list):
        raise RegistryError("Runtime-skill index needs a skill list")
    declared_ids = [item.get("skill_id") for item in listed]
    if len(declared_ids) != len(set(declared_ids)):
        raise RegistryError("Duplicate runtime skill IDs in registry")
    known = set(declared_ids)
    optional_ids = {item["skill_id"] for item in listed if item.get("optional") is True}
    skills: dict[str, SkillDefinition] = {}
    unavailable: dict[str, str] = {}
    for item in listed:
        skill_id = item["skill_id"]
        try:
            skill = _load_skill(root, _path(root, item["manifest"]))
            if skill.skill_id != skill_id:
                raise RegistryError(f"Manifest skill ID disagrees with registry: {skill_id}")
            missing = set(skill.manifest["required_capabilities"]) - available_capabilities
            if missing:
                unavailable[skill_id] = f"{skill_id} needs capability: {', '.join(sorted(missing))}"
            else:
                skills[skill_id] = skill
        except RegistryError as exc:
            if item.get("optional") is True:
                unavailable[skill_id] = f"Optional {skill_id} unavailable: {exc}"
            else:
                raise
    for role in roles.values():
        for ref in role["skill_refs"]:
            skill_id, version = ref["skill_id"], ref["version"]
            if skill_id not in known:
                raise RegistryError(f"Role {role['role_id']} names unknown skill {skill_id}")
            if skill_id in skills and skills[skill_id].version != version:
                message = f"{skill_id} version {skills[skill_id].version} is incompatible with role request {version}"
                if skill_id in optional_ids:
                    unavailable[skill_id] = message
                    del skills[skill_id]
                else:
                    raise RegistryError(message)
    return SkillRegistry(roles, skills, unavailable)
