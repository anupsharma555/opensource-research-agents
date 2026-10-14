"""Task-scoped, model-authored continuation summaries and exact history paging."""

from __future__ import annotations

import hashlib
import json

from ora.coordination.board import ref
from ora.state.source_lineage import inherited_public_source
from ora.state.store import canonical

from .context import ContextError, WorkingContext

MAX_SUMMARY_CHARS = 8000
MAX_HISTORY_PAGE_CHARS = 1800


def _scoped_source_copy(context: WorkingContext, pointer: object, *,
                        mission_id: str, actor_id: str, task_id: str) -> dict | None:
    """Resolve a local or exactly inherited source for this peer's current task."""
    if (type(pointer) is not dict or set(pointer) != {"kind", "id", "version"}
            or pointer["kind"] != "source_copy"
            or type(pointer["id"]) is not str
            or type(pointer["version"]) is not int or pointer["version"] <= 0):
        return None
    copy = context.store.get(**pointer)
    if copy is None or ref(copy) != pointer:
        return None
    owner = copy["body"].get("mission_id")
    if owner in (None, mission_id):
        return copy
    actor = context.store.get("agent_instance", actor_id, 1)
    if (actor is None or actor["body"].get("mission_id") != mission_id
            or not inherited_public_source(
                context.store, mission_id=mission_id,
                run_id=actor["body"].get("run_id"), copy_ref=pointer,
                actor_id=actor_id, task_id=task_id,
            )):
        return None
    return copy


def _source_dependencies(context: WorkingContext, pointers: list[dict], *,
                         mission_id: str, actor_id: str, task_id: str) -> list[dict]:
    """Require complete source lineage for every prior model input and saved result."""
    store = context.store
    dependencies: dict[tuple[str, str, int], dict] = {}

    def add(pointer: dict) -> None:
        if _scoped_source_copy(
            context, pointer, mission_id=mission_id, actor_id=actor_id, task_id=task_id,
        ) is None:
            raise ContextError("Conversation source lineage is incomplete")
        dependencies[(pointer["kind"], pointer["id"], pointer["version"])] = pointer

    for response_ref in pointers:
        response = store.get(**response_ref)
        if (response is None or ref(response) != response_ref
                or response["body"].get("mission_id") != mission_id
                or response["body"].get("task_id") != task_id):
            raise ContextError("Conversation turn is unavailable")
        call_ref = response["body"].get("call_ref")
        call = store.get(**call_ref) if isinstance(call_ref, dict) else None
        if (call is None or ref(call) != call_ref
                or call["body"].get("instance_id") != actor_id
                or call["body"].get("mission_id") != mission_id
                or call["body"].get("task_id") != task_id):
            raise ContextError("Conversation call lineage is incomplete")
        intent = store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
        if intent is None or intent["body"].get("call_ref") != call_ref:
            raise ContextError("Conversation input lineage is incomplete")
        for field in ("source_refs", "context_source_dependency_refs"):
            refs = intent["body"].get(field)
            if not isinstance(refs, list):
                raise ContextError("Conversation source lineage is incomplete")
            for pointer in refs:
                add(pointer)
        result = store.get("context_tool_result", f"tool-result:{response['id']}")
        if result is None or result["body"].get("response_ref") != response_ref:
            raise ContextError("Conversation result lineage is incomplete")
        try:
            payload = json.loads(store.read_blob(result["body"]["blob"]["sha256"]))
            content = json.loads(payload["content"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ContextError("Conversation result projection is unavailable") from exc

        def visit(value: object) -> None:
            if isinstance(value, list):
                for item in value:
                    visit(item)
            elif isinstance(value, dict):
                if set(("kind", "id", "version")) <= value.keys():
                    pointer = {key: value[key] for key in ("kind", "id", "version")}
                    if pointer["kind"] == "source_copy":
                        add(pointer)
                    elif pointer["kind"] in {"source_read_window", "source_passage"}:
                        record = store.get(**pointer)
                        if record is None or ref(record) != pointer:
                            raise ContextError("Conversation source window lineage is incomplete")
                        add(record["body"].get("copy_ref"))
                for item in value.values():
                    visit(item)

        visit(content)
        if payload.get("name") == "read_source_window" and isinstance(content, dict) and content.get("window_ref"):
            window_ref = content["window_ref"]
            window = store.get(**window_ref) if isinstance(window_ref, dict) else None
            if (window is None or ref(window) != window_ref
                    or window["body"].get("copy_ref") != content.get("copy_ref")
                    or window["body"].get("passage_refs") != content.get("passage_refs")):
                raise ContextError("Conversation source window lineage is incomplete")
            add(window["body"]["copy_ref"])
        effect_ref = result["body"].get("effect_ref")
        if (payload.get("name") in {"inspect_hit", "fetch_source"}
                and isinstance(content, dict) and content.get("status") == "inspected"
                and (not isinstance(effect_ref, dict)
                     or effect_ref.get("kind") != "source_copy")):
            raise ContextError("Inspected source lineage is incomplete")
        if effect_ref:
            effect = store.get(**effect_ref)
            if effect is None:
                raise ContextError("Conversation effect lineage is incomplete")
            if effect_ref["kind"] == "source_copy":
                add(effect_ref)
            for pointer in effect["body"].get("output_refs", []):
                if isinstance(pointer, dict) and pointer.get("kind") == "source_copy":
                    add(pointer)
            for pointer in effect["body"].get("source_refs", []):
                add(pointer)
        if payload.get("name") == "read_conversation_history" and isinstance(content, dict) and content.get("schema") == "ora.conversation_history_page.v1":
            archive_ref = content.get("archive_ref")
            prior_archive = store.get(**archive_ref) if isinstance(archive_ref, dict) else None
            if (prior_archive is None or ref(prior_archive) != archive_ref
                    or prior_archive["body"].get("mission_id") != call["body"].get("mission_id")
                    or prior_archive["body"].get("actor_id") != call["body"].get("instance_id")
                    or prior_archive["body"].get("task_id") != call["body"].get("task_id")):
                raise ContextError("Nested conversation page is outside this task")
            for pointer in prior_archive["body"].get("source_dependency_refs", []):
                add(pointer)
    for pointer in dependencies.values():
        copy = store.get(**pointer)
        if context._source_status(copy)["model_input_status"] != "permitted":
            raise ContextError("Conversation source use is unavailable")
    return list(dependencies.values())


def save_compaction(context: WorkingContext, *, control: dict, response_ref: dict,
                    summary: str, operation_id: str) -> dict:
    """Save an immutable snapshot and its peer-written summary for one tool call."""
    if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_SUMMARY_CHARS:
        raise ContextError("A nonempty summary of at most 8000 characters is required")
    body = control["body"]
    response = context.store.get(**response_ref)
    if (response is None or response["body"].get("mission_id") != body["mission_id"]
            or response["body"].get("task_id") != body["task_id"]
            or response["body"].get("status") != "tool_requested"):
        raise ContextError("Compaction must follow this peer's saved tool request")
    call = context.store.get(**response["body"]["call_ref"])
    if call is None or call["body"].get("instance_id") != body["actor_id"]:
        raise ContextError("Compaction actor differs from its saved call")
    archive_id = f"conversation-archive:{response['id']}"
    summary_id = f"conversation-summary:{response['id']}"
    existing_archive = context.store.get("conversation_archive", archive_id, 1)
    existing_summary = context.store.get("context_summary", summary_id, 1)
    if existing_archive or existing_summary:
        if (existing_archive is None or existing_summary is None
                or existing_summary["body"].get("text") != summary.strip()
                or existing_summary["body"].get("archive_ref") != ref(existing_archive)
                or any(existing_archive["body"].get(key) != body[value]
                       or existing_summary["body"].get(key) != body[value]
                       for key, value in (("mission_id", "mission_id"),
                                          ("actor_id", "actor_id"),
                                          ("task_id", "task_id")))):
            raise ContextError("Compaction replay differs from its saved snapshot")
        for pointer in existing_archive["body"].get("source_dependency_refs", []):
            copy = _scoped_source_copy(
                context, pointer, mission_id=body["mission_id"],
                actor_id=body["actor_id"], task_id=body["task_id"],
            )
            if copy is None or context._source_status(copy)["model_input_status"] != "permitted":
                raise ContextError("Conversation source use is unavailable")
        return {"archive_ref": ref(existing_archive), "summary_ref": ref(existing_summary)}
    turns = list(body.get("tool_turn_refs", []))
    for pointer in turns:
        prior = context.store.get(**pointer)
        prior_call = context.store.get(**prior["body"]["call_ref"]) if prior else None
        if (prior is None or prior["body"].get("mission_id") != body["mission_id"]
                or prior["body"].get("task_id") != body["task_id"]
                or prior_call is None or prior_call["body"].get("instance_id") != body["actor_id"]):
            raise ContextError("Conversation contains a foreign or missing turn")
    source_refs = _source_dependencies(
        context, turns, mission_id=body["mission_id"], actor_id=body["actor_id"],
        task_id=body["task_id"],
    )
    current_intent = context.store.get(
        "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
    )
    if current_intent is None or current_intent["body"].get("call_ref") != ref(call):
        raise ContextError("Current compaction input lineage is incomplete")
    for field in ("source_refs", "context_source_dependency_refs"):
        pointers = current_intent["body"].get(field)
        if not isinstance(pointers, list):
            raise ContextError("Current compaction source lineage is incomplete")
        for pointer in pointers:
            copy = _scoped_source_copy(
                context, pointer, mission_id=body["mission_id"],
                actor_id=body["actor_id"], task_id=body["task_id"],
            )
            if copy is None:
                raise ContextError("Current compaction source lineage is incomplete")
            if pointer not in source_refs:
                source_refs.append(pointer)
    prior_summary_ref = body.get("conversation_summary_ref")
    if prior_summary_ref:
        prior_summary = context.store.get(**prior_summary_ref)
        if (prior_summary is None or ref(prior_summary) != prior_summary_ref
                or any(prior_summary["body"].get(key) != body[value] for key, value in (
                    ("mission_id", "mission_id"), ("actor_id", "actor_id"), ("task_id", "task_id")
                ))):
            raise ContextError("Prior conversation summary is outside this task")
        for pointer in prior_summary["body"].get("source_dependency_refs", []):
            if _scoped_source_copy(
                context, pointer, mission_id=body["mission_id"],
                actor_id=body["actor_id"], task_id=body["task_id"],
            ) is None:
                raise ContextError("Prior conversation source lineage is incomplete")
            if pointer not in source_refs:
                source_refs.append(pointer)
    for pointer in source_refs:
        copy = _scoped_source_copy(
            context, pointer, mission_id=body["mission_id"],
            actor_id=body["actor_id"], task_id=body["task_id"],
        )
        if copy is None or context._source_status(copy)["model_input_status"] != "permitted":
            raise ContextError("Conversation source use is unavailable")
    archived_note_refs = []
    after = ""
    while True:
        page = context.store.records("working_note", after_id=after)
        archived_note_refs.extend(ref(item) for item in page if all(
            item["body"].get(key) == body[value] for key, value in (
                ("mission_id", "mission_id"), ("actor_id", "actor_id"), ("task_id", "task_id")
            )
        ))
        if len(page) < 1000:
            break
        after = page[-1]["id"]
    archive_body = {
        "schema": "ora.conversation_archive.v1", "mission_id": body["mission_id"],
        "actor_id": body["actor_id"], "task_id": body["task_id"],
        "turn_refs": turns, "source_dependency_refs": source_refs,
        "archived_note_refs": archived_note_refs,
        "prior_summary_ref": prior_summary_ref,
    }
    summary_body = {
        "schema": "ora.context_summary.v2", "mission_id": body["mission_id"],
        "actor_id": body["actor_id"], "task_id": body["task_id"],
        "text": summary.strip(), "authority": "model_authored_continuation_aid",
        "archive_ref": {"kind": "conversation_archive", "id": archive_id, "version": 1},
        "archived_note_refs": archived_note_refs,
        "source_dependency_refs": source_refs, "input_refs": [], "source_refs": source_refs,
    }

    def write(tx):
        old_archive = tx.get("conversation_archive", archive_id)
        old_summary = tx.get("context_summary", summary_id)
        if old_archive or old_summary:
            if (old_archive is None or old_summary is None
                    or old_archive["body"] != archive_body or old_summary["body"] != summary_body):
                raise ContextError("Compaction replay differs from its saved snapshot")
            return {"archive_ref": ref(old_archive), "summary_ref": ref(old_summary)}
        archive = tx.put("conversation_archive", archive_id, archive_body)
        saved = tx.put("context_summary", summary_id, summary_body)
        tx.link(saved, archive, "summarizes")
        return {"archive_ref": ref(archive), "summary_ref": ref(saved)}

    return context.store.transact(operation_id, {"archive": archive_body, "summary": summary_body},
                                  write, mission_id=body["mission_id"])


def read_history(context: WorkingContext, *, archive_id: str, mission_id: str,
                 actor_id: str, task_id: str, cursor: int, limit: int,
                 projection_sha256: str) -> dict:
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= MAX_HISTORY_PAGE_CHARS:
        raise ContextError("Conversation page needs a bounded cursor and limit")
    if not isinstance(projection_sha256, str):
        raise ContextError("Conversation projection hash is required")
    archive = context.store.get("conversation_archive", archive_id, 1)
    if archive is None or any(archive["body"].get(key) != value for key, value in (
        ("mission_id", mission_id), ("actor_id", actor_id), ("task_id", task_id)
    )):
        raise ContextError("Conversation archive is outside this peer's task")
    dependencies = _source_dependencies(
        context, archive["body"].get("turn_refs", []), mission_id=mission_id,
        actor_id=actor_id, task_id=task_id,
    )
    saved_dependencies = archive["body"].get("source_dependency_refs")
    if (not isinstance(saved_dependencies, list)
            or any(pointer not in saved_dependencies for pointer in dependencies)):
        raise ContextError("Conversation source lineage changed")
    for pointer in saved_dependencies:
        copy = _scoped_source_copy(
            context, pointer, mission_id=mission_id,
            actor_id=actor_id, task_id=task_id,
        )
        if copy is None or context._source_status(copy)["model_input_status"] != "permitted":
            raise ContextError("Conversation source use is unavailable")
    authorization = [context._source_status(context.store.get(**pointer))
                     for pointer in saved_dependencies]
    digest_state = hashlib.sha256()
    selected_parts = []
    position = 0

    def append(part: str) -> None:
        nonlocal position
        digest_state.update(part.encode())
        start = max(0, cursor - position)
        stop = min(len(part), cursor + limit - position)
        if start < stop:
            selected_parts.append(part[start:stop])
        position += len(part)

    append("[")
    for number, pointer in enumerate(archive["body"]["turn_refs"]):
        if number:
            append(",")
        response = context.store.get(**pointer)
        raw = json.loads(context.store.read_blob(response["body"]["blob"]["sha256"]))
        calls = raw.get("tool_calls") or []
        if len(calls) != 1:
            raise ContextError("Archived conversation turn is malformed")
        name = calls[0].get("function", {}).get("name")
        if name == "read_conversation_history":
            result = context.store.get("context_tool_result", f"tool-result:{response['id']}")
            payload = json.loads(context.store.read_blob(result["body"]["blob"]["sha256"]))
            if payload.get("tool_call_id") != calls[0].get("id") or payload.get("name") != name:
                raise ContextError("Nested conversation result differs from its call")
            exchange = [
                {"role": "assistant", "content": raw.get("content"), "tool_calls": calls},
                payload,
            ]
        else:
            exchange, _ = context._tool_exchange(pointer, scope={
                "mission_id": mission_id, "actor_id": actor_id, "task_id": task_id,
            })
        append(canonical({"response_ref": pointer, "messages": exchange}))
    append("]")
    digest_state.update(b"\0")
    digest_state.update(canonical(authorization).encode())
    digest = digest_state.hexdigest()
    if cursor and projection_sha256 != digest:
        raise ContextError("Conversation projection changed; restart at cursor zero")
    if cursor > position:
        raise ContextError("Conversation cursor is beyond this projection")
    chunk = "".join(selected_parts)
    next_cursor = cursor + len(chunk)
    return {
        "schema": "ora.conversation_history_page.v1", "archive_ref": ref(archive),
        "cursor": cursor, "content": chunk, "projection_sha256": digest,
        "next_request": ({"tool": "read_conversation_history", "archive_id": archive_id,
                          "cursor": next_cursor, "limit": limit,
                          "projection_sha256": digest} if next_cursor < position else None),
    }
