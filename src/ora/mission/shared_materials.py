"""List compact, currently usable pointers to another peer's saved work.

This is a discovery view, not source admission or a report-text grant. Exact
source and report readers still check task scope, producer lineage and current
use when a peer chooses to inspect a listed pointer.
"""

from __future__ import annotations

from ora.coordination.board import ref
from ora.evidence.hit_use import checked_hit_dependencies as _checked_hit_dependencies
from ora.evidence.hit_use import hit_allows_reinspection
from ora.state import StateError, StateStore, Transaction
from ora.state.store import unpack

from .report_lineage import accepted_report, report_text_access


def _heads(store: StateStore | Transaction, kind: str) -> list[dict]:
    if isinstance(store, Transaction):
        return [unpack(row) for row in store.db.execute(
            "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) "
            "WHERE r.kind=? ORDER BY r.id", (kind,),
        )]
    after, records = "", []
    while True:
        page = store.records(kind, limit=500, after_id=after)
        records.extend(page)
        if len(page) < 500:
            return records
        after = page[-1]["id"]


def _exact(pointer: object, kind: str) -> bool:
    return bool(
        type(pointer) is dict
        and set(pointer) == {"kind", "id", "version"}
        and pointer.get("kind") == kind
        and type(pointer.get("id")) is str and pointer["id"]
        and type(pointer.get("version")) is int and pointer["version"] > 0
    )


def _requester_control(
    controls: list[dict], mission_id: str, actor_id: str, task_id: str,
) -> bool:
    return any(
        item["body"].get("mission_id") == mission_id
        and item["body"].get("actor_id") == actor_id
        and item["body"].get("task_id") == task_id
        for item in controls
    )


def shared_hit_access(store: StateStore | Transaction, hit: dict) -> bool:
    """Veto another intake from a hit whose same-byte checked copy lost use.

    An independent retrieval hit has a different exact identity. Never-inspected
    and metadata-only hits remain candidates, not model-source permissions.
    """
    return hit_allows_reinspection(store, hit)


def _origin(store: StateStore | Transaction, *, mission_id: str, actor_id: str,
            task_id: str, run_id: str | None) -> dict | None:
    actor = store.get("agent_instance", actor_id, 1)
    task = store.get("board_proposal", task_id)
    if (actor is None or task is None
            or actor["body"].get("mission_id") != mission_id
            or task["body"].get("mission_id") != mission_id
            or not isinstance(run_id, str)
            or actor["body"].get("run_id") != run_id):
        return None
    name = actor["body"].get("display_name")
    return {"origin_actor_id": actor_id, "origin_task_id": task_id,
            "origin_run_id": run_id,
            "origin_display_name": name[:100] if isinstance(name, str) else None}


def _entry(
    store: StateStore | Transaction, *, mission_id: str, actor_id: str, task_id: str,
    pointer: dict, controls: list[dict],
) -> dict | None:
    saved = store.get(**pointer)
    if saved is None or ref(saved) != pointer:
        return None
    if pointer["kind"] == "retrieval_hit":
        body = saved["body"]
        dependencies = _checked_hit_dependencies(store, saved)
        if (store.get("retrieval_hit", saved["id"]) != saved
                or body.get("schema") != "ora.retrieval.hit.v1"
                or body.get("mission_id") != mission_id
                or body.get("agent_instance_id") == actor_id
                or body.get("route") == "groq.browser_search.url_leads"
                or not isinstance(body.get("source"), dict)
                or body["source"].get("origin") != "original_source"
                or body["source"].get("rights_status") in {"restricted", "not_retained"}
                or dependencies is None):
            return None
        peer = store.get("agent_instance", body.get("agent_instance_id"), 1)
        origin = _origin(
            store, mission_id=mission_id, actor_id=body["agent_instance_id"],
            task_id=body.get("task_id"),
            run_id=peer["body"].get("run_id") if peer else None,
        )
        if origin is None:
            return None
        source = body["source"]
        metadata_only = source.get("access_surface") == "metadata"
        actionable = metadata_only or source.get("rights_status") == "permitted"
        title = source.get("title")
        return {
            "type": "shared_scholarly_hit", "ref": pointer, **origin,
            "status": "metadata_only" if metadata_only else "candidate_requires_inspection"
            if actionable else "source_use_unresolved",
            "route": body.get("route"),
            "title": title[:160] if isinstance(title, str) else None,
            "source_dependency_refs": dependencies,
            "fetch": {"tool": "inspect_hit", "hit_id": saved["id"]} if actionable else None,
        }

    for control in controls:
        body = control["body"]
        if (body.get("mission_id") != mission_id
                or body.get("actor_id") == actor_id
                or body.get("task_id") == task_id
                or body.get("report_ref") != pointer):
            continue
        try:
            report = accepted_report(store, control, mission_id)
        except (KeyError, TypeError, ValueError, StateError):
            continue
        if report is None or ref(report) != pointer:
            continue
        origin = _origin(
            store, mission_id=mission_id, actor_id=body.get("actor_id"),
            task_id=body.get("task_id"), run_id=body.get("run_id"),
        )
        if origin is None:
            continue
        attempt_ref = report["body"].get("producer_attempt_ref")
        if attempt_ref is not None:
            attempt = store.get(**attempt_ref) if _exact(attempt_ref, "agent_attempt") else None
            if (attempt is None or attempt["body"].get("agentId") != body["actor_id"]
                    or attempt["body"].get("task_id") != body["task_id"]
                    or attempt["body"].get("run_id") != body["run_id"]):
                continue
        access = report_text_access(store, report)
        available = access["status"] == "available"
        title = report["body"].get("title") if available else None
        return {
            "type": "shared_accepted_report", "ref": pointer, **origin,
            "status": "accepted_text_available" if available else "accepted_text_withheld",
            "title": title[:160] if isinstance(title, str) else None,
            "reader_tool": "read_context_record",
        }
    return None


def shared_material_entry(
    store: StateStore | Transaction, *, mission_id: str, actor_id: str, task_id: str,
    pointer: dict,
) -> dict | None:
    """Recheck one exact pointer at a scoped context-page/read boundary."""
    if (not isinstance(mission_id, str) or not mission_id
            or not isinstance(actor_id, str) or not actor_id
            or not isinstance(task_id, str) or not task_id
            or not (_exact(pointer, "retrieval_hit") or _exact(pointer, "report"))):
        return None
    controls = _heads(store, "mission_task_control")
    if not _requester_control(controls, mission_id, actor_id, task_id):
        return None
    return _entry(store, mission_id=mission_id, actor_id=actor_id,
                  task_id=task_id, pointer=pointer, controls=controls)


def shared_material_view(entry: dict) -> dict:
    """Show stable metadata while keeping copy dependencies host-internal."""
    return {key: value for key, value in entry.items()
            if key != "source_dependency_refs"}


def shared_material_entries(
    store: StateStore, *, mission_id: str, actor_id: str, task_id: str,
) -> list[dict]:
    """List every eligible pointer in stable order; context owns bounded paging."""
    if (not isinstance(mission_id, str) or not mission_id
            or not isinstance(actor_id, str) or not actor_id
            or not isinstance(task_id, str) or not task_id):
        return []
    controls = _heads(store, "mission_task_control")
    if not _requester_control(controls, mission_id, actor_id, task_id):
        return []
    pointers = [ref(item) for item in _heads(store, "retrieval_hit")]
    pointers += [body["report_ref"] for control in controls
                 if (body := control["body"]).get("mission_id") == mission_id
                 and _exact(body.get("report_ref"), "report")]
    result, seen = [], set()
    for pointer in pointers:
        key = pointer["kind"], pointer["id"], pointer["version"]
        if key in seen:
            continue
        seen.add(key)
        entry = _entry(store, mission_id=mission_id, actor_id=actor_id,
                       task_id=task_id, pointer=pointer, controls=controls)
        if entry is not None:
            result.append(entry)
    return sorted(result, key=lambda item: (
        item["type"], item["ref"]["id"], item["ref"]["version"]
    ))
