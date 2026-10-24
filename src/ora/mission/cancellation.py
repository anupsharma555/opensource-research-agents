"""Trusted, task-scoped cancellation at the durable mission boundary.

The board version is the dispatch and acceptance fence. Capacity and control
changes share its transaction so a callback sees either the old runnable task
or the cancelled task, never a partially applied cancellation.
"""

from __future__ import annotations

import json

from ora.coordination.board import ProposalBoard, ref
from ora.coordination.executor import ConcurrentExecutor
from ora.state import StateConflict, StateError, StateStore, Transaction

KIND = "mission_branch_cancellation"
RUN_KIND = "mission_run_cancellation"
_UNRESOLVED_MAIL = frozenset({
    "unanswered", "partly_answered", "awaiting_review", "partial_awaiting_review",
})
_NO_EFFECT_CAPACITY = frozenset({"queued", "waiting", "admitted"})
_TERMINAL_CAPACITY = frozenset({"completed", "known_unaccepted", "failed", "canceled"})


def _heads(tx: Transaction, kind: str) -> list[dict]:
    rows = tx.db.execute(
        "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) WHERE r.kind=?",
        (kind,),
    ).fetchall()
    return [
        {"kind": kind, "id": row["id"], "version": row["version"],
         "body": json.loads(row["body"])}
        for row in rows
    ]


def cancel_run_tx(
    tx: Transaction, store: StateStore, board: ProposalBoard, *,
    request_id: str, run_id: str, expected_run_version: int, reason: str,
) -> dict:
    """Stop every unfinished part of one run in a single durable transaction."""
    requested = {
        "request_id": request_id, "run_id": run_id,
        "expected_run_version": expected_run_version, "reason": reason,
    }
    prior = tx.get(RUN_KIND, request_id)
    if prior is not None:
        if prior["body"]["request"] != requested:
            raise StateConflict("Cancellation request ID already names different inputs")
        return {
            "reused": True, "status": prior["body"]["status"],
            "cancellation_ref": ref(prior), "run_ref": prior["body"]["run_ref"],
        }
    run = tx.get("mission_run", run_id)
    if run is None:
        raise StateError("Run is unavailable")
    if run["version"] != expected_run_version:
        raise StateConflict("Run version changed; reread before cancellation")
    mission_id = run["body"]["mission_id"]
    controls = [item for item in _heads(tx, "mission_task_control")
                if (item["body"].get("mission_id"), item["body"].get("run_id"))
                == (mission_id, run_id)]
    initialized = {
        item["body"].get("alias") for item in controls
        if item["body"].get("requested_participant") is True
        and item["body"].get("origin_proposal_ref") is None
    }
    if set(run["body"].get("agent_ids") or []) - initialized:
        raise StateConflict("Run initialization is incomplete; retry after start returns")
    unfinished = [item for item in controls
                  if item["body"].get("phase") not in {"done", "deferred", "cancelled"}]
    if run["body"].get("status") == "cancelled" or (
        run["body"].get("status") == "completed" and not unfinished
    ):
        raise StateConflict("Run has no continuing work to cancel")
    if run["body"].get("status") not in {"active", "completed"}:
        raise StateConflict("Run is not cancellable")

    proposal_refs = []
    for proposal in _heads(tx, "board_proposal"):
        if (proposal["body"].get("mission_id") != mission_id
                or proposal["body"].get("status") != "open"):
            continue
        saved = tx.put("board_proposal", proposal["id"],
                       {**proposal["body"], "status": "cancelled"},
                       expected_version=proposal["version"])
        board._revoke_claim_tx(tx, mission_id, proposal["id"])
        tx.link(saved, proposal, "derived_from")
        proposal_refs.append(ref(saved))

    control_refs = []
    for control in unfinished:
        saved = tx.put("mission_task_control", control["id"], {
            **control["body"], "phase": "cancelled", "scope_status": "cancelled",
            "waiting_reason": None, "operator_credential_hold": None,
            "cancellation_request_id": request_id,
        }, expected_version=control["version"])
        control_refs.append(ref(saved))

    search_refs = []
    for search in _heads(tx, "web_search_operation"):
        body = search["body"]
        if (body.get("mission_id"), body.get("run_id")) != (mission_id, run_id):
            continue
        if (not body.get("cancel_requested")
                and len(body.get("deliveries") or []) < len(body.get("items") or [])):
            saved = tx.put("web_search_operation", search["id"],
                           {**body, "cancel_requested": True},
                           expected_version=search["version"])
            search_refs.append(ref(saved))

    capacity_refs = []
    uncertain_effect = False
    for capacity in _heads(tx, "capacity_operation"):
        body = capacity["body"]
        if (body.get("mission_id"), body.get("run_id")) != (mission_id, run_id):
            continue
        phase = body.get("phase")
        if phase in _TERMINAL_CAPACITY:
            continue
        if phase in _NO_EFFECT_CAPACITY:
            updated = {
                **body, "phase": "canceled", "cancel_requested": True,
                "diagnostic": "run_cancel", "reservation": False,
                "local_active": False, "external_active": False,
                "owner": None, "lease_until": None, "wake_at": None,
                "generation": body["generation"] + 1,
            }
        else:
            uncertain_effect = True
            updated = {**body, "cancel_requested": True, "diagnostic": "run_cancel"}
        saved = tx.put("capacity_operation", capacity["id"], updated,
                       expected_version=capacity["version"])
        capacity_refs.append(ref(saved))

    work_refs = []
    for work in _heads(tx, "executor_work"):
        spec = work["body"].get("spec") or {}
        if (spec.get("mission_id"), spec.get("run_id")) != (mission_id, run_id):
            continue
        if work["body"].get("phase") != "pending":
            continue
        capacity_id = ConcurrentExecutor._capacity_id(ConcurrentExecutor._spec_from(work))
        capacity = tx.get("capacity_operation", capacity_id)
        if capacity is not None and capacity["body"].get("phase") != "canceled":
            continue
        saved = tx.put("executor_work", work["id"],
                       {**work["body"], "phase": "cancelled"},
                       expected_version=work["version"])
        work_refs.append(ref(saved))

    run_saved = tx.put("mission_run", run_id, {
        **run["body"], "status": "cancelled", "cancelled_at": store.now(),
        "cancellation_request_id": request_id,
        "closure_event_through": tx.db.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE mission_id=?",
            (mission_id,),
        ).fetchone()[0],
    }, expected_version=run["version"])
    status = "cancellation_requested" if uncertain_effect else "cancelled"
    cancellation = tx.put(RUN_KIND, request_id, {
        "schema": "ora.mission_run_cancellation.v1", "mission_id": mission_id,
        "run_id": run_id, "request": requested, "reason": reason,
        "status": status, "prior_run_ref": ref(run), "run_ref": ref(run_saved),
        "proposal_refs": proposal_refs, "control_refs": control_refs,
        "search_refs": search_refs, "capacity_refs": capacity_refs,
        "work_refs": work_refs, "created_at": store.now(),
    })
    tx.link(cancellation, run_saved, "about_run")
    tx.link(run_saved, run, "derived_from")

    for request in _heads(tx, "mail_request"):
        body = request["body"]
        if (body.get("mission_id") != mission_id
                or body.get("status") not in _UNRESOLVED_MAIL):
            continue
        saved = tx.put("mail_request", request["id"], {
            **body, "status": "cancelled", "reason": "source_run_cancelled",
            "cancellation_ref": ref(cancellation), "resolved_at": store.now(),
        }, expected_version=request["version"])
        tx.link(saved, request, "derived_from")
        message_id = (body.get("message_ref") or {}).get("id")
        outbox = tx.get("mail_outbox", message_id) if message_id else None
        if outbox and outbox["body"].get("status") == "pending":
            tx.put("mail_outbox", message_id, {
                **outbox["body"], "status": "cancelled", "updated_at": store.now(),
            }, expected_version=outbox["version"])
    for outbox in _heads(tx, "mail_outbox"):
        if (outbox["body"].get("mission_id") == mission_id
                and outbox["body"].get("status") == "pending"):
            tx.put("mail_outbox", outbox["id"], {
                **outbox["body"], "status": "cancelled", "updated_at": store.now(),
            }, expected_version=outbox["version"])
    tx.emit(mission_id, "mission_run_cancelled", {
        "cancellation_ref": ref(cancellation), "run_ref": ref(run_saved),
        "status": status, "subjects": [{"kind": "run", "id": run_id}],
    })
    return {"reused": False, "status": status,
            "cancellation_ref": ref(cancellation), "run_ref": ref(run_saved)}


def cancel_branch_tx(
    tx: Transaction,
    store: StateStore,
    board: ProposalBoard,
    *,
    request_id: str,
    run_id: str,
    task_id: str,
    expected_task_version: int,
    reason: str,
) -> dict:
    """Atomically fence one task and return references for the operator receipt."""
    requested = {
        "request_id": request_id,
        "run_id": run_id,
        "task_id": task_id,
        "expected_task_version": expected_task_version,
        "reason": reason,
    }
    prior = tx.get(KIND, request_id)
    if prior is not None:
        if prior["body"]["request"] != requested:
            raise StateConflict("Cancellation request ID already names different inputs")
        return {
            "reused": True,
            "status": prior["body"]["status"],
            "cancellation_ref": ref(prior),
            "task_ref": prior["body"]["task_ref"],
        }

    run = tx.get("mission_run", run_id)
    if run is None:
        raise StateError("Run is unavailable")
    mission_id = run["body"]["mission_id"]
    task = tx.get("board_proposal", task_id)
    if task is None or task["body"].get("mission_id") != mission_id:
        raise StateError("Task is unavailable in this run")
    controls = [
        item for item in _heads(tx, "mission_task_control")
        if item["body"].get("mission_id") == mission_id
        and item["body"].get("run_id") == run_id
        and item["body"].get("task_id") == task_id
    ]
    if not controls:
        raise StateError("Task has no control in this run")
    if task["version"] != expected_task_version:
        raise StateConflict("Task version changed; reread before cancellation")
    if task["body"].get("status") != "open" or all(
        item["body"].get("phase") in {"done", "deferred", "cancelled"}
        for item in controls
    ):
        raise StateConflict("Task is already terminal or not open")
    task_saved = tx.put(
        "board_proposal", task_id,
        {**task["body"], "status": "cancelled"},
        expected_version=task["version"],
    )
    board._revoke_claim_tx(tx, mission_id, task_id)
    planned_child_ids: set[str] = set()
    search_refs = []
    search_ids: set[str] = set()
    for search in _heads(tx, "web_search_operation"):
        body = search["body"]
        if (body.get("mission_id"), body.get("run_id"), body.get("task_id")) != (
            mission_id, run_id, task_id,
        ):
            continue
        search_ids.add(search["id"])
        planned_child_ids.update(body.get("items") or [])
        if not body.get("cancel_requested"):
            saved = tx.put(
                "web_search_operation", search["id"],
                {**body, "cancel_requested": True},
                expected_version=search["version"],
            )
            search_refs.append(ref(saved))
    for item in _heads(tx, "web_search_item"):
        if item["body"].get("operation_id") in search_ids:
            planned_child_ids.add(item["id"])
    child_task_refs = []
    child_task_ids: set[str] = set()
    for child_id in sorted(planned_child_ids):
        child = tx.get("board_proposal", child_id)
        if child is None or child["body"].get("mission_id") != mission_id:
            continue
        if (
            (child["body"].get("branch_of") or {}).get("id") != task_id
            or (child["body"].get("actor_ref") or {}).get("id")
            != (task["body"].get("actor_ref") or {}).get("id")
        ):
            continue
        child_task_ids.add(child_id)
        if child["body"].get("status") != "open":
            continue  # Keep accepted child receipts and their proposal history.
        saved = tx.put(
            "board_proposal", child_id,
            {**child["body"], "status": "cancelled"},
            expected_version=child["version"],
        )
        board._revoke_claim_tx(tx, mission_id, child_id)
        tx.link(saved, child, "derived_from")
        child_task_refs.append(ref(saved))
    affected_task_ids = {task_id, *child_task_ids}
    control_refs = []
    for control in controls:
        if control["body"].get("phase") in {"done", "deferred"}:
            continue  # Keep a completed invitation review or old defer as history.
        saved = tx.put(
            "mission_task_control", control["id"],
            {
                **control["body"],
                "proposal_ref": ref(task_saved),
                "phase": "cancelled",
                "scope_status": "cancelled",
                "waiting_reason": None,
                "operator_credential_hold": None,
                "cancellation_request_id": request_id,
            },
            expected_version=control["version"],
        )
        control_refs.append(ref(saved))

    capacity_refs = []
    uncertain_effect = False
    for capacity in _heads(tx, "capacity_operation"):
        body = capacity["body"]
        if (body.get("mission_id"), body.get("run_id")) != (mission_id, run_id) or body.get("task_id") not in affected_task_ids:
            continue
        phase = body.get("phase")
        if phase in _TERMINAL_CAPACITY:
            continue  # Keep known usage, receipts, and old terminal state intact.
        if phase in _NO_EFFECT_CAPACITY:
            updated = {
                **body, "phase": "canceled", "cancel_requested": True,
                "diagnostic": "branch_cancel", "reservation": False,
                "local_active": False, "external_active": False,
                "owner": None, "lease_until": None, "wake_at": None,
                "generation": body["generation"] + 1,
            }
        else:
            # A dispatched or uncertain effect may have happened remotely. Keep
            # its owner, generation, attempt, reservation, and accounting state.
            uncertain_effect = True
            updated = {**body, "cancel_requested": True, "diagnostic": "branch_cancel"}
        saved = tx.put(
            "capacity_operation", capacity["id"], updated,
            expected_version=capacity["version"],
        )
        capacity_refs.append(ref(saved))

    work_refs = []
    for work in _heads(tx, "executor_work"):
        spec = work["body"].get("spec") or {}
        proposal = spec.get("proposal_ref") or {}
        if spec.get("run_id") != run_id or proposal.get("id") not in affected_task_ids:
            continue
        if work["body"].get("phase") != "pending":
            continue
        capacity_id = ConcurrentExecutor._capacity_id(ConcurrentExecutor._spec_from(work))
        capacity = tx.get("capacity_operation", capacity_id)
        if capacity is not None and capacity["body"].get("phase") not in {"canceled"}:
            continue  # Preserve a dispatched work identity for a late observation.
        saved = tx.put(
            "executor_work", work["id"],
            {**work["body"], "phase": "cancelled"},
            expected_version=work["version"],
        )
        work_refs.append(ref(saved))

    status = "cancellation_requested" if uncertain_effect else "cancelled"
    cancellation = tx.put(KIND, request_id, {
        "schema": "ora.mission_branch_cancellation.v1",
        "mission_id": mission_id,
        "run_id": run_id,
        "task_id": task_id,
        "request": requested,
        "reason": reason,
        "status": status,
        "prior_task_ref": ref(task),
        "task_ref": ref(task_saved),
        "control_refs": control_refs,
        "child_task_refs": child_task_refs,
        "search_refs": search_refs,
        "capacity_refs": capacity_refs,
        "work_refs": work_refs,
        "created_at": store.now(),
    })
    tx.link(cancellation, task_saved, "about_task")
    tx.link(task_saved, task, "derived_from")

    # An outgoing request belongs to its cause task, even if its recipient has
    # other work. Preserve old answers; resolve only unresolved requests here.
    recipient_actor = (task["body"].get("actor_ref") or {}).get("id")
    other_recipient_work = any(
        item["body"].get("actor_id") == recipient_actor
        and item["body"].get("run_id") == run_id
        and item["body"].get("task_id") != task_id
        and item["body"].get("phase") not in {"cancelled", "deferred"}
        for item in _heads(tx, "mission_task_control")
    )
    for request in _heads(tx, "mail_request"):
        body = request["body"]
        if body.get("mission_id") != mission_id or body.get("status") not in _UNRESOLVED_MAIL:
            continue
        outgoing = (body.get("cause_ref") or {}).get("id") == task_id
        unavailable_recipient = (
            not outgoing and not other_recipient_work
            and body.get("recipient_id") == recipient_actor
            and body.get("status") in {"unanswered", "partly_answered"}
        )
        if not outgoing and not unavailable_recipient:
            continue
        saved = tx.put(
            "mail_request", request["id"],
            {**body, "status": (
                "cancelled" if outgoing else "recipient_unavailable_due_branch_cancel"
             ), "reason": (
                "source_task_cancelled" if outgoing else "recipient_task_cancelled"
             ),
             "cancellation_ref": ref(cancellation), "resolved_at": store.now()},
            expected_version=request["version"],
        )
        tx.link(saved, request, "derived_from")
        message_id = (body.get("message_ref") or {}).get("id")
        outbox = tx.get("mail_outbox", message_id) if message_id else None
        if outbox and outbox["body"].get("status") == "pending":
            tx.put(
                "mail_outbox", message_id,
                {**outbox["body"], "status": "cancelled", "updated_at": store.now()},
                expected_version=outbox["version"],
            )
    tx.emit(mission_id, "mission_branch_cancelled", {
        "cancellation_ref": ref(cancellation), "task_ref": ref(task_saved),
        "status": status, "subjects": [{"kind": "task", "id": task_id}],
    })
    return {
        "reused": False,
        "status": status,
        "cancellation_ref": ref(cancellation),
        "task_ref": ref(task_saved),
    }
