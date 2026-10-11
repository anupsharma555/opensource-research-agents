"""Classify peer requests opened after an exact mission scope closure.

This module records a reviewable disposition only. It never reopens a run,
reactivates a task, or decides whether the scientific request was answered.
"""

from __future__ import annotations

import hashlib
import json

from ora.state import StateConflict, StateStore, Transaction
from ora.state.store import canonical, identifier


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _exact(tx: Transaction, pointer: object, kind: str) -> dict | None:
    if not _exact_ref_shape(pointer, kind):
        return None
    return tx.get(**pointer)


def _observation(tx: Transaction, completed_run_ref: dict, request_id: str) -> dict | None:
    run = _exact(tx, completed_run_ref, "mission_run")
    current_run = tx.get("mission_run", run["id"]) if run else None
    if run is None or current_run is None or _ref(current_run) != completed_run_ref:
        return None
    body = run["body"]
    cutoff = body.get("closure_event_through")
    if body.get("status") != "completed" or type(cutoff) is not int or cutoff < 0:
        return None  # Legacy completion and cancellation have no defensible late cutoff.
    mission_id = body.get("mission_id")
    if not isinstance(mission_id, str):
        return None
    rows = tx.db.execute(
        "SELECT id,sequence,body FROM events WHERE mission_id=? "
        "AND event_type='peer_request_opened' AND sequence>? "
        "AND json_extract(body,'$.request_ref.id')=? ORDER BY sequence",
        (mission_id, cutoff, request_id),
    ).fetchall()
    if len(rows) != 1:
        return None
    opened = rows[0]
    request_ref = json.loads(opened["body"]).get("request_ref")
    request = _exact(tx, request_ref, "mail_request")
    if request is None or request["id"] != request_id or request["body"].get("mission_id") != mission_id:
        return None
    cause_proposal_ref = request["body"].get("cause_ref")
    proposal = _exact(tx, cause_proposal_ref, "board_proposal")
    if proposal is None or proposal["body"].get("mission_id") != mission_id:
        return None
    scope_ref = body.get("scope_disposition_ref")
    if scope_ref is not None:
        scope = _exact(tx, scope_ref, "mission_scope_disposition")
        if (
            scope is None
            or scope["body"].get("mission_id") != mission_id
            or (scope["body"].get("run_ref") or {}).get("id") != run["id"]
        ):
            return None

    # The opening request pins a proposal, not a control. A unique current
    # control for that proposal/task and requester is the strongest saved
    # classification-time relation; do not infer an opening-time control version.
    controls = []
    for row in tx.db.execute(
        "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) "
        "WHERE r.kind='mission_task_control'"
    ):
        item = json.loads(row["body"])
        if (
            item.get("mission_id") == mission_id
            and item.get("run_id") == run["id"]
            and item.get("task_id") == proposal["id"]
            and item.get("actor_id") == request["body"].get("requester_id")
        ):
            controls.append({"kind": row["kind"], "id": row["id"], "version": row["version"], "body": item})
    if len(controls) != 1:
        return None
    control = controls[0]
    decision = (
        "continue_under_saved_optional_scope"
        if control["body"].get("scope_status") == "continued"
        else "followup_required"
    )
    return {
        "schema": "ora.mission_post_scope_disposition.v1",
        "mission_id": mission_id,
        "run_id": run["id"],
        "completed_run_ref": completed_run_ref,
        "scope_disposition_ref": scope_ref,
        "request_ref": request_ref,
        "opening_event_id": opened["id"],
        "opening_event_sequence": opened["sequence"],
        "cause_proposal_ref": cause_proposal_ref,
        "cause_control_ref": _ref(control),
        "cause_control_scope_status": control["body"].get("scope_status"),
        "decision": decision,
    }


def record_post_scope_disposition(
    store: StateStore, *, completed_run_ref: dict, request_id: str
) -> dict | None:
    """Save one exact late-request decision, or return None when it is unknown.

    The read preflight avoids caching an unknown result under the idempotency
    key. The acceptance transaction repeats every check against one DB view.
    """
    identifier(request_id)
    if not _exact_ref_shape(completed_run_ref, "mission_run"):
        return None
    suffix = hashlib.sha256(canonical([completed_run_ref["id"], request_id]).encode()).hexdigest()[:32]
    record_id = f"mission-post-scope-{suffix}"
    existing = store.get("mission_post_scope_disposition", record_id)
    if existing is not None:
        body = existing["body"]
        if body["completed_run_ref"] != completed_run_ref or body["request_ref"]["id"] != request_id:
            raise StateConflict("Post-scope disposition identity changed")
        return {"status": "recorded", "disposition_ref": _ref(existing), "decision": body["decision"], "created": False}
    db = store._connect()
    try:
        observed = _observation(Transaction(store, db), completed_run_ref, request_id)
        if observed is None:
            return None
    finally:
        db.close()

    operation_id = f"mission-post-scope-disposition:{record_id}"
    mission_id = observed["mission_id"]

    def save(tx: Transaction) -> dict:
        observed = _observation(tx, completed_run_ref, request_id)
        if observed is None:
            # A concurrent change invalidated the preflight. Returning this
            # status would cache it, so the caller should retry on the next
            # loop rather than save an unsupported classification.
            raise PostScopeChanged("Late request or cause control changed before disposition")
        saved = tx.put("mission_post_scope_disposition", record_id, {
            **observed, "recorded_at": store.now(),
        })
        for pointer, relation in (
            (observed["completed_run_ref"], "about_run"),
            (observed["request_ref"], "about_request"),
            (observed["cause_proposal_ref"], "caused_by"),
            (observed["cause_control_ref"], "classified_from_control"),
        ):
            tx.link(saved, tx.get(**pointer), relation)
        return {"status": "recorded", "disposition_ref": _ref(saved), "decision": observed["decision"], "created": True}

    try:
        return store.transact(
            operation_id,
            {"completed_run_ref": completed_run_ref, "request_id": request_id},
            save,
            mission_id=mission_id,
        )
    except PostScopeChanged:
        return None


def _exact_ref_shape(pointer: object, kind: str) -> bool:
    return (
        type(pointer) is dict
        and set(pointer) == {"kind", "id", "version"}
        and pointer["kind"] == kind
        and type(pointer["id"]) is str
        and type(pointer["version"]) is int
        and pointer["version"] >= 1
    )


class PostScopeChanged(StateConflict):
    """The exact late-request observation changed before it was saved."""
