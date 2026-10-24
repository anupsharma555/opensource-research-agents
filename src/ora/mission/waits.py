"""Read-only, branch-scoped diagnosis of saved mission waits.

Peer requests are communication, not wait-for edges. A cycle requires current
waiting controls with explicit exact wait declarations and unresolved targets.
This observer never cancels, schedules, polls a model or chooses research work.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict

from ora.coordination.executor import _id
from ora.state import StateStore
from ora.state.store import canonical


class WaitInspectionError(ValueError):
    """The requested mission or saved wait projection is unavailable."""


_OPEN_REQUEST = {"unanswered", "awaiting_review", "partial_awaiting_review", "partly_answered"}


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _snapshot(store: StateStore, mission_id: str) -> dict[str, dict[str, dict]]:
    """Read related heads from one SQLite snapshot without changing state."""
    db = store._connect()
    try:
        db.execute("BEGIN")
        result = {}
        for kind in (
            "mission_task_control",
            "mail_request",
            "board_proposal",
            "executor_work",
            "capacity_operation",
        ):
            rows = db.execute(
                "SELECT r.id,r.version,r.body FROM records r "
                "JOIN heads h USING(kind,id,version) "
                "WHERE r.kind=? AND json_extract(r.body,'$.mission_id')=? ORDER BY r.id",
                (kind, mission_id),
            )
            result[kind] = {
                row["id"]: {
                    "kind": kind,
                    "id": row["id"],
                    "version": row["version"],
                    "body": json.loads(row["body"]),
                }
                for row in rows
            }
        db.rollback()
        return result
    finally:
        db.close()


def _cycles(nodes: set[str], edges: list[dict]) -> list[list[str]]:
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        adjacency[edge["from_control_ref"]["id"]].append(edge["to_control_ref"]["id"])
    order, low, stack, stacked, components = {}, {}, [], set(), []

    def visit(node: str) -> None:
        order[node] = low[node] = len(order)
        stack.append(node)
        stacked.add(node)
        for target in sorted(set(adjacency[node])):
            if target not in order:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in stacked:
                low[node] = min(low[node], order[target])
        if low[node] == order[node]:
            component = []
            while True:
                item = stack.pop()
                stacked.remove(item)
                component.append(item)
                if item == node:
                    break
            if len(component) > 1 or node in adjacency[node]:
                components.append(sorted(component))

    for node in sorted(nodes):
        if node not in order:
            visit(node)
    return sorted(components)


def inspect_waits(store: StateStore, mission_id: str, *, now: float | None = None) -> dict:
    """Classify current waits and return evidence for peer-directed continuation.

    An optional control ``wait_for`` item is host-saved as either
    ``{kind:'peer_request', request_ref, target_control_ref}`` or
    ``{kind:'proposal_dependency', proposal_ref, target_control_ref}``.
    A source control waits on an exact unresolved input even while its target
    peer works. An edge enters cycle detection only when that exact target is
    also waiting. An unresolved peer wait follows the same recipient task's
    current control version; changed request identity and proposal dependencies
    remain exact and return for review.
    """
    if not isinstance(mission_id, str) or not mission_id:
        raise WaitInspectionError("Mission ID is required")
    timestamp = store.clock() if now is None else now
    if (
        not isinstance(timestamp, (int, float))
        or isinstance(timestamp, bool)
        or not math.isfinite(timestamp)
    ):
        raise WaitInspectionError("Inspection time must be a finite numeric clock reading")
    saved = _snapshot(store, mission_id)
    controls = saved["mission_task_control"]
    requests = saved["mail_request"]
    proposals = saved["board_proposal"]
    work = saved["executor_work"]
    capacity = saved["capacity_operation"]
    work_by_id = {item["body"].get("spec", {}).get("work_id"): item for item in work.values()}
    edges, stale, normal, stranded, uncertain = [], [], [], [], []
    peer_waiting: dict[str, list[dict]] = defaultdict(list)

    def eligible(control: dict) -> bool:
        body = control["body"]
        return (
            body.get("phase") == "waiting"
            and not body.get("work_id")
            and body.get("retry_at") is None
            and not body.get("operator_credential_hold")
            and not body.get("search_wait")
            and body.get("scope_status") not in {"deferred", "cancelled"}
        )

    for control in sorted(controls.values(), key=lambda item: item["id"]):
        body = control["body"]
        if not eligible(control):
            continue
        declarations = body.get("wait_for") or []
        if not isinstance(declarations, list):
            stale.append(
                {
                    "control_ref": _ref(control),
                    "actor_id": body.get("actor_id"),
                    "reason": "invalid_wait_declaration",
                }
            )
            continue
        for declared in declarations:
            target_ref = declared.get("target_control_ref") if isinstance(declared, dict) else None
            target = controls.get(target_ref.get("id")) if isinstance(target_ref, dict) else None
            kind = declared.get("kind") if isinstance(declared, dict) else None
            current_target_ref = _ref(target) if target else None
            same_target = target is not None and current_target_ref == target_ref
            if (
                not same_target and kind == "peer_request"
                and isinstance(target_ref, dict)
                and target_ref.get("kind") == "mission_task_control"
                and isinstance(target_ref.get("id"), str)
                and type(target_ref.get("version")) is int
            ):
                previous = store.get(
                    "mission_task_control", target_ref["id"], target_ref["version"]
                )
                same_target = bool(
                    previous and target and body.get("run_id")
                    and previous["id"] == target["id"]
                    and all(
                        previous["body"].get(key) == target["body"].get(key)
                        for key in ("mission_id", "run_id", "actor_id", "task_id")
                    )
                    and target["body"].get("mission_id") == mission_id
                    and target["body"].get("run_id") == body["run_id"]
                )
            if (
                not isinstance(declared, dict)
                or not isinstance(target_ref, dict)
                or target is None
                or not same_target
            ):
                stale.append(
                    {
                        "control_ref": _ref(control),
                        "actor_id": body.get("actor_id"),
                        "declared_target_ref": target_ref,
                        "current_target_ref": current_target_ref,
                        "reason": "target_not_current_at_exact_version",
                    }
                )
                continue
            if (
                kind == "peer_request"
                and target["body"].get("run_id") != body.get("run_id")
            ):
                stale.append(
                    {
                        "control_ref": _ref(control),
                        "actor_id": body.get("actor_id"),
                        "declared_target_ref": target_ref,
                        "current_target_ref": current_target_ref,
                        "reason": "peer_target_outside_waiting_run",
                    }
                )
                continue
            if kind == "peer_request":
                request_ref = declared.get("request_ref")
                request = (
                    requests.get(request_ref.get("id")) if isinstance(request_ref, dict) else None
                )
                if (
                    request is None
                    or _ref(request) != request_ref
                    or request["body"].get("status") != "unanswered"
                    or request["body"].get("requester_id") != body.get("actor_id")
                    or request["body"].get("recipient_id") != target["body"].get("actor_id")
                ):
                    stale.append(
                        {
                            "control_ref": _ref(control),
                            "actor_id": body.get("actor_id"),
                            "request_ref": request_ref,
                            "current_request_ref": _ref(request) if request else None,
                            "reason": "request_answered_changed_or_not_owned",
                        }
                    )
                    continue
                input_ref = request_ref
            elif kind == "proposal_dependency":
                proposal_ref = declared.get("proposal_ref")
                proposal = (
                    proposals.get(proposal_ref.get("id"))
                    if isinstance(proposal_ref, dict)
                    else None
                )
                source_proposal = proposals.get(body.get("task_id"))
                if (
                    proposal is None
                    or _ref(proposal) != proposal_ref
                    or proposal["body"].get("status") != "open"
                    or target["body"].get("task_id") != proposal["id"]
                    or source_proposal is None
                    or source_proposal["body"].get("status") != "open"
                    or body.get("proposal_ref") != _ref(source_proposal)
                    or proposal_ref not in source_proposal["body"].get("depends_on", [])
                ):
                    stale.append(
                        {
                            "control_ref": _ref(control),
                            "actor_id": body.get("actor_id"),
                            "proposal_ref": proposal_ref,
                            "current_proposal_ref": _ref(proposal) if proposal else None,
                            "reason": "proposal_dependency_changed_or_completed",
                        }
                    )
                    continue
                input_ref = proposal_ref
            else:
                stale.append(
                    {
                        "control_ref": _ref(control),
                        "actor_id": body.get("actor_id"),
                        "reason": "unsupported_wait_kind",
                    }
                )
                continue
            if (
                target["body"].get("phase") in {"done", "deferred", "cancelled"}
                or target["body"].get("scope_status") in {"deferred", "cancelled"}
            ):
                stale.append(
                    {
                        "control_ref": _ref(control),
                        "actor_id": body.get("actor_id"),
                        "declared_target_ref": target_ref,
                        "current_target_ref": current_target_ref,
                        "reason": "target_finished_or_deferred_without_waited_input",
                    }
                )
                continue
            peer_waiting[control["id"]].append(
                {
                    "kind": kind,
                    "input_ref": input_ref,
                    "target_control_ref": _ref(target),
                    "target_phase": target["body"].get("phase"),
                }
            )
            if eligible(target):
                edges.append(
                    {
                        "kind": kind,
                        "from_control_ref": _ref(control),
                        "from_actor_id": body.get("actor_id"),
                        "to_control_ref": _ref(target),
                        "to_actor_id": target["body"].get("actor_id"),
                        "input_ref": input_ref,
                    }
                )

    for control in sorted(controls.values(), key=lambda item: item["id"]):
        body = control["body"]
        phase = body.get("phase")
        if phase not in {"waiting", "retry_at", "pending_model", "pending_tool"}:
            continue
        pointer = _ref(control)
        if body.get("operator_credential_hold"):
            normal.append({
                "control_ref": pointer,
                "actor_id": body.get("actor_id"),
                "kind": "operator_credential_hold",
            })
            continue
        if phase == "waiting" and body.get("search_wait"):
            normal.append({
                "control_ref": pointer,
                "actor_id": body.get("actor_id"),
                "kind": "saved_search_wait",
                "operation_ref": body["search_wait"].get("operation_ref"),
            })
            continue
        retry_at = body.get("retry_at")
        if (
            phase == "retry_at"
            and isinstance(retry_at, (int, float))
            and not isinstance(retry_at, bool)
            and math.isfinite(retry_at)
        ):
            normal.append(
                {
                    "control_ref": pointer,
                    "actor_id": body.get("actor_id"),
                    "kind": "saved_retry_timer",
                    "wake_at": retry_at,
                    "due": retry_at <= timestamp,
                }
            )
            continue
        work_id = body.get("work_id")
        item = work_by_id.get(work_id) if work_id else None
        cap_id = _id("executor-cap:", [mission_id, work_id]) if work_id else None
        cap = capacity.get(cap_id) if cap_id else None
        if cap and cap["body"].get("phase") == "uncertain":
            uncertain.append(
                {
                    "control_ref": pointer,
                    "actor_id": body.get("actor_id"),
                    "work_ref": _ref(item) if item else None,
                    "capacity_ref": _ref(cap),
                    "explanation": "External effect is uncertain; reconcile saved attempt before retry",
                }
            )
            continue
        if item and item["body"].get("phase") == "result_unaccepted":
            stranded.append(
                {
                    "control_ref": pointer,
                    "actor_id": body.get("actor_id"),
                    "work_ref": _ref(item),
                    "result_ref": item["body"].get("result_ref"),
                    "reason": "known_result_needs_acceptance_repair",
                    "possible_continuations": ["review_saved_result", "repair_local_acceptance"],
                }
            )
            continue
        if cap and cap["body"].get("phase") in {"queued", "waiting", "admitted", "dispatched"}:
            wake_at = cap["body"].get("wake_at")
            if wake_at is None:
                wake_at = cap["body"].get("lease_until")
            details = {
                "control_ref": pointer,
                "actor_id": body.get("actor_id"),
                "work_ref": _ref(item) if item else None,
                "capacity_ref": _ref(cap),
                "capacity_phase": cap["body"]["phase"],
                "wake_at": wake_at,
            }
            if (
                isinstance(wake_at, (int, float))
                and not isinstance(wake_at, bool)
                and math.isfinite(wake_at)
            ):
                normal.append(
                    {
                        **details,
                        "kind": "capacity_or_provider_wait",
                        "due": wake_at <= timestamp,
                    }
                )
            elif cap["body"]["phase"] == "queued":
                normal.append(
                    {
                        **details,
                        "kind": "capacity_ready_for_local_admission",
                        "due": True,
                    }
                )
            else:
                stranded.append(
                    {
                        **details,
                        "reason": "capacity_has_no_saved_future_wake",
                        "possible_continuations": [
                            "recheck_local_admission",
                            "inspect_reservations_and_quota",
                        ],
                    }
                )
            continue
        if control["id"] in peer_waiting:
            normal.append(
                {
                    "control_ref": pointer,
                    "actor_id": body.get("actor_id"),
                    "kind": "explicit_peer_or_dependency_wait",
                    "dependencies": peer_waiting[control["id"]],
                    "waiting_target_count": sum(
                        edge["from_control_ref"]["id"] == control["id"] for edge in edges
                    ),
                }
            )
            continue
        if phase in {"waiting", "pending_model", "pending_tool", "retry_at"}:
            stranded.append(
                {
                    "control_ref": pointer,
                    "actor_id": body.get("actor_id"),
                    "work_ref": _ref(item) if item else None,
                    "capacity_ref": _ref(cap) if cap else None,
                    "waiting_reason": body.get("waiting_reason"),
                    "reason": (
                        "invalid_retry_timer" if phase == "retry_at" else "no_recorded_future_wake"
                    ),
                    "possible_continuations": [
                        "reread_current_state",
                        "repair_affected_branch",
                        "choose_alternate_work_or_qualified_deferral",
                    ],
                }
            )

    components = _cycles(set(controls), edges)
    cycle_control_ids = {item for component in components for item in component}
    normal = [
        item
        for item in normal
        if not (
            item["kind"] == "explicit_peer_or_dependency_wait"
            and item["control_ref"]["id"] in cycle_control_ids
        )
    ]
    cycles = []
    for ids in components:
        inside = [
            edge
            for edge in edges
            if edge["from_control_ref"]["id"] in ids and edge["to_control_ref"]["id"] in ids
        ]
        refs = [_ref(controls[item]) for item in ids]
        actors = sorted({controls[item]["body"].get("actor_id") for item in ids})
        cycle = {
            "control_refs": refs,
            "actor_ids": actors,
            "edges": inside,
            "explanation": "These current waiting controls explicitly depend on each other through unresolved saved inputs",
            "possible_continuations": [
                "review_exact_wait_relationships",
                "ask_for_partial_peer_answer_or_redirect",
                "choose_independent_work",
                "record_qualified_deferral",
            ],
        }
        cycle["fingerprint"] = hashlib.sha256(canonical(cycle).encode()).hexdigest()[:20]
        cycles.append(cycle)
    pending_requests = [
        {
            "request_ref": _ref(item),
            "status": item["body"].get("status"),
            "requester_id": item["body"].get("requester_id"),
            "recipient_id": item["body"].get("recipient_id"),
        }
        for item in sorted(requests.values(), key=lambda item: item["id"])
        if item["body"].get("status") in _OPEN_REQUEST
    ]
    report = {
        "schema": "ora.wait_diagnosis.v1",
        "mission_id": mission_id,
        "cycles": cycles,
        "stale_waits": stale,
        "normal_waits": normal,
        "stranded_waits": stranded,
        "uncertain_effects": uncertain,
        "pending_requests": pending_requests,
    }
    report["fingerprint"] = hashlib.sha256(canonical(report).encode()).hexdigest()[:24]
    return report
