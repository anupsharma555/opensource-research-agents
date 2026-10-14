"""Read a bounded visible draft from one exact length-cutoff model response.

The caller supplies the source copies selected for its current call. This
reader does not prepare or dispatch that call; ModelSession checks those same
copies again at the actual send boundary. Neither tool fragments nor private
reasoning are recovered as model input.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ora.agents.capabilities import content_hash
from ora.models.session import SessionError, _current_model_use, _explicit_prohibition
from ora.state import IntegrityFault, StateStore
from ora.state.store import canonical

MAX_OUTPUT_WINDOW_CHARS = 4000


class OutputRecoveryError(ValueError):
    """The requested response is not an exact partial result for this actor."""


def _ref(pointer: Any, kind: str) -> dict[str, Any]:
    if (
        type(pointer) is not dict
        or set(pointer) != {"kind", "id", "version"}
        or pointer.get("kind") != kind
        or not isinstance(pointer.get("id"), str)
        or not pointer["id"]
        or type(pointer.get("version")) is not int
        or pointer["version"] < 1
    ):
        raise OutputRecoveryError(f"Exact {kind} reference is required")
    return pointer


def _record(store: StateStore, pointer: Any, kind: str) -> dict[str, Any]:
    exact = _ref(pointer, kind)
    record = store.get(**exact)
    if record is None:
        raise OutputRecoveryError(f"Exact {kind} record is unavailable")
    return record


def _source_refs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        raise OutputRecoveryError("Current call needs exact source-copy references")
    refs = [_ref(pointer, "source_copy") for pointer in value]
    if len({(item["id"], item["version"]) for item in refs}) != len(refs):
        raise OutputRecoveryError("Current call selected a source more than once")
    return refs


def _permitted_source(store: StateStore, pointer: dict, expected_hash: str | None,
                      mission_id: str) -> tuple[bool, dict | None]:
    copy = store.get(**pointer)
    if copy is None or (expected_hash is not None and content_hash(copy["body"]) != expected_hash):
        return False, None
    body = copy["body"]
    current = store.get("source_copy", pointer["id"])
    if (
        body.get("mission_id") not in (None, mission_id)
        or body.get("rights_status") not in (None, "permitted")
        or current is None
        or current["body"].get("rights_status") not in (None, "permitted")
        or body.get("retain_bytes") is not True
        or not isinstance(body.get("blob"), dict)
        or not isinstance(body.get("content_sha256"), str)
        or _explicit_prohibition(body)
        or _explicit_prohibition(current["body"])
        or (isinstance(body.get("use_record"), dict)
            and body["use_record"].get("use") == "unknown")
        or (isinstance(current["body"].get("use_record"), dict)
            and current["body"]["use_record"].get("use") == "unknown")
    ):
        return False, None
    try:
        use_ref = _current_model_use(store, pointer)
        current_ref = {key: current[key] for key in ("kind", "id", "version")}
        current_use_ref = _current_model_use(store, current_ref)
    except SessionError:
        return False, None
    for policy_ref in (use_ref, current_use_ref):
        if policy_ref is not None:
            use = store.get(**policy_ref)
            if use is None or use["body"].get("policy_status") not in (None, "granted"):
                return False, None
    return True, use_ref


def _tool_source_refs(store: StateStore, pointer: dict, *, mission_id: str,
                      task_id: str, actor_id: str, tool_message: dict) -> list[dict] | None:
    """Resolve only known source-bearing current tool results to exact copies."""
    try:
        response = _record(store, pointer, "model_response_receipt")
        call = _record(store, response["body"].get("call_ref"), "agent_call")
        result = store.get("context_tool_result", f"tool-result:{response['id']}")
        if (
            response["body"].get("mission_id") != mission_id
            or response["body"].get("task_id") != task_id
            or response["body"].get("status") != "tool_requested"
            or call["body"].get("instance_id") != actor_id
            or result is None
            or result["body"].get("mission_id") != mission_id
            or result["body"].get("response_ref") != pointer
        ):
            return None
        message = json.loads(store.read_blob(result["body"]["blob"]["sha256"]))
        if (
            not isinstance(message, dict)
            or message != tool_message
            or hashlib.sha256(canonical(message).encode()).hexdigest()
            != result["body"].get("message_sha256")
            or message.get("role") != "tool"
            or message.get("tool_call_id") != result["body"].get("tool_call_id")
        ):
            return None
        name = message.get("name")
        if name in {"inspect_hit", "fetch_source"}:
            source_ref = _ref(result["body"].get("effect_ref"), "source_copy")
            return [source_ref] if store.get(**source_ref) else None
        if name == "read_source_window":
            tool_result = json.loads(message["content"])
            window = _record(store, tool_result.get("window_ref"), "source_read_window")
            source_ref = _ref(window["body"].get("copy_ref"), "source_copy")
            if tool_result.get("copy_ref") != source_ref or store.get(**source_ref) is None:
                return None
            return [source_ref]
    except (OutputRecoveryError, IntegrityFault, KeyError, TypeError, ValueError):
        return None
    # Search hits and other tool payloads need their own dependency mapping.
    return None


def _context_source_refs(store: StateStore, *, intent: dict, call: dict, config: dict,
                         mission_id: str, task_id: str, actor_id: str,
                         saved_refs: list[dict]) -> list[dict] | None:
    """Conservatively account for task, included-record and current-tool sources."""
    budget = store.get("mission_call_budget", call["id"])
    inclusion = store.get("context_inclusion", f"context:{intent['id']}")
    if budget is None or inclusion is None:
        return None
    prior = intent["body"]
    planned = budget["body"]
    included = inclusion["body"]
    call_ref = {key: call[key] for key in ("kind", "id", "version")}
    config_ref = {key: config[key] for key in ("kind", "id", "version")}
    intent_ref = {key: intent[key] for key in ("kind", "id", "version")}
    if (
        planned.get("mission_id") != mission_id
        or planned.get("call_ref") != call_ref
        or planned.get("source_turn_mode") not in {
            "all_allowed_sources_fit", "model_working_set", "source_free_recovery",
            "requested_window_recovery",
        }
        or included.get("mission_id") != mission_id
        or included.get("intent_ref") != intent_ref
        or included.get("call_ref") != call_ref
        or included.get("configuration_ref") != config_ref
        or included.get("source_refs") != saved_refs
        or included.get("request_sha256") != prior.get("request_sha256")
        or included.get("parent_run_ref") is not None
        or included.get("followup_target_ref") is not None
        or included.get("tool_result_compactions")
    ):
        return None
    try:
        request_bytes = store.read_blob(prior["blob"]["sha256"])
        if hashlib.sha256(request_bytes).hexdigest() != prior["request_sha256"]:
            return None
        request = json.loads(request_bytes)
        messages = request.get("messages")
        if not isinstance(messages, list):
            return None
        tool_messages = [item for item in messages if isinstance(item, dict)
                         and item.get("role") == "tool"]
        retained = _source_refs(planned.get("retained_source_refs"))
        included_refs = included.get("included_refs")
        archived = planned.get("compacted_tool_turn_refs")
        if not isinstance(included_refs, list) or not isinstance(archived, list):
            return None
        dependencies = list(retained)
        for pointer in included_refs:
            if not isinstance(pointer, dict):
                return None
            kind = pointer.get("kind")
            if kind in {"agent_call", "agent_configuration", "board_proposal"}:
                _record(store, pointer, kind)
                continue
            if kind == "source_copy":
                dependencies.append(_ref(pointer, "source_copy"))
                continue
            record = _record(store, pointer, kind)
            if record["body"].get("mission_id") not in (None, mission_id):
                return None
            declared = []
            for key in ("source_refs", "input_refs", "source_dependency_refs"):
                value = record["body"].get(key)
                if value is None:
                    continue
                if not isinstance(value, list):
                    return None
                declared.extend(_ref(item, "source_copy") for item in value
                                if isinstance(item, dict) and item.get("kind") == "source_copy")
            if not declared:
                return None  # Peer, note or summary text has unknown source ancestry.
            dependencies.extend(declared)
        for pointer in archived:
            response = _record(store, pointer, "model_response_receipt")
            prior_call = _record(store, response["body"].get("call_ref"), "agent_call")
            if (
                response["body"].get("mission_id") != mission_id
                or response["body"].get("task_id") != task_id
                or response["body"].get("status") != "tool_requested"
                or prior_call["body"].get("instance_id") != actor_id
            ):
                return None
        current_turn = planned.get("current_tool_turn_ref")
        if current_turn is None:
            if tool_messages:
                return None
        elif len(tool_messages) != 1:
            return None
        else:
            tool_refs = _tool_source_refs(
                store, current_turn, mission_id=mission_id, task_id=task_id,
                actor_id=actor_id, tool_message=tool_messages[0],
            )
            if tool_refs is None:
                return None
            dependencies.extend(tool_refs)
        return list({(p["kind"], p["id"], p["version"]): p for p in dependencies}.values())
    except (OutputRecoveryError, IntegrityFault, KeyError, TypeError, ValueError):
        return None


def read_partial_output_window(
    store: StateStore,
    response_ref: dict[str, Any],
    *,
    mission_id: str,
    task_id: str,
    actor_id: str,
    current_call_source_refs: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    offset: int = 0,
    limit: int = 1600,
) -> dict[str, Any]:
    """Return visible text only when its prior source set remains selected and usable.

    ``current_call_source_refs`` must come from the host's pinned current call.
    A later ModelSession dispatch rechecks that call and its source policies.
    Unsafe source changes return a diagnostic without any draft text.
    """
    if not all(isinstance(value, str) and value for value in (mission_id, task_id, actor_id)):
        raise OutputRecoveryError("Mission, task and actor identity are required")
    if type(offset) is not int or offset < 0:
        raise OutputRecoveryError("Visible-output offset must be nonnegative")
    if type(limit) is not int or not 1 <= limit <= MAX_OUTPUT_WINDOW_CHARS:
        raise OutputRecoveryError("Visible-output limit must be within the bounded range")
    selected = _source_refs(current_call_source_refs)
    receipt = _record(store, response_ref, "model_response_receipt")
    body = receipt["body"]
    if (
        body.get("mission_id") != mission_id
        or body.get("task_id") != task_id
        or body.get("status") != "partial"
        or body.get("finish_reason") != "length"
        or body.get("error_kind") is not None
    ):
        raise OutputRecoveryError("Response is not this task's saved length cutoff")
    intent = _record(store, body.get("intent_ref"), "model_request_intent")
    call = _record(store, body.get("call_ref"), "agent_call")
    config = _record(store, body.get("configuration_ref"), "agent_configuration")
    actor = store.get("agent_instance", actor_id, 1)
    prior = intent["body"]
    selection = call["body"]
    saved_config = config["body"]
    if (
        actor is None
        or actor["body"].get("mission_id") != mission_id
        or prior.get("mission_id") != mission_id
        or prior.get("task_id") != task_id
        or prior.get("instance_id") != actor_id
        or prior.get("attempt_id") != body.get("attempt_id")
        or prior.get("call_ref") != body.get("call_ref")
        or prior.get("configuration_ref") != body.get("configuration_ref")
        or prior.get("request_sha256") != body.get("request_sha256")
        or selection.get("mission_id") != mission_id
        or selection.get("task_id") != task_id
        or selection.get("instance_id") != actor_id
        or selection.get("attempt_id") != body.get("attempt_id")
        or selection.get("configuration_id") != config["id"]
        or selection.get("configuration_sha256") != prior.get("configuration_sha256")
        or saved_config.get("mission_id") != mission_id
        or saved_config.get("instance_id") != actor_id
        or saved_config.get("configuration_sha256") != prior.get("configuration_sha256")
        or content_hash({k: v for k, v in saved_config.items()
                         if k != "configuration_sha256"}) != prior.get("configuration_sha256")
        or selection.get("input_refs") != saved_config.get("input_refs")
    ):
        raise OutputRecoveryError("Partial response lineage changed or belongs to another actor")

    decisions = prior.get("source_decisions")
    saved_refs = prior.get("source_refs")
    pinned = selection.get("input_refs")
    if not isinstance(decisions, list) or not isinstance(saved_refs, list) or not isinstance(pinned, list):
        raise OutputRecoveryError("Saved source lineage is unavailable")
    expected = {}
    for item in pinned:
        if not isinstance(item, dict) or item.get("kind") != "source_copy":
            continue
        pointer = _ref({key: item.get(key) for key in ("kind", "id", "version")}, "source_copy")
        key = pointer["id"], pointer["version"]
        if not isinstance(item.get("sha256"), str) or key in expected:
            raise OutputRecoveryError("Saved call source pin is invalid")
        expected[key] = (pointer, item["sha256"])
    saved_keys = []
    for pointer in saved_refs:
        exact = _ref(pointer, "source_copy")
        saved_keys.append((exact["id"], exact["version"]))
    if (
        len(decisions) != len(saved_refs)
        or len(set(saved_keys)) != len(saved_keys)
        or set(saved_keys) != set(expected)
        or any(not isinstance(item, dict) or item.get("copy_ref") != pointer
               for item, pointer in zip(decisions, saved_refs, strict=True))
    ):
        raise OutputRecoveryError("Saved source decisions do not match the pinned call")

    base = {
        "response_ref": response_ref,
        "intent_ref": body["intent_ref"],
        "call_ref": body["call_ref"],
    }
    context_refs = _context_source_refs(
        store, intent=intent, call=call, config=config, mission_id=mission_id,
        task_id=task_id, actor_id=actor_id, saved_refs=saved_refs,
    )
    if context_refs is None:
        return {**base, "status": "reissue_required", "reason": "context_dependencies_unverified"}
    dependencies = list({
        (pointer["id"], pointer["version"]): pointer
        for pointer in [*saved_refs, *context_refs]
    }.values())
    selected_keys = {(item["id"], item["version"]) for item in selected}
    decisions_by_key = {
        (item["copy_ref"]["id"], item["copy_ref"]["version"]): item
        for item in decisions
    }
    use_refs = []
    for pointer in dependencies:
        key = pointer["id"], pointer["version"]
        decision = decisions_by_key.get(key)
        if (
            decision is not None
            and (decision.get("use") != "permitted"
                 or decision.get("allowed_for_model") is not True)
            or key not in selected_keys
        ):
            return {**base, "status": "reissue_required", "reason": "source_context_changed"}
        permitted, use_ref = _permitted_source(
            store, pointer, expected[key][1] if key in expected else None, mission_id
        )
        if not permitted:
            return {**base, "status": "reissue_required", "reason": "source_use_changed"}
        if use_ref is not None:
            use_refs.append(use_ref)

    blob = body.get("blob")
    try:
        if not isinstance(blob, dict) or not isinstance(blob.get("sha256"), str):
            raise ValueError("Missing response blob")
        payload = json.loads(store.read_blob(blob["sha256"]))
    except (IntegrityFault, ValueError, TypeError, UnicodeError):
        return {**base, "status": "content_unavailable"}
    text = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text:
        return {**base, "status": "content_unavailable"}
    if offset > len(text):
        raise OutputRecoveryError("Visible-output offset exceeds saved content")
    window = text[offset:offset + limit]
    next_offset = offset + len(window)
    return {
        **base,
        "status": "available",
        "text": window,
        "window_sha256": hashlib.sha256(window.encode()).hexdigest(),
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "total_characters": len(text),
        "offset": offset,
        "next_offset": next_offset if next_offset < len(text) else None,
        "source_refs": dependencies,
        "source_use_refs": use_refs,
    }
