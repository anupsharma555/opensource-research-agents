"""A saved pressure deferral may carry only across its exact same-task turn."""

from __future__ import annotations

from copy import deepcopy

import pytest

from ora.coordination.board import ref
from ora.mission.tool_attachment_policy import carry_forward_core_deferral


def _saved_turn() -> tuple[dict, dict, dict]:
    prior_call = {
        "kind": "agent_call", "id": "prior-call", "version": 1,
        "body": {
            "mission_id": "mission-a", "instance_id": "peer-a", "task_id": "task-a",
            "configuration_sha256": "a" * 64,
            "exposed_tools": [
                "discover_capabilities", "request_capability", "read_context_record",
            ],
        },
    }
    control = {"body": {
        "mission_id": "mission-a", "run_id": "run-a", "actor_id": "peer-a",
        "task_id": "task-a", "call_ref": ref(prior_call),
        "requested_capabilities": ["read_context_record", "review_peer_answer"],
    }}
    budget = {
        "kind": "mission_call_budget", "id": "prior-call", "version": 1,
        "body": {
            "mission_id": "mission-a", "run_id": "run-a",
            "call_ref": ref(prior_call),
            "exposed_tool_ids": prior_call["body"]["exposed_tools"],
            "deferred_core_tool_ids": [
                "board_read", "peer_request", "review_peer_answer",
                "search_scholarly",
            ],
        },
    }
    return control, prior_call, budget


def test_exact_prior_budget_preserves_default_core_deferral():
    control, prior_call, budget = _saved_turn()
    assert carry_forward_core_deferral(
        control, prior_call, budget, current_configuration_sha256="a" * 64,
    )
    # The helper is advisory. The host still attaches explicitly selected
    # review_peer_answer and read_context_record through its grant intersection.
    assert "review_peer_answer" in control["body"]["requested_capabilities"]
    assert "read_context_record" in prior_call["body"]["exposed_tools"]


@pytest.mark.parametrize("target,value", [
    ("control.mission_id", "other-mission"),
    ("control.run_id", "other-run"),
    ("control.actor_id", "other-peer"),
    ("control.task_id", "other-task"),
    ("control.call_ref", {"kind": "agent_call", "id": "other-call", "version": 1}),
    ("budget.call_ref", {"kind": "agent_call", "id": "other-call", "version": 1}),
    ("budget.run_id", "other-run"),
    ("budget.exposed_tool_ids", ["discover_capabilities"]),
    ("budget.deferred_core_tool_ids", ["unknown_tool"]),
    ("budget.deferred_core_tool_ids", ["read_context_record"]),
    ("budget.deferred_core_tool_ids", []),
    ("call.configuration_sha256", "b" * 64),
])
def test_stale_cross_scope_or_unproved_deferral_is_not_carried(target, value):
    control, prior_call, budget = _saved_turn()
    changed = {"control": control["body"],
               "call": prior_call["body"], "budget": budget["body"]}
    area, field = target.split(".")
    changed[area][field] = value
    assert not carry_forward_core_deferral(
        control, prior_call, budget, current_configuration_sha256="a" * 64,
    )


def test_missing_legacy_budget_and_changed_configuration_do_not_carry():
    control, prior_call, budget = _saved_turn()
    assert not carry_forward_core_deferral(
        control, prior_call, None, current_configuration_sha256="a" * 64,
    )
    assert not carry_forward_core_deferral(
        control, prior_call, budget, current_configuration_sha256="b" * 64,
    )
    older = deepcopy(budget)
    older["body"].pop("deferred_core_tool_ids")
    assert not carry_forward_core_deferral(
        control, prior_call, older, current_configuration_sha256="a" * 64,
    )


def test_invitation_review_keeps_its_distinct_tool_policy():
    control, prior_call, budget = _saved_turn()
    control["body"]["control_type"] = "invitation_review"
    assert not carry_forward_core_deferral(
        control, prior_call, budget, current_configuration_sha256="a" * 64,
    )


def test_malformed_matching_exposure_and_wrong_budget_id_fail_closed():
    control, prior_call, budget = _saved_turn()
    malformed = ["discover_capabilities", {"name": "request_capability"}]
    prior_call["body"]["exposed_tools"] = malformed
    budget["body"]["exposed_tool_ids"] = malformed
    assert not carry_forward_core_deferral(
        control, prior_call, budget, current_configuration_sha256="a" * 64,
    )
    control, prior_call, budget = _saved_turn()
    budget["id"] = "different-call"
    assert not carry_forward_core_deferral(
        control, prior_call, budget, current_configuration_sha256="a" * 64,
    )
