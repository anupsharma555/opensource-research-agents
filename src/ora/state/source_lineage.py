"""Recognize an exact public source inherited from an accepted parent view.

This is a read-only exception for a child run's source copy. It does not grant
source use, make other parent records local, or change board reference policy.
"""

from __future__ import annotations

from contextlib import closing

from .snapshot_storage import read_snapshot
from .store import StateStore, Transaction


def _ref(value: object, kind: str) -> bool:
    return (
        type(value) is dict and set(value) == {"kind", "id", "version"}
        and value["kind"] == kind and type(value["id"]) is str and bool(value["id"])
        and type(value["version"]) is int and value["version"] > 0
    )


def _current_control(reader: Transaction, mission_id: str, run_id: str,
                     actor_id: str, task_id: str) -> dict | None:
    rows = reader.db.execute(
        "SELECT r.id FROM records r JOIN heads h USING(kind,id,version) "
        "WHERE r.kind='mission_task_control' "
        "AND json_extract(r.body,'$.mission_id')=? "
        "AND json_extract(r.body,'$.run_id')=? "
        "AND json_extract(r.body,'$.actor_id')=? "
        "AND json_extract(r.body,'$.task_id')=? LIMIT 2",
        (mission_id, run_id, actor_id, task_id),
    ).fetchall()
    return reader.get("mission_task_control", rows[0]["id"]) if len(rows) == 1 else None


def inherited_public_source(
    reader: StateStore | Transaction, *, mission_id: str, run_id: str,
    copy_ref: dict, actor_id: str | None = None, task_id: str | None = None,
    control: dict | None = None,
) -> bool:
    """Require exact child, accepted-parent, copy, encounter and task ancestry.

    With an actor/task, the current task control must still contain the copy.
    Snapshot projection has no single task control, so it checks the inherited
    run source and exact accepted parent view without actor/task parameters.
    """
    if isinstance(reader, StateStore):
        with closing(reader._connect()) as db:
            return inherited_public_source(
                Transaction(reader, db), mission_id=mission_id, run_id=run_id,
                copy_ref=copy_ref, actor_id=actor_id, task_id=task_id,
                control=control,
            )
    if not _ref(copy_ref, "source_copy") or not mission_id or not run_id:
        return False
    if (actor_id is None) != (task_id is None):
        return False
    run = reader.get("mission_run", run_id)
    original_run = reader.get("mission_run", run_id, 1)
    copy = reader.get(**copy_ref)
    if not run or not original_run or not copy:
        return False
    body, initial = run["body"], original_run["body"]
    owner = copy["body"].get("mission_id")
    if (
        body.get("mission_id") != mission_id
        or body.get("run_id") != run_id or initial.get("run_id") != run_id
        or owner in (None, mission_id) or type(owner) is not str
        or copy_ref not in body.get("source_refs", [])
        or copy_ref not in initial.get("source_refs", [])
        or any(initial.get(key) != body.get(key) for key in (
            "mission_id", "parent_run_id", "parent_snapshot_id", "target_ref"
        ))
    ):
        return False
    parent_id, snapshot_id, target_ref = (
        body.get("parent_run_id"), body.get("parent_snapshot_id"), body.get("target_ref")
    )
    if (type(parent_id) is not str or not parent_id or type(snapshot_id) is not str
            or parent_id == run_id or not snapshot_id or not _ref(target_ref, "report")):
        return False
    parent = reader.get("mission_run", parent_id)
    snapshot = reader.get("research_snapshot", snapshot_id, 1)
    target = reader.get(**target_ref)
    if (
        not parent or not snapshot or not target
        or parent["body"].get("mission_id") == mission_id
        or snapshot["body"].get("mission_id") != parent["body"].get("mission_id")
        or target["body"].get("mission_id") != parent["body"].get("mission_id")
        or target_ref not in snapshot["body"].get("inspection", {}).get(
            "acceptedOutputReferences", []
        )
    ):
        return False
    encounter_ref = copy["body"].get("encounter")
    encounter = reader.get(**encounter_ref) if _ref(encounter_ref, "source_encounter") else None
    document_ref = copy["body"].get("document_ref")
    navigation_ref = copy["body"].get("navigation_ref")
    origin_ref = document_ref if _ref(document_ref, "public_document_fetch") else navigation_ref
    if (
        not encounter or not (
            _ref(origin_ref, "public_document_fetch")
            or _ref(origin_ref, "venue_navigation")
        )
        or bool(document_ref) == bool(navigation_ref)
    ):
        return False
    origin = reader.get(**origin_ref)
    if document_ref and origin is not None:
        from ora.evidence.source_assets import document_copy_reference_matches

        source_url_matches = document_copy_reference_matches(
            reader, origin, copy["body"].get("reference"))
    else:
        source_url_matches = bool(
            origin and origin["body"].get("final_url") == copy["body"].get("reference"))
    if (
        not origin or origin["body"].get("mission_id") != owner
        or encounter["body"].get("mission_id") not in (None, owner)
        or encounter["body"].get(
            "document_ref" if document_ref else "navigation_ref"
        ) != origin_ref
        or origin["body"].get("content_sha256") != copy["body"].get("content_sha256")
        or not source_url_matches
    ):
        return False
    matching = [item for item in snapshot["body"].get("inspection", {}).get("sources", [])
                if item.get("copyReference") == copy_ref]
    if len(matching) != 1 or matching[0].get("sourceId") != copy["id"]:
        return False
    encounters = matching[0].get("encounterReferences")
    if type(encounters) is not list or encounter_ref not in encounters or not all(
        _ref(pointer, "source_encounter") for pointer in encounters
    ):
        return False
    saved_view = read_snapshot(reader.store, snapshot)
    sources = [item for item in saved_view.get("sources", [])
               if item.get("id") == copy["id"]]
    if (
        len(sources) != 1
        or sources[0].get("copyId") != copy["id"]
        or sources[0].get("version") != copy["version"]
        or sources[0].get("contentHash") != copy["body"].get("content_sha256")
    ):
        return False
    if actor_id is not None:
        if control is None:
            control = _current_control(reader, mission_id, run_id, actor_id, task_id)
        else:
            current = reader.get("mission_task_control", control.get("id"))
            if current != control:
                return False
        control_body = control["body"] if control else {}
        if any(control_body.get(key) != value for key, value in (
            ("mission_id", mission_id), ("run_id", run_id),
            ("actor_id", actor_id), ("task_id", task_id),
        )) or copy_ref not in control_body.get("source_refs", []):
            return False
    return True


def scoped_note_source_input(
    reader: StateStore | Transaction, *, mission_id: str, run_id: str,
    actor_id: str, task_id: str, pointer: dict,
) -> dict | None:
    """Resolve one exact source input for a task note, including inherited reads.

    Reading windows have no mission owner. Their saved task membership, source,
    quality, use and passage links must establish their scope instead.
    """
    if isinstance(reader, StateStore):
        with closing(reader._connect()) as db:
            return scoped_note_source_input(
                Transaction(reader, db), mission_id=mission_id, run_id=run_id,
                actor_id=actor_id, task_id=task_id, pointer=pointer,
            )
    if not any(_ref(pointer, kind) for kind in (
        "source_copy", "source_passage", "source_read_window"
    )):
        return None
    record = reader.get(**pointer)
    control = _current_control(reader, mission_id, run_id, actor_id, task_id)
    if record is None or control is None:
        return None
    copy_ref = pointer if pointer["kind"] == "source_copy" else record["body"].get("copy_ref")
    if not _ref(copy_ref, "source_copy"):
        return None
    copy = reader.get(**copy_ref)
    sources = control["body"].get("source_refs")
    if (copy is None or copy["body"].get("inspection_state") != "checked"
            or type(sources) is not list or copy_ref not in sources):
        return None
    owner = copy["body"].get("mission_id")
    if owner not in (None, mission_id) and not inherited_public_source(
        reader, mission_id=mission_id, run_id=run_id, copy_ref=copy_ref,
        actor_id=actor_id, task_id=task_id, control=control,
    ):
        return None
    if pointer["kind"] == "source_copy":
        return record
    windows = control["body"].get("read_window_refs", [])
    if type(windows) is not list:
        return None

    def linked(source_ref: dict, target_ref: dict, relation: str) -> bool:
        return reader.db.execute(
            "SELECT 1 FROM links WHERE source_kind=? AND source_id=? AND source_version=? "
            "AND target_kind=? AND target_id=? AND target_version=? AND relation=?",
            (source_ref["kind"], source_ref["id"], source_ref["version"],
             target_ref["kind"], target_ref["id"], target_ref["version"], relation),
        ).fetchone() is not None

    def checked_window(window_ref: dict) -> dict | None:
        if (not _ref(window_ref, "source_read_window")
                or window_ref not in windows):
            return None
        window = reader.get(**window_ref)
        if window is None:
            return None
        body = window["body"]
        use_ref, quality_ref = body.get("source_use_ref"), body.get("quality_ref")
        if not _ref(use_ref, "source_use") or not _ref(quality_ref, "intake_quality"):
            return None
        use, quality = reader.get(**use_ref), reader.get(**quality_ref)
        if (body.get("schema") != "ora.evidence.read_window_request.v1"
                or body.get("copy_ref") != copy_ref
                or body.get("content_sha256") != copy["body"].get("content_sha256")
                or use is None or use["body"].get("copy_ref") != copy_ref
                or use["body"].get("source_sha256") != body["content_sha256"]
                or use["body"].get("quality_ref") != quality_ref
                or quality is None
                or quality["body"].get("source_sha256") != body["content_sha256"]
                or not linked(window_ref, copy_ref, "depends_on")
                or not linked(window_ref, use_ref, "depends_on")
                or not linked(window_ref, quality_ref, "depends_on")):
            return None
        passages = body.get("passage_refs")
        if not isinstance(passages, list) or not passages:
            return None
        for passage_ref in passages:
            passage = reader.get(**passage_ref) if _ref(passage_ref, "source_passage") else None
            if (passage is None or passage["body"].get("copy_ref") != copy_ref
                    or passage["body"].get("inspection_state") != "checked"
                    or not linked(window_ref, passage_ref, "includes_passage")
                    or not linked(passage_ref, copy_ref, "depends_on")):
                return None
        return window

    if pointer["kind"] == "source_read_window":
        return checked_window(pointer)
    if (record["body"].get("inspection_state") != "checked"
            or not linked(pointer, copy_ref, "depends_on")):
        return None
    passage_ids = copy["body"].get("passage_ids", [])
    if type(passage_ids) is list and record["id"] in passage_ids:
        return record
    for window_ref in windows:
        window = checked_window(window_ref)
        if window and pointer in window["body"]["passage_refs"]:
            return record
    return None
