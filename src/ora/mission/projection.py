"""Project exact mission records into the same saved Atlas/Codex snapshot."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone

from ora.contracts.models import ResearchSnapshot
from ora.coordination.board import ref
from ora.evidence.intake import extract_copy_blocks
from ora.state import StateConflict, StateStore
from ora.state.inspection import load_snapshot, save_snapshot
from ora.state.store import StateError, canonical

from .call_inspection import (
    inspect_provider_token_quota,
    inspect_provider_tool_validation,
    provider_token_quota_explanation,
)
from .latency import (
    inspect_latency,
    read_due_review,
    read_expectation,
    read_review_delivery_response,
)
from .report_lineage import (
    accepted_report as _accepted_report,
)
from .report_lineage import (
    report_text_access,
)

PROJECTION_VERSION = "stage4-atlas-v21"

_REPORT_TEXT_UNAVAILABLE = "Report text unavailable because source-use permission changed."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _records(store: StateStore, kind: str, mission_id: str) -> list[dict]:
    after, output = "", []
    while True:
        page = store.records(kind, limit=500, after_id=after)
        output.extend(item for item in page if item["body"].get("mission_id") == mission_id)
        if len(page) < 500:
            return output
        after = page[-1]["id"]


def _task_display_numbers(store: StateStore, mission_id: str, root_task_id: str) -> dict[str, int]:
    """Number exact task-creation events, including work not yet visible in a control."""
    numbers = {root_task_id: 1}
    cursor = 0
    while True:
        page = store.events(mission_id, after=cursor, limit=500)
        for event in page:
            if event["type"] != "proposal_created":
                continue
            pointer = event["body"].get("proposal_ref")
            if (
                not isinstance(pointer, dict)
                or set(pointer) != {"kind", "id", "version"}
                or pointer["kind"] != "board_proposal"
                or pointer["version"] != 1
                or pointer["id"] in numbers
            ):
                continue
            created = store.get(**pointer)
            if created is None or created["body"].get("mission_id") != mission_id:
                continue
            numbers[pointer["id"]] = len(numbers) + 1
        if len(page) < 500:
            return numbers
        cursor = page[-1]["sequence"]


def _host_diagnostic_view(store: StateStore, item: dict, mission_id: str) -> dict:
    """Keep an elapsed review interval distinct from a host failure."""
    body = item["body"]
    view = {
        "id": item["id"], "kind": body["error_class"], "status": "host_error",
        "message": body["message"], "occurredAt": body["recorded_at"],
    }
    if body.get("category") == "latency_review_due":
        current = read_due_review(store, mission_id, body.get("actor_id"), body.get("task_id"))
        status = "historical_review"
        if current and ref(current) == ref(item):
            delivered = read_review_delivery_response(store, item)
            status = "notice_delivered" if delivered else "review_due"
            if delivered:
                view["message"] += (
                    " This notice was included in a model call that returned successfully; "
                    "no review decision is inferred."
                )
        view.update({
            "kind": "latency_review", "taskId": body["task_id"],
            "status": status,
        })
    return view


def _capacity_wait(store: StateStore, control: dict) -> dict | None:
    """Resolve a saved wait only for the exact operation this task still owns."""
    body = control["body"]
    pointer = body.get("capacity_wait_ref")
    if (body.get("phase") not in {"pending_model", "pending_tool"}
            or not isinstance(pointer, dict) or pointer.get("kind") != "capacity_operation"):
        return None
    saved = store.get(**pointer)
    cap = saved["body"] if saved else {}
    call = body.get("call_ref") or {}
    if (cap.get("phase") != "waiting" or cap.get("mission_id") != body["mission_id"]
            or cap.get("task_id") != body["task_id"] or cap.get("call_id") != call.get("id")):
        return None
    return {"reference": pointer, "reason": body.get("waiting_reason"),
            "until": datetime.fromtimestamp(cap["wake_at"], timezone.utc).isoformat()
            if isinstance(cap.get("wake_at"), (int, float)) else None}


def _context_preparation_wait(store: StateStore, control: dict) -> str | None:
    """Name only the exact next prepared call stalled before provider dispatch."""
    body = control["body"]
    sequence = body.get("sequence")
    if (body.get("phase") != "waiting" or body.get("work_id") is not None
            or not str(body.get("waiting_reason", "")).startswith("ContextOverflow:")
            or type(sequence) is not int or sequence < 0):
        return None
    parts = [body["run_id"], body["actor_id"]]
    if body.get("scope_required") is False:
        parts.append(control["id"])
    parts.append(sequence + 1)
    suffix = hashlib.sha256(canonical(parts).encode()).hexdigest()[:32]
    call_id, attempt_id = f"call-{suffix}", f"attempt-{suffix}"
    call_ref = {"kind": "agent_call", "id": call_id, "version": 1}
    call = store.get(**call_ref)
    attempt = store.get("agent_attempt", attempt_id)
    budget = store.get("mission_call_budget", call_id)
    if (call is None or attempt is None or budget is None
            or call["body"].get("attempt_id") != attempt_id
            or call["body"].get("mission_id") != body["mission_id"]
            or call["body"].get("task_id") != body["task_id"]
            or call["body"].get("instance_id") != body["actor_id"]
            or attempt["body"].get("status") != "running"
            or attempt["body"].get("call_ref") != call_ref
            or attempt["body"].get("mission_id") != body["mission_id"]
            or attempt["body"].get("run_id") != body["run_id"]
            or attempt["body"].get("task_id") != body["task_id"]
            or attempt["body"].get("agentId") != body["actor_id"]
            or budget["body"].get("call_ref") != call_ref
            or budget["body"].get("mission_id") != body["mission_id"]
            or budget["body"].get("run_id") != body["run_id"]):
        return None
    intent_id = f"model-intent:{call_id}:1"
    if (store.get("model_request_intent", intent_id)
            or store.get("model_dispatch_attempt", f"model-dispatch:{intent_id}")
            or store.get("model_response_receipt", f"model-response:{intent_id}")):
        return None
    return attempt_id


def _historical_preparation_failure(store: StateStore, diagnostic: dict, run_id: str) -> str | None:
    """Attribute one saved local failure only to its exact prepared attempt."""
    body = diagnostic["body"]
    if (body.get("category") != "local_context_preparation"
            or body.get("error_class") != "ContextOverflow"
            or body.get("run_id") != run_id):
        return None
    pointers = [body.get(key) for key in ("control_ref", "call_ref", "attempt_ref", "budget_ref")]
    if any(not isinstance(pointer, dict) or set(pointer) != {"kind", "id", "version"}
           for pointer in pointers):
        return None
    control_ref, call_ref, attempt_ref, budget_ref = pointers
    if [pointer["kind"] for pointer in pointers] != [
        "mission_task_control", "agent_call", "agent_attempt", "mission_call_budget"
    ]:
        return None
    control, call, attempt, budget = (store.get(**pointer) for pointer in pointers)
    if any(item is None for item in (control, call, attempt, budget)):
        return None
    control_body = control["body"]
    sequence = control_body.get("sequence")
    if (control_body.get("phase") != "ready_model"
            or type(sequence) is not int or sequence < 0
            or control_body.get("run_id") != run_id
            or control_body.get("mission_id") != body.get("mission_id")
            or control_body.get("task_id") != body.get("task_id")
            or control_body.get("actor_id") != body.get("actor_id")):
        return None
    parts = [run_id, body["actor_id"]]
    if control_body.get("scope_required") is False:
        parts.append(control["id"])
    parts.append(sequence + 1)
    suffix = hashlib.sha256(canonical(parts).encode()).hexdigest()[:32]
    if (call_ref != {"kind": "agent_call", "id": f"call-{suffix}", "version": 1}
            or attempt_ref["id"] != f"attempt-{suffix}"
            or budget_ref["id"] != call_ref["id"]
            or call["body"].get("attempt_id") != attempt_ref["id"]
            or call["body"].get("mission_id") != body["mission_id"]
            or call["body"].get("task_id") != body["task_id"]
            or call["body"].get("instance_id") != body["actor_id"]
            or attempt["body"].get("status") != "running"
            or attempt["body"].get("call_ref") != call_ref
            or attempt["body"].get("mission_id") != body["mission_id"]
            or attempt["body"].get("run_id") != run_id
            or attempt["body"].get("task_id") != body["task_id"]
            or attempt["body"].get("agentId") != body["actor_id"]
            or budget["body"].get("call_ref") != call_ref
            or budget["body"].get("mission_id") != body["mission_id"]
            or budget["body"].get("run_id") != run_id):
        return None
    return attempt_ref["id"]


def _source_free_preparation_failure(store: StateStore, diagnostic: dict, run_id: str) -> str | None:
    """Attribute a failed local preflight only to its saved call and budget."""
    body = diagnostic["body"]
    if (body.get("category") != "local_context_source_free_recovery"
            or body.get("error_class") != "ContextOverflow"
            or body.get("run_id") != run_id):
        return None
    pointers = [body.get(key) for key in ("call_ref", "attempt_ref", "budget_ref")]
    if (any(not isinstance(pointer, dict) or set(pointer) != {"kind", "id", "version"}
            for pointer in pointers)
            or [pointer["kind"] for pointer in pointers] != [
                "agent_call", "agent_attempt", "mission_call_budget"
            ]):
        return None
    call_ref, attempt_ref, budget_ref = pointers
    call, attempt, budget = (store.get(**pointer) for pointer in pointers)
    if (call is None or attempt is None or budget is None
            or budget_ref["id"] != call_ref["id"]
            or call["body"].get("attempt_id") != attempt_ref["id"]
            or call["body"].get("mission_id") != body.get("mission_id")
            or call["body"].get("task_id") != body.get("task_id")
            or attempt["body"].get("status") != "failed"
            or attempt["body"].get("call_ref") != call_ref
            or attempt["body"].get("mission_id") != body.get("mission_id")
            or attempt["body"].get("run_id") != run_id
            or attempt["body"].get("task_id") != body.get("task_id")
            or attempt["body"].get("agentId") != call["body"].get("instance_id")
            or budget["body"].get("call_ref") != call_ref
            or budget["body"].get("mission_id") != body.get("mission_id")
            or budget["body"].get("run_id") != run_id):
        return None
    intent_id = f"model-intent:{call_ref['id']}:{call_ref['version']}"
    if (store.get("model_request_intent", intent_id)
            or store.get("model_dispatch_attempt", f"model-dispatch:{intent_id}")
            or store.get("model_response_receipt", f"model-response:{intent_id}")):
        return None
    return attempt_ref["id"]


def _saved_tool_recovery_state(store: StateStore, works: list[dict], run: dict,
                               attempt: dict, call_ref: dict, response: dict,
                               tool: dict) -> str | None:
    """Separate an unaccepted result from an accepted recovery still awaiting a tool turn."""
    for work in works:
        work_body = work["body"]
        spec = work_body.get("spec") or {}
        pointer = work_body.get("result_ref")
        if (work_body.get("phase") != "result_unaccepted"
                or not isinstance(spec, dict)
                or spec.get("kind") != "compute"
                or spec.get("handler_id") != "mission-tool"
                or spec.get("mission_id") != run["body"]["mission_id"]
                or spec.get("run_id") != run["id"]
                or spec.get("attempt_id") != attempt["id"]
                or spec.get("call_id") != call_ref["id"]
                or spec.get("call_version") != call_ref["version"]
                or not isinstance(pointer, dict)
                or set(pointer) != {"kind", "id", "version"}
                or pointer.get("kind") != "executor_unaccepted_result"):
            continue
        observation = store.get(**pointer)
        if observation is None:
            continue
        saved = observation["body"]
        result = saved.get("result")
        payload = result.get("payload") if isinstance(result, dict) else None
        if (saved.get("mission_id") != spec["mission_id"]
                or saved.get("work_id") != spec.get("work_id")
                or saved.get("handler_id") != spec["handler_id"]
                or saved.get("handler_version") != spec.get("handler_version")
                or saved.get("configuration_ref") != spec.get("configuration_ref")
                or saved.get("proposal_ref") != spec.get("proposal_ref")
                or saved.get("input_refs") != spec.get("input_refs")
                or saved.get("reason_code") != "board_rejected"
                or saved.get("external_effect") != "known"
                or not isinstance(result, dict)
                or saved.get("result_sha256") != hashlib.sha256(canonical(result).encode()).hexdigest()
                or not isinstance(payload, dict)
                or payload.get("tool_call_id") != tool.get("id")
                or payload.get("name") != tool.get("function", {}).get("name")
                or response["body"].get("call_ref") != call_ref
                or response["body"].get("attempt_id") != attempt["id"]):
            continue
        suffix = hashlib.sha256(canonical([work["id"]]).encode()).hexdigest()[:32]
        recovery = store.get("mission_context_read_recovery", f"context-read-recovery-{suffix}")
        outcome = store.get("board_outcome", f"context-read-recovery-outcome-{suffix}")
        if recovery and outcome:
            receipt = recovery["body"]
            accepted = outcome["body"]
            if (receipt.get("mission_id") == spec["mission_id"]
                    and receipt.get("run_id") == spec["run_id"]
                    and receipt.get("task_id") == spec["proposal_ref"]["id"]
                    and receipt.get("actor_id") == spec.get("actor_id")
                    and receipt.get("work_ref") == ref(work)
                    and receipt.get("observation_ref") == ref(observation)
                    and receipt.get("response_ref") == ref(response)
                    and receipt.get("result_sha256") == saved["result_sha256"]
                    and receipt.get("external_action") == "none"
                    and accepted.get("mission_id") == spec["mission_id"]
                    and accepted.get("proposal_ref") == spec.get("proposal_ref")
                    and (accepted.get("actor_ref") or {}).get("id") == spec.get("actor_id")
                    and accepted.get("completes_task") is False
                    and ref(recovery) in accepted.get("result_refs", [])):
                return "acceptance_recorded"
        return "result_unaccepted"
    return None


def _exact_report(store: StateStore, pointer: dict | None, mission_id: str) -> dict | None:
    if not isinstance(pointer, dict) or pointer.get("kind") != "report":
        return None
    report = store.get(**pointer)
    return report if report and report["body"].get("mission_id") == mission_id else None


def _report_policy_state(store: StateStore, controls: list[dict], mission_id: str) -> list[dict]:
    """Bind a refreshed view to current use of each accepted report's exact copies."""
    state = []
    for control in controls:
        report = _accepted_report(store, control, mission_id)
        if report is None:
            continue
        access = report_text_access(store, report)
        copies = []
        for pointer in access["source_dependency_refs"]:
            current = store.get("source_copy", pointer["id"])
            selected_use = store.resolve(
                "source_use.copy", f"{pointer['id']}:{pointer['version']}"
            )
            current_use = store.resolve(
                "source_use.copy", f"{current['id']}:{current['version']}"
            ) if current else None
            copies.append({
                "copy_ref": pointer,
                "current_copy_ref": ref(current) if current else None,
                "selected_use_ref": ref(selected_use) if selected_use else None,
                "current_use_ref": ref(current_use) if current_use else None,
            })
        state.append({
            "report_ref": ref(report),
            "status": access["status"],
            "lineage_complete": access["dependency_lineage_complete"],
            "copies": copies,
        })
    return sorted(state, key=lambda item: item["report_ref"]["id"])


def _source(store: StateStore, pointer: dict, window_refs: list[dict]) -> tuple[dict, dict]:
    copy = store.get(**pointer)
    body = copy["body"]
    encounter = store.get(**body["encounter"])
    publication = store.get(**body["publication"]) if body.get("publication") else None
    navigation = store.get(**body["navigation_ref"]) if body.get("navigation_ref") else None
    document = store.get(**body["document_ref"]) if body.get("document_ref") else None
    if sum(origin is not None for origin in (publication, navigation, document)) != 1:
        raise StateError("Source copy needs one exact publication or document origin")
    route = encounter["body"]["route"]
    supplied = route == "user_supplied"
    passages = []
    seen_spans: dict[str, tuple[str, str]] = {}
    for passage_id in body.get("passage_ids", []):
        passage = store.get("source_passage", passage_id)
        if passage:
            if passage["body"].get("copy_ref") != pointer:
                raise StateError("Saved passage belongs to a different source copy")
            seen_spans[passage["body"]["span_id"]] = (
                passage["body"]["locator"],
                passage["body"]["text"],
            )
            passages.append(
                {
                    "spanId": passage["body"]["span_id"],
                    "locator": passage["body"]["locator"],
                    "text": passage["body"]["text"],
                }
            )
    if window_refs:
        blob = body.get("blob")
        if not blob:
            raise StateError("Saved source window lacks retained copy bytes")
        content = store.read_blob(blob["sha256"])
        digest = hashlib.sha256(content).hexdigest()
        if digest != body.get("content_sha256"):
            raise StateError("Saved source window differs from its source-copy hash")
        blocks, parser = extract_copy_blocks(content, body)
        by_locator: dict[str, list[str]] = {}
        for block in blocks:
            by_locator.setdefault(block.locator, []).append(block.text)
        for window_ref in window_refs:
            if (
                set(window_ref) != {"kind", "id", "version"}
                or window_ref["kind"] != "source_read_window"
            ):
                raise StateError("Source window reference is not exact")
            window = store.get(**window_ref)
            if (
                window is None
                or window["body"].get("copy_ref") != pointer
                or window["body"].get("content_sha256") != digest
                or window["body"].get("parser") != parser
            ):
                raise StateError("Source window does not match this exact source copy")
            selected = []
            for passage_ref in window["body"].get("passage_refs", []):
                if (
                    set(passage_ref) != {"kind", "id", "version"}
                    or passage_ref["kind"] != "source_passage"
                ):
                    raise StateError("Source window passage reference is not exact")
                passage = store.get(**passage_ref)
                if passage is None or passage["body"].get("copy_ref") != pointer:
                    raise StateError("Source window passage belongs to another source copy")
                saved = passage["body"]
                locator = saved["locator"]
                prefix, marker, bounds = locator.rpartition("; chars=")
                if not marker or not re.fullmatch(r"\d+:\d+", bounds):
                    raise StateError("Source window passage has no exact text locator")
                start, end = (int(value) for value in bounds.split(":"))
                matched = by_locator.get(prefix, [])
                stable = hashlib.sha256(
                    f"{pointer['id']}:{pointer['version']}:{locator}:{saved['text']}".encode(
                        "utf-8"
                    )
                ).hexdigest()[:32]
                if (
                    saved.get("inspection_state") != "checked"
                    or saved.get("validation_record")
                    != f"ora-read-window-v1:{parser}:exact-substring"
                    or passage["id"] != f"{pointer['id']}:window-{stable}"
                    or saved.get("span_id") != f"window-{stable}"
                    or len(matched) != 1
                    or matched[0][start:end] != saved["text"]
                ):
                    raise StateError("Source window passage does not replay against retained copy")
                selected.append(locator)
                existing = seen_spans.get(saved["span_id"])
                if existing and existing != (locator, saved["text"]):
                    raise StateError("Source window span conflicts with another saved passage")
                if not existing:
                    passages.append(
                        {"spanId": saved["span_id"], "locator": locator, "text": saved["text"]}
                    )
                    seen_spans[saved["span_id"]] = (locator, saved["text"])
            if selected != window["body"].get("selected_locators", []):
                raise StateError("Source window locators differ from exact selected passages")
    view = {
        "id": copy["id"],
        "copyId": copy["id"],
        "version": copy["version"],
        "title": (
            publication["body"]["titles"][0] if publication else
            navigation["body"].get("title") or navigation["body"].get("heading") or "Saved web document"
            if navigation else "Saved web document"
        ),
        "accessState": (
            "navigation_text"
            if navigation
            else "document_text"
            if document
            else "supplied_text"
            if supplied
            else "full_text"
            if body["access_surface"] == "full_text"
            else "locator_only"
            if not body["retain_bytes"]
            else "abstract_only"
        ),
        "retrievalStatus": "user supplied" if supplied else route,
        "contentHash": body.get("content_sha256"),
        "provider": "user_supplied" if supplied else route.split(".", 1)[0],
        "url": body.get("reference"),
        "passages": passages,
    }
    provenance = {
        "sourceId": copy["id"],
        "copyReference": pointer,
        "encounterReferences": [body["encounter"]],
        "origin": "supplied" if supplied else "retrieved",
    }
    return view, provenance


def build_snapshot(
    store: StateStore, run: dict, controls: list[dict], *, snapshot_id: str,
    pinned_report_access: dict[str, str] | None = None,
) -> tuple[dict, list[dict], list[dict], list[dict]]:
    """Build a read model only from saved versions; never infer scientific support."""
    body = run["body"]
    mission_id = body["mission_id"]
    source_refs = list(
        {
            (p["kind"], p["id"], p["version"]): p
            for control in controls
            for p in control["body"]["source_refs"]
        }.values()
    )
    selected_windows: dict[tuple[str, str, int], list[dict]] = {}
    for control in controls:
        selected_copies = {
            tuple(pointer[key] for key in ("kind", "id", "version"))
            for pointer in control["body"]["source_refs"]
        }
        for window_ref in control["body"].get("read_window_refs", []):
            window = (
                store.get(**window_ref) if window_ref.get("kind") == "source_read_window" else None
            )
            copy_ref = window["body"].get("copy_ref") if window else None
            key = (
                tuple(copy_ref[field] for field in ("kind", "id", "version")) if copy_ref else None
            )
            if key not in selected_copies:
                raise StateError("A task's source window is not linked to its exact source copy")
            selected_windows.setdefault(key, []).append(window_ref)
    sources, source_provenance = [], []
    for pointer in source_refs:
        view, provenance = _source(
            store,
            pointer,
            selected_windows.get(tuple(pointer[key] for key in ("kind", "id", "version")), []),
        )
        sources.append(view)
        source_provenance.append(provenance)
    if parent_snapshot_id := body.get("parent_snapshot_id"):
        parent = store.get("research_snapshot", parent_snapshot_id, 1)
        if parent is None:
            raise StateError("Follow-up source lineage needs its exact parent snapshot")
        inherited = {
            tuple(item["copyReference"][key] for key in ("kind", "id", "version")): item
            for item in parent["body"]["inspection"]["sources"]
        }
        initial_refs = body.get("source_refs", [])
        for item in source_provenance:
            if item["copyReference"] not in initial_refs:
                continue
            key = tuple(item["copyReference"][field] for field in ("kind", "id", "version"))
            origin = inherited.get(key)
            if origin is None or origin["encounterReferences"] != item["encounterReferences"]:
                raise StateError("Inherited source differs from its exact parent provenance")
            item["origin"] = "reused"
            item["fromSnapshotId"] = parent_snapshot_id
    accepted = list({
        (report["kind"], report["id"], report["version"]): ref(report)
        for control in controls
        if (report := _accepted_report(store, control, mission_id)) is not None
    }.values())
    reports = [store.get(**pointer) for pointer in accepted]
    if pinned_report_access is not None and any(
        item["id"] not in pinned_report_access for item in reports
    ):
        raise StateConflict("Accepted report changed while building current snapshot")
    report_access = {
        item["id"]: (
            {"status": pinned_report_access[item["id"]]}
            if pinned_report_access is not None
            else report_text_access(store, item)
        ) for item in reports
    }
    artifacts = [
        ({
            key: item["body"][key]
            for key in ("id", "version", "kind", "title", "body", "citations", "limitations")
        } if report_access[item["id"]]["status"] == "available" else {
            "id": item["body"]["id"], "version": item["body"]["version"],
            "kind": item["body"]["kind"],
            "title": _REPORT_TEXT_UNAVAILABLE,
            "body": _REPORT_TEXT_UNAVAILABLE,
            "citations": [], "limitations": [_REPORT_TEXT_UNAVAILABLE],
        }) | {"producerAttemptId": item["body"]["producer_attempt_ref"]["id"]}
        for item in reports
    ]
    attempts, attempt_provenance = [], []
    saved_attempts = _records(store, "agent_attempt", mission_id)
    saved_host_diagnostics = _records(store, "mission_host_diagnostic", mission_id)
    saved_executor_work = _records(store, "executor_work", mission_id)
    capacity_waits = {control["id"]: _capacity_wait(store, control) for control in controls}
    preparation_wait_ids = Counter(
        attempt_id for control in controls
        if (attempt_id := _context_preparation_wait(store, control)) is not None
    )
    preparation_failure_ids = Counter(
        attempt_id for diagnostic in saved_host_diagnostics
        if (attempt_id := _historical_preparation_failure(store, diagnostic, run["id"])) is not None
    )
    source_free_failure_ids = Counter(
        attempt_id for diagnostic in saved_host_diagnostics
        if (attempt_id := _source_free_preparation_failure(store, diagnostic, run["id"])) is not None
    )
    waiting_calls = {
        canonical(control["body"]["call_ref"])
        for control in controls if capacity_waits[control["id"]]
    }
    operator_held_prepared_calls = {
        canonical(control["body"]["call_ref"])
        for control in controls
        if control["body"].get("phase") == "waiting"
        and isinstance(control["body"].get("operator_credential_hold"), dict)
        and control["body"]["operator_credential_hold"].get("phase") == "pending_model"
        and isinstance(control["body"].get("call_ref"), dict)
        and control["body"].get("work_id")
    }
    active_call_refs = {
        canonical(control["body"]["call_ref"])
        for control in controls
        if control["body"].get("phase") not in {"done", "deferred"}
        and isinstance(control["body"].get("call_ref"), dict)
    }
    for item in saved_attempts:
        attempt_body = item["body"]
        call_ref = attempt_body["call_ref"]
        call = store.get(**call_ref)
        configuration_ref = attempt_body["configuration_ref"]
        config = store.get(**configuration_ref)
        request_intent = store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
        response = store.get(
            "model_response_receipt", f"model-response:model-intent:{call['id']}:{call['version']}"
        )
        validation_failure = inspect_provider_tool_validation(store, call, item, response)
        reasoning_status = response["body"].get("reasoning_status") if response else "absent"
        if reasoning_status not in {"returned", "truncated"}:
            reasoning_status = "absent"
        else:
            reasoning_status = "restricted" if reasoning_status == "returned" else "truncated"
        report = _exact_report(store, attempt_body.get("report_ref"), mission_id)
        accepted_report = report if report and ref(report) in accepted else None
        tool_calls = []
        if response and response["body"].get("status") == "tool_requested":
            raw = json.loads(store.read_blob(response["body"]["blob"]["sha256"]))
            for tool in raw.get("tool_calls", []):
                saved = store.get("context_tool_result", f"tool-result:{response['id']}")
                name = tool["function"]["name"]
                published = (
                    name == "publish_result"
                    and accepted_report is not None
                    and accepted_report["body"]["producer_attempt_ref"]["id"] == item["id"]
                )
                result_status = "waiting"
                if (saved and saved["body"].get("tool_call_id") == tool.get("id")):
                    saved_message = json.loads(store.read_blob(saved["body"]["blob"]["sha256"]))
                    if saved_message.get("name") == name:
                        try:
                            result_body = json.loads(saved_message.get("content", ""))
                        except (TypeError, ValueError):
                            result_body = None
                        if isinstance(result_body, dict):
                            observed = result_body.get("status") or result_body.get("outcome")
                            result_status = observed if isinstance(observed, str) and observed else "completed"
                else:
                    result_status = _saved_tool_recovery_state(
                        store, saved_executor_work, run, item, call_ref, response, tool
                    ) or "waiting"
                tool_calls.append(
                    {
                        "name": name,
                        "status": "published" if published else result_status,
                    }
                )
        status = attempt_body["status"]
        explanation = (
            "Selected model and exact call inputs were saved; response uptake is not inferred."
        )
        if status == "running" and accepted_report:
            status = "completed"
            explanation = (
                "An accepted report names this attempt. Its original status and unknown "
                "completion time remain in the saved attempt record."
            )
        elif status == "running" and preparation_wait_ids[item["id"]] == 1:
            status = "waiting"
            explanation = (
                "Context preparation needs local repair. This exact next call has a saved "
                "budget and attempt but no model request intent, provider dispatch, or response. "
                "The original attempt record remains running; no provider failure is inferred."
            )
        elif (status in {"running", "superseded"}
              and preparation_failure_ids[item["id"]] == 1
              and request_intent is None
              and response is None
              and canonical(call_ref) not in active_call_refs):
            status = "superseded"
            explanation = (
                "Context preparation failed locally for this exact call before provider dispatch "
                "at the saved failure time. The original attempt record and failure-time control "
                "and budget remain saved; no later provider action is inferred."
            )
        elif (status == "failed" and source_free_failure_ids[item["id"]] == 1
              and request_intent is None and response is None):
            explanation = (
                "Context preparation failed locally for this exact source-free recovery "
                "call before provider dispatch. Its saved diagnostic, failed attempt, call "
                "and budget identify this failure; any later recovery would have its own attempt."
            )
        elif status == "running" and canonical(call_ref) not in active_call_refs:
            status = "superseded"
            explanation = (
                "No current task control names this call. Its original running status, "
                "any captured response, and unknown end time remain in saved records."
            )
        elif status == "running" and canonical(call_ref) in waiting_calls:
            status = "waiting"
            explanation = (
                "This attempt is waiting on its exact saved capacity operation. "
                "Its original attempt record remains unchanged."
            )
        elif (status == "running" and canonical(call_ref) in operator_held_prepared_calls
              and request_intent is not None and response is None):
            status = "waiting"
            explanation = (
                "Operator paused this prepared model call. The current task control names this "
                "exact call; its saved attempt record remains running, and no provider response "
                "is saved for it. Earlier provider rate limits are separate saved attempts."
            )
        elif status == "superseded" and attempt_body.get("status_basis") == "prepared_call_replaced_before_dispatch":
            explanation = "This prepared call was replaced before provider dispatch; its successor has a separate saved attempt."
        if (preparation_failure_ids[item["id"]] == 1
                and not explanation.startswith("Context preparation failed locally")):
            explanation += (
                " An earlier ContextOverflow was saved for this exact call before provider "
                "dispatch at that time; any later model outcome is shown separately."
            )
        if validation_failure is not None and accepted_report is None:
            explanation = (
                "Saved provider tool validation: The provider rejected "
                f"{validation_failure['function_name']} because required "
                f"{validation_failure['field']} was missing from the model's proposed tool call. "
                "No host tool ran from this rejected call; no accepted report is linked to it."
            )
        token_quota = inspect_provider_token_quota(store, call, item, response)
        if token_quota is not None and accepted_report is None:
            explanation = provider_token_quota_explanation(token_quota)
        summary = (
            accepted_report["body"].get("body", "")[:500]
            if report_access[accepted_report["id"]]["status"] == "available"
            else _REPORT_TEXT_UNAVAILABLE
        ) if accepted_report else None
        attempts.append(
            {
                "id": item["id"],
                "taskId": attempt_body["task_id"],
                "agentId": attempt_body["agentId"],
                "status": status,
                "startedAt": attempt_body["startedAt"],
                "endedAt": attempt_body.get("endedAt"),
                "toolCalls": tool_calls,
                "selectedModel": config["body"]["model_id"],
                "skillVersions": [
                    f"{name}@{value['version']}" for name, value in config["body"]["skills"].items()
                ],
                "resultSummary": summary,
                "returnedReasoning": {"availability": reasoning_status},
            }
        )
        output_refs = [ref(accepted_report)] if accepted_report else []
        attempt_provenance.append(
            {
                "attemptId": item["id"],
                "instanceReference": {
                    "kind": "agent_instance",
                    "id": attempt_body["agentId"],
                    "version": 1,
                },
                "configurationReference": configuration_ref,
                "callReferences": [call_ref],
                "inputReferences": [
                    {key: pointer[key] for key in ("kind", "id", "version")}
                    for pointer in call["body"]["input_refs"]
                ],
                "acceptedOutputReferences": output_refs,
                "explanation": explanation,
            }
        )
    display_numbers = _task_display_numbers(store, mission_id, body["root_task_id"])
    agents_by_id: dict[str, dict] = {}
    tasks = []
    for control in controls:
        actor = store.get("agent_instance", control["body"]["actor_id"], 1)
        task = store.get("board_proposal", control["body"]["task_id"])
        phase = control["body"]["phase"]
        capacity_wait = capacity_waits[control["id"]]
        status = (
            "completed"
            if phase == "done"
            else "deferred"
            if phase == "deferred"
            else "waiting"
            if phase in {"waiting", "retry_at"} or capacity_wait
            else "running"
            if phase.startswith("pending")
            else "queued"
        )
        expectation = read_expectation(store, mission_id, actor["id"], task["id"])
        if expectation:
            expected_for = expectation["body"]["task_ref"]
            latency_class = expectation["body"]["category"]
            version_note = ""
            if expectation["task_version_mismatch"]:
                change = expectation["task_meaning_changed"]
                version_note = (
                    f"Estimated for earlier task version v{expected_for['version']}; "
                    + (
                        "the work purpose changed. "
                        if change is True
                        else "the task state changed without a recorded purpose change. "
                        if change is False
                        else "the current work purpose could not be compared. "
                    )
                )
            latency_basis = version_note + expectation["body"]["basis"]
        else:
            latency_class = None
            latency_basis = "No peer-authored duration estimate is saved for this task."
        first_contribution = inspect_latency(store, run["id"], task_id=task["id"])[
            "first_accepted_contribution_at"
        ]
        agent = agents_by_id.setdefault(
            actor["id"],
            {
                "id": actor["id"],
                "name": actor["body"]["display_name"],
                "role": actor["body"]["role_id"].replace("_", " "),
                "status": status,
            },
        )
        if {"deferred": -1, "completed": 0, "queued": 1, "waiting": 2, "running": 3}[status] > {
            "deferred": -1,
            "completed": 0,
            "queued": 1,
            "waiting": 2,
            "running": 3,
        }[agent["status"]]:
            agent["status"] = status
        tasks.append(
            {
                "id": task["id"],
                "displayNumber": display_numbers.get(task["id"]),
                "version": task["version"],
                "parentTaskId": body["root_task_id"],
                "targetArtifactId": body["target_ref"]["id"] if body.get("target_ref") else None,
                "title": task["body"]["objective"],
                "status": status,
                "latencyClass": latency_class,
                "latencyBasis": latency_basis,
                "firstContributionAt": first_contribution,
                "activeOrWaiting": "waiting"
                if status == "waiting"
                else "active"
                if status == "running"
                else status,
                "waitingReason": control["body"].get("waiting_reason")
                if status == "waiting"
                else None,
                "waitingUntil": capacity_wait["until"] if capacity_wait else None,
                "waitingOperationReference": capacity_wait["reference"] if capacity_wait else None,
                "attemptIds": [
                    attempt["id"] for attempt in attempts if attempt["taskId"] == task["id"]
                ],
            }
        )
    for actor in _records(store, "agent_instance", mission_id):
        agents_by_id.setdefault(
            actor["id"],
            {
                "id": actor["id"],
                "name": actor["body"]["display_name"],
                "role": actor["body"]["role_id"].replace("_", " "),
                "status": "registered",
            },
        )
    agents = list(agents_by_id.values())
    root_status = "completed" if body["status"] == "completed" else "running"
    mission_contribution = inspect_latency(store, run["id"])["first_accepted_contribution_at"]
    tasks.insert(
        0,
        {
            "id": body["root_task_id"],
            "displayNumber": 1,
            "title": body["question"],
            "status": root_status,
            "latencyClass": None,
            "latencyBasis": "No single peer-authored estimate covers this whole mission; review task estimates.",
            "firstContributionAt": mission_contribution,
            "activeOrWaiting": "completed" if root_status == "completed" else "active",
            "attemptIds": [],
        },
    )
    requests = {item["id"]: item for item in _records(store, "mail_request", mission_id)}
    messages = []
    for message in _records(store, "mail_message", mission_id):
        saved = message["body"]
        request = requests.get(saved.get("request_id"))
        status = (
            "fulfilled"
            if request and request["body"].get("status") == "answered"
            else "awaiting_review"
            if request
            and request["body"].get("status") in {"awaiting_review", "partial_awaiting_review"}
            else "acknowledged"
            if saved.get("acknowledged_at")
            else "delivered"
            if saved.get("delivered_at")
            else "requested"
        )
        messages.append(
            {
                "id": message["id"],
                "requestId": saved.get("request_id"),
                "fromAgentId": saved["sender_ref"]["id"],
                "toAgentId": saved["recipient_ref"]["id"],
                "taskId": saved["task_ref"]["id"]
                if saved.get("task_ref")
                else body["root_task_id"],
                "status": status,
                "summary": str(
                    request["body"]["question"]
                    if request
                    else saved.get("payload", {}).get("question", "Peer message")
                    if isinstance(saved.get("payload"), dict)
                    else "Peer message"
                )[:500],
                "createdAt": saved["created_at"],
                "updatedAt": request["body"].get("resolved_at")
                if status == "fulfilled"
                else saved.get("acknowledged_at") or saved.get("delivered_at"),
            }
        )
    for answer in _records(store, "mail_answer", mission_id):
        saved = answer["body"]
        request = requests.get(saved["request_id"])
        report = _exact_report(store, saved.get("artifact_ref"), mission_id)
        if not request or not report or ref(answer) not in request["body"].get("answer_refs", []):
            continue
        originating = store.get(**request["body"]["message_ref"])
        task_ref = originating["body"].get("task_ref") if originating else None
        messages.append(
            {
                "id": answer["id"],
                "requestId": request["id"],
                "fromAgentId": saved["responder_id"],
                "toAgentId": request["body"]["requester_id"],
                "taskId": task_ref["id"] if task_ref else body["root_task_id"],
                "status": "fulfilled"
                if request["body"].get("accepted_answer_ref") == ref(answer)
                else "awaiting_review"
                if request["body"].get("status") in {"awaiting_review", "partial_awaiting_review"}
                else "acknowledged",
                "summary": (
                    report["body"].get("body", "")[:500] or report["body"].get("title")
                    if (
                        report_access[report["id"]]["status"]
                        if report["id"] in report_access
                        else report_text_access(store, report)["status"]
                    ) == "available"
                    else _REPORT_TEXT_UNAVAILABLE
                ),
                "createdAt": saved["created_at"],
                "updatedAt": request["body"].get("resolved_at"),
            }
        )
    timeline = []
    cursor = 0
    while True:
        page = store.events(mission_id, after=cursor, limit=500)
        for event in page:
            timeline.append(
                {
                    "id": event["id"],
                    "time": event["created_at"],
                    "kind": event["type"],
                    "taskId": next(
                        (
                            subject["id"]
                            for subject in event["body"].get("subjects", [])
                            if subject.get("kind") == "task"
                        ),
                        None,
                    ),
                    "summary": event["type"].replace("_", " "),
                }
            )
        if len(page) < 500:
            break
        cursor = page[-1]["sequence"]
    diagnostics = [_host_diagnostic_view(store, item, mission_id) for item in saved_host_diagnostics]
    for control in controls:
        control_body = control["body"]
        candidate = _exact_report(store, control_body.get("report_ref"), mission_id)
        if candidate is None or _accepted_report(store, control, mission_id) is not None:
            continue
        attempt_ref = candidate["body"].get("producer_attempt_ref")
        attempt = store.get(**attempt_ref) if isinstance(attempt_ref, dict) else None
        exact_attempt = bool(
            attempt and attempt["body"].get("run_id") == run["id"]
            and attempt["body"].get("task_id") == control_body["task_id"]
            and attempt["body"].get("agentId") == control_body["actor_id"]
        )
        diagnostics.append({
            "id": "report-candidate-" + hashlib.sha256(
                canonical([control["id"], ref(candidate)]).encode()
            ).hexdigest()[:32],
            "taskId": control_body["task_id"],
            "attemptId": attempt["id"] if exact_attempt else None,
            "kind": "report_candidate",
            "status": "not_accepted",
            "message": "A report candidate was saved, but this exact report is not linked to a completed accepted outcome for its task in this snapshot.",
            "occurredAt": candidate["created_at"],
        })
    report_id = accepted[0]["id"] if len(accepted) == 1 else None
    model_inputs = [
        pointer
        for item in saved_attempts
        for pointer in store.get(**item["body"]["call_ref"])["body"]["input_refs"]
        if pointer["kind"] == "source_copy"
    ]
    retrieved_used = any(
        store.get(
            **store.get(pointer["kind"], pointer["id"], pointer["version"])["body"]["encounter"]
        )["body"].get("route")
        != "user_supplied"
        for pointer in model_inputs
    )
    has_response = any(
        (
            receipt := store.get(
                "model_response_receipt",
                f"model-response:model-intent:{item['body']['call_ref']['id']}:"
                f"{item['body']['call_ref']['version']}",
            )
        )
        is not None
        and receipt["body"].get("status") in {"complete", "tool_requested"}
        for item in saved_attempts
    )
    proof = (
        "offline_test"
        if body["profile"].get("executionMode") == "offline_test"
        else "live_model_with_retrieval"
        if has_response and retrieved_used
        else "live_model_on_supplied_evidence"
        if has_response and body.get("source_refs")
        else "live_mission_pending"
    )
    snapshot = {
        "schemaVersion": "1.0",
        "snapshotId": snapshot_id,
        "sampleVersion": "stage4-mission-v1",
        "capturedAt": _now(),
        "proofKind": proof,
        "parentSnapshotId": body.get("parent_snapshot_id"),
        "inputQuestion": body["question"],
        "mission": {
            "id": mission_id,
            "version": run["version"],
            "question": body["question"],
            "scope": body["scope"],
            "status": body["status"],
            "latencyClass": None,
            "latencyBasis": "No single peer-authored duration estimate covers this mission; review task estimates.",
            "startedAt": body["created_at"],
            "reportArtifactId": report_id,
        },
        "agents": agents,
        "tasks": tasks,
        "attempts": attempts,
        "messages": messages,
        "sources": sources,
        "claims": [],
        "artifacts": artifacts,
        "events": timeline,
        "diagnostics": diagnostics,
    }
    validated = ResearchSnapshot.model_validate(snapshot).model_dump(mode="json", by_alias=True)
    return validated, source_provenance, attempt_provenance, accepted


def persist_snapshot(store: StateStore, run: dict, controls: list[dict]) -> dict:
    """Save once per changed exact mission state; reading a snapshot is side-effect free."""
    mission_id = run["body"]["mission_id"]
    inspected_kinds = (
        "agent_attempt",
        "mail_message",
        "mail_request",
        "mail_answer",
        "mission_latency_expectation",
        "board_proposal",
        "board_invitation",
        "board_outcome",
        "board_dissent",
        "context_inclusion",
        "model_request_intent",
        "model_response_receipt",
        "context_tool_result",
        "executor_unaccepted_result",
        "retrieval_hit",
        "mission_host_diagnostic",
        "mission_post_scope_disposition",
    )
    detail_refs = [
        ref(item) for kind in inspected_kinds for item in _records(store, kind, mission_id)
    ]
    for control in controls:
        for window_ref in control["body"].get("read_window_refs", []):
            window = (
                store.get(**window_ref) if window_ref.get("kind") == "source_read_window" else None
            )
            if window is None:
                raise StateError("Task source window is unavailable")
            detail_refs.extend(window["body"].get("passage_refs", []))
    cursor, relevant_events = 0, []
    while True:
        page = store.events(mission_id, after=cursor, limit=500)
        relevant_events.extend(
            event["id"] for event in page if event["type"] != "snapshot_accepted"
        )
        if len(page) < 500:
            break
        cursor = page[-1]["sequence"]
    report_policy = _report_policy_state(store, controls, mission_id)
    signature = hashlib.sha256(
        canonical(
            {
                "projection_version": PROJECTION_VERSION,
                "run": ref(run),
                "controls": [ref(item) for item in controls],
                "details": detail_refs,
                "events": relevant_events,
                "report_source_use": report_policy,
            }
        ).encode()
    ).hexdigest()[:32]
    snapshot_id = f"snapshot-{run['id']}-{signature}"
    existing = store.get("research_snapshot", snapshot_id)
    if existing:
        if _report_policy_state(store, controls, mission_id) != report_policy:
            raise StateConflict("Report source use changed while reading current snapshot")
        return ref(existing)
    pinned_report_access = {
        item["report_ref"]["id"]: item["status"] for item in report_policy
    }
    snapshot, source_prov, attempt_prov, outputs = build_snapshot(
        store, run, controls, snapshot_id=snapshot_id,
        pinned_report_access=pinned_report_access,
    )
    def validate_output_projection(tx, report: dict, projected: dict) -> bool:
        if _report_policy_state(tx, controls, mission_id) != report_policy:
            raise StateConflict("Report source use changed while saving current snapshot")
        status = pinned_report_access.get(report["id"])
        if status is None:
            return False
        expected = (
            {field: report["body"].get(field) for field in (
                "title", "body", "citations", "limitations",
            )}
            if status == "available"
            else {
                "title": _REPORT_TEXT_UNAVAILABLE,
                "body": _REPORT_TEXT_UNAVAILABLE,
                "citations": [],
                "limitations": [_REPORT_TEXT_UNAVAILABLE],
            }
        )
        return all(projected[field] == value for field, value in expected.items())
    initial = [pointer["id"] for pointer in run["body"].get("source_refs", [])]
    parent = (
        {"kind": "research_snapshot", "id": run["body"]["parent_snapshot_id"], "version": 1}
        if run["body"].get("parent_snapshot_id")
        else None
    )
    try:
        return save_snapshot(
            store,
            f"mission-snapshot:{snapshot_id}",
            snapshot,
            source_provenance=source_prov,
            attempt_provenance=attempt_prov,
            detail_refs=detail_refs,
            accepted_outputs=outputs,
            parent_ref=parent,
            followup_target=run["body"].get("target_ref"),
            initial_source_ids=initial,
            run_id=run["id"],
            validate_output_projection=validate_output_projection,
        )
    except StateConflict:
        # Two builders can choose the same input fingerprint but different
        # capturedAt times. The first accepted immutable view owns this ID.
        if _report_policy_state(store, controls, mission_id) != report_policy:
            raise
        accepted = store.get("research_snapshot", snapshot_id, 1)
        if accepted is None:
            raise
        return ref(accepted)


def current_report_view(store: StateStore, snapshot: dict) -> dict:
    """Mask revoked report prose in a returned view, including historical comparisons.

    The saved snapshot remains immutable. Its inspection records are forensic
    material, so this current UI view must mask those separately from artifacts.
    """
    view = json.loads(canonical(snapshot))
    hidden: set[tuple[str, int]] = set()
    hidden_attempts: set[str] = set()
    for artifact in view.get("artifacts", []):
        report = store.get("report", artifact["id"], artifact["version"])
        if report is None or report_text_access(store, report)["status"] != "available":
            hidden.add((artifact["id"], artifact["version"]))
            hidden_attempts.add(artifact.get("producerAttemptId"))
            artifact.update({
                "title": _REPORT_TEXT_UNAVAILABLE,
                "body": _REPORT_TEXT_UNAVAILABLE,
                "citations": [],
                "limitations": [_REPORT_TEXT_UNAVAILABLE],
            })
    for attempt in view.get("attempts", []):
        if attempt["id"] in hidden_attempts:
            attempt["resultSummary"] = _REPORT_TEXT_UNAVAILABLE
    inspection = view.get("inspection") or {}
    for entry in inspection.get("records", []):
        pointer = entry.get("reference") or {}
        if (pointer.get("kind"), pointer.get("id"), pointer.get("version")) in {
            ("report", report_id, version) for report_id, version in hidden
        }:
            entry["data"] = {
                "availability": "data_unavailable_due_source_use",
                "explanation": _REPORT_TEXT_UNAVAILABLE,
            }
    for message in view.get("messages", []):
        answer = store.get("mail_answer", message["id"])
        pointer = answer["body"].get("artifact_ref") if answer else None
        if isinstance(pointer, dict) and (pointer.get("id"), pointer.get("version")) in hidden:
            message["summary"] = _REPORT_TEXT_UNAVAILABLE
    return view


def latest_snapshot(store: StateStore, run_id: str) -> dict | None:
    pointers = [
        item for item in _records(store, "mission_snapshot_head", "") if item["id"] == run_id
    ]
    if not pointers:
        record = store.get("mission_snapshot_head", run_id)
        return load_snapshot(store, record["body"]["snapshot_ref"]["id"]) if record else None
    return load_snapshot(store, pointers[-1]["body"]["snapshot_ref"]["id"])
