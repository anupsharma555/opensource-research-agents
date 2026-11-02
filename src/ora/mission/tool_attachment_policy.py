"""Preserve tool deferral and project exact callability into model context.

The host still computes current grants and attaches explicit model selections.
These policies never grant, invoke, or remove a tool from the catalog. The
model-facing projection omits a repeated list only when the saved configuration
and current call reconstruct its exact members.
"""

from __future__ import annotations

import hashlib
import json

from ora.agents.capabilities import content_hash
from ora.coordination.board import ref

from .skill_activation import ROLE_CATALOG_ID_SCHEMA, ROLE_CATALOG_PURPOSE_SCHEMA
from .tool_catalog import DEFERABLE_CORE_TOOLS

_KNOWN_UNAVAILABLE_BEFORE_SELECTION = frozenset({"release_capability"})


def compact_model_tool_availability(
    availability: dict, *, call: dict, configuration: dict | None,
    unhandled_tools: frozenset[str], current_guidance: str,
) -> dict:
    """Omit repeated deferred names only when exact callability is reconstructible.

    The saved configuration catalog is the authority for names and purposes;
    the call manifest owns attached schemas. This is a model-facing projection,
    never a change to grants or the host's full availability result. Older or
    malformed configurations keep their original explicit list.
    """
    if (
        not isinstance(availability, dict)
        or availability.get("authority") != "trusted_host_attached_tool_availability"
        or availability.get("native_browser_now") is not False
        or not isinstance(call, dict)
        or not isinstance(configuration, dict)
        or configuration.get("kind") != "agent_configuration"
        or configuration.get("version") != 1
        or not isinstance(call.get("body"), dict)
        or not isinstance(configuration.get("body"), dict)
        or not isinstance(unhandled_tools, frozenset)
    ):
        return availability
    call_body = call["body"]
    config_body = configuration["body"]
    instructions = config_body.get("instructions")
    permitted = config_body.get("permitted_tools")
    expected_hash = config_body.get("configuration_sha256")
    if (
        call_body.get("configuration_id") != configuration.get("id")
        or call_body.get("configuration_sha256") != expected_hash
        or call_body.get("permitted_tools") != permitted
        or call_body.get("mission_id") != config_body.get("mission_id")
        or call_body.get("instance_id") != config_body.get("instance_id")
        or not isinstance(instructions, str)
        or instructions.count(current_guidance) != 1
        or not isinstance(permitted, dict)
        or content_hash({key: value for key, value in config_body.items()
                         if key != "configuration_sha256"}) != expected_hash
        or config_body.get("instructions_sha256")
        != hashlib.sha256(instructions.encode()).hexdigest()
        or call_body.get("instructions_sha256")
        != hashlib.sha256(instructions.encode()).hexdigest()
    ):
        return availability
    marker = "Role capability catalog: "
    if instructions.count(marker) != 1:
        return availability
    try:
        catalog, _ = json.JSONDecoder().raw_decode(instructions.split(marker, 1)[1])
    except (TypeError, ValueError):
        return availability
    if not isinstance(catalog, dict) or not isinstance(catalog.get("skills"), dict):
        return availability
    if catalog.get("schema") == ROLE_CATALOG_ID_SCHEMA:
        ids = catalog.get("tool_ids")
        if (set(catalog) != {"schema", "skills", "tool_ids"}
                or not isinstance(ids, list)
                or not all(isinstance(name, str) and name for name in ids)
                or ids != sorted(set(ids))):
            return availability
        catalog_names = set(ids)
    elif catalog.get("schema") == ROLE_CATALOG_PURPOSE_SCHEMA:
        purposes = catalog.get("tools")
        if (set(catalog) != {"schema", "skills", "tools"}
                or not isinstance(purposes, dict)
                or not all(isinstance(name, str) and name
                           and isinstance(purpose, str) and purpose.strip()
                           for name, purpose in purposes.items())):
            return availability
        catalog_names = set(purposes)
    elif "schema" not in catalog:
        purposes = catalog.get("tools")
        if (not isinstance(purposes, dict)
                or not all(isinstance(name, str) and name
                           and isinstance(purpose, str) and purpose.strip()
                           for name, purpose in purposes.items())):
            return availability
        catalog_names = set(purposes)
    else:
        return availability
    if catalog_names != set(permitted) - unhandled_tools:
        return availability
    exposed = call_body.get("exposed_tools")
    now = availability.get("call_now_local_functions")
    deferred = availability.get("request_first_local_functions")
    not_ready = availability.get("not_ready_local_functions")
    native = availability.get("request_first_native_tools")
    if (
        not isinstance(exposed, list)
        or not isinstance(now, list)
        or not isinstance(deferred, list)
        or not isinstance(not_ready, dict)
        or not isinstance(native, list)
        or any(not isinstance(name, str) or not name for names in (exposed, now, deferred, native)
               for name in names)
        or any(len(names) != len(set(names)) for names in (exposed, now, deferred, native))
        or set(now) != set(exposed)
        or not set(now) <= catalog_names
        or not set(not_ready) <= catalog_names
        or not set(native) <= {"browser_search"}
    ):
        return availability
    candidate = catalog_names - set(now) - set(not_ready) - set(native) - {"browser_search"}
    actual = set(deferred)
    exceptions = candidate - actual
    if not actual <= candidate or not exceptions <= _KNOWN_UNAVAILABLE_BEFORE_SELECTION:
        return availability
    if ("release_capability" in exceptions
            and "release_capability" in now):
        return availability
    compact = {key: value for key, value in availability.items()
               if key != "request_first_local_functions"}
    if exceptions:
        compact["unavailable_now"] = sorted(exceptions)
    compact["request_first_rule"] = (
        "Deferred local catalog tools require request_capability except not_ready/"
        "unavailable_now; request_first_native_tools use a native provider turn. "
        "discover_capabilities can refresh access."
    )
    compact["next_action"] = (
        "Call attached tools directly. Request a known eligible deferred tool by exact ID; "
        "native browser tools are separate; discover_capabilities is optional when access is unclear."
    )
    return compact


def carry_forward_core_deferral(
    control: dict, prior_call: dict | None, prior_budget: dict | None,
    *, current_configuration_sha256: str,
) -> bool:
    """Keep the immediately prior same-task core deferral when its receipt agrees.

    The caller must resolve ``control.call_ref`` to the exact prior call and
    read its saved call budget. An absent or older-format budget has no effect.
    Explicit selected tools are handled by the host after this decision.
    """
    if (not isinstance(control, dict) or not isinstance(prior_call, dict)
            or not isinstance(prior_budget, dict)
            or prior_call.get("kind") != "agent_call"
            or prior_budget.get("kind") != "mission_call_budget"
            or not isinstance(prior_call.get("id"), str)
            or type(prior_call.get("version")) is not int
            or prior_call["version"] < 1
            or prior_budget.get("id") != prior_call["id"]
            or not isinstance(current_configuration_sha256, str)
            or not current_configuration_sha256):
        return False
    body = control.get("body")
    old = prior_call.get("body")
    budget = prior_budget.get("body")
    if not all(isinstance(value, dict) for value in (body, old, budget)):
        return False
    if (any(not isinstance(body.get(key), str) or not body[key]
            for key in ("mission_id", "run_id", "task_id", "actor_id"))
            or body.get("control_type") == "invitation_review"
            or body.get("call_ref") != ref(prior_call)
            or budget.get("call_ref") != ref(prior_call)
            or any(body.get(current) != old.get(previous) for current, previous in (
                ("mission_id", "mission_id"),
                ("task_id", "task_id"), ("actor_id", "instance_id"),
            ))
            or budget.get("mission_id") != body.get("mission_id")
            or budget.get("run_id") != body.get("run_id")
            or old.get("configuration_sha256") != current_configuration_sha256
            or budget.get("exposed_tool_ids") != old.get("exposed_tools")):
        return False
    deferred = budget.get("deferred_core_tool_ids")
    exposed = old.get("exposed_tools")
    return bool(
        isinstance(deferred, list) and deferred
        and all(isinstance(tool, str) and tool in DEFERABLE_CORE_TOOLS
                for tool in deferred)
        and len(set(deferred)) == len(deferred)
        and isinstance(exposed, list)
        and all(isinstance(tool, str) for tool in exposed)
        and len(set(exposed)) == len(exposed)
        and not set(deferred) & set(exposed)
    )
