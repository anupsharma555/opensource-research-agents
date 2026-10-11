"""Exact working-source choices and source-free context recovery.

The host may switch representation when saved source text cannot fit an input
allowance. It never ranks or discards scientific sources: every copy remains in
the task and a peer chooses any smaller working set after an indexed turn.
"""

from __future__ import annotations

import hashlib
import math
from typing import Iterable

from ora.coordination.board import ref, require_ref
from ora.evidence import EvidenceError, EvidenceLibrary
from ora.evidence.intake import checked_model_text
from ora.state import StateConflict, StateError, StateStore, Transaction
from ora.state.store import canonical, identifier

from .context import MAX_CONTEXT_PAGE_CHARS, WorkingContext


class SourceContextError(StateError):
    """An exact source choice or inventory needs local repair."""


def _id(prefix: str, value: object) -> str:
    return prefix + hashlib.sha256(canonical(value).encode()).hexdigest()


def _control(store: StateStore, pointer: dict) -> dict:
    if (
        type(pointer) is not dict
        or set(pointer) != {"kind", "id", "version"}
        or pointer["kind"] != "mission_task_control"
    ):
        raise SourceContextError("An exact mission control reference is required")
    control = store.get(**pointer)
    current = store.get("mission_task_control", pointer["id"])
    if control is None or current is None or ref(current) != pointer:
        raise StateConflict("Task control changed; reread before selecting source context")
    return control


def _refs(items: Iterable[dict]) -> list[dict]:
    seen, result = set(), []
    for pointer in items:
        if (
            type(pointer) is not dict
            or set(pointer) != {"kind", "id", "version"}
            or pointer["kind"] != "source_copy"
            or type(pointer["version"]) is not int
            or pointer["version"] < 1
        ):
            raise SourceContextError("Working sources need exact source-copy references")
        key = pointer["kind"], pointer["id"], pointer["version"]
        if key in seen:
            raise SourceContextError("A source copy is selected more than once")
        seen.add(key)
        result.append(pointer)
    return result


def _selection_id(control: dict) -> str:
    body = control["body"]
    return _id("mission-working-sources:", [body["mission_id"], body["actor_id"], body["task_id"]])


def _input_status(store: StateStore, copy: dict, context: WorkingContext) -> str:
    status = context._source_status(copy)["model_input_status"]
    if status != "unknown":
        return status
    pointer = ref(copy)
    encounter = store.get(**copy["body"]["encounter"])
    if (
        encounter is not None
        and encounter["body"].get("route") == "user_supplied"
        and copy["body"].get("rights_status") in {None, "permitted"}
        and store.resolve("source_use.copy", f"{pointer['id']}:{pointer['version']}") is None
    ):
        return "permitted_user_supplied_without_policy_record"
    return status


def save_working_sources(
    store: StateStore,
    operation_id: str,
    *,
    control_ref: dict,
    response_ref: dict,
    selected_source_refs: list[dict],
    expected_version: int = 0,
) -> dict:
    """Persist one model-selected subset, including an explicit empty set.

    The caller maps the choice from the referenced observed model response.
    Existing task copies and passage choices remain saved separately.
    """
    identifier(operation_id)
    selected = _refs(selected_source_refs)
    if type(expected_version) is not int or expected_version < 0:
        raise SourceContextError("Expected working-source version must be nonnegative")
    control = _control(store, control_ref)
    mission_id = control["body"]["mission_id"]
    request = {
        "control_ref": control_ref,
        "response_ref": response_ref,
        "selected_source_refs": selected,
        "expected_version": expected_version,
    }

    def save(tx: Transaction) -> dict:
        current = tx.get(**control_ref)
        if current is None or ref(tx.get("mission_task_control", control_ref["id"])) != control_ref:
            raise StateConflict("Task control changed during source selection")
        body = current["body"]
        available = _refs(body.get("source_refs", []))
        if any(pointer not in available for pointer in selected):
            raise SourceContextError("Selected copy is not a saved task source")
        response = require_ref(tx, mission_id, response_ref)
        if (
            response["kind"] != "model_response_receipt"
            or response["body"].get("status") not in {"complete", "tool_requested"}
            or response["body"].get("task_id") != body["task_id"]
        ):
            raise SourceContextError("Working set needs a matching observed model response")
        call = require_ref(tx, mission_id, response["body"].get("call_ref"))
        if (
            call["kind"] != "agent_call"
            or call["body"].get("instance_id") != body["actor_id"]
            or call["body"].get("task_id") != body["task_id"]
        ):
            raise SourceContextError("Working set response belongs to another peer or task")
        sources = [require_ref(tx, mission_id, pointer) for pointer in selected]
        record_id = _selection_id(current)
        previous = tx.get("mission_working_sources", record_id)
        if (previous["version"] if previous else 0) != expected_version:
            raise StateConflict("Working-source selection changed")
        if previous and previous["body"].get("response_ref") == response_ref:
            raise SourceContextError("A revised working set needs a new model response")
        saved = tx.put(
            "mission_working_sources",
            record_id,
            {
                "schema": "ora.mission_working_sources.v1",
                "mission_id": mission_id,
                "run_id": body["run_id"],
                "actor_id": body["actor_id"],
                "task_id": body["task_id"],
                "control_ref": control_ref,
                "response_ref": ref(response),
                "selected_source_refs": [ref(item) for item in sources],
                "choice": "model_authored",
                "recorded_at": store.now(),
            },
            expected_version=expected_version,
        )
        tx.link(saved, response, "input")
        for item in sources:
            tx.link(saved, item, "input")
        if previous:
            tx.link(saved, previous, "derived_from")
        return {"working_sources_ref": ref(saved)}

    return store.transact(operation_id, request, save, mission_id=mission_id)["working_sources_ref"]


def plan_source_turn(
    store: StateStore,
    control_ref: dict,
    *,
    source_input_allowance_tokens: int,
) -> dict:
    """Choose full saved sources if they fit, else a source-free recovery turn.

    The allowance is the selected endpoint's remaining input budget after
    instructions, exposed tools, output reserve, and required context. The
    same conservative source estimate is used by the current mission service.
    Model-selected passage/window references apply to supplied copies too.
    """
    if type(source_input_allowance_tokens) is not int or source_input_allowance_tokens <= 0:
        raise SourceContextError("Source input allowance must be positive")
    control = _control(store, control_ref)
    body = control["body"]
    available = _refs(body.get("source_refs", []))
    selection = store.get("mission_working_sources", _selection_id(control))
    if selection and any(
        selection["body"].get(key) != body.get(key) for key in ("mission_id", "actor_id", "task_id")
    ):
        raise SourceContextError("Working-source record belongs to another task")
    candidate = _refs(selection["body"]["selected_source_refs"]) if selection else available
    if any(pointer not in available for pointer in candidate):
        return _recovery(
            control_ref, available, "saved_working_source_no_longer_in_task", selection
        )
    context = WorkingContext(store)
    source_status = []
    for pointer in available:
        copy = store.get(**pointer)
        if copy is None:
            raise SourceContextError("A saved source copy is unavailable")
        source_status.append((pointer, _input_status(store, copy, context)))
    status_by_ref = {canonical(pointer): status for pointer, status in source_status}

    def allowed(pointer: dict) -> bool:
        return status_by_ref[canonical(pointer)] in {
            "permitted",
            "permitted_user_supplied_without_policy_record",
        }

    if not selection:
        candidate = [pointer for pointer in candidate if allowed(pointer)]
    elif any(not allowed(pointer) for pointer in candidate):
        return _recovery(control_ref, available, "selected_source_use_changed", selection)
    source_inputs, source_bytes = [], 0
    for pointer in candidate:
        copy = store.get(**pointer)
        chosen = body.get("selected_passage_refs", {}).get(pointer["id"])
        if chosen is not None:
            if not isinstance(chosen, list):
                return _recovery(
                    control_ref, available, "invalid_saved_passage_selection", selection
                )
            passages = chosen
        else:
            encounter = store.get(**copy["body"]["encounter"])
            if encounter is None:
                raise SourceContextError("Source encounter is unavailable")
            passages = (
                []
                if encounter["body"].get("route") == "user_supplied"
                else [
                    ref(store.get("source_passage", item))
                    for item in copy["body"].get("passage_ids", [])
                ]
            )
        if passages:
            try:
                checked = checked_model_text(EvidenceLibrary(store), pointer, passages)
            except EvidenceError:
                return _recovery(
                    control_ref, available, "selected_source_window_needs_repair", selection
                )
            source_bytes += len(checked["text"].encode("utf-8"))
        else:
            if chosen == []:
                return _recovery(control_ref, available, "empty_selected_source_window", selection)
            blob = copy["body"].get("blob")
            if not isinstance(blob, dict) or not str(copy["body"].get("mime_type", "")).startswith(
                "text/"
            ):
                return _recovery(control_ref, available, "source_needs_checked_window", selection)
            source_bytes += blob["size"]
        source_inputs.append({"copy_ref": pointer, "passage_refs": passages})
    estimate = math.ceil((source_bytes + 200 * len(source_inputs)) / 4 * 1.25)
    if estimate > source_input_allowance_tokens:
        return _recovery(
            control_ref, available, "exact_source_text_exceeds_allowance", selection, estimate
        )
    return {
        "schema": "ora.source_turn_plan.v1",
        "mode": "model_working_set" if selection else "all_allowed_sources_fit",
        "control_ref": control_ref,
        "working_sources_ref": ref(selection) if selection else None,
        "source_inputs": source_inputs,
        "source_input_estimate_tokens": estimate,
        "source_input_allowance_tokens": source_input_allowance_tokens,
        "inventory_count": len(available),
        "retained_source_refs": available,
        "excluded_source_refs": [pointer for pointer in available if not allowed(pointer)],
        "estimate_basis": "1.25_times_utf8_bytes_plus_per_copy_envelope_divided_by_four",
    }


def _recovery(
    control_ref: dict,
    available: list[dict],
    reason: str,
    selection: dict | None,
    estimate: int | None = None,
) -> dict:
    return {
        "schema": "ora.source_turn_plan.v1",
        "mode": "source_free_recovery",
        "reason": reason,
        "control_ref": control_ref,
        "working_sources_ref": ref(selection) if selection else None,
        "source_inputs": [],
        "source_input_estimate_tokens": estimate,
        "inventory_count": len(available),
        "retained_source_refs": available,
        "next_action": "Show a compact exact source inventory; let the peer select copies or read smaller windows",
    }


def save_source_inventory(store: StateStore, operation_id: str, *, control_ref: dict) -> dict:
    """Pin every current task copy for a source-free recovery turn."""
    identifier(operation_id)
    control = _control(store, control_ref)
    body = control["body"]
    available = _refs(body.get("source_refs", []))
    record_id = _id("mission-source-inventory:", [control_ref, available])
    request = {"control_ref": control_ref, "source_refs": available}

    def save(tx: Transaction) -> dict:
        current = tx.get(**control_ref)
        if current is None or ref(tx.get("mission_task_control", control_ref["id"])) != control_ref:
            raise StateConflict("Task sources changed before inventory was saved")
        records = [require_ref(tx, body["mission_id"], pointer) for pointer in available]
        existing = tx.get("mission_source_inventory", record_id)
        if existing:
            if (
                existing["body"].get("control_ref") != control_ref
                or existing["body"].get("source_refs") != available
            ):
                raise StateConflict("Source inventory ID has different exact inputs")
            return {"inventory_ref": ref(existing)}
        saved = tx.put(
            "mission_source_inventory",
            record_id,
            {
                "schema": "ora.mission_source_inventory.v1",
                "mission_id": body["mission_id"],
                "run_id": body["run_id"],
                "actor_id": body["actor_id"],
                "task_id": body["task_id"],
                "control_ref": control_ref,
                "source_refs": available,
                "count": len(available),
                "status": "exact_index_not_model_source_input",
            },
        )
        tx.link(saved, current, "about_task")
        for item in records:
            tx.link(saved, item, "input")
        return {"inventory_ref": ref(saved)}

    return store.transact(operation_id, request, save, mission_id=body["mission_id"])[
        "inventory_ref"
    ]


def source_inventory_page(
    store: StateStore,
    *,
    inventory_ref: dict,
    actor_id: str,
    task_id: str,
    cursor: int = 0,
    limit: int = 20,
    reply_char_limit: int = MAX_CONTEXT_PAGE_CHARS,
) -> dict:
    """Return a bounded page of exact copies and authorized fetch routes."""
    if (
        type(cursor) is not int
        or cursor < 0
        or type(limit) is not int
        or not 1 <= limit <= 20
        or type(reply_char_limit) is not int
        or reply_char_limit < 500
    ):
        raise SourceContextError("Inventory page needs a bounded cursor, count and reply size")
    if (
        type(inventory_ref) is not dict
        or set(inventory_ref) != {"kind", "id", "version"}
        or inventory_ref["kind"] != "mission_source_inventory"
    ):
        raise SourceContextError("Exact source inventory reference is required")
    inventory = store.get(**inventory_ref)
    if inventory is None or inventory["kind"] != "mission_source_inventory":
        raise SourceContextError("Exact source inventory is unavailable")
    body = inventory["body"]
    if body["actor_id"] != actor_id or body["task_id"] != task_id:
        raise SourceContextError("Inventory belongs to another peer or task")
    sources = body["source_refs"]
    control_ref = body.get("control_ref")
    control = store.get(**control_ref) if isinstance(control_ref, dict) else None
    if (
        control is None or control["kind"] != "mission_task_control"
        or control["body"].get("mission_id") != body["mission_id"]
        or control["body"].get("actor_id") != actor_id
        or control["body"].get("task_id") != task_id
        or _refs(control["body"].get("source_refs", [])) != sources
    ):
        raise SourceContextError("Inventory lost its exact task reading state")
    if cursor > len(sources):
        raise SourceContextError("Inventory cursor is outside the saved index")
    context = WorkingContext(store)
    items = []
    for pointer in sources[cursor : cursor + limit]:
        copy = store.get(**pointer)
        if copy is None:
            raise SourceContextError("Indexed source copy is unavailable")
        status = _input_status(store, copy, context)
        publication_ref = copy["body"].get("publication")
        publication = store.get(**publication_ref) if isinstance(publication_ref, dict) else None
        titles = publication["body"].get("titles", []) if publication else []
        title = titles[0] if titles else None
        item = {
            "ref": pointer,
            "title": title[:160] if isinstance(title, str) else None,
            "model_input_status": status,
            "fetch": (
                {
                    "tool": "read_source_window",
                    "source_id": pointer["id"],
                    "source_version": pointer["version"],
                }
                if status == "permitted"
                else None
            ),
        }
        if status == "permitted":
            progress = context._source_progress(control, [copy], body["mission_id"])[0]
            navigation = context._source_free_navigation_item(control, progress)
            # The item already carries its exact copy ref and fetch route.
            # Page only saved positions, never checked source text or a claim
            # that a previous model request received that text.
            item["saved_reading"] = {
                key: value for key, value in navigation.items()
                if key not in {"copy_ref", "fetch_first_uncovered"}
            }
        def candidate_page() -> dict:
            return {
                "schema": "ora.source_inventory_page.v1",
                "inventory_ref": inventory_ref,
                "cursor": cursor,
                "next_cursor": cursor + len(items) + 1,
                "total": len(sources),
                "items": [*items, item],
            }

        candidate = candidate_page()
        if len(canonical(candidate)) > reply_char_limit:
            if items:
                break
            # A single JSON-escaped display title must not make the exact
            # source ID unpageable. Preserve reading navigation first; under
            # a caller-chosen tiny allowance, return the core ref/status/fetch
            # with an explicit omission instead of an empty successful page.
            if item["title"] is not None:
                item["title_omitted_for_page_budget"] = True
            item["title"] = None
            candidate = candidate_page()
            if len(canonical(candidate)) > reply_char_limit and "saved_reading" in item:
                reading = item["saved_reading"]
                item["saved_reading"] = {
                    key: reading[key] for key in (
                        "saved_coverage", "latest_window_ref",
                        "latest_window_next_cursor", "source_text_in_this_call",
                        "past_model_delivery",
                    )
                }
                item["saved_reading_compacted_for_page_budget"] = True
                candidate = candidate_page()
            if len(canonical(candidate)) > reply_char_limit and "saved_reading" in item:
                item.pop("saved_reading")
                item.pop("saved_reading_compacted_for_page_budget", None)
                item["saved_reading_omitted_for_page_budget"] = True
                candidate = candidate_page()
            if len(canonical(candidate)) > reply_char_limit:
                item.pop("title", None)
                if isinstance(item.get("fetch"), dict):
                    # The exact source id/version remain in item.ref. A tiny
                    # page can still name the available tool without repeating
                    # those same fields in its fetch hint.
                    item["fetch"] = {"tool": "read_source_window"}
                candidate = candidate_page()
            if len(canonical(candidate)) > reply_char_limit:
                raise SourceContextError(
                    "One exact inventory reference exceeds the reply allowance"
                )
        items.append(item)
    end = cursor + len(items)
    return {
        "schema": "ora.source_inventory_page.v1",
        "inventory_ref": inventory_ref,
        "cursor": cursor,
        "next_cursor": end if end < len(sources) else None,
        "total": len(sources),
        "items": items,
    }
