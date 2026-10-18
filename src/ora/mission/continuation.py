"""Bounded, source-checked working handoff for an explicit accepted follow-up.

The handoff pins one accepted report's producing peer and saved navigation. It
does not grant access to sibling work or make private notes accepted evidence.
"""

from __future__ import annotations

import hashlib
import json

from ora.coordination.board import ref
from ora.state import StateError, StateStore, Transaction
from ora.state.source_lineage import inherited_public_source
from ora.state.store import canonical

HANDOFF_KIND = "mission_followup_handoff"
MAX_NOTE_REFS = 12
MAX_WINDOW_REFS = 4
MAX_PAGE_CHARS = 1200
MAX_NOTE_TEXT_CHARS = 64_000


class ContinuationError(StateError):
    """An explicit follow-up handoff lost its scope or current permission."""


def _exact(store: StateStore, pointer: dict, kind: str) -> dict:
    if (type(pointer) is not dict or set(pointer) != {"kind", "id", "version"}
            or pointer.get("kind") != kind or type(pointer.get("id")) is not str
            or type(pointer.get("version")) is not int or pointer["version"] < 1):
        raise ContinuationError(f"Exact {kind} reference is required")
    saved = store.get(**pointer)
    if saved is None or ref(saved) != pointer:
        raise ContinuationError(f"Exact {kind} record is unavailable")
    return saved


def _asof(store: StateStore, kind: str, cutoff: str, *, mission_id: str,
          actor_id: str, task_id: str) -> list[dict]:
    """Read each record head as it existed when the parent snapshot was saved."""
    db = store._connect()
    try:
        rows = db.execute(
            "SELECT kind,id,version,body,created_at FROM records "
            "WHERE kind=? AND created_at<=? "
            "AND json_extract(body,'$.mission_id')=? "
            "AND json_extract(body,'$.actor_id')=? "
            "AND json_extract(body,'$.task_id')=? "
            "ORDER BY id,version DESC",
            (kind, cutoff, mission_id, actor_id, task_id),
        ).fetchall()
    finally:
        db.close()
    seen = set()
    result = []
    for row in rows:
        if row["id"] not in seen:
            seen.add(row["id"])
            result.append({"kind": row["kind"], "id": row["id"],
                           "version": row["version"],
                           "body": json.loads(row["body"]),
                           "created_at": row["created_at"]})
    return result


def save_followup_handoff(
    store: StateStore, operation_id: str, *, child_run_ref: dict,
    parent_snapshot_ref: dict, target_ref: dict,
) -> dict:
    """Pin a small historical handoff after an explicitly requested child run exists."""
    child = _exact(store, child_run_ref, "mission_run")
    snapshot = _exact(store, parent_snapshot_ref, "research_snapshot")
    target = _exact(store, target_ref, "report")
    parent = store.get("mission_run", child["body"].get("parent_run_id"))
    if (parent is None or child["body"].get("parent_snapshot_id") != snapshot["id"]
            or child["body"].get("target_ref") != target_ref
            or snapshot["body"].get("mission_id") != parent["body"].get("mission_id")
            or target["body"].get("mission_id") != parent["body"].get("mission_id")
            or target_ref not in snapshot["body"].get("inspection", {}).get(
                "acceptedOutputReferences", [])):
        raise ContinuationError("Handoff requires the child's exact accepted parent target")
    cutoff = snapshot["created_at"]
    attempt_ref = target["body"].get("producer_attempt_ref")
    attempt = store.get(**attempt_ref) if isinstance(attempt_ref, dict) else None
    actor_id = attempt["body"].get("agentId") if attempt else None
    task_id = attempt["body"].get("task_id") if attempt else None
    call_ref = attempt["body"].get("call_ref") if attempt else None
    call = store.get(**call_ref) if isinstance(call_ref, dict) else None
    actor = store.get("agent_instance", actor_id, 1) if isinstance(actor_id, str) else None
    producer_ok = bool(
        attempt and ref(attempt) == attempt_ref and attempt["created_at"] <= cutoff
        and call and ref(call) == call_ref and actor
        and attempt["body"].get("mission_id") == parent["body"].get("mission_id")
        and attempt["body"].get("run_id") == parent["id"]
        and call["body"].get("instance_id") == actor_id
        and call["body"].get("task_id") == task_id
        and actor["body"].get("run_id") == parent["id"]
    )
    controls = (_asof(store, "mission_task_control", cutoff,
                      mission_id=parent["body"]["mission_id"], actor_id=actor_id,
                      task_id=task_id) if producer_ok else [])
    controls = [item for item in controls if item["body"].get("run_id") == parent["id"]]
    control = controls[0] if len(controls) == 1 else None
    producer_ok = producer_ok and control is not None
    notes = (_asof(store, "working_note", cutoff,
                   mission_id=parent["body"]["mission_id"], actor_id=actor_id,
                   task_id=task_id) if producer_ok else [])
    notes.sort(key=lambda item: (
        item["body"].get("planning") is not None,
        item["body"].get("category") in {"next_action", "contradiction"},
        item["created_at"], item["id"]), reverse=True)
    selected_notes = notes[:MAX_NOTE_REFS]
    snapshot_sources = {
        canonical(item["copyReference"]): item["copyReference"]
        for item in snapshot["body"].get("inspection", {}).get("sources", [])
        if isinstance(item, dict) and isinstance(item.get("copyReference"), dict)
    }
    navigation = []
    if producer_ok:
        for pointer in control["body"].get("read_window_refs", [])[-MAX_WINDOW_REFS:]:
            window = store.get(**pointer) if isinstance(pointer, dict) else None
            if (window is None or ref(window) != pointer
                    or canonical(window["body"].get("copy_ref")) not in snapshot_sources):
                continue
            navigation.append({"window_ref": pointer,
                               "copy_ref": window["body"]["copy_ref"],
                               "next_cursor": window["body"].get("next_cursor")})
    handoff_id = f"followup-handoff:{child['id']}"
    body = {
        "schema": "ora.mission_followup_handoff.v1",
        "mission_id": child["body"]["mission_id"], "run_id": child["id"],
        "child_run_ref": child_run_ref, "parent_run_ref": ref(parent),
        "parent_snapshot_ref": parent_snapshot_ref, "target_ref": target_ref,
        "producer_status": "exact" if producer_ok else "limited_parent_lineage",
        "parent_actor_id": actor_id if producer_ok else None,
        "parent_task_id": task_id if producer_ok else None,
        "parent_alias": control["body"].get("alias") if producer_ok else None,
        "parent_control_ref": ref(control) if producer_ok else None,
        "note_refs": [ref(item) for item in selected_notes],
        "additional_note_count": max(0, len(notes) - len(selected_notes)),
        "navigation": navigation,
        "authority": "parent_peer_working_aid_not_accepted_child_evidence",
    }

    def save(tx: Transaction) -> dict:
        if (tx.get(**child_run_ref) != child or tx.get(**parent_snapshot_ref) != snapshot
                or tx.get(**target_ref) != target):
            raise ContinuationError("Handoff ancestry changed before save")
        existing = tx.get(HANDOFF_KIND, handoff_id)
        if existing is not None:
            if existing["body"] != body:
                raise ContinuationError("Saved handoff has different parent state")
            return ref(existing)
        saved = tx.put(HANDOFF_KIND, handoff_id, body)
        tx.link(saved, child, "about_run")
        tx.link(saved, snapshot, "derived_from")
        tx.link(saved, target, "target")
        return ref(saved)

    return store.transact(operation_id, {"handoff": body}, save,
                          mission_id=body["mission_id"])


def _child_scope(store: StateStore, handoff_ref: dict, child_run_ref: dict,
                 actor_id: str, task_id: str) -> tuple[dict, dict, bool]:
    handoff = _exact(store, handoff_ref, HANDOFF_KIND)
    child = _exact(store, child_run_ref, "mission_run")
    saved = handoff["body"]
    parent = _exact(store, saved["parent_run_ref"], "mission_run")
    snapshot = _exact(store, saved["parent_snapshot_ref"], "research_snapshot")
    target = _exact(store, saved["target_ref"], "report")
    actor = store.get("agent_instance", actor_id, 1)
    controls = []
    after = ""
    while True:
        page = store.records("mission_task_control", after_id=after)
        controls.extend(item for item in page
                        if item["body"].get("run_id") == child["id"]
                        and item["body"].get("actor_id") == actor_id
                        and item["body"].get("task_id") == task_id)
        if len(page) < 1000:
            break
        after = page[-1]["id"]
    if (saved.get("child_run_ref") != child_run_ref or saved.get("run_id") != child["id"]
            or saved.get("mission_id") != child["body"].get("mission_id")
            or saved.get("parent_snapshot_ref", {}).get("id")
            != child["body"].get("parent_snapshot_id")
            or saved.get("target_ref") != child["body"].get("target_ref")
            or child["body"].get("parent_run_id") != parent["id"]
            or snapshot["body"].get("mission_id") != parent["body"].get("mission_id")
            or target["body"].get("mission_id") != parent["body"].get("mission_id")
            or ref(target) not in snapshot["body"].get("inspection", {}).get(
                "acceptedOutputReferences", [])
            or actor is None or actor["body"].get("run_id") != child["id"]
            or len(controls) != 1):
        raise ContinuationError("Handoff is outside this child peer task")
    control = controls[0]
    same_alias = (saved.get("producer_status") == "exact"
                  and control["body"].get("alias") == saved.get("parent_alias"))
    return handoff, control, same_alias


def _safe_content(store: StateStore, handoff: dict, control: dict,
                  same_alias: bool) -> tuple[str, list[dict]]:
    """Build only currently usable parent note text and selected navigation."""
    from .context import WorkingContext
    from .report_lineage import report_text_access

    saved = handoff["body"]
    child_mission = saved["mission_id"]
    parent = _exact(store, saved["parent_run_ref"], "mission_run")
    target = _exact(store, saved["target_ref"], "report")
    if report_text_access(store, target)["status"] != "available":
        return canonical({"status": "data_unavailable_due_source_use",
                          "target_ref": saved["target_ref"]}), []
    dependencies: dict[tuple[str, str, int], dict] = {}
    notes = []
    if same_alias:
        context = WorkingContext(store)
        for pointer in saved["note_refs"]:
            note = _exact(store, pointer, "working_note")
            if (note["body"].get("mission_id") != parent["body"].get("mission_id")
                    or note["body"].get("actor_id") != saved["parent_actor_id"]
                    or note["body"].get("task_id") != saved["parent_task_id"]):
                raise ContinuationError("Handoff note belongs to another parent task")
            if len(note["body"].get("text", "")) > MAX_NOTE_TEXT_CHARS:
                notes.append({"ref": pointer, "status": "note_too_large_for_handoff"})
                continue
            access = context.working_note_text_access(note)
            allowed = access["status"] == "available"
            for copy_ref in access["source_dependency_refs"]:
                if not inherited_public_source(
                    store, mission_id=child_mission, run_id=saved["run_id"],
                    copy_ref=copy_ref, actor_id=control["body"]["actor_id"],
                    task_id=control["body"]["task_id"], control=control,
                ):
                    allowed = False
                    break
            if allowed:
                notes.append({"ref": pointer, "category": note["body"].get("category"),
                              "status": "parent_private_working_aid_unverified",
                              "text": note["body"]["text"]})
                for pointer_source in access["source_dependency_refs"]:
                    dependencies[(pointer_source["kind"], pointer_source["id"],
                                  pointer_source["version"])] = pointer_source
            else:
                notes.append({"ref": pointer,
                              "status": "data_unavailable_due_source_use"})
    navigation = []
    for item in saved["navigation"] if same_alias else []:
        if inherited_public_source(
            store, mission_id=child_mission, run_id=saved["run_id"],
            copy_ref=item["copy_ref"], actor_id=control["body"]["actor_id"],
            task_id=control["body"]["task_id"], control=control,
        ):
            navigation.append(item)
    return canonical({"schema": "ora.followup_working_handoff_document.v1",
                      "authority": "parent_peer_working_aid_not_accepted_child_evidence",
                      "target_ref": saved["target_ref"], "notes": notes,
                      "additional_note_count": saved["additional_note_count"],
                      "navigation": navigation}), list(dependencies.values())


def followup_handoff_view(store: StateStore, *, handoff_ref: dict,
                          child_run_ref: dict, actor_id: str, task_id: str,
                          inclusion_id: str) -> dict:
    """Small model-facing handle; detailed content stays in the scoped reader."""
    if not isinstance(inclusion_id, str) or not inclusion_id:
        raise ContinuationError("Handoff view needs an exact context inclusion ID")
    handoff, _control, same_alias = _child_scope(
        store, handoff_ref, child_run_ref, actor_id, task_id)
    saved = handoff["body"]
    return {"schema": "ora.followup_working_handoff_view.v1",
            "authority": saved["authority"], "handoff_ref": handoff_ref,
            "status": ("available_subject_to_current_source_use" if same_alias
                       else "target_only_or_other_peer"),
            "note_count": len(saved["note_refs"]) if same_alias else 0,
            "additional_note_count": saved["additional_note_count"] if same_alias else 0,
            "navigation_count": len(saved["navigation"]) if same_alias else 0,
            "read_request": {"tool": "read_context_record",
                             "inclusion_id": inclusion_id, **handoff_ref,
                             "offset": 0, "limit": MAX_PAGE_CHARS}}


def read_followup_handoff(store: StateStore, *, handoff_ref: dict,
                          child_run_ref: dict, actor_id: str, task_id: str,
                          inclusion_id: str, offset: int, limit: int,
                          projection_sha256: str = "") -> dict:
    """Page the exact allowed projection, rejecting changed permissions mid-read."""
    if (not isinstance(inclusion_id, str) or not inclusion_id
            or type(offset) is not int or offset < 0 or type(limit) is not int
            or not 1 <= limit <= MAX_PAGE_CHARS or not isinstance(projection_sha256, str)):
        raise ContinuationError("Handoff page needs a bounded offset, limit and hash")
    handoff, control, same_alias = _child_scope(
        store, handoff_ref, child_run_ref, actor_id, task_id)
    document, dependencies = _safe_content(store, handoff, control, same_alias)
    digest = hashlib.sha256(document.encode()).hexdigest()
    if offset > len(document) or projection_sha256 and projection_sha256 != digest:
        raise ContinuationError("Handoff projection changed; restart at offset zero")
    end = min(offset + limit, len(document))
    return {"schema": "ora.followup_working_handoff_page.v1",
            "handoff_ref": handoff_ref, "offset": offset, "content": document[offset:end],
            "projection_sha256": digest, "total_chars": len(document),
            "source_dependency_refs": dependencies,
            "next_request": ({"tool": "read_context_record",
                              "inclusion_id": inclusion_id, **handoff_ref,
                              "offset": end, "limit": limit}
                             if end < len(document) else None)}
