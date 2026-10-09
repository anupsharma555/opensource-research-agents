"""Host-granted capability catalog and safe, local fixture invocation.

Skill prose and model-selected tool names never grant permission. The production
host supplies handlers; this module only executes explicitly registered, pure
local handlers and records what actually ran.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .json_shapes import (
    JsonShapeError,
    finite_json,
    validate_value,
)
from .json_shapes import (
    validate_schema as validate_json_schema,
)


class CapabilityError(ValueError):
    """A capability is unavailable, ungranted, or called with invalid arguments."""


def content_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _object_schema(properties: dict[str, dict[str, Any]], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


COMMON_SCHEMAS: dict[str, dict[str, Any]] = {
    "board_read": _object_schema({"topic": {"type": "string"}}, []),
    "peer_request": _object_schema(
        {"target_instance_id": {"type": "string", "minLength": 1},
         "question": {"type": "string", "minLength": 1}},
        ["target_instance_id", "question"],
    ),
    "peer_invite": _object_schema(
        {"target_instance_id": {"type": "string", "minLength": 1},
         "work_id": {"type": "string", "minLength": 1}},
        ["target_instance_id", "work_id"],
    ),
    "subscribe": _object_schema({"subject_id": {"type": "string", "minLength": 1}}, ["subject_id"]),
    "claim_work": _object_schema({"work_id": {"type": "string", "minLength": 1}}, ["work_id"]),
    "evidence_lookup": _object_schema({"source_id": {"type": "string", "minLength": 1}}, ["source_id"]),
    "discover_capabilities": _object_schema({}, []),
    "request_capability": _object_schema({"tool_id": {"type": "string", "minLength": 1}}, ["tool_id"]),
}
COMMON_TOOL_IDS = frozenset(COMMON_SCHEMAS)
CONTEXT_TOOL_IDS = frozenset({"discover_capabilities", "request_capability"})


def _context_implementation_ref() -> str:
    """Pin the installed module bytes that implement bound catalog operations."""
    return "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


@dataclass(frozen=True)
class Capability:
    tool_id: str
    version: str
    schema: dict[str, Any]
    permission: str
    handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    side_effects: str = "none"
    optional: bool = False
    execution_model: str | None = None
    implementation_ref: str | None = None

    def descriptor(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "version": self.version,
            "schema": self.schema,
            "schema_sha256": content_hash(self.schema),
            "permission": self.permission,
            "side_effects": self.side_effects,
            "optional": self.optional,
            "execution_model": self.execution_model,
            "implementation_ref": self.implementation_ref,
        }


class CapabilityCatalog:
    def __init__(self, capabilities: list[Capability]) -> None:
        self.tools: dict[str, Capability] = {}
        self.unavailable_optional: dict[str, dict[str, Any]] = {}
        for item in capabilities:
            if (
                not item.tool_id
                or not item.version
                or not item.permission
                or item.tool_id in self.tools
            ):
                raise CapabilityError("Each capability needs a unique ID, version and permission")
            try:
                validate_schema(item.schema)
            except (CapabilityError, TypeError, ValueError):
                if not item.optional:
                    raise
                self.unavailable_optional[item.tool_id] = {
                    "tool_id": item.tool_id, "version": item.version,
                    "permission": item.permission,
                    "reason": "Optional tool schema is unsupported or invalid",
                }
                continue
            valid_ref = isinstance(item.implementation_ref, str) and bool(
                re.fullmatch(r"sha256:[0-9a-f]{64}|build:[A-Za-z0-9._-]+", item.implementation_ref)
            )
            if item.handler is not None and not (valid_ref):
                raise CapabilityError("Executable tool needs a host-supplied implementation ref")
            if item.tool_id in CONTEXT_TOOL_IDS and (
                item.handler is not None
                or item.side_effects != "none"
                or item.implementation_ref != _context_implementation_ref()
            ):
                raise CapabilityError("Bound capability tools use the internal call context")
            self.tools[item.tool_id] = item
        missing = COMMON_TOOL_IDS - self.tools.keys()
        if missing:
            raise CapabilityError(f"Common collaboration tools are missing: {sorted(missing)}")

    @classmethod
    def with_common(
        cls,
        specialists: list[Capability] | None = None,
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
        implementation_refs: dict[str, str] | None = None,
    ) -> "CapabilityCatalog":
        handlers = handlers or {}
        implementation_refs = implementation_refs or {}
        if any(tool_id in CONTEXT_TOOL_IDS for tool_id in handlers):
            raise CapabilityError("Bound capability handlers cannot be overridden")
        common = [
            Capability(
                tool_id,
                "1",
                schema,
                f"common:{tool_id}",
                handlers.get(tool_id),
                implementation_ref=(
                    _context_implementation_ref()
                    if tool_id in CONTEXT_TOOL_IDS
                    else implementation_refs.get(tool_id)
                ),
            )
            for tool_id, schema in COMMON_SCHEMAS.items()
        ]
        classifier = Capability(
            "jevk5-0.2",
            "0.2",
            _object_schema(
                {"article_id": {"type": "string"}, "article_sha256": {"type": "string"}},
                ["article_id", "article_sha256"],
            ),
            "specialist:article_classification",
            None,
            optional=True,
            execution_model="jevk5-0.2",
        )
        specialists = specialists or []
        declared_classifier = any(item.tool_id == classifier.tool_id for item in specialists)
        return cls(common + ([] if declared_classifier else [classifier]) + specialists)

    def permitted(self, grants: set[str]) -> dict[str, dict[str, Any]]:
        common_grants = {f"common:{tool_id}" for tool_id in COMMON_TOOL_IDS}
        if not common_grants.issubset(grants):
            raise CapabilityError("Every actor must inherit the common collaboration grants")
        return {
            tool_id: tool.descriptor()
            for tool_id, tool in self.tools.items()
            if tool.permission in grants and (not tool.optional or tool.handler is not None)
        }


def capability_view(manifest: dict[str, Any], catalog: CapabilityCatalog) -> dict[str, Any]:
    """Show only the saved grant set and its currently available tool views."""
    body = manifest["body"]
    permitted = body["permitted_tools"]
    grants = set(body["grants"])
    return {
        "call_id": manifest["id"],
        "manifest_version": manifest["version"],
        "available": [
            {**descriptor, "exposed": tool_id in body["exposed_tools"]}
            for tool_id, descriptor in sorted(permitted.items())
        ],
        "unavailable_optional": [
            tool.descriptor()
            for tool in catalog.tools.values()
            if tool.optional and tool.handler is None and tool.permission in grants
        ] + [dict(item) for item in catalog.unavailable_optional.values() if item["permission"] in grants],
    }


def validate_schema(schema: dict[str, Any]) -> None:
    """Reject unsupported shape keywords before a capability is registered."""
    try:
        validate_json_schema(schema)
        if schema.get("type") != "object":
            raise JsonShapeError("Tool arguments need an object at the root")
    except JsonShapeError as exc:
        raise CapabilityError(str(exc)) from exc


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    try:
        validate_value(schema, arguments)
    except JsonShapeError as exc:
        raise CapabilityError(str(exc)) from exc


def run_local_tool(
    store: Any,
    catalog: CapabilityCatalog,
    operation_id: str,
    *,
    mission_id: str,
    call_id: str,
    manifest_version: int,
    tool_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Execute one pure local handler exactly once per durable operation ID.

    A missing or denied tool creates a scoped rejected receipt. External effects
    and remote calls belong to the host's separate operation protocol.
    """
    if type(manifest_version) is not int or manifest_version < 1:
        raise CapabilityError("Tool invocation needs an exact call-manifest version")
    try:
        finite_json(arguments)
    except JsonShapeError as exc:
        raise CapabilityError(str(exc)) from exc
    frozen_arguments = json.loads(json.dumps(arguments, ensure_ascii=False, allow_nan=False))
    request = {
        "mission_id": mission_id,
        "call_id": call_id,
        "manifest_version": manifest_version,
        "tool_id": tool_id,
        "arguments": frozen_arguments,
    }

    def execute(tx: Any) -> dict[str, Any]:
        manifest = tx.get("agent_call", call_id, version=manifest_version)
        if manifest is None or manifest["body"]["mission_id"] != mission_id:
            raise CapabilityError("Call manifest is unavailable")
        permitted = manifest["body"]["permitted_tools"]
        exposed = manifest["body"]["exposed_tools"]
        tool = catalog.tools.get(tool_id)
        status = "completed"
        reason = None
        result: dict[str, Any] | None = None
        try:
            if tool_id not in permitted or tool_id not in exposed:
                raise CapabilityError("Tool is not granted and exposed for this call")
            saved = permitted[tool_id]
            if tool is None or tool.descriptor() != saved:
                raise CapabilityError("Pinned tool version or schema is unavailable")
            validate_arguments(tool.schema, frozen_arguments)
            if tool_id == "discover_capabilities":
                result = capability_view(manifest, catalog)
            elif tool_id == "request_capability":
                requested_id = frozen_arguments["tool_id"]
                requested = catalog.tools.get(requested_id)
                pinned = permitted.get(requested_id)
                if pinned is None or requested is None or requested.descriptor() != pinned:
                    raise CapabilityError("Requested capability is not granted and available")
                current = tx.get("agent_call", call_id)
                if current["version"] != manifest_version:
                    raise CapabilityError("Call view changed; discover the current version")
                if requested_id in exposed:
                    result = {
                        "tool_id": requested_id,
                        "status": "already_exposed",
                        "manifest_version": current["version"],
                    }
                else:
                    updated_body = {
                        **current["body"],
                        "exposed_tools": sorted([*exposed, requested_id]),
                    }
                    updated = tx.put(
                        "agent_call", call_id, updated_body, expected_version=current["version"]
                    )
                    tx.link(updated, current, "expands_tool_view")
                    tx.emit(
                        mission_id,
                        "agent_tool_exposed",
                        {
                            "call_id": call_id,
                            "tool_id": requested_id,
                            "manifest_version": updated["version"],
                        },
                    )
                    result = {
                        "tool_id": requested_id,
                        "status": "exposed",
                        "manifest_version": updated["version"],
                    }
            else:
                if tool.side_effects != "none" or tool.handler is None:
                    raise CapabilityError(
                        "Tool has no safe local handler; use the host operation route"
                    )
                result = tool.handler(json.loads(json.dumps(frozen_arguments)))
            if not isinstance(result, dict):
                raise CapabilityError("Tool handler must return an object")
            content_hash(result)
        except CapabilityError as exc:
            status, reason = "rejected", str(exc)
            result = None
        except Exception:
            status, reason = "failed", "Local handler failed; no result was accepted"
            result = None
        body = {
            "mission_id": mission_id,
            "call_id": call_id,
            "manifest_version": manifest_version,
            "tool_id": tool_id,
            "arguments_sha256": content_hash(frozen_arguments),
            "arguments": frozen_arguments if status == "completed" else None,
            "status": status,
            "result": result,
            "result_sha256": content_hash(result) if result is not None else None,
            "reason": reason,
        }
        receipt = tx.put(
            "capability_invocation", f"invocation-{uuid.uuid4().hex}", body, expected_version=0
        )
        tx.link(receipt, manifest, "invoked_from")
        tx.emit(
            mission_id,
            "capability_result",
            {
                "call_id": call_id,
                "tool_id": tool_id,
                "status": status,
                "call_ref": {key: manifest[key] for key in ("kind", "id", "version")},
                "result_ref": {key: receipt[key] for key in ("kind", "id", "version")},
            },
        )
        return receipt

    return store.transact(operation_id, request, execute, mission_id=mission_id)
