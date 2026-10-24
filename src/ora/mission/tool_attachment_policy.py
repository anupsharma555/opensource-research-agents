"""Carry a saved same-task tool-deferral choice into the next model turn.

The host still computes current grants and attaches explicit model selections.
This policy only preserves a prior pressure-driven reduction of default core
schemas; it never grants, invokes, or removes a tool from the catalog.
"""

from __future__ import annotations

from ora.coordination.board import ref

from .tool_catalog import DEFERABLE_CORE_TOOLS


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
