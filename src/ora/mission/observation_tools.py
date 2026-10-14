"""Small model-facing choices for latency estimates and explicit waits.

Callbacks save the peer's choice and return a proposed control change. They do
not mutate a leased mission control or choose a scientific continuation.
"""

from __future__ import annotations

import hashlib

from ora.coordination.board import ref, require_ref
from ora.state import StateConflict, StateError, StateStore, Transaction
from ora.state.store import canonical, identifier

from .latency import save_expectation
from .waits import inspect_waits


class ObservationToolError(StateError):
    """A model choice needs current exact inputs or a local replan."""


def _schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_DEPENDENCY = _schema(
    {
        "kind": {"type": "string", "enum": ["peer_request", "proposal_dependency"]},
        "request_id": {"type": "string"},
        "request_version": {"type": "integer"},
        "proposal_id": {"type": "string"},
        "proposal_version": {"type": "integer"},
        "target_control_id": {"type": "string"},
        "target_control_version": {"type": "integer"},
    },
    ["kind", "target_control_id", "target_control_version"],
)

OBSERVATION_TOOL_SCHEMAS = {
    "estimate_latency": _schema(
        {
            "category": {"type": "string", "enum": ["minimal", "medium", "long", "very_long"]},
            "basis": {"type": "string"},
            "expected_version": {"type": "integer"},
            "review_after_seconds": {"type": "number"},
        },
        ["category", "basis", "expected_version"],
    ),
    "wait_for_dependencies": _schema(
        {
            "dependencies": {"type": "array", "items": _DEPENDENCY},
            "reason": {"type": "string"},
        },
        ["dependencies", "reason"],
    ),
}

OBSERVATION_TOOL_DESCRIPTIONS = {
    "estimate_latency": "Record or revise your rough task duration category with a plain-language basis. You may choose positive review_after_seconds for one future progress review; this is not a deadline or work limit. Pass the current saved expectation version, or zero for the first estimate.",
    "wait_for_dependencies": "Wait when progress depends on an unresolved peer answer or "
    "prerequisite proposal. Supply dependencies with their exact request/proposal and "
    "target_control IDs/versions, plus reason. Saves a wait; already answered or completed "
    "inputs return for review instead. Peers choose how to continue after a cycle notice.",
}


def _saved_control(store: StateStore, control_ref: dict) -> dict:
    if (
        type(control_ref) is not dict
        or set(control_ref) != {"kind", "id", "version"}
        or control_ref["kind"] != "mission_task_control"
        or type(control_ref["version"]) is not int
        or control_ref["version"] < 1
    ):
        raise ObservationToolError("An exact task control reference is required")
    control = store.get(**control_ref)
    if control is None:
        raise ObservationToolError("Saved task control is unavailable")
    return control


def _current_control(store: StateStore, control_ref: dict) -> dict:
    control = _saved_control(store, control_ref)
    head = store.get("mission_task_control", control_ref["id"])
    if head is None or ref(head) != control_ref:
        raise StateConflict("Task control changed; reread before recording a choice")
    return control


def _response(tx: Transaction, mission_id: str, body: dict, response_ref: dict) -> dict:
    response = require_ref(tx, mission_id, response_ref)
    if (
        response["kind"] != "model_response_receipt"
        or response["body"].get("status") != "tool_requested"
        or response["body"].get("task_id") != body["task_id"]
        or body.get("response_ref") != response_ref
    ):
        raise ObservationToolError("Choice needs this control's observed model tool response")
    call = require_ref(tx, mission_id, response["body"].get("call_ref"))
    if (
        call["kind"] != "agent_call"
        or call["body"].get("instance_id") != body["actor_id"]
        or call["body"].get("task_id") != body["task_id"]
    ):
        raise ObservationToolError("Model response belongs to another peer or task")
    return response


def record_latency_choice(
    store: StateStore,
    operation_id: str,
    *,
    control_ref: dict,
    response_ref: dict,
    category: str,
    basis: str,
    expected_version: int,
    review_after_seconds: float | None = None,
) -> dict:
    """Invoke the versioned expectation writer for this exact model response."""
    control = _saved_control(store, control_ref)
    body = control["body"]
    db = store._connect()
    try:
        response = _response(Transaction(store, db), body["mission_id"], body, response_ref)
    finally:
        db.close()
    actor_ref = {"kind": "agent_instance", "id": body["actor_id"], "version": 1}
    task_ref = body.get("proposal_ref")
    task = store.get(**task_ref) if isinstance(task_ref, dict) else None
    if task is None or task["body"].get("mission_id") != body["mission_id"]:
        raise ObservationToolError("Current task is unavailable")
    expectation_ref = save_expectation(
        store,
        operation_id,
        mission_id=body["mission_id"],
        run_id=body["run_id"],
        actor_ref=actor_ref,
        task_ref=task_ref,
        response_ref=ref(response),
        category=category,
        basis=basis,
        expected_version=expected_version,
        review_after_seconds=review_after_seconds,
    )
    return {
        "status": "latency_expectation_saved",
        "expectation_ref": expectation_ref,
        "control_ref": control_ref,
        "control_delta": {},
    }


def record_wait_choice(
    store: StateStore,
    operation_id: str,
    *,
    control_ref: dict,
    response_ref: dict,
    dependencies: list[dict],
    reason: str,
) -> dict:
    """Save an immutable chosen wait, or return already available local input.

    The returned control_delta is a proposal for the caller to apply only after
    the executor accepts this tool result and releases the task lease.
    """
    identifier(operation_id)
    if (
        not isinstance(dependencies, list)
        or not dependencies
        or not isinstance(reason, str)
        or not reason.strip()
    ):
        raise ObservationToolError("A wait needs dependencies and a plain-language reason")
    control = _saved_control(store, control_ref)
    body = control["body"]
    mission_id = body["mission_id"]
    request = {
        "control_ref": control_ref,
        "response_ref": response_ref,
        "dependencies": dependencies,
        "reason": reason.strip(),
    }

    def save(tx: Transaction) -> dict:
        current = tx.get(**control_ref)
        head = tx.get("mission_task_control", control_ref["id"])
        if current is None or head is None or ref(head) != control_ref:
            raise StateConflict("Task control changed during wait decision")
        source_task = tx.get("board_proposal", body["task_id"])
        if source_task is None or source_task["body"].get("mission_id") != mission_id:
            raise ObservationToolError("Current proposal is unavailable")
        actor = require_ref(
            tx, mission_id, {"kind": "agent_instance", "id": body["actor_id"], "version": 1}
        )
        if actor["body"].get("run_id") != body["run_id"]:
            raise ObservationToolError("Wait author belongs to another run")
        response = _response(tx, mission_id, body, response_ref)
        if source_task["body"].get("status") != "open" or body.get("phase") in {"done", "deferred"}:
            return {
                "status": "dependency_available_for_review",
                "available": [
                    {
                        "kind": "own_task",
                        "task_ref": ref(source_task),
                        "status": source_task["body"].get("status"),
                    }
                ],
                "next_action": "Review the current task status and choose a continuation",
                "control_delta": {},
            }
        wait_for, targets, available = [], [], []
        for item in dependencies:
            if not isinstance(item, dict) or item.get("kind") not in {
                "peer_request",
                "proposal_dependency",
            }:
                raise ObservationToolError("Unknown wait dependency")
            target_ref = {
                "kind": "mission_task_control",
                "id": item.get("target_control_id"),
                "version": item.get("target_control_version"),
            }
            target = require_ref(tx, mission_id, target_ref)
            current_target = tx.get("mission_task_control", target["id"])
            if target["kind"] != "mission_task_control":
                raise ObservationToolError("Wait target must be a mission task control")
            if target["id"] == control_ref["id"]:
                raise ObservationToolError("A task cannot wait on itself")
            if target["body"].get("run_id") != body["run_id"]:
                raise ObservationToolError("Wait target belongs to another run")
            if item["kind"] == "peer_request":
                if set(item) != {
                    "kind",
                    "request_id",
                    "request_version",
                    "target_control_id",
                    "target_control_version",
                }:
                    raise ObservationToolError("Peer wait needs exact request and target control")
                input_ref = {
                    "kind": "mail_request",
                    "id": item["request_id"],
                    "version": item["request_version"],
                }
                request_record = require_ref(tx, mission_id, input_ref)
                current_request = tx.get("mail_request", request_record["id"])
                request_body = request_record["body"]
                if request_body.get("requester_id") != body["actor_id"] or request_body.get(
                    "recipient_id"
                ) != target["body"].get("actor_id"):
                    raise ObservationToolError(
                        "Peer request belongs to another requester or recipient"
                    )
                latest = current_request["body"] if current_request else request_body
                if latest.get("status") != "unanswered":
                    available.append(
                        {
                            "kind": "peer_request",
                            "request_ref": ref(current_request) if current_request else input_ref,
                            "status": latest.get("status"),
                            "answer_refs": latest.get("answer_refs", []),
                            "accepted_answer_ref": latest.get("accepted_answer_ref"),
                            "replacement_request_ref": latest.get("replacement_request_ref"),
                        }
                    )
                    continue
                if current_request is None or ref(current_request) != input_ref:
                    return {
                        "status": "dependency_changed",
                        "current_input_ref": ref(current_request) if current_request else None,
                        "control_delta": {},
                    }
                edge = {
                    "kind": "peer_request",
                    "request_ref": input_ref,
                    "target_control_ref": target_ref,
                }
            else:
                if set(item) != {
                    "kind",
                    "proposal_id",
                    "proposal_version",
                    "target_control_id",
                    "target_control_version",
                }:
                    raise ObservationToolError(
                        "Proposal wait needs exact prerequisite and target control"
                    )
                input_ref = {
                    "kind": "board_proposal",
                    "id": item["proposal_id"],
                    "version": item["proposal_version"],
                }
                proposal = require_ref(tx, mission_id, input_ref)
                current_proposal = tx.get("board_proposal", proposal["id"])
                if (
                    input_ref not in source_task["body"].get("depends_on", [])
                    or target["body"].get("task_id") != proposal["id"]
                ):
                    raise ObservationToolError("Proposal is not this task's exact prerequisite")
                latest = current_proposal["body"] if current_proposal else proposal["body"]
                if latest.get("status") != "open":
                    available.append(
                        {
                            "kind": "proposal_dependency",
                            "proposal_ref": ref(current_proposal)
                            if current_proposal
                            else input_ref,
                            "status": latest.get("status"),
                            "outcome_ref": latest.get("outcome_ref"),
                        }
                    )
                    continue
                if current_proposal is None or ref(current_proposal) != input_ref:
                    return {
                        "status": "dependency_changed",
                        "current_input_ref": ref(current_proposal) if current_proposal else None,
                        "control_delta": {},
                    }
                if body.get("proposal_ref") != ref(source_task):
                    return {
                        "status": "dependency_changed",
                        "current_input_ref": ref(source_task),
                        "control_delta": {},
                    }
                edge = {
                    "kind": "proposal_dependency",
                    "proposal_ref": input_ref,
                    "target_control_ref": target_ref,
                }
            if current_target is None or ref(current_target) != target_ref:
                # A peer can advance its own control while this requester is
                # preparing a chosen wait. Rebind only the same recipient's
                # same task; the exact unanswered request was checked above.
                prior = target["body"]
                updated = current_target["body"] if current_target else {}
                if item["kind"] != "peer_request" or any(
                    updated.get(key) != prior.get(key)
                    for key in ("mission_id", "run_id", "actor_id", "task_id")
                ):
                    return {
                        "status": "dependency_changed",
                        "current_target_ref": ref(current_target) if current_target else None,
                        "control_delta": {},
                    }
                target = current_target
                target_ref = ref(target)
                edge["target_control_ref"] = target_ref
            if (
                target["body"].get("phase") in {"done", "deferred"}
                or target["body"].get("scope_status") == "deferred"
            ):
                available.append(
                    {
                        "kind": "target_control",
                        "control_ref": target_ref,
                        "phase": target["body"].get("phase"),
                    }
                )
            wait_for.append(edge)
            targets.extend((target, request_record if item["kind"] == "peer_request" else proposal))
        if available:
            return {
                "status": "dependency_available_for_review",
                "available": available,
                "next_action": "Review the saved answer or changed prerequisite and choose a continuation",
                "control_delta": {},
            }
        decision_id = (
            "mission-wait-decision:"
            + hashlib.sha256(
                canonical([control_ref, response_ref, wait_for, reason.strip()]).encode()
            ).hexdigest()
        )
        existing = tx.get("mission_wait_decision", decision_id)
        if existing:
            return {
                "status": "wait_decision_saved",
                "decision_ref": ref(existing),
                "control_ref": control_ref,
                "control_delta": {
                    "phase": "waiting",
                    "work_id": None,
                    "retry_at": None,
                    "wait_for": wait_for,
                    "wait_decision_ref": ref(existing),
                    "waiting_reason": reason.strip(),
                },
            }
        decision = tx.put(
            "mission_wait_decision",
            decision_id,
            {
                "schema": "ora.mission_wait_decision.v1",
                "mission_id": mission_id,
                "run_id": body["run_id"],
                "actor_id": body["actor_id"],
                "actor_ref": ref(actor),
                "task_ref": ref(source_task),
                "control_ref": control_ref,
                "response_ref": ref(response),
                "wait_for": wait_for,
                "reason": reason.strip(),
                "status": "model_selected_wait",
                "recorded_at": store.now(),
            },
        )
        tx.link(decision, response, "input")
        tx.link(decision, actor, "authored_by")
        tx.link(decision, current, "about_task")
        for target in targets:
            tx.link(decision, target, "waits_for")
        return {
            "status": "wait_decision_saved",
            "decision_ref": ref(decision),
            "control_ref": control_ref,
            "control_delta": {
                "phase": "waiting",
                "work_id": None,
                "retry_at": None,
                "wait_for": wait_for,
                "wait_decision_ref": ref(decision),
                "waiting_reason": reason.strip(),
            },
        }

    return store.transact(operation_id, request, save, mission_id=mission_id)


def wait_replan_notification(
    store: StateStore,
    *,
    control_ref: dict,
    last_fingerprint: str | None = None,
) -> dict | None:
    """Offer a fresh cycle/stranding diagnosis to the affected peer once."""
    control = _current_control(store, control_ref)
    if control["body"].get("phase") != "waiting":
        return None
    report = inspect_waits(store, control["body"]["mission_id"])
    affected_cycles = [item for item in report["cycles"] if control_ref in item["control_refs"]]
    affected_stranded = [
        item for item in report["stranded_waits"] if item["control_ref"] == control_ref
    ]
    if (not affected_cycles and not affected_stranded) or report["fingerprint"] == last_fingerprint:
        return None
    feedback = {
        "status": "wait_needs_peer_replan",
        "diagnosis_fingerprint": report["fingerprint"],
        "cycles": affected_cycles,
        "stranded_waits": affected_stranded,
        "next_action": "Review the exact changed wait, then choose independent work, a partial answer, redirection, or qualified deferral",
    }
    return {
        "status": "replan_notification_available",
        "control_ref": control_ref,
        "fingerprint": report["fingerprint"],
        "feedback": feedback,
        "control_delta": {
            "phase": "ready_model",
            "work_id": None,
            "retry_at": None,
            "wait_for": [],
            "waiting_reason": None,
            "feedback": feedback,
            "wait_diagnostic_fingerprint": report["fingerprint"],
        },
    }
