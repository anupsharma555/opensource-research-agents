"""Exact local operation identity for a delayed mission tool segment.

The executor owns dispatch and acceptance. This record links the original
model tool call to that work before the callback can begin; its read view uses
saved executor/capacity evidence rather than claiming an upstream job API.
"""

from __future__ import annotations

import hashlib
import json

from ora.coordination.board import ref
from ora.state import StateConflict, StateStore
from ora.state.store import Transaction, canonical

KIND = "mission_tool_operation"


def operation_ref(work_id: str) -> dict:
    return {"kind": KIND, "id": f"mission-tool-operation:{work_id}", "version": 1}


def save_navigation_operation_tx(
    tx: Transaction, *, control: dict, work_ref: dict, spec: dict, capacity_operation_id: str
) -> dict:
    """Save the local operation in the same transaction as runnable work."""
    body = control["body"]
    requested = body["tool_call"]
    if requested is None or requested["name"] != "navigate_venue":
        raise StateConflict("Only a saved venue navigation call has this operation")
    if body.get("tool_operation_ref") != operation_ref(spec["work_id"]):
        raise StateConflict("Tool operation differs from the submitted control")
    response = tx.get(**body["response_ref"])
    call = tx.get(**body["call_ref"])
    raw = (
        json.loads(tx.store.read_blob(response["body"]["blob"]["sha256"]))
        if response is not None
        else {}
    )
    calls = raw.get("tool_calls") if isinstance(raw, dict) else None
    function = (
        calls[0].get("function", {})
        if isinstance(calls, list) and len(calls) == 1 and isinstance(calls[0], dict)
        else {}
    )
    try:
        returned_arguments = json.loads(function.get("arguments", ""))
    except (TypeError, ValueError):
        returned_arguments = None
    if (
        response is None
        or response["body"].get("status") != "tool_requested"
        or response["body"].get("call_ref") != body["call_ref"]
        or response["body"].get("attempt_id") != spec["attempt_id"]
        or not isinstance(calls, list)
        or len(calls) != 1
        or not isinstance(calls[0], dict)
        or calls[0].get("id") != requested["id"]
        or function.get("name") != "navigate_venue"
        or returned_arguments != requested["arguments"]
        or call is None
        or call["id"] != spec["call_id"]
        or call["version"] != spec["call_version"]
        or call["body"].get("attempt_id") != spec["attempt_id"]
        or spec["proposal_ref"]["id"] != body["task_id"]
        or spec["actor_id"] != body["actor_id"]
        or spec["mission_id"] != body["mission_id"]
        or spec["run_id"] != body["run_id"]
    ):
        raise StateConflict("Tool operation lineage changed before submission")
    saved = tx.put(
        KIND,
        operation_ref(spec["work_id"])["id"],
        {
            "schema": "ora.mission_tool_operation.v1",
            "mission_id": body["mission_id"],
            "run_id": body["run_id"],
            "actor_id": body["actor_id"],
            "task_id": body["task_id"],
            "control_ref": ref(control),
            "call_ref": body["call_ref"],
            "response_ref": body["response_ref"],
            "tool_call_id": requested["id"],
            "tool_name": requested["name"],
            "arguments_sha256": hashlib.sha256(
                canonical(requested["arguments"]).encode()
            ).hexdigest(),
            "attempt_id": spec["attempt_id"],
            "work_ref": work_ref,
            "work_id": spec["work_id"],
            "capacity_operation_id": capacity_operation_id,
            "configuration_ref": spec["configuration_ref"],
            "operation_id": f"mission-venue-navigation:{spec['work_id']}",
            "prepared_at": tx.store.now(),
        },
    )
    return ref(saved)


def read_navigation_operation(
    store: StateStore, pointer: dict, *, mission_id: str, actor_id: str, task_id: str
) -> dict | None:
    """Read only matching saved scope and actual work/capacity transitions."""
    if not isinstance(pointer, dict) or pointer.get("kind") != KIND:
        return None
    record = store.get(pointer.get("kind"), pointer.get("id"), pointer.get("version"))
    if record is None or ref(record) != pointer:
        return None
    body = record["body"]
    if any(
        body.get(key) != value
        for key, value in (
            ("mission_id", mission_id),
            ("actor_id", actor_id),
            ("task_id", task_id),
        )
    ):
        return None
    original_control = store.get(**body["control_ref"])
    if (
        original_control is None
        or original_control["body"].get("tool_operation_ref") != pointer
        or original_control["body"].get("response_ref") != body["response_ref"]
        or original_control["body"].get("call_ref") != body["call_ref"]
        or original_control["body"].get("tool_call", {}).get("id") != body["tool_call_id"]
        or original_control["body"].get("tool_call", {}).get("name") != "navigate_venue"
        or hashlib.sha256(
            canonical(original_control["body"]["tool_call"]["arguments"]).encode()
        ).hexdigest()
        != body["arguments_sha256"]
    ):
        return None
    work = store.get(body["work_ref"]["kind"], body["work_ref"]["id"])
    if work is None or work["body"].get("spec", {}).get("work_id") != body["work_id"]:
        return None
    spec = work["body"].get("spec", {})
    if (
        spec.get("mission_id") != mission_id
        or spec.get("run_id") != body["run_id"]
        or spec.get("actor_id") != actor_id
        or spec.get("proposal_ref", {}).get("id") != task_id
        or spec.get("call_id") != body["call_ref"]["id"]
        or spec.get("call_version") != body["call_ref"]["version"]
        or spec.get("attempt_id") != body["attempt_id"]
        or spec.get("configuration_ref") != body["configuration_ref"]
    ):
        return None
    response = store.get(**body["response_ref"])
    if (
        response is None
        or response["body"].get("status") != "tool_requested"
        or response["body"].get("call_ref") != body["call_ref"]
        or response["body"].get("attempt_id") != body["attempt_id"]
    ):
        return None
    raw_arguments = original_control["body"]["tool_call"]["arguments"]
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
    expected_request = {
        "mission_id": mission_id,
        "run_id": body["run_id"],
        "actor_id": actor_id,
        "catalog_ref": arguments.get("catalog_ref"),
        "link_index": arguments.get("link_index", 0),
        "max_bytes": 2 * 1024 * 1024,
    }
    capacity = store.get("capacity_operation", body["capacity_operation_id"])
    status = "prepared"
    capacity_ref = None
    if capacity is not None:
        if any(
            capacity["body"].get(k) != v
            for k, v in (
                ("mission_id", mission_id),
                ("run_id", body["run_id"]),
                ("task_id", task_id),
                ("attempt_id", body["attempt_id"]),
                ("call_id", body["call_ref"]["id"]),
            )
        ):
            return None
        capacity_ref = ref(capacity)
        phase = capacity["body"].get("phase")
        if phase in {"admitted", "dispatched"}:
            status = "callback_dispatched" if phase == "dispatched" else "admitted"
        elif phase in {"uncertain", "failed", "waiting"}:
            status = phase
    key = hashlib.sha256(
        canonical({"mission_id": mission_id, "operation_id": body["operation_id"]}).encode()
    ).hexdigest()[:32]
    dispatch = store.get("venue_navigation_dispatch", f"venue-navigation-dispatch-{key}")
    navigation = store.get("venue_navigation", f"venue-navigation-{key}")
    requests = [item["body"].get("request") for item in (dispatch, navigation) if item]
    if requests and (
        expected_request["catalog_ref"] is None
        or any(
            not isinstance(item, dict) or any(item.get(k) != v for k, v in expected_request.items())
            for item in requests
        )
        or len({item.get("requested_url") for item in requests}) != 1
        or any(not isinstance(item.get("requested_url"), str) for item in requests)
    ):
        return None
    if navigation is not None and (
        dispatch is None or navigation["body"].get("dispatch_ref") != ref(dispatch)
    ):
        return None
    if dispatch is not None and status in {"prepared", "admitted", "callback_dispatched"}:
        status = "fetch_dispatch_recorded"
    if navigation is not None and work["body"].get("phase") == "pending":
        status = "fetch_recorded"
    if work["body"].get("phase") == "result_unaccepted":
        status = "result_unaccepted"
    artifact = (
        store.get(**work["body"]["result_ref"])
        if work["body"].get("phase") == "completed" and work["body"].get("result_ref")
        else None
    )
    outcome = None
    if work["body"].get("phase") == "completed" and artifact is None:
        status = "inconsistent_saved_result"
    if artifact is not None:
        payload = artifact["body"].get("payload") or {}
        if (
            artifact["kind"] != "executor_artifact"
            or artifact["body"].get("mission_id") != mission_id
            or artifact["body"].get("work_id") != body["work_id"]
            or payload.get("tool_call_id") != body["tool_call_id"]
            or payload.get("name") != "navigate_venue"
            or not isinstance(payload.get("result"), dict)
            or payload["result"].get("operation_ref") != pointer
        ):
            return None
        outcome = payload["result"].get("status")
        if navigation is not None:
            if payload["result"].get("navigation_ref") != ref(navigation):
                return None
            status = "accepted"
        elif outcome == "uncertain_dispatch" and dispatch is not None:
            status = "uncertain_dispatch"
        elif outcome == "rejected" and dispatch is None:
            status = "rejected_before_fetch"
        else:
            status = "inconsistent_saved_result"
    tool_result = store.get("context_tool_result", f"tool-result:{body['response_ref']['id']}")
    tool_result_ref = None
    if (
        tool_result is not None
        and artifact is not None
        and tool_result["body"].get("tool_call_id") == body["tool_call_id"]
        and tool_result["body"].get("response_ref") == body["response_ref"]
    ):
        message = json.loads(store.read_blob(tool_result["body"]["blob"]["sha256"]))
        if (
            message.get("name") == "navigate_venue"
            and message.get("tool_call_id") == body["tool_call_id"]
            and message.get("content") == canonical(artifact["body"]["payload"]["result"])
        ):
            tool_result_ref = ref(tool_result)
    return {
        "operation_ref": pointer,
        "operation_id": body["operation_id"],
        "status": status,
        "work_ref": ref(work),
        "capacity_ref": capacity_ref,
        "dispatch_ref": ref(dispatch) if dispatch else None,
        "navigation_ref": ref(navigation) if navigation else None,
        "result_ref": work["body"].get("result_ref"),
        "tool_result_ref": tool_result_ref,
        "tool_outcome": outcome,
        "partial_refs": [],
    }
