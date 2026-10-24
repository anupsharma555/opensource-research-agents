"""Read model intent settlement from one durable state snapshot.

Evaluation controllers use this to stop new dispatch without treating a
concurrent receipt or work transition as conflicting evidence. The executor
remains the authority for acceptance and uncertain external effects.
"""

from __future__ import annotations

import json
import sqlite3


def unsettled_peer_intents(store, actor_ids: set[str]) -> tuple[list[dict], list[dict]]:
    """Return runnable and unsafe intents from one SQLite read transaction.

    A superseded pre-dispatch intent has no external effect. A pending intent
    with an exact dispatch marker may still settle. Missing or conflicting
    lineage remains unsafe; this reader never repairs durable state.
    """
    location = f"{store.db_path.as_uri()}?mode=ro"
    db = sqlite3.connect(location, uri=True, isolation_level=None)
    try:
        db.execute("BEGIN")
        records = {}
        for kind in ("executor_work", "model_request_intent",
                     "model_dispatch_attempt", "model_response_receipt",
                     "capacity_operation"):
            rows = db.execute(
                "SELECT r.id, r.version, r.body FROM records r "
                "JOIN heads h USING(kind,id,version) WHERE r.kind=?", (kind,)
            ).fetchall()
            records[kind] = {
                row[0]: {"id": row[0], "version": row[1], "body": json.loads(row[2])}
                for row in rows
            }
        db.commit()
    finally:
        db.close()

    works = list(records["executor_work"].values())
    active, unsafe = [], []
    for intent in records["model_request_intent"].values():
        body = intent["body"]
        if body.get("instance_id") not in actor_ids:
            continue
        call_ref = body.get("call_ref")
        matches = [work for work in works
                   if work["body"].get("spec", {}).get("kind") == "model"
                   and work["body"]["spec"].get("call_id") == (
                       call_ref.get("id") if isinstance(call_ref, dict) else None
                   )
                   and work["body"]["spec"].get("call_version") == (
                       call_ref.get("version") if isinstance(call_ref, dict) else None
                   )]
        if len(matches) != 1:
            unsafe.append(intent)
            continue
        work_body = matches[0]["body"]
        pointer = {"kind": "model_request_intent", "id": intent["id"],
                   "version": intent["version"]}
        marker = records["model_dispatch_attempt"].get(f"model-dispatch:{intent['id']}")
        receipt = records["model_response_receipt"].get(f"model-response:{intent['id']}")
        if receipt is not None:
            receipt_pointer = {"kind": "model_response_receipt",
                               "id": receipt["id"], "version": receipt["version"]}
            if (marker is None
                    or marker["body"].get("status") != "captured"
                    or marker["body"].get("intent_ref") != pointer
                    or marker["body"].get("receipt_ref") != receipt_pointer
                    or receipt["body"].get("intent_ref") != pointer
                    or any(receipt["body"].get(key) != body.get(key)
                           for key in ("request_sha256", "call_ref", "attempt_id",
                                       "task_id", "mission_id"))):
                unsafe.append(intent)
            elif work_body.get("phase") == "pending":
                active.append(intent)
            elif work_body.get("phase") != "completed":
                unsafe.append(intent)
            continue
        if marker is not None:
            if marker["body"].get("status") == "blocked_before_external_effect":
                spec = work_body.get("spec", {})
                capacities = [item for item in records["capacity_operation"].values()
                              if item["body"].get("call_id") == spec.get("call_id")
                              and item["body"].get("attempt_id") == spec.get("attempt_id")
                              and item["body"].get("mission_id") == spec.get("mission_id")
                              and item["body"].get("task_id") == (
                                  spec.get("proposal_ref") or {}).get("id")
                              and (item["body"].get("request_token_estimate") or {}).get(
                                  "intent_ref") == pointer]
                cap_body = capacities[0]["body"] if len(capacities) == 1 else {}
                attempts = cap_body.get("attempts", [])
                last = attempts[-1] if attempts else {}
                settled_zero = (
                    cap_body.get("phase") == "failed"
                    and cap_body.get("reservation") is False
                    and cap_body.get("external_active") is False
                    and cap_body.get("owner") is None
                    and last.get("status") == "terminal_no_effect"
                    and last.get("charged_tokens") == 0
                    and last.get("usage_known") is True
                )
                unresolved_local_settlement = (
                    cap_body.get("phase") in {"dispatched", "uncertain"}
                    and cap_body.get("reservation") is True
                    and last.get("status") in {"dispatched", "uncertain"}
                )
                exact_block = (
                    marker["body"].get("intent_ref") == pointer
                    and marker["body"].get("call_ref") == call_ref
                    and marker["body"].get("mission_id") == body.get("mission_id")
                    and marker["body"].get("request_sha256") == body.get("request_sha256")
                    and spec.get("actor_id") == body.get("instance_id")
                    and spec.get("attempt_id") == body.get("attempt_id")
                )
                if exact_block and (
                    work_body.get("phase") == "pending"
                    and (settled_zero or unresolved_local_settlement)
                ):
                    active.append(intent)
                elif (exact_block and settled_zero
                      and work_body.get("phase") == "superseded"
                      and work_body.get("superseded_intent_ref") == pointer
                      and work_body.get("status_basis") == "local_model_admission_blocked"):
                    pass
                else:
                    unsafe.append(intent)
                continue
            if (work_body.get("phase") == "pending"
                    and marker["body"].get("status") == "attempted_external_effect"
                    and marker["body"].get("intent_ref") == pointer
                    and marker["body"].get("request_sha256")
                    == body.get("request_sha256")):
                active.append(intent)
            else:
                unsafe.append(intent)
            continue
        if (work_body.get("phase") == "superseded"
                and work_body.get("superseded_intent_ref") == pointer):
            continue
        if work_body.get("phase") == "pending":
            active.append(intent)
        else:
            unsafe.append(intent)
    return active, unsafe
