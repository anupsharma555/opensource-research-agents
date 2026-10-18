"""Exact local operation identity for a delayed mission tool segment.

The executor owns dispatch and acceptance. This record links the original
model tool call to that work before the callback can begin; its read view uses
saved executor/capacity evidence rather than claiming an upstream job API.
"""

from __future__ import annotations

import hashlib
import json

from ora.coordination.board import ref
from ora.coordination.executor import ConcurrentExecutor, WorkResult
from ora.evidence.hit_use import hit_allows_reinspection
from ora.evidence.intake import IntakeService, detected_content_type
from ora.retrieval.catalog_tools import _navigation_result
from ora.state import StateConflict, StateStore
from ora.state.store import Transaction, canonical

from .context import ContextError, WorkingContext
from .document_tools import supported_public_document_type

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


def reconcile_late_navigation(
    executor: ConcurrentExecutor, control: dict, work: dict,
) -> dict | None:
    """Adopt a saved page after shutdown lost the waiting callback.

    A dispatch marker by itself is insufficient. The exact navigation must
    already be retained, and the executor performs fenced acceptance without
    invoking the fetch again. ``None`` leaves unresolved effects uncertain.
    """
    body = control["body"]
    pointer = body.get("tool_operation_ref")
    if (
        body.get("phase") not in {"pending_tool", "waiting"}
        or body.get("work_id") is None
        or (body.get("tool_call") or {}).get("name") != "navigate_venue"
        or pointer != operation_ref(body["work_id"])
        or work["body"].get("phase") != "pending"
    ):
        return None
    spec = executor._spec_from(work)
    if (
        spec.work_id != body["work_id"]
        or spec.mission_id != body["mission_id"]
        or spec.run_id != body["run_id"]
        or spec.actor_id != body["actor_id"]
        or spec.proposal_ref["id"] != body["task_id"]
    ):
        raise StateConflict("Late navigation differs from the pending task")
    operation = read_navigation_operation(
        executor.store, pointer, mission_id=spec.mission_id,
        actor_id=spec.actor_id, task_id=spec.proposal_ref["id"],
    )
    if operation is None or operation["status"] != "fetch_recorded":
        return None
    navigation_ref = operation["navigation_ref"]
    if navigation_ref is None or operation["result_ref"] is not None:
        return None
    saved_operation = executor.store.get(**pointer)
    original_control = executor.store.get(**saved_operation["body"]["control_ref"])
    if (
        body.get("tool_call") != original_control["body"].get("tool_call")
        or body.get("call_ref") != saved_operation["body"]["call_ref"]
        or body.get("response_ref") != saved_operation["body"]["response_ref"]
    ):
        raise StateConflict("Late navigation call differs from the original response")
    navigation = executor.store.get(**navigation_ref)
    if navigation is None or navigation["body"].get("schema") != "ora.venue_navigation_attempt.v1":
        raise StateConflict("Late navigation's saved result is unavailable")
    args = body["tool_call"]["arguments"]
    if not isinstance(args, dict):
        raise StateConflict("Late navigation has invalid saved arguments")
    result = _navigation_result(
        executor.store, navigation,
        args.get("link_offset", 0), args.get("link_limit", 25),
    )
    result["operation_ref"] = pointer
    return executor.accept_saved_navigation_result(
        spec,
        WorkResult(
            disposition="continue_task",
            payload={
                "tool_call_id": body["tool_call"]["id"],
                "name": "navigate_venue",
                "result": result,
            },
            output_refs=(navigation_ref,),
        ),
        navigation_ref,
    )


def reconcile_late_navigation_document(
    executor: ConcurrentExecutor, intake: IntakeService, control: dict, work: dict,
) -> dict | None:
    """Finish only a local inspection whose original operation already saved its copy.

    The uncertain callback may have stopped between copy and source-use commits.
    Reuse its exact operation ID against retained navigation bytes; never repeat
    the venue fetch or replay an unrelated uncertain tool.
    """
    body = control["body"]
    requested = body.get("tool_call") or {}
    if (
        body.get("phase") not in {"pending_tool", "waiting"}
        or not body.get("work_id")
        or requested.get("name") != "inspect_navigation_document"
        or work["body"].get("phase") != "pending"
    ):
        return None
    spec = executor._spec_from(work)
    if (
        spec.handler_id != "mission-tool"
        or spec.work_id != body["work_id"]
        or spec.mission_id != body["mission_id"]
        or spec.run_id != body["run_id"]
        or spec.actor_id != body["actor_id"]
        or spec.proposal_ref["id"] != body["task_id"]
        or body.get("call_ref") != {
            "kind": "agent_call", "id": spec.call_id, "version": spec.call_version,
        }
    ):
        raise StateConflict("Late document inspection differs from the pending task")
    capacity = executor.capacity.get(executor._capacity_id(spec))
    proposal = executor.store.get(**spec.proposal_ref)
    if (
        capacity is None or capacity["phase"] != "uncertain"
        or capacity.get("cancel_requested")
        or proposal is None
        or ref(proposal) != spec.proposal_ref
        or proposal["body"].get("status") != "open"
    ):
        return None
    current = next(
        (item for item in executor.board.open_work(spec.mission_id)
         if item["proposal_ref"] == spec.proposal_ref), None,
    )
    if current is None:
        return None
    claim = current["current_claim"]
    if claim is not None:
        segments = work["body"].get("runtime_segments") or []
        claim_record = executor.store.get(
            "board_claim", f"{spec.mission_id}:{spec.proposal_ref['id']}:{claim['generation']}",
        )
        if (
            claim["owner"] != spec.actor_id
            or not segments or claim_record is None
            or claim_record["body"].get("proposal_ref") != spec.proposal_ref
            or claim_record["created_at"] > segments[-1]["recorded_at"]
        ):
            return None  # A replacement claim may own different work.
    response_ref = body.get("response_ref")
    response = executor.store.get(**response_ref) if isinstance(response_ref, dict) else None
    if (
        response is None or response["body"].get("status") != "tool_requested"
        or response["body"].get("call_ref") != body["call_ref"]
        or response["body"].get("attempt_id") != spec.attempt_id
    ):
        raise StateConflict("Late document inspection lacks its original model response")
    raw = json.loads(executor.store.read_blob(response["body"]["blob"]["sha256"]))
    calls = raw.get("tool_calls") if isinstance(raw, dict) else None
    function = (
        calls[0].get("function", {})
        if isinstance(calls, list) and len(calls) == 1 and isinstance(calls[0], dict)
        else {}
    )
    try:
        arguments = json.loads(function.get("arguments", ""))
    except (TypeError, ValueError):
        arguments = None
    if (
        not isinstance(calls, list) or len(calls) != 1
        or not isinstance(calls[0], dict)
        or calls[0].get("id") != requested.get("id")
        or function.get("name") != requested["name"]
        or arguments != requested.get("arguments")
    ):
        raise StateConflict("Late document inspection changed its model tool call")
    navigation_ref = arguments.get("navigation_ref") if isinstance(arguments, dict) else None
    if (
        not isinstance(navigation_ref, dict)
        or set(navigation_ref) != {"kind", "id", "version"}
        or navigation_ref.get("kind") != "venue_navigation"
        or not isinstance(navigation_ref.get("id"), str)
        or type(navigation_ref.get("version")) is not int
        or navigation_ref not in body.get("navigation_refs", [])
    ):
        raise StateConflict("Late document inspection lacks its task navigation")
    operation_id = f"mission-navigation-document:{spec.work_id}"
    copy = executor.store.resolve(
        "source_copy.navigation", f"{navigation_ref['id']}:{navigation_ref['version']}"
    )
    if copy is None:
        return None  # No saved local effect proves the callback reached intake.
    if (
        copy["body"].get("navigation_ref") != navigation_ref
        or copy["body"].get("intake_operation_id") != operation_id
        or copy["body"].get("mission_id") != spec.mission_id
    ):
        return None  # Another operation owns this saved copy.
    result = intake.inspect_navigation_document(
        operation_id, navigation_ref, mission_id=spec.mission_id,
        run_id=spec.run_id, actor_id=spec.actor_id,
    )
    copy_ref = result.get("copy_ref")
    if copy_ref is not None and copy_ref != ref(copy):
        raise StateConflict("Recovered inspection returned a different source copy")
    return executor.accept_saved_inspection_result(
        spec,
        WorkResult(
            disposition="continue_task",
            payload={
                "tool_call_id": requested["id"], "name": requested["name"],
                "result": result,
            },
            output_refs=(copy_ref,) if copy_ref else (),
        ),
        navigation_ref,
    )


def reconcile_late_public_document_fetch(
    executor: ConcurrentExecutor, intake: IntakeService, control: dict, work: dict,
) -> dict | None:
    """Finish a saved browser HTML fetch without repeating its external request."""
    body = control["body"]
    requested = body.get("tool_call") or {}
    if (
        body.get("phase") not in {"pending_tool", "waiting"}
        or requested.get("name") != "fetch_source"
        or work["body"].get("phase") != "pending"
    ):
        return None
    spec = executor._spec_from(work)
    if (
        spec.handler_id != "mission-tool"
        or spec.work_id != body.get("work_id")
        or any(body.get(key) != value for key, value in (
            ("mission_id", spec.mission_id), ("run_id", spec.run_id),
            ("actor_id", spec.actor_id), ("task_id", spec.proposal_ref["id"]),
        ))
        or body.get("call_ref") != {"kind": "agent_call", "id": spec.call_id, "version": spec.call_version}
    ):
        raise StateConflict("Late public-document fetch differs from the pending task")
    capacity = executor.capacity.get(executor._capacity_id(spec))
    if capacity is None or capacity["phase"] != "uncertain":
        return None
    response_ref = body.get("response_ref")
    response = executor.store.get(**response_ref) if isinstance(response_ref, dict) else None
    if (
        response is None or response["body"].get("status") != "tool_requested"
        or response["body"].get("call_ref") != body["call_ref"]
        or response["body"].get("attempt_id") != spec.attempt_id
    ):
        raise StateConflict("Late public-document fetch lacks its original tool request")
    raw = json.loads(executor.store.read_blob(response["body"]["blob"]["sha256"]))
    calls = raw.get("tool_calls") if isinstance(raw, dict) else None
    function = (
        calls[0].get("function", {})
        if isinstance(calls, list) and len(calls) == 1 and isinstance(calls[0], dict)
        else {}
    )
    try:
        arguments = json.loads(function.get("arguments", ""))
    except (TypeError, ValueError):
        arguments = None
    if (
        not isinstance(calls, list) or len(calls) != 1
        or calls[0].get("id") != requested.get("id")
        or function.get("name") != "fetch_source"
        or arguments != requested.get("arguments")
    ):
        raise StateConflict("Late public-document fetch changed its model tool call")
    hit_id = arguments.get("hit_id") if isinstance(arguments, dict) else None
    fetch_operation = f"mission-public-document-fetch:{spec.work_id}"
    document = executor.store.get("public_document_fetch", f"public-document-fetch:{fetch_operation}")
    if document is None:
        return None
    document_ref = ref(document)
    hit_ref = document["body"].get("hit_ref")
    if (
        document["body"].get("operation_id") != fetch_operation
        or document["body"].get("mission_id") != spec.mission_id
        or document["body"].get("run_id") != spec.run_id
        or document["body"].get("actor_id") != spec.actor_id
        or document["body"].get("task_id") != spec.proposal_ref["id"]
        or not isinstance(hit_ref, dict) or hit_ref.get("id") != hit_id
    ):
        raise StateConflict("Late public-document fetch lost its exact hit")
    current = next(
        (item for item in executor.board.open_work(spec.mission_id)
         if item["proposal_ref"] == spec.proposal_ref), None,
    )
    claim = current["current_claim"] if current else None
    if claim is not None:
        segments = work["body"].get("runtime_segments") or []
        claim_record = executor.store.get(
            "board_claim", f"{spec.mission_id}:{spec.proposal_ref['id']}:{claim['generation']}",
        )
        if (
            claim["owner"] != spec.actor_id or not segments or claim_record is None
            or claim_record["body"].get("proposal_ref") != spec.proposal_ref
            or claim_record["created_at"] > segments[-1]["recorded_at"]
        ):
            return None
    proposal = executor.store.get(spec.proposal_ref["kind"], spec.proposal_ref["id"])
    stale = (
        capacity.get("cancel_requested") or proposal is None
        or ref(proposal) != spec.proposal_ref
        or proposal["body"].get("status") != "open"
    )
    hit = executor.store.get(**hit_ref)
    if hit is None or not hit_allows_reinspection(executor.store, hit):
        stale = True  # Preserve a revoked copy without granting it again on restart.
    try:
        browser_response = WorkingContext(executor.store).browser_hit_response(
            hit, mission_id=spec.mission_id, actor_id=spec.actor_id,
            task_id=spec.proposal_ref["id"],
        ) if hit else None
    except ContextError:
        browser_response = None
    if browser_response != document["body"].get("response_ref"):
        stale = True
    result = {
        "status": "source_unavailable", "hit_id": hit_id,
        "document_ref": document_ref,
    }
    copy_ref = None
    if not stale:
        content = executor.store.read_blob(document["body"]["blob"]["sha256"])
        if not supported_public_document_type(detected_content_type(content, document["body"]["content_type"])):
            result["classification"] = "unsupported_public_document_type"
        else:
            inspected = intake.inspect_public_document(
                f"mission-public-document-inspect:{spec.work_id}", document_ref,
                mission_id=spec.mission_id, run_id=spec.run_id,
                actor_id=spec.actor_id, task_id=spec.proposal_ref["id"],
            )
            copy_ref = inspected["copy_ref"]
            result["classification"] = inspected["classification"]
            if copy_ref:
                result.update(
                    status="inspected", copy_ref=copy_ref,
                    source_id=copy_ref["id"], source_version=copy_ref["version"],
                    source_url=document["body"]["final_url"],
                    next_action="Read exact document text with read_source_window before using its contents",
                )
            else:
                result["model_input_status"] = inspected["model_input_status"]
    return executor.accept_saved_public_document_result(
        spec,
        WorkResult(
            disposition="continue_task",
            payload={"tool_call_id": requested["id"], "name": "fetch_source", "result": result},
            output_refs=(copy_ref,) if copy_ref else (),
        ),
        document_ref,
    )
