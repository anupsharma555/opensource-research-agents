"""Read one exact saved peer exchange item for the authenticated local Atlas."""

from __future__ import annotations

import json
import re

from ora.retrieval.redact import redact, redact_url
from ora.state import StateStore
from ora.state.store import canonical


class PeerInspectionError(ValueError):
    """A malformed peer-record selection."""

    status = 400


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _pointer(value: object, kind: str | None = None) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"kind", "id", "version"}
        and isinstance(value["kind"], str)
        and isinstance(value["id"], str)
        and type(value["version"]) is int
        and value["version"] > 0
        and (kind is None or value["kind"] == kind)
    )


def _safe(value: object) -> tuple[object, bool]:
    """Redact JSON fields and credential-shaped text inside JSON strings."""
    clean, paths = redact(value)

    def clean_url(match: re.Match[str]) -> str:
        original = match.group(0)
        url = original.rstrip(".,;!?)")
        cleaned, changed = redact_url(url)
        return (cleaned if changed else url) + original[len(url):]

    def embedded(item: object) -> object:
        if isinstance(item, dict):
            return {key: embedded(child) for key, child in item.items()}
        if isinstance(item, list):
            return [embedded(child) for child in item]
        if isinstance(item, str):
            item = re.sub(r"https?://[^\s<>\"']+", clean_url, item)
            try:
                decoded = json.loads(item)
            except (ValueError, TypeError):
                return item
            if isinstance(decoded, (dict, list)):
                cleaned, changed = redact(decoded)
                return canonical(cleaned) if changed else item
        return item

    finished = embedded(clean)
    return finished, bool(paths) or finished != value


def _snapshot_for_run(store: StateStore, run: dict, snapshot_ref: dict | None) -> dict | None:
    if not _pointer(snapshot_ref, "research_snapshot") or snapshot_ref["version"] != 1:
        return None
    snapshot = store.get(**snapshot_ref)
    if (
        snapshot is None
        or not snapshot["id"].startswith(f"snapshot-{run['id']}-")
        or snapshot["body"].get("mission_id") != run["body"].get("mission_id")
    ):
        return None
    expected_parent = run["body"].get("parent_snapshot_id")
    actual_parent = snapshot["body"].get("parent")
    if expected_parent:
        if actual_parent != {"kind": "research_snapshot", "id": expected_parent, "version": 1}:
            return None
    elif actual_parent is not None:
        return None
    return snapshot


def _visible_snapshots(store: StateStore, run_id: str) -> list[tuple[dict, dict]]:
    run = store.get("mission_run", run_id)
    if run is None:
        return []
    head = store.get("mission_snapshot_head", run_id)
    if (
        head is None
        or head["body"].get("run_id") != run_id
        or head["body"].get("mission_id") != run["body"].get("mission_id")
    ):
        return []
    snapshot = _snapshot_for_run(store, run, head["body"].get("snapshot_ref"))
    if snapshot is None:
        return []
    visible = [(run, snapshot)]
    seen = {run_id}
    while parent_id := run["body"].get("parent_run_id"):
        if parent_id in seen:
            break
        seen.add(parent_id)
        parent = store.get("mission_run", parent_id)
        if parent is None:
            break
        pointer = snapshot["body"].get("parent")
        inherited = _snapshot_for_run(store, parent, pointer)
        if inherited is None:
            break
        visible.append((parent, inherited))
        run, snapshot = parent, inherited
    return visible


def _included(snapshot: dict, pointer: dict) -> bool:
    return pointer in snapshot["body"].get("detail_refs", [])


def _request_in(snapshot: dict, store: StateStore, request_id: str) -> dict | None:
    pointers = [pointer for pointer in snapshot["body"].get("detail_refs", [])
                if _pointer(pointer, "mail_request") and pointer["id"] == request_id]
    return store.get(**pointers[0]) if len(pointers) == 1 else None


def _actor(store: StateStore, pointer: object, run: dict) -> dict | None:
    if not _pointer(pointer, "agent_instance") or pointer["version"] != 1:
        return None
    actor = store.get(**pointer)
    return actor if actor and actor["body"].get("mission_id") == run["body"]["mission_id"] \
        and actor["body"].get("run_id") == run["id"] else None


def _request_message(store: StateStore, snapshot: dict, run: dict, request: dict) -> dict | None:
    body = request["body"]
    pointer = body.get("message_ref")
    if not _pointer(pointer, "mail_message"):
        return None
    included = [item for item in snapshot["body"].get("detail_refs", [])
                if _pointer(item, "mail_message") and item["id"] == pointer["id"]]
    if len(included) != 1 or included[0]["version"] < pointer["version"]:
        return None
    message = store.get(**pointer)
    sender = message["body"].get("sender_ref") if message else None
    recipient = message["body"].get("recipient_ref") if message else None
    if (
        message is None or body.get("mission_id") != run["body"]["mission_id"]
        or message["body"].get("mission_id") != run["body"]["mission_id"]
        or message["body"].get("request_id") != request["id"]
        or message["body"].get("correlation_id") != request["id"]
        or not _actor(store, sender, run) or not _actor(store, recipient, run)
        or sender["id"] != body.get("requester_id")
        or recipient["id"] != body.get("recipient_id")
    ):
        return None
    return message


def _message_content(store: StateStore, snapshot: dict, run: dict, record: dict) -> tuple[object, dict] | None:
    body = record["body"]
    sender = body.get("sender_ref")
    recipient = body.get("recipient_ref")
    if (
        body.get("mission_id") != run["body"]["mission_id"]
        or not _actor(store, sender, run) or not _actor(store, recipient, run)
    ):
        return None
    request_id = body.get("request_id")
    if request_id is not None:
        request = _request_in(snapshot, store, request_id)
        original = _request_message(store, snapshot, run, request) if request else None
        if (
            original is None or original["id"] != record["id"]
            or original["version"] > record["version"]
            or body.get("correlation_id") != request_id
            or original["body"].get("sender_ref") != sender
            or original["body"].get("recipient_ref") != recipient
            or original["body"].get("payload") != body.get("payload")
        ):
            return None
    return body.get("payload"), {
        "senderId": sender["id"], "recipientId": recipient["id"],
        "requestId": request_id, "createdAt": body.get("created_at"),
        "deliveredAt": body.get("delivered_at"),
        "acknowledgedAt": body.get("acknowledged_at"),
    }


def _answer_content(store: StateStore, snapshot: dict, run: dict, record: dict) -> tuple[object, dict, dict] | None:
    body = record["body"]
    request = _request_in(snapshot, store, body.get("request_id", ""))
    if (
        body.get("mission_id") != run["body"]["mission_id"]
        or request is None
        or request["body"].get("mission_id") != run["body"]["mission_id"]
        or _ref(record) not in request["body"].get("answer_refs", [])
        or body.get("responder_id") != request["body"].get("recipient_id")
        or _request_message(store, snapshot, run, request) is None
    ):
        return None
    requester = store.get("agent_instance", request["body"]["requester_id"], 1)
    responder = store.get("agent_instance", body["responder_id"], 1)
    if (
        not requester or not responder
        or not _actor(store, _ref(requester), run)
        or not _actor(store, _ref(responder), run)
    ):
        return None
    pointer = body.get("artifact_ref")
    if not _pointer(pointer, "report"):
        return None
    report = store.get(**pointer)
    if (
        report is None
        or report["body"].get("mission_id") != run["body"]["mission_id"]
        or report["body"].get("run_id") != run["id"]
        or report["body"].get("kind") != "research_contribution"
    ):
        return None
    attempt_ref = report["body"].get("producer_attempt_ref")
    attempt = store.get(**attempt_ref) if _pointer(attempt_ref, "agent_attempt") else None
    if (
        attempt is None
        or attempt["body"].get("mission_id") != run["body"]["mission_id"]
        or attempt["body"].get("run_id") != run["id"]
        or attempt["body"].get("agentId") != body["responder_id"]
    ):
        return None
    content = {key: report["body"].get(key) for key in ("title", "body", "citations", "limitations")}
    return content, {
        "senderId": responder["id"], "recipientId": requester["id"],
        "requestId": request["id"], "createdAt": body.get("created_at"),
        "partial": body.get("partial") is True,
    }, pointer


def inspect_peer_record(store: StateStore, run_id: str, kind: str, record_id: str, version: int) -> dict:
    """Return one exact saved message or permitted scientific answer body."""
    if (
        not isinstance(run_id, str) or not run_id
        or kind not in {"mail_message", "mail_answer"}
        or not isinstance(record_id, str) or not record_id
        or type(version) is not int or version < 1
    ):
        raise PeerInspectionError("One exact run, peer kind, ID and positive version are required")
    pointer = {"kind": kind, "id": record_id, "version": version}
    base = {
        "runId": run_id, "originRunId": None, "reference": pointer,
        "senderId": None, "recipientId": None, "requestId": None,
        "createdAt": None, "status": "unavailable", "content": None,
        "contentReference": None, "redacted": False,
    }
    for run, snapshot in _visible_snapshots(store, run_id):
        if not _included(snapshot, pointer):
            continue
        record = store.get(**pointer)
        if record is None:
            return {**base, "reason": "saved_record_unavailable"}
        if kind == "mail_message":
            result = _message_content(store, snapshot, run, record)
            if result is None:
                return {**base, "reason": "saved_lineage_unavailable"}
            content, details = result
            content_ref = pointer
        else:
            result = _answer_content(store, snapshot, run, record)
            if result is None:
                return {**base, "reason": "answer_content_unavailable"}
            content, details, content_ref = result
        safe, redacted = _safe(content)
        return {
            **base, **details, "originRunId": run["id"],
            "status": "available", "content": safe,
            "contentReference": content_ref, "redacted": redacted,
        }
    return {**base, "reason": "record_not_in_selected_run"}
