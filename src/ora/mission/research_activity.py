"""Scoped, pageable peer activity from saved operations, plans and checked sources.

This is an operational view. It does not infer a scientific conclusion from a
search, tool result, or another peer's draft.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ora.coordination.board import ref
from ora.evidence.hit_use import _checked_copy_current_use, checked_hit_dependencies
from ora.models.token_estimate import TokenEstimateUnavailable, estimate_context_tokens
from ora.state import StateError
from ora.state.store import Transaction, canonical

from .evidence_library import _origin_current
from .planning import planning_note_id

MAX_DIGEST_CHARS = 1600  # Conservative: below 512 estimated tokens for ordinary JSON.


def _too_large(value: dict) -> bool:
    encoded = canonical(value)
    try:
        return estimate_context_tokens(encoded) > 512
    except TokenEstimateUnavailable:
        return len(encoded.encode("utf-8")) > MAX_DIGEST_CHARS


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _short(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value.strip() else None


def _copy_current(host: Any, reader: Any, pointer: dict) -> bool:
    copy = reader.get(**pointer) if (
        isinstance(pointer, dict) and set(pointer) == {"kind", "id", "version"}
        and pointer.get("kind") == "source_copy"
    ) else None
    return bool(copy and _checked_copy_current_use(reader, copy)
                and _origin_current(host, reader, pointer))


def _scope(host: Any, *, mission_id: str, run_id: str, actor_id: str,
           task_id: str) -> list[dict]:
    controls = host._controls(run_id)
    own = [item for item in controls if all(item["body"].get(key) == value for key, value in (
        ("mission_id", mission_id), ("run_id", run_id), ("actor_id", actor_id),
        ("task_id", task_id),
    ))]
    actor = host.store.get("agent_instance", actor_id, 1)
    if (not own or actor is None
            or actor["body"].get("mission_id") != mission_id
            or actor["body"].get("run_id") != run_id
            or all(item["body"].get("phase") in {"cancelled", "closed"} for item in own)):
        raise StateError("Research activity reader is outside this peer task")
    return [item for item in controls if item["body"].get("mission_id") == mission_id
            and item["body"].get("run_id") == run_id
            and item["body"].get("actor_id") != actor_id]


def _plan(host: Any, mission_id: str, run_id: str, actor_id: str,
          task_id: str) -> tuple[dict | None, list[dict], dict | None]:
    note = host.store.get("working_note", planning_note_id(mission_id, actor_id, task_id))
    if (note is None or note["body"].get("planning", {}).get("run_id") != run_id
            or any(note["body"].get(key) != value for key, value in (
                ("mission_id", mission_id), ("actor_id", actor_id), ("task_id", task_id),
            ))):
        return None, [], None
    access = host.context.working_note_text_access(note)
    if access["status"] != "available" or any(
        not _copy_current(host, host.store, pointer)
        for pointer in access["source_dependency_refs"]
    ):
        return {"status": "withheld_due_current_source_use"}, [], ref(note)
    try:
        document = json.loads(note["body"]["text"])
    except (TypeError, ValueError, KeyError):
        return {"status": "unavailable"}, [], ref(note)
    steps = [step for step in document.get("steps", [])
             if isinstance(step, dict) and step.get("status") in {"todo", "in_progress", "blocked"}]
    plan = {
        "status": document.get("status"),
        "unresolved_gaps": [text[:180] for text in document.get("uncertainties", [])
                            if isinstance(text, str)][:3],
        "next_steps": [{"status": step.get("status"),
                        "description": _short(step.get("description"), 180)}
                       for step in steps[:2]],
    }
    return plan, access["source_dependency_refs"], ref(note)


def _producer_sources(host: Any, reader: Any, operation: dict) -> tuple[bool, list[dict]]:
    parent = operation["body"].get("parent_spec") or {}
    call_id, version = parent.get("call_id"), parent.get("call_version")
    if not call_id:
        return True, []
    intent = reader.get("model_request_intent", f"model-intent:{call_id}:{version}")
    call = reader.get("agent_call", call_id, version)
    if intent is None or call is None or intent["body"].get("call_ref") != ref(call):
        return False, []
    dependencies = []
    for field in ("source_refs", "context_source_dependency_refs"):
        pointers = intent["body"].get(field)
        if not isinstance(pointers, list):
            return False, []
        for pointer in pointers:
            if not _copy_current(host, reader, pointer):
                return False, []
            if pointer not in dependencies:
                dependencies.append(pointer)
    return True, dependencies


def _proposal_sources(host: Any, reader: Any, proposal: dict) -> tuple[bool, list[dict]]:
    revision = proposal["body"].get("revision") or {}
    revision_refs = revision.get("evidence_refs", [])
    if not isinstance(revision_refs, list):
        return False, []
    pointers = revision_refs
    dependencies = []
    for pointer in pointers:
        if not isinstance(pointer, dict):
            return False, []
        saved = reader.get(**pointer) if set(pointer) == {"kind", "id", "version"} else None
        if saved is None:
            return False, []
        if saved["kind"] in {"source_passage", "source_read_window"}:
            pointer = saved["body"].get("copy_ref")
            saved = reader.get(**pointer) if isinstance(pointer, dict) else None
        if saved and saved["kind"] == "source_copy":
            if not _copy_current(host, reader, pointer):
                return False, []
            if pointer not in dependencies:
                dependencies.append(pointer)
        elif pointer in revision_refs:
            # A revision backed by an unsupported artifact has no source-use
            # proof that can safely authorize its changed prose in this view.
            return False, []
    return True, dependencies


def _entries(host: Any, mission_id: str, run_id: str,
             actor_id: str, task_id: str) -> list[dict]:
    controls = _scope(host, mission_id=mission_id, run_id=run_id,
                      actor_id=actor_id, task_id=task_id)
    operations = host._heads("web_search_operation", mission_id)
    hits = host._heads("retrieval_hit", mission_id)
    jobs = {item["id"]: item for kind in ("web_shared_query_job", "web_search_item")
            for item in host._heads(kind, mission_id)}
    operations_by_peer: dict[tuple[str, str], list[dict]] = {}
    for operation in operations:
        saved = operation["body"]
        if saved.get("run_id") == run_id:
            operations_by_peer.setdefault(
                (saved.get("actor_id"), saved.get("task_id")), []
            ).append(operation)
    hits_by_peer: dict[tuple[str, str], list[dict]] = {}
    for hit in hits:
        saved = hit["body"]
        source = saved.get("source") or {}
        if (saved.get("run_id", run_id) == run_id
                and source.get("origin") == "original_source"
                and source.get("rights_status") not in {"restricted", "not_retained"}):
            hits_by_peer.setdefault(
                (saved.get("agent_instance_id"), saved.get("task_id")), []
            ).append(hit)
    run = host._run(run_id) if hasattr(host, "_run") else None
    initial_refs = run["body"].get("source_refs", []) if run else []
    entries = []
    for control in sorted(controls, key=lambda item: (item["body"].get("task_id", ""), item["id"])):
        body = control["body"]
        peer_id, peer_task = body.get("actor_id"), body.get("task_id")
        peer = host.store.get("agent_instance", peer_id, 1)
        proposal = host.store.get("board_proposal", peer_task)
        if (peer is None or proposal is None
                or peer["body"].get("mission_id") != mission_id
                or peer["body"].get("run_id") != run_id
                or proposal["body"].get("mission_id") != mission_id
                or proposal["body"].get("actor_ref", {}).get("id") != peer_id):
            continue
        searches = []
        peer_operations = sorted(operations_by_peer.get((peer_id, peer_task), []),
                                 key=lambda item: (item["created_at"], item["id"]))
        for index, operation in enumerate(peer_operations):
            saved = operation["body"]
            if (index < len(peer_operations) - 4 and (saved.get("cancel_requested")
                    or len(saved.get("deliveries", [])) >= len(saved.get("items", [])))):
                continue  # Keep every pending round and the four latest settled rounds.
            args = saved.get("arguments") or {}
            queries = args.get("queries") if isinstance(args.get("queries"), list) else []
            deliveries = saved.get("deliveries") if isinstance(saved.get("deliveries"), list) else []
            query_available, query_deps = _producer_sources(host, host.store, operation)
            providers = {args.get("provider")}
            providers.update(delivery.get("provider") for delivery in deliveries)
            for item_id in saved.get("items", []):
                job = jobs.get(item_id)
                if job:
                    providers.add(job["body"].get("provider"))
            searches.append({"operation_ref": ref(operation),
                             "provider": _short(args.get("provider"), 40),
                             "providers": sorted(provider for provider in providers if isinstance(provider, str)),
                             "information_need": args.get("information_need") if query_available else None,
                             "queries": [item for item in queries
                                         if isinstance(item, str)] if query_available else [],
                             "query_count": len(queries),
                             "planned_count": len(saved.get("items", [])),
                             "completed_count": len(deliveries),
                             "progress_units": "provider_requests_including_fallback",
                             "query_status": "available" if query_available else "withheld_due_source_use",
                             "_source_deps": query_deps,
                             "status": "cancel_requested" if saved.get("cancel_requested")
                             else "pending_or_running" if len(deliveries) < len(saved.get("items", []))
                             else "completed"})
        checked = []
        hit_refs = []
        for pointer in body.get("source_refs", []):
            if (pointer not in initial_refs and _copy_current(host, host.store, pointer)
                    and pointer not in checked):
                checked.append(pointer)
        for hit in hits_by_peer.get((peer_id, peer_task), []):
            deps = checked_hit_dependencies(host.store, hit)
            if not deps:
                continue
            for pointer in deps:
                if not _copy_current(host, host.store, pointer):
                    continue
                if pointer not in checked:
                    checked.append(pointer)
            if any(pointer in checked for pointer in deps):
                hit_refs.append(ref(hit))
        plan, plan_deps, plan_ref = _plan(host, mission_id, run_id, peer_id, peer_task)
        proposal_body = proposal["body"]
        proposal_available, proposal_deps = _proposal_sources(host, host.store, proposal)
        entries.append({
            "actor_id": peer_id, "task_id": peer_task,
            "agent_name": _short(peer["body"].get("display_name"), 100),
            "objective": _short(proposal_body.get("objective"), 240) if proposal_available else None,
            "task_status": proposal_body.get("status"),
            "phase": body.get("phase"), "scope_status": body.get("scope_status"),
            "searches": searches, "search_operation_count": len(peer_operations),
            "checked_source_refs": checked[:6], "checked_source_count": len(checked),
            "plan": plan,
            "next_action": _short(proposal_body.get("next_action"), 180) if proposal_available else None,
            "_control_ref": ref(control), "_proposal_ref": ref(proposal),
            "_proposal_source_deps": proposal_deps,
            "_plan_ref": plan_ref, "_plan_source_deps": plan_deps,
            "_hit_refs": hit_refs,
        })
    return entries


def _current_entry(host: Any, reader: Any, entry: dict) -> dict:
    """Keep frozen operational positions while withholding revoked source text."""
    result = {key: value for key, value in entry.items() if not key.startswith("_")}
    safe = []
    for pointer in entry.get("checked_source_refs", []):
        if _copy_current(host, reader, pointer):
            safe.append(pointer)
    if len(safe) != len(entry.get("checked_source_refs", [])):
        result["checked_source_refs"] = safe
        result["checked_source_count"] = len(safe)
        result["source_use_status"] = "some_sources_withheld_due_current_use"
    if any(not _copy_current(host, reader, pointer)
           for pointer in entry.get("_proposal_source_deps", [])):
        result["objective"] = None
        result["next_action"] = None
        result["proposal_status"] = "withheld_due_current_source_use"
    result["searches"] = []
    for search in entry.get("searches", []):
        visible_search = {key: value for key, value in search.items() if not key.startswith("_")}
        if any(not _copy_current(host, reader, pointer)
               for pointer in search.get("_source_deps", [])):
            visible_search["queries"] = []
            visible_search["information_need"] = None
            visible_search["query_status"] = "withheld_due_current_source_use"
        result["searches"].append(visible_search)
    if (entry.get("_plan_ref") and isinstance(entry.get("plan"), dict)
            and entry["plan"].get("status") != "withheld_due_current_source_use"):
        note = reader.get(**entry["_plan_ref"])
        current = reader.get("working_note", entry["_plan_ref"]["id"])
        if note is None or current != note:
            result["plan"] = {"status": "stale_plan_revision"}
        elif any(not _copy_current(host, reader, pointer)
                 for pointer in entry.get("_plan_source_deps", [])):
            result["plan"] = {"status": "withheld_due_current_source_use"}
    return result


def read_research_activity(host: Any, spec: Any, args: dict) -> dict:
    """Page one immutable activity snapshot for this exact caller and run."""
    cursor, limit = args.get("cursor", 0), args.get("limit", 10)
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise StateError("Activity cursor or page limit is invalid")
    scope = {"mission_id": spec.mission_id, "run_id": spec.run_id,
             "actor_id": spec.actor_id, "task_id": spec.proposal_ref["id"]}
    _scope(host, **scope)
    snapshot_id = args.get("snapshot_id")
    if snapshot_id is None:
        snapshot_id = f"research-activity:{_digest([spec.mission_id, spec.work_id])}"
        request = {"scope": scope, "work_id": spec.work_id}

        def create(tx: Transaction) -> dict:
            previous = tx.get("research_activity_snapshot", snapshot_id)
            if previous is not None:
                return ref(previous)
            entries = _entries(host, **scope)
            record = tx.put("research_activity_snapshot", snapshot_id, {
                "schema": "ora.mission.research_activity_snapshot.v1",
                **scope, "entries": entries,
            })
            return ref(record)

        host.store.transact(f"research-activity-snapshot:{spec.work_id}", request,
                            create, mission_id=spec.mission_id)
    snapshot = host.store.get("research_activity_snapshot", snapshot_id, 1)
    if snapshot is None or any(snapshot["body"].get(key) != value for key, value in scope.items()):
        raise StateError("Activity snapshot belongs to another peer task")
    saved = snapshot["body"]["entries"]
    selected = saved[cursor:cursor + limit]
    page = [_current_entry(host, host.store, item) for item in selected]
    source_dependencies = []
    for original, visible in zip(selected, page):
        permitted = [*visible.get("checked_source_refs", [])]
        if visible.get("objective") or visible.get("next_action"):
            permitted.extend(original.get("_proposal_source_deps", []))
        if (isinstance(visible.get("plan"), dict)
                and visible["plan"].get("status") not in {
                    "withheld_due_current_source_use", "stale_plan_revision", "unavailable",
                }):
            permitted.extend(original.get("_plan_source_deps", []))
        for old_search, visible_search in zip(original.get("searches", []),
                                              visible.get("searches", [])):
            if visible_search.get("query_status") == "available":
                permitted.extend(old_search.get("_source_deps", []))
        for pointer in permitted:
            if pointer not in source_dependencies:
                source_dependencies.append(pointer)
    next_cursor = cursor + len(page)
    return {"status": "completed", "snapshot_id": snapshot_id,
            "cursor": cursor, "limit": limit, "total": len(saved), "entries": page,
            "next_cursor": next_cursor if next_cursor < len(saved) else None,
            "source_dependency_refs": source_dependencies,
            "snapshot_scope": "same_mission_run_other_peers",
            "evidence_status": "operational_activity_not_scientific_summary"}


def _meaningful(entry: dict) -> dict:
    return {key: entry.get(key) for key in (
        "objective", "next_action", "checked_source_refs", "checked_source_count",
    )} | {
        "queries": [(search.get("provider"), search.get("queries"), search.get("information_need"))
                    for search in entry.get("searches", [])],
        "plan": entry.get("plan"),
    }


def activity_digest(host: Any, control: dict) -> dict | None:
    """Preview up to four substantive changes without marking them delivered."""
    body = control["body"]
    scope = {"mission_id": body["mission_id"], "run_id": body["run_id"],
             "actor_id": body["actor_id"], "task_id": body["task_id"]}
    entries = _entries(host, **scope)
    delivery_id = f"research-activity-delivery:{_digest(list(scope.values()))}"
    delivered = host.store.get("research_activity_delivery", delivery_id)
    seen = delivered["body"].get("seen", {}) if delivered else {}
    changed = []
    observed = {}
    dependencies = []
    dependencies_by_key = {}
    changed_count = 0
    for item in entries:
        key = f"{item['actor_id']}:{item['task_id']}"
        signature = _digest(_meaningful(item))
        if seen.get(key) == signature:
            continue
        changed_count += 1
        visible = _current_entry(host, host.store, item)
        compact = {"actor_id": item["actor_id"], "task_id": item["task_id"],
                   "agent_name": item["agent_name"],
                   "objective": _short(visible.get("objective"), 100),
                   "query": (_short(visible["searches"][-1]["queries"][0], 80)
                             if visible.get("searches")
                             and visible["searches"][-1].get("queries") else None),
                   "checked_source_refs": visible.get("checked_source_refs", [])[:2],
                   "unresolved_gaps": (visible.get("plan") or {}).get("unresolved_gaps", [])[:1],
                   "next_action": _short(visible.get("next_action"), 100)}
        if len(changed) < 4:
            changed.append(compact)
            observed[key] = signature
            dependencies_by_key[key] = [*compact["checked_source_refs"],
                                        *(item.get("_plan_source_deps", [])
                                          if compact["unresolved_gaps"] else []),
                                        *(item.get("_proposal_source_deps", [])
                                          if compact["objective"] or compact["next_action"] else [])]
            for search in item.get("searches", []):
                if search.get("query_status") == "available":
                    dependencies_by_key[key].extend(search.get("_source_deps", []))
            for pointer in dependencies_by_key[key]:
                if pointer not in dependencies:
                    dependencies.append(pointer)
    if not changed:
        return None
    result = {"status": "peer_activity_changed", "entries": changed,
              "more_changed": changed_count > len(changed),
              "read_more": {"tool": "read_research_activity", "cursor": 0},
              "observed": observed, "source_dependency_refs": dependencies}
    while _too_large(result):
        if len(result["entries"]) > 1:
            removed = result["entries"].pop()
            removed_key = f"{removed['actor_id']}:{removed['task_id']}"
            observed.pop(removed_key, None)
            dependencies_by_key.pop(removed_key, None)
            result["more_changed"] = True
            remaining_refs = [pointer for refs in dependencies_by_key.values()
                              for pointer in refs]
            result["source_dependency_refs"] = [pointer for pointer in dependencies
                                                 if pointer in remaining_refs]
        else:
            entry = result["entries"][0]
            # First keep a shorter but still useful observation with its exact
            # dependency refs. Only a fully stripped navigation hint must stay
            # undelivered so a later turn can present the peer's actual change.
            for field, limit in (("agent_name", 60), ("objective", 60),
                                 ("next_action", 60), ("query", 50)):
                if isinstance(entry.get(field), str):
                    entry[field] = entry[field][:limit]
            entry["unresolved_gaps"] = [gap[:80] for gap in entry["unresolved_gaps"]]
            if not _too_large(result):
                break
            result["entries"][0]["objective"] = None
            result["entries"][0]["next_action"] = None
            result["entries"][0]["unresolved_gaps"] = []
            result["entries"][0]["query"] = None
            result["entries"][0]["checked_source_refs"] = []
            result["source_dependency_refs"] = []
            observed.clear()
            result["more_changed"] = True
            if _too_large(result):
                result["entries"] = []
            break
    return result


def current_activity_use_for_intent(host: Any, tx: Transaction, intent: dict) -> bool:
    """Final dispatch veto for exact source and plan dependencies in a digest."""
    payload = intent.get("research_activity_digest")
    if payload is None:
        return True
    if not isinstance(payload, dict) or payload.get("status") != "peer_activity_changed":
        return False
    for pointer in payload.get("source_dependency_refs", []):
        if not _copy_current(host, tx, pointer):
            return False
    return True


def record_activity_delivery(host: Any, spec: Any, intent: dict) -> dict:
    """Advance seen signatures only after this exact request received a response."""
    payload = intent["body"].get("research_activity_digest")
    if not isinstance(payload, dict) or not isinstance(payload.get("observed"), dict):
        return {"status": "no_activity_digest"}
    if (intent["body"].get("mission_id") != spec.mission_id
            or intent["body"].get("instance_id") != spec.actor_id
            or intent["body"].get("task_id") != spec.proposal_ref["id"]):
        raise StateError("Activity delivery intent belongs to another peer task")
    response = host.store.get("model_response_receipt", f"model-response:{intent['id']}")
    if (response is None or response["body"].get("status") not in {
        "complete", "tool_requested",
    } or response["body"].get("intent_ref") != ref(intent)):
        raise StateError("Activity was not delivered by an accepted model response")
    scope = {"mission_id": spec.mission_id, "run_id": spec.run_id,
             "actor_id": spec.actor_id, "task_id": spec.proposal_ref["id"]}
    delivery_id = f"research-activity-delivery:{_digest(list(scope.values()))}"

    def save(tx: Transaction) -> dict:
        old = tx.get("research_activity_delivery", delivery_id)
        seen = dict(old["body"].get("seen", {})) if old else {}
        seen.update(payload["observed"])
        saved = tx.put("research_activity_delivery", delivery_id, {
            "schema": "ora.mission.research_activity_delivery.v1", **scope,
            "seen": seen, "last_intent_ref": ref(intent),
            "last_response_ref": ref(response),
        }, expected_version=old["version"] if old else 0)
        return ref(saved)

    pointer = host.store.transact(f"research-activity-delivered:{intent['id']}",
                                  {"intent_ref": ref(intent), "response_ref": ref(response)},
                                  save, mission_id=spec.mission_id)
    return {"status": "recorded", "delivery_ref": pointer}
