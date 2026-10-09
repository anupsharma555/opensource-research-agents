"""Pin effective model, instruction, skill and tool inputs for each agent call."""

from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ora.contracts.models import EvidencePacket
from ora.skills import load_registry

from .capabilities import COMMON_TOOL_IDS, CapabilityCatalog, capability_view, content_hash
from .json_shapes import JsonShapeError, finite_json


class ConfigurationError(ValueError):
    """An effective configuration is invalid or cannot be resumed unchanged."""


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            "Endpoint must be an HTTP URL without embedded credentials or query"
        )
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ConfigurationError("Nonlocal model endpoints require HTTPS")


def _check_settings(settings: dict[str, Any]) -> None:
    if type(settings) is not dict:
        raise ConfigurationError("Model settings must be an object")
    try:
        finite_json(settings)
    except JsonShapeError as exc:
        raise ConfigurationError(str(exc)) from exc

    def inspect(value: Any) -> None:
        if type(value) is list:
            for child in value:
                inspect(child)
        elif type(value) is dict:
            for key, child in value.items():
                normalized = "".join(
                    character.casefold()
                    for character in unicodedata.normalize("NFKC", key)
                    if character.isalnum()
                )
                if (
                    not normalized
                    or normalized in {"token", "key", "auth", "authorization", "secret"}
                    or normalized.endswith("token")
                    or normalized.endswith("key")
                    or normalized.endswith("auth")
                    or any(term in normalized for term in (
                        "secret", "password", "credential", "apikey", "accesskey",
                        "authkey", "privatekey", "sessionkey", "authorization", "bearer",
                    ))
                ):
                    raise ConfigurationError("Model settings need nonsecret JSON keys")
                inspect(child)

    inspect(settings)


def _check_refs(refs: list[dict[str, Any]]) -> None:
    if not isinstance(refs, list):
        raise ConfigurationError("Input references must be a list")
    for ref in refs:
        if (
            not isinstance(ref, dict)
            or set(ref) != {"kind", "id", "version", "sha256"}
            or not isinstance(ref["kind"], str)
            or not ref["kind"]
            or not isinstance(ref["id"], str)
            or not ref["id"]
            or type(ref["version"]) is not int
            or ref["version"] < 1
            or not isinstance(ref["sha256"], str)
            or len(ref["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in ref["sha256"])
        ):
            raise ConfigurationError("Input references need kind, ID, version and SHA-256")


class ConfigurationRegistry:
    """Store configuration snapshots in the common versioned state store."""

    def __init__(
        self,
        store: Any,
        project_root: Path,
        catalog: CapabilityCatalog,
        *,
        available_skill_capabilities: set[str] | None = None,
    ) -> None:
        self.store = store
        self.project_root = Path(project_root)
        self.catalog = catalog
        self.skill_capabilities = available_skill_capabilities or set()

    def _skills(self, role_id: str, choices: dict[str, str]) -> dict[str, dict[str, Any]]:
        registry = load_registry(self.project_root, self.skill_capabilities)
        role = registry.roles.get(role_id)
        if role is None and role_id != "temporary_specialist":
            raise ConfigurationError(f"Unknown agent role: {role_id}")
        allowed = {ref["skill_id"]: ref["version"] for ref in role["skill_refs"]} if role else {}
        plan_path = self.project_root / "runtime_skills" / "qualification" / "packet.json"
        try:
            plan_bytes = plan_path.read_bytes()
            plan = json.loads(plan_bytes)
            cases = (
                plan.get("role_skill_cases", [])
                if isinstance(plan, dict)
                and plan.get("schema") == "ora.runtime_skill.qualification.v1" else []
            )
            if not isinstance(cases, list):
                cases = []
        except (OSError, ValueError, KeyError, TypeError):
            plan_bytes, cases = None, []
        snapshots: dict[str, dict[str, Any]] = {}
        for skill_id, variant in choices.items():
            skill = registry.skills.get(skill_id)
            if skill is None:
                raise ConfigurationError(
                    registry.unavailable.get(skill_id, f"Unknown skill: {skill_id}")
                )
            if skill_id not in allowed and role_id != "temporary_specialist":
                raise ConfigurationError(f"Role {role_id} does not declare skill {skill_id}")
            if skill_id in allowed and allowed[skill_id] != skill.version:
                raise ConfigurationError("Role skill version has drifted")
            if variant not in skill.instruction_paths:
                raise ConfigurationError(f"Unknown instruction variant: {variant}")
            instructions = registry.render_instructions(skill_id, variant)
            planned_case = next(
                (index for index, case in enumerate(cases)
                 if isinstance(case, dict) and case.get("role_id") == role_id
                 and case.get("skill_id") == skill_id),
                None,
            )
            snapshots[skill_id] = {
                "skill_id": skill_id,
                "version": skill.version,
                "manifest_sha256": content_hash(skill.manifest),
                "variant": variant,
                "instructions": instructions,
                "instructions_sha256": _text_sha256(instructions),
                "helper_sha256": skill.manifest["helper"]["sha256"],
                "trigger": skill.manifest["trigger"],
                "purpose": skill.manifest["purpose"],
                "input_contract": skill.manifest["input_contract"],
                "output_contract": skill.manifest["output_contract"],
                "required_capabilities": skill.manifest["required_capabilities"],
                "example_sha256": skill.manifest["example"]["sha256"],
                "value_test_plan_ref": (
                    {"path": "runtime_skills/qualification/packet.json",
                     "schema_version": "1", "case_index": planned_case,
                     "sha256": hashlib.sha256(plan_bytes).hexdigest(),
                     "status": "intended_not_evaluated"}
                    if planned_case is not None and plan_bytes is not None else None
                ),
                "value_test_result_ref": None,
            }
        return snapshots

    def save_configuration(
        self,
        operation_id: str,
        *,
        instance_id: str,
        mission_id: str,
        task_scope: str,
        provider: str,
        endpoint: str | None,
        model_id: str,
        settings: dict[str, Any],
        reasoning_settings: dict[str, Any],
        instructions: str,
        skill_choices: dict[str, str],
        grants: set[str],
        input_refs: list[dict[str, Any]],
        qualification_refs: dict[str, dict[str, Any]] | None = None,
        value_test_refs: dict[str, dict[str, Any]] | None = None,
        previous_configuration_id: str | None = None,
        change_reason: str | None = None,
    ) -> dict[str, Any]:
        if not all(
            (operation_id, instance_id, mission_id, task_scope, provider, model_id, instructions)
        ):
            raise ConfigurationError(
                "Configuration needs operation, actor, mission, model and instructions"
            )
        if endpoint is not None:
            _safe_endpoint(endpoint)
        if (previous_configuration_id is None) != (change_reason is None):
            raise ConfigurationError("A configuration change needs predecessor and reason")
        if change_reason is not None and not change_reason.strip():
            raise ConfigurationError("Configuration change reason must be nonempty")
        _check_settings(settings)
        _check_settings(reasoning_settings)
        _check_refs(input_refs)
        permitted = self.catalog.permitted(grants)
        if any(f"skill:{skill_id}" not in grants for skill_id in skill_choices):
            raise ConfigurationError("Selected skill lacks a host grant")
        catalog_snapshot = {
            tool_id: {**tool.descriptor(), "local_handler_available": tool.handler is not None}
            for tool_id, tool in sorted(self.catalog.tools.items())
        }
        catalog_snapshot.update({
            tool_id: {**item, "local_handler_available": False, "registration_status": "unavailable"}
            for tool_id, item in self.catalog.unavailable_optional.items()
        })
        skills = self._skills(self._actor_role(instance_id, mission_id), skill_choices)
        if value_test_refs is not None and not isinstance(value_test_refs, dict):
            raise ConfigurationError("Skill value-test references must be a mapping")
        value_test_refs = value_test_refs or {}
        if set(value_test_refs) - skills.keys():
            raise ConfigurationError("Value test references an unselected skill")
        _check_refs(list(value_test_refs.values()))
        for skill_id, pointer in value_test_refs.items():
            if pointer["kind"] != "skill_value_test":
                raise ConfigurationError("Skill value test needs a saved evaluation record")
            skills[skill_id]["value_test_result_ref"] = pointer
        qualifications = {
            capability_id: {"status": "provisional"} for capability_id in {*permitted, *skills}
        }
        qualifications.update(qualification_refs or {})
        for capability_id, value in qualifications.items():
            if capability_id not in permitted and capability_id not in skills:
                raise ConfigurationError("Qualification references an unselected capability")
            if not isinstance(value, dict) or value.get("status") not in {
                "provisional",
                "qualified",
            }:
                raise ConfigurationError("Qualification needs a provisional or qualified status")
            if value["status"] == "qualified":
                ref = value.get("evaluation_ref")
                if (
                    not isinstance(ref, dict)
                    or set(ref) != {"kind", "id", "version"}
                    or ref["kind"] != "capability_evaluation"
                    or type(ref["version"]) is not int
                    or ref["version"] < 1
                ):
                    raise ConfigurationError(
                        "Qualified default needs an exact evaluation reference"
                    )
        body = {
            "instance_id": instance_id,
            "mission_id": mission_id,
            "task_scope": task_scope,
            "provider": provider,
            "endpoint": endpoint,
            "model_id": model_id,
            "settings": settings,
            "reasoning_settings": reasoning_settings,
            "instructions": instructions,
            "instructions_sha256": _text_sha256(instructions),
            "skills": skills,
            "catalog_snapshot": catalog_snapshot,
            "catalog_sha256": content_hash(catalog_snapshot),
            "permitted_tools": permitted,
            "grants": sorted(grants),
            "input_refs": input_refs,
            "qualification": qualifications,
            "previous_configuration_id": previous_configuration_id,
            "change_reason": change_reason,
        }
        body["configuration_sha256"] = content_hash(body)
        request = body

        def save(tx: Any) -> dict[str, Any]:
            actor = tx.get("agent_instance", instance_id, version=1)
            if actor is None or actor["body"]["mission_id"] != mission_id:
                raise ConfigurationError("Named instance is unavailable in this mission")
            previous = None
            if previous_configuration_id is not None:
                previous = tx.get("agent_configuration", previous_configuration_id, version=1)
                if previous is None or previous["body"]["instance_id"] != instance_id:
                    raise ConfigurationError("Previous configuration belongs to another instance")
            for ref in input_refs:
                source = tx.get(ref["kind"], ref["id"], version=ref["version"])
                if source is None or content_hash(source["body"]) != ref["sha256"]:
                    raise ConfigurationError("An input artifact version or body hash changed")
            value_test_records = []
            for skill_id, pointer in value_test_refs.items():
                result = tx.get(pointer["kind"], pointer["id"], version=pointer["version"])
                expected = {
                    "skill_id": skill_id,
                    "skill_version": skills[skill_id]["version"],
                    "role_id": actor["body"]["role_id"],
                    "model_id": model_id,
                    "task_scope": task_scope,
                }
                if (
                    result is None
                    or content_hash(result["body"]) != pointer["sha256"]
                    or any(result["body"].get(key) != value for key, value in expected.items())
                ):
                    raise ConfigurationError("Skill value-test record does not match this selection")
                value_test_records.append(result)
            for capability_id, value in qualifications.items():
                if value["status"] == "qualified":
                    ref = value["evaluation_ref"]
                    evaluation = tx.get(ref["kind"], ref["id"], version=ref["version"])
                    if evaluation is None or any(
                        (
                            evaluation["body"].get("capability_id") != capability_id,
                            evaluation["body"].get("role_id") != actor["body"]["role_id"],
                            evaluation["body"].get("model_id") != model_id,
                            evaluation["body"].get("status") != "qualified",
                            evaluation["body"].get("task_scope") != task_scope,
                        )
                    ):
                        raise ConfigurationError(
                            "Qualification evidence does not match capability, role and model"
                        )
            config = tx.put(
                "agent_configuration", f"config-{uuid.uuid4().hex}", body, expected_version=0
            )
            tx.link(config, actor, "configured_for")
            for result in value_test_records:
                tx.link(config, result, "value_test_result")
            if previous is not None:
                tx.link(config, previous, "supersedes_configuration")
            tx.emit(
                mission_id,
                "agent_configuration_saved",
                {"instance_id": instance_id, "configuration_id": config["id"]},
            )
            return config

        return self.store.transact(operation_id, request, save, mission_id=mission_id)

    def _actor_role(self, instance_id: str, mission_id: str) -> str:
        actor = self.store.get("agent_instance", instance_id, version=1)
        if actor is None or actor["body"]["mission_id"] != mission_id:
            raise ConfigurationError("Named instance is unavailable in this mission")
        return actor["body"]["role_id"]

    def start_call(
        self,
        operation_id: str,
        *,
        call_id: str,
        configuration_id: str,
        mission_id: str,
        task_id: str,
        attempt_id: str,
        loaded_skill_ids: list[str],
        exposed_tool_ids: list[str],
        require_common_tools: bool = True,
    ) -> dict[str, Any]:
        if not all((operation_id, call_id, configuration_id, mission_id, task_id, attempt_id)):
            raise ConfigurationError("A call needs stable operation, call, task and attempt IDs")
        if len(loaded_skill_ids) != len(set(loaded_skill_ids)) or len(exposed_tool_ids) != len(
            set(exposed_tool_ids)
        ):
            raise ConfigurationError("Duplicate loaded or exposed capability")
        request = {
            "call_id": call_id,
            "configuration_id": configuration_id,
            "mission_id": mission_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "loaded_skill_ids": loaded_skill_ids,
            "exposed_tool_ids": exposed_tool_ids,
        }
        if not require_common_tools:
            request["require_common_tools"] = False

        def start(tx: Any) -> dict[str, Any]:
            config = tx.get("agent_configuration", configuration_id, version=1)
            if config is None or config["body"]["mission_id"] != mission_id:
                raise ConfigurationError("Saved configuration is unavailable")
            saved = config["body"]
            if (
                set(loaded_skill_ids) - saved["skills"].keys()
                or set(exposed_tool_ids) - saved["permitted_tools"].keys()
            ):
                raise ConfigurationError("Call cannot load or expose an unselected capability")
            if not COMMON_TOOL_IDS.issubset(saved["permitted_tools"]):
                raise ConfigurationError("Common capabilities were removed")
            if require_common_tools and not COMMON_TOOL_IDS.issubset(exposed_tool_ids):
                raise ConfigurationError("Every call must expose the common collaboration tools")
            body = {
                **request,
                "instance_id": saved["instance_id"],
                "configuration_sha256": saved["configuration_sha256"],
                "provider": saved["provider"],
                "endpoint": saved["endpoint"],
                "model_id": saved["model_id"],
                "task_scope": saved["task_scope"],
                "settings": saved["settings"],
                "reasoning_settings": saved["reasoning_settings"],
                "instructions_sha256": saved["instructions_sha256"],
                "skills": {name: saved["skills"][name] for name in loaded_skill_ids},
                "exposed_skill_ids": sorted(loaded_skill_ids),
                "permitted_tools": saved["permitted_tools"],
                "grants": saved["grants"],
                "exposed_tools": sorted(exposed_tool_ids),
                "input_refs": saved["input_refs"],
            }
            call = tx.put("agent_call", call_id, body, expected_version=0)
            tx.link(call, config, "uses_configuration")
            tx.emit(mission_id, "agent_call_prepared", {"call_id": call_id, "manifest_version": 1})
            return call

        return self.store.transact(operation_id, request, start, mission_id=mission_id)

    def expose_tool(
        self, operation_id: str, *, call_id: str, mission_id: str, tool_id: str
    ) -> dict[str, Any]:
        request = {"call_id": call_id, "mission_id": mission_id, "tool_id": tool_id}

        def expose(tx: Any) -> dict[str, Any]:
            prior = tx.get("agent_call", call_id)
            if prior is None or prior["body"]["mission_id"] != mission_id:
                raise ConfigurationError("Call is unavailable")
            body = prior["body"].copy()
            if tool_id not in body["permitted_tools"]:
                raise ConfigurationError("Tool is not host-granted for this call")
            current = self.catalog.tools.get(tool_id)
            if current is None or current.descriptor() != body["permitted_tools"][tool_id]:
                raise ConfigurationError("Pinned tool version is unavailable")
            if tool_id in body["exposed_tools"]:
                return prior
            body["exposed_tools"] = sorted([*body["exposed_tools"], tool_id])
            newer = tx.put("agent_call", call_id, body, expected_version=prior["version"])
            tx.link(newer, prior, "expands_tool_view")
            tx.emit(
                mission_id,
                "agent_tool_exposed",
                {"call_id": call_id, "tool_id": tool_id, "manifest_version": newer["version"]},
            )
            return newer

        return self.store.transact(operation_id, request, expose, mission_id=mission_id)

    def discover(self, call_id: str) -> dict[str, Any]:
        """Describe granted tools and missing optional routes without exposing them."""
        call = self.store.get("agent_call", call_id)
        if call is None:
            raise ConfigurationError("Call is unavailable")
        return capability_view(call, self.catalog)

    def invoke_skill(
        self,
        operation_id: str,
        *,
        mission_id: str,
        call_id: str,
        manifest_version: int,
        skill_id: str,
        evidence_ref: dict[str, Any],
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute one project-owned pure helper against a pinned evidence packet."""
        _check_refs([evidence_ref])
        if evidence_ref["kind"] != "evidence_packet":
            raise ConfigurationError("Skill input must name a saved evidence packet")
        request = {
            "mission_id": mission_id,
            "call_id": call_id,
            "manifest_version": manifest_version,
            "skill_id": skill_id,
            "evidence_ref": evidence_ref,
            "arguments": arguments,
        }

        def invoke(tx: Any) -> dict[str, Any]:
            call = tx.get("agent_call", call_id, version=manifest_version)
            if call is None or call["body"]["mission_id"] != mission_id:
                raise ConfigurationError("Pinned call manifest is unavailable")
            status, reason, result = "completed", None, None
            try:
                snapshot = call["body"]["skills"].get(skill_id)
                if snapshot is None or skill_id not in call["body"]["exposed_skill_ids"]:
                    raise ConfigurationError("Skill was not loaded and exposed for this call")
                if evidence_ref not in call["body"]["input_refs"]:
                    raise ConfigurationError("Evidence packet is not a pinned input")
                source = tx.get(
                    "evidence_packet", evidence_ref["id"], version=evidence_ref["version"]
                )
                if source is None or content_hash(source["body"]) != evidence_ref["sha256"]:
                    raise ConfigurationError("Pinned evidence packet is unavailable")
                registry = load_registry(self.project_root, self.skill_capabilities)
                skill = registry.skills.get(skill_id)
                if (
                    skill is None
                    or skill.version != snapshot["version"]
                    or content_hash(skill.manifest) != snapshot["manifest_sha256"]
                    or skill.manifest["helper"].get("side_effects") != "none"
                ):
                    raise ConfigurationError("Pinned skill helper is unavailable or changed")
                evidence = EvidencePacket.model_validate(source["body"])
                result = skill.helper(evidence, **arguments)
                if not isinstance(result, dict):
                    raise ConfigurationError("Skill helper did not return a record")
                content_hash(result)
            except (ConfigurationError, ValueError, TypeError, KeyError):
                status, reason = "rejected", "Skill input or pinned helper was rejected"
                result = None
            except Exception:
                status, reason = "failed", "Local skill helper failed"
                result = None
            body = {
                "mission_id": mission_id,
                "call_id": call_id,
                "manifest_version": manifest_version,
                "skill_id": skill_id,
                "skill_version": call["body"]["skills"].get(skill_id, {}).get("version"),
                "evidence_ref": evidence_ref,
                "arguments_sha256": content_hash(arguments),
                "status": status,
                "result": result,
                "result_sha256": content_hash(result) if result is not None else None,
                "reason": reason,
            }
            receipt = tx.put(
                "skill_invocation", f"skill-use-{uuid.uuid4().hex}", body, expected_version=0
            )
            tx.link(receipt, call, "invoked_from")
            tx.emit(
                mission_id,
                "skill_result",
                {
                    "call_id": call_id,
                    "skill_id": skill_id,
                    "status": status,
                    "call_ref": {key: call[key] for key in ("kind", "id", "version")},
                    "result_ref": {key: receipt[key] for key in ("kind", "id", "version")},
                },
            )
            return receipt

        return self.store.transact(operation_id, request, invoke, mission_id=mission_id)

    def resume(
        self,
        call_id: str,
        *,
        available_models: set[tuple[str, str]],
        catalog: CapabilityCatalog | None = None,
    ) -> dict[str, Any]:
        """Return the saved call and explicit environment drift, without fallback."""
        call = self.store.get("agent_call", call_id)
        if call is None:
            raise ConfigurationError("Call manifest is unavailable")
        body = call["body"]
        config = self.store.get("agent_configuration", body["configuration_id"], version=1)
        if (
            config is None
            or content_hash(
                {k: v for k, v in config["body"].items() if k != "configuration_sha256"}
            )
            != body["configuration_sha256"]
        ):
            raise ConfigurationError("Saved configuration integrity failed")
        drift: list[str] = []
        if (body["provider"], body["model_id"]) not in available_models:
            drift.append("selected_model_unavailable")
        current = catalog or self.catalog
        for tool_id, descriptor in body["permitted_tools"].items():
            tool = current.tools.get(tool_id)
            if tool is None or tool.descriptor() != descriptor:
                drift.append(f"tool_changed:{tool_id}")
        try:
            registry = load_registry(self.project_root, self.skill_capabilities)
            for skill_id, snapshot in body["skills"].items():
                skill = registry.skills.get(skill_id)
                if (
                    skill is None
                    or skill.version != snapshot["version"]
                    or content_hash(skill.manifest) != snapshot["manifest_sha256"]
                    or _text_sha256(registry.render_instructions(skill_id, snapshot["variant"]))
                    != snapshot["instructions_sha256"]
                ):
                    drift.append(f"skill_changed:{skill_id}")
                value_test = snapshot.get("value_test_result_ref")
                if value_test is not None:
                    saved_test = self.store.get(
                        value_test["kind"], value_test["id"], version=value_test["version"],
                    )
                    if saved_test is None or content_hash(saved_test["body"]) != value_test["sha256"]:
                        drift.append(f"value_test_changed:{skill_id}")
        except (ValueError, OSError):
            drift.append("skill_registry_unavailable")
        return {
            "call": call,
            "configuration": config,
            "status": "ready" if not drift else "drift",
            "drift": drift,
        }
