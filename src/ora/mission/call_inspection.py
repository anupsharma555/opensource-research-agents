"""Read scoped model call evidence for Atlas without exposing private instructions."""

from __future__ import annotations

import json
import math
import re

from ora.coordination.traces import TraceJournal
from ora.credentials import (
    LEGACY_QUOTA_DOMAIN,
    CredentialProfileError,
    validate_selection,
)
from ora.models.groq import classify_missing_required_tool_argument
from ora.retrieval.redact import redact
from ora.state import StateError, StateStore
from ora.state.store import canonical

_TOKEN_QUOTA_ERROR = re.compile(
    r"\bon tokens per (?:(day)\s*\(TPD\)|(minute)\s*\(TPM\)):\s*"
    r"Limit\s+([0-9]{1,12})\s*,\s*Used\s+([0-9]{1,12})\s*,\s*"
    r"Requested\s+([0-9]{1,12})\b",
    re.IGNORECASE,
)


def _heads(store: StateStore, kind: str, mission_id: str) -> list[dict]:
    cursor, found = "", []
    while True:
        page = store.records(kind, limit=500, after_id=cursor)
        found.extend(item for item in page if item["body"].get("mission_id") == mission_id)
        if len(page) < 500:
            return found
        cursor = page[-1]["id"]


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _blob_json(store: StateStore, record: dict) -> dict:
    blob = record["body"].get("blob")
    value = json.loads(store.read_blob(blob["sha256"])) if blob else {}
    return value if isinstance(value, dict) else {}


def inspect_provider_tool_validation(
    store: StateStore, call: dict, attempt: dict, receipt: dict | None,
) -> dict | None:
    """Return only a schema-verified function and missing field from one captured 400."""
    if receipt is None:
        return None
    call_ref = _ref(call)
    body = receipt["body"]
    if (body.get("status") != "failed" or body.get("http_status") != 400
            or body.get("error_kind") != "http_error"
            or body.get("call_ref") != call_ref
            or body.get("mission_id") != call["body"].get("mission_id")
            or body.get("task_id") != call["body"].get("task_id")
            or body.get("attempt_id") != attempt["id"]
            or attempt["body"].get("call_ref") != call_ref
            or attempt["body"].get("mission_id") != body.get("mission_id")
            or attempt["body"].get("task_id") != body.get("task_id")
            or attempt["body"].get("agentId") != call["body"].get("instance_id")
            or call["body"].get("attempt_id") != attempt["id"]):
        return None
    intent_ref = body.get("intent_ref")
    if not isinstance(intent_ref, dict) or intent_ref.get("kind") != "model_request_intent":
        return None
    intent = store.get(**intent_ref)
    if (intent is None or intent["body"].get("call_ref") != call_ref
            or intent["body"].get("mission_id") != body["mission_id"]
            or intent["body"].get("task_id") != body["task_id"]
            or intent["body"].get("attempt_id") != attempt["id"]
            or intent["body"].get("request_sha256") != body.get("request_sha256")):
        return None
    try:
        request = _blob_json(store, intent)
        response = _blob_json(store, receipt)
    except (StateError, ValueError, TypeError, KeyError):
        return None
    tools, error = request.get("tools"), response.get("provider_error")
    if not isinstance(tools, list) or not isinstance(error, dict):
        return None
    matched = classify_missing_required_tool_argument(tools, error)
    if matched is None or matched[0] not in call["body"].get("exposed_tools", []):
        return None
    return {"kind": "missing_required_argument", "function_name": matched[0], "field": matched[1]}


def inspect_provider_token_quota(
    store: StateStore, call: dict, attempt: dict, receipt: dict | None,
) -> dict | None:
    """Classify only one exact saved 429 token bucket, without exposing error text."""
    if receipt is None:
        return None
    call_ref = _ref(call)
    body = receipt["body"]
    if (body.get("status") != "failed" or body.get("http_status") != 429
            or body.get("error_kind") != "http_error"):
        return None
    if (
        store.get(**_ref(call)) != call
        or store.get(**_ref(attempt)) != attempt
        or store.get(**_ref(receipt)) != receipt
        or body.get("call_ref") != call_ref
        or body.get("mission_id") != call["body"].get("mission_id")
        or body.get("task_id") != call["body"].get("task_id")
        or body.get("attempt_id") != attempt["id"]
        or attempt["body"].get("call_ref") != call_ref
        or attempt["body"].get("mission_id") != body.get("mission_id")
        or attempt["body"].get("task_id") != body.get("task_id")
        or attempt["body"].get("agentId") != call["body"].get("instance_id")
        or call["body"].get("attempt_id") != attempt["id"]
    ):
        return None
    intent_ref = body.get("intent_ref")
    if (
        not isinstance(intent_ref, dict)
        or set(intent_ref) != {"kind", "id", "version"}
        or intent_ref.get("kind") != "model_request_intent"
        or not isinstance(intent_ref.get("id"), str)
        or type(intent_ref.get("version")) is not int
    ):
        return None
    intent = store.get(**intent_ref)
    if (
        intent is None
        or intent["body"].get("call_ref") != call_ref
        or intent["body"].get("mission_id") != body["mission_id"]
        or intent["body"].get("task_id") != body["task_id"]
        or intent["body"].get("attempt_id") != attempt["id"]
        or intent["body"].get("request_sha256") != body.get("request_sha256")
    ):
        return None
    try:
        response = _blob_json(store, receipt)
    except (StateError, ValueError, TypeError, KeyError):
        return None
    error = response.get("provider_error")
    if not isinstance(error, dict) or error.get("code") != "rate_limit_exceeded" or error.get("type") != "tokens":
        return None
    message = error.get("message")
    matched = _TOKEN_QUOTA_ERROR.search(message) if isinstance(message, str) else None
    if matched is None:
        return None
    limit, used, requested = map(int, matched.group(3, 4, 5))
    if limit <= 0 or requested <= 0:
        return None
    retry = body.get("retry_after_seconds")
    retry = retry if isinstance(retry, (int, float)) and math.isfinite(retry) and retry >= 0 else None
    return {
        "bucket": "daily" if matched.group(1) else "minute",
        "limit": limit, "used": used, "requested": requested,
        "retry_after_seconds": retry,
    }


def provider_token_quota_explanation(quota: dict) -> str:
    """Present only classified counters and request-specific retry guidance."""
    name = "daily" if quota["bucket"] == "daily" else "per-minute"
    remaining = max(0, quota["limit"] - quota["used"])
    retry = quota["retry_after_seconds"]
    wait = (
        "No retry interval was recorded."
        if retry is None else
        f"The provider advised waiting {retry:g} seconds for the rejected request size."
    )
    return (
        f"Provider reported a {name} token allowance (HTTP 429): limit {quota['limit']:,}, "
        f"used {quota['used']:,}, remaining {remaining:,}, "
        f"requested {quota['requested']:,} for this call. "
        f"{wait} This is not a full allowance reset or a promise that a larger or "
        "different request will fit. No accepted research report came from this call."
    )


def _safe_text(value: object) -> object:
    """Redact credential-shaped fields even inside JSON string payloads."""
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return redact(value)[0]
    if isinstance(decoded, (dict, list)):
        return canonical(redact(decoded)[0])
    return redact(value)[0]


def inspect_calls(store: StateStore, run_id: str, attempt_id: str | None = None) -> list[dict]:
    """Return exact, redacted call records from this run or an inherited parent attempt.

    The request's developer instructions and private provider reasoning are not
    returned. References and inclusion receipts identify inputs; response and
    host tool-result text comes only from saved blobs and is redacted.
    """
    selected = store.get("mission_run", run_id)
    if selected is None:
        raise StateError("Unknown run")
    runs = [selected]
    if attempt_id:
        seen = {run_id}
        while (parent_id := runs[-1]["body"].get("parent_run_id")) and parent_id not in seen:
            seen.add(parent_id)
            parent = store.get("mission_run", parent_id)
            if parent is None:
                break
            runs.append(parent)
    for run in runs:
        mission_id = run["body"]["mission_id"]
        attempts = {
            item["id"]: item for item in _heads(store, "agent_attempt", mission_id)
            if item["body"].get("run_id") == run["id"]
        }
        if attempt_id and attempt_id not in attempts:
            continue
        intents = {
            (item["body"]["call_ref"]["id"], item["body"]["call_ref"]["version"]): item
            for item in _heads(store, "model_request_intent", mission_id)
        }
        responses = {
            (item["body"]["call_ref"]["id"], item["body"]["call_ref"]["version"]): item
            for item in _heads(store, "model_response_receipt", mission_id)
        }
        inclusions = {
            (item["body"]["call_ref"]["id"], item["body"]["call_ref"]["version"]): item
            for item in _heads(store, "context_inclusion", mission_id)
        }
        tool_results = {
            (item["body"]["response_ref"]["id"], item["body"]["response_ref"]["version"]): item
            for item in _heads(store, "context_tool_result", mission_id)
        }
        records = []
        for call in _heads(store, "agent_call", mission_id):
            call_body = call["body"]
            attempt = attempts.get(call_body["attempt_id"])
            if attempt is None or (attempt_id and attempt["id"] != attempt_id):
                continue
            key = (call["id"], call["version"])
            intent, receipt, inclusion = intents.get(key), responses.get(key), inclusions.get(key)
            actor = store.get("agent_instance", call_body["instance_id"], 1)
            config = store.get("agent_configuration", call_body["configuration_id"], 1)
            raw_response = _blob_json(store, receipt) if receipt else {}
            response = None
            outputs = []
            if receipt:
                requested_tools = raw_response.get("tool_calls") or []
                if isinstance(requested_tools, list):
                    requested_tools = [
                        {**tool, "function": {
                            **tool["function"],
                            "arguments": _safe_text(tool["function"].get("arguments")),
                        }} if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
                        else tool for tool in requested_tools
                    ]
                response = {
                    "model": receipt["body"].get("returned_model"),
                    "content": _safe_text(raw_response.get("content")),
                    "tool_calls": requested_tools,
                }
                if response["content"]:
                    outputs.append({"authoredByAgentId": actor["id"], "recordPath": "response.content",
                                    "reference": _ref(receipt)})
                if response["tool_calls"]:
                    outputs.append({"authoredByAgentId": actor["id"], "recordPath": "response.tool_calls",
                                    "reference": _ref(receipt)})
            saved_tool = tool_results.get((receipt["id"], receipt["version"])) if receipt else None
            results = [_blob_json(store, saved_tool)] if saved_tool else []
            inputs = [
                {"reference": pointer, "role": "saved_call_input"}
                for pointer in call_body.get("input_refs", [])
            ]
            if inclusion:
                inputs.extend(
                    {"reference": pointer, "role": "included_in_prepared_request"}
                    for pointer in inclusion["body"].get("included_refs", [])
                )
            record = {
                "id": call["id"], "runId": run["id"], "attemptId": attempt["id"],
                "taskId": call_body["task_id"], "agentId": actor["id"],
                "status": receipt["body"]["status"] if receipt else "prepared",
                "phase": intent["body"].get("phase") if intent else None,
                "createdAt": intent["created_at"] if intent else call["created_at"],
                "completedAt": receipt["created_at"] if receipt else None,
                "response": response, "toolResults": results,
                "returnedReasoning": {"availability": (
                    "restricted" if receipt and receipt["body"].get("reasoning_status") == "returned"
                    else "truncated" if receipt and receipt["body"].get("reasoning_status") == "truncated"
                    else "absent")},
                "trace": {
                    "basis": "linked_saved_records",
                    "author": {
                        "agentId": actor["id"], "name": actor["body"]["display_name"],
                        "role": actor["body"]["role_id"], "taskId": call_body["task_id"],
                        "attemptId": attempt["id"], "runId": run["id"],
                    },
                    "configurationReference": _ref(config) if config else None,
                    "selectedModel": config["body"].get("model_id") if config else None,
                    "callReference": _ref(call),
                    "intentReference": _ref(intent) if intent else None,
                    "inclusionReference": _ref(inclusion) if inclusion else None,
                    "responseReference": _ref(receipt) if receipt else None,
                    "inputs": inputs, "outputs": outputs,
                    "toolResultReferences": [_ref(saved_tool)] if saved_tool else [],
                    "peerInputs": [],
                },
            }
            validation = inspect_provider_tool_validation(store, call, attempt, receipt)
            if validation is not None:
                record["providerValidation"] = validation
            safe, _paths = redact(record)
            # These two validated labels identify routing without disclosing or
            # relaxing redaction of arbitrary credential-shaped record fields.
            if config:
                settings = config["body"].get("settings") or {}
                profile = settings.get("credential_profile", "primary")
                domain = settings.get("quota_domain", LEGACY_QUOTA_DOMAIN)
                try:
                    validate_selection(profile, domain)
                except CredentialProfileError:
                    pass
                else:
                    safe["trace"]["providerProfile"] = profile
                    safe["trace"]["quotaDomain"] = domain
            records.append(safe)
        return sorted(records, key=lambda item: (item["createdAt"], item["id"]))
    return []


class CallInspectionError(StateError):
    """An exact call is unavailable in the selected run."""

    status = 404


def inspect_returned_reasoning(
    store: StateStore,
    traces: TraceJournal,
    run_id: str,
    call_id: str,
    *,
    diagnostic_granted: bool = False,
) -> dict:
    """Read only one authenticated, exact provider reasoning record.

    This projection is intentionally separate from ordinary calls, snapshots,
    peer context and trace exports. A caller must supply the host diagnostic
    grant after local-session authentication.
    """
    if diagnostic_granted is not True:
        raise PermissionError("A local diagnostic session is required")
    run = store.get("mission_run", run_id)
    call = store.get("agent_call", call_id)
    if run is None or call is None:
        raise CallInspectionError("Call unavailable in selected run")
    mission_id = run["body"]["mission_id"]
    call_body = call["body"]
    attempt = store.get("agent_attempt", call_body.get("attempt_id", ""))
    if (
        call_body.get("mission_id") != mission_id
        or attempt is None
        or attempt["body"].get("mission_id") != mission_id
        or attempt["body"].get("run_id") != run_id
        or attempt["body"].get("agentId") != call_body.get("instance_id")
        or attempt["body"].get("call_ref") != _ref(call)
    ):
        raise CallInspectionError("Call unavailable in selected run")
    result = {
        "runId": run_id,
        "callId": call_id,
        "agentId": call_body["instance_id"],
        "attemptId": attempt["id"],
        "status": "unavailable",
    }
    receipt = store.get(
        "model_response_receipt", f"model-response:model-intent:{call_id}:{call['version']}"
    )
    if receipt is None:
        return {**result, "reason": "not_captured"}
    body = receipt["body"]
    if (
        body.get("mission_id") != mission_id
        or body.get("task_id") != call_body.get("task_id")
        or body.get("attempt_id") != attempt["id"]
        or body.get("call_ref") != _ref(call)
    ):
        raise CallInspectionError("Call unavailable in selected run")
    status = body.get("reasoning_status", "unknown")
    if status not in {"returned", "truncated"}:
        return {**result, "reason": status if status in {"absent", "unknown", "unsupported"} else "unavailable"}
    pointer = body.get("reasoning_ref")
    if not isinstance(pointer, dict) or not any(
        edge["relation"] == "has_restricted_reasoning" and edge["target"] == pointer
        for edge in store.links(receipt)
    ):
        return {**result, "reason": "saved_reasoning_unavailable"}
    reasoning = store.get(**pointer)
    if reasoning is None or any(
        reasoning["body"].get(key) != value
        for key, value in {
            "mission_id": mission_id,
            "run_id": run_id,
            "task_id": call_body["task_id"],
            "attempt_id": attempt["id"],
            "instance_id": call_body["instance_id"],
            "call_id": call_id,
            "call_version": call["version"],
            "origin": "provider_returned",
            "access": "restricted_diagnostics",
        }.items()
    ):
        return {**result, "reason": "saved_reasoning_unavailable"}
    try:
        saved = traces.read_reasoning(
            pointer, mission_id=mission_id, diagnostic_granted=True
        )
    except PermissionError:
        return {**result, "reason": "saved_reasoning_unavailable"}
    if saved["status"] != status or not saved.get("text"):
        return {**result, "reason": "saved_reasoning_unavailable"}
    return {
        **result,
        "status": "available",
        "text": saved["text"],
        "origin": saved["origin"],
        "redacted": saved["redacted"],
        "reason": status,
    }
