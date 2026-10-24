"""Agent-curated pointers to checked local evidence, with explicit task selection.

Library notes are peer interpretations. They never replace source passages or
grant new rights. The authoritative source copy and use records remain in the
existing StateStore; this module stores only exact references and metadata.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from types import SimpleNamespace
from typing import Any

from ora.coordination.board import ref
from ora.evidence import EvidenceError
from ora.evidence.intake import _require_model_use_record
from ora.evidence.windows import _COPY_CLASSES
from ora.mission.report_lineage import report_text_access
from ora.state import StateConflict, StateError
from ora.state.blobs import IntegrityFault
from ora.state.store import canonical, unpack

ENTRY_KIND = "evidence_library_entry"
BINDING_KIND = "evidence_library_task_binding"
_INDEX_TABLE = "evidence_library_term_index"
_MAX_NOTE = 1000


def _exact(pointer: object, kind: str) -> bool:
    return bool(
        type(pointer) is dict
        and set(pointer) == {"kind", "id", "version"}
        and pointer.get("kind") == kind
        and type(pointer.get("id")) is str
        and bool(pointer["id"])
        and type(pointer.get("version")) is int
        and pointer["version"] > 0
    )


def _id(prefix: str, *parts: object) -> str:
    return prefix + hashlib.sha256(canonical(parts).encode("utf-8")).hexdigest()[:32]


def _binding_id(spec: Any, copy_ref: dict) -> str:
    return _id("library-task:", spec.mission_id, spec.run_id, spec.actor_id,
               spec.proposal_ref["id"], copy_ref)


def _origin_current(host: Any, reader: Any, copy_ref: dict) -> bool:
    """Recheck an external BHEB source when the retained copy came from it."""
    binding = reader.get(
        "bheb_source_binding", f"bheb-binding:{copy_ref['id']}:{copy_ref['version']}"
    )
    if binding is None:
        return True
    body = binding["body"]
    bheb = getattr(host, "bheb", None)
    return bool(
        body.get("copy_ref") == copy_ref
        and bheb is not None
        and bheb.summary().get("config_digest") == body.get("config_digest")
        and bheb.current_source_use(
            body["source_id"], body["source_version"], body["upload_sha256"]
        )
    )


def _current_copy(host: Any, reader: Any, copy_ref: dict,
                  *, verify_blob: bool = False) -> tuple[dict, dict, dict] | None:
    if not _exact(copy_ref, "source_copy"):
        return None
    copy = reader.get(**copy_ref)
    head = reader.get("source_copy", copy_ref["id"])
    if (copy is None or head is None or ref(head) != copy_ref
            or copy["body"].get("inspection_state") != "checked"
            or copy["body"].get("retain_bytes") is not True):
        return None
    blob = copy["body"].get("blob")
    digest = copy["body"].get("content_sha256")
    if (not isinstance(blob, dict) or blob.get("sha256") != digest
            or type(blob.get("size")) is not int or blob["size"] < 1):
        return None
    try:
        use = reader.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
        use_ref = _require_model_use_record(use, copy_ref)
        quality_ref = use["body"].get("quality_ref")
        quality = reader.get(**quality_ref) if _exact(quality_ref, "intake_quality") else None
        if (use["body"].get("source_sha256") != digest
                or quality is None or quality["body"].get("source_sha256") != digest
                or quality["body"].get("classification") not in _COPY_CLASSES
                or not _origin_current(host, reader, copy_ref)):
            return None
        if verify_blob:
            content = host.store.read_blob(blob["sha256"])
            if len(content) != blob["size"] or hashlib.sha256(content).hexdigest() != digest:
                return None
    except (EvidenceError, StateError, IntegrityFault, KeyError, TypeError, ValueError, OSError):
        return None
    return copy, use_ref, quality_ref


def _control(host: Any, spec: Any) -> dict:
    control = host._control_for_spec(spec)
    body = control["body"]
    if (body.get("mission_id") != spec.mission_id
            or body.get("run_id") != spec.run_id
            or body.get("actor_id") != spec.actor_id
            or body.get("task_id") != spec.proposal_ref["id"]):
        raise StateError("Library action does not belong to the current peer task")
    return control


def _finding_refs(host: Any, spec: Any, pointers: object) -> list[dict]:
    if not isinstance(pointers, list) or len(pointers) > 10:
        raise ValueError("Library finding refs must be a bounded list")
    seen: set[tuple[str, str, int]] = set()
    output = []
    for pointer in pointers:
        if not _exact(pointer, "report"):
            raise ValueError("Library finding refs must name exact reports")
        key = pointer["kind"], pointer["id"], pointer["version"]
        if key in seen:
            raise ValueError("Library finding refs must be distinct")
        seen.add(key)
        report = host.store.get(**pointer)
        if (report is None or report["body"].get("mission_id") != spec.mission_id
                or report["body"].get("run_id") != spec.run_id
                or report_text_access(host.store, report)["status"] != "available"):
            raise ValueError("Library finding must be an exact saved report in this run")
        output.append(pointer)
    return output


def _findings_current(reader: Any, pointers: object) -> bool:
    if not isinstance(pointers, list) or len(pointers) > 10:
        return False
    for pointer in pointers:
        if not _exact(pointer, "report"):
            return False
        report = reader.get(**pointer)
        if report is None or report_text_access(reader, report)["status"] != "available":
            return False
    return True


def retain_evidence(host: Any, spec: Any, args: dict) -> dict:
    """Curate currently permitted task copies without duplicating source bytes."""
    sources = args.get("source_refs")
    note = args.get("relevance_note")
    if (not isinstance(sources, list) or not 1 <= len(sources) <= 10
            or any(not _exact(pointer, "source_copy") for pointer in sources)
            or len({(p["id"], p["version"]) for p in sources}) != len(sources)):
        raise ValueError("Retain 1 to 10 distinct exact source-copy refs")
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= _MAX_NOTE:
        raise ValueError("Library relevance note must contain 1 to 1000 characters")
    note = note.strip()
    findings = _finding_refs(host, spec, args.get("finding_refs", []))
    control = _control(host, spec)
    if any(pointer not in control["body"].get("source_refs", []) for pointer in sources):
        raise ValueError("Only selected copies in this peer's current task can be retained")
    for pointer in sources:
        if _current_copy(host, host.store, pointer, verify_blob=True) is None:
            raise ValueError("Library source is not a current checked and permitted copy")
    request = {"control_ref": ref(control), "source_refs": sources,
               "relevance_note": note, "finding_refs": findings}

    def save(tx):
        current = tx.get("mission_task_control", control["id"])
        if current is None or ref(current) != ref(control):
            raise StateConflict("Peer task changed before library retention")
        if any(pointer not in current["body"].get("source_refs", []) for pointer in sources):
            raise StateConflict("Task source selection changed before retention")
        if not _findings_current(tx, findings):
            raise StateConflict("Finding lineage changed before library retention")
        entry_refs = []
        indexed_entries = []
        for pointer in sources:
            checked = _current_copy(host, tx, pointer)
            if checked is None:
                raise StateConflict("Source use changed before library retention")
            copy, use_ref, quality_ref = checked
            entry_id = _id("library-entry:", spec.mission_id, spec.run_id,
                           spec.actor_id, spec.proposal_ref["id"], pointer)
            previous = tx.get(ENTRY_KIND, entry_id)
            body = {
                "schema": "ora.evidence_library.entry.v1",
                "copy_ref": pointer,
                "source_use_ref": use_ref,
                "quality_ref": quality_ref,
                "content_sha256": copy["body"]["content_sha256"],
                "source_version": copy["body"].get("source_version"),
                "mission_id": spec.mission_id,
                "run_id": spec.run_id,
                "actor_id": spec.actor_id,
                "task_id": spec.proposal_ref["id"],
                "relevance_note": note,
                "note_authority": "peer_interpretation_not_checked_source_evidence",
                "finding_refs": findings,
                "curated_at": host.store.now(),
            }
            if previous and all(previous["body"].get(key) == value for key, value in body.items()
                                if key != "curated_at"):
                saved = previous
            else:
                saved = tx.put(ENTRY_KIND, entry_id, body,
                               expected_version=previous["version"] if previous else 0)
                tx.link(saved, copy, "depends_on")
                tx.link(saved, tx.get(**use_ref), "depends_on")
                tx.link(saved, tx.get(**quality_ref), "depends_on")
                for finding in findings:
                    tx.link(saved, tx.get(**finding), "derived_from")
            entry_refs.append(ref(saved))
            indexed_entries.append(saved)
        if _create_index_tx(tx):
            _rebuild_index_tx(tx)
        else:
            for entry in indexed_entries:
                _index_entry_tx(tx, entry)
        return {"status": "retained", "entry_refs": entry_refs,
                "note_authority": "peer_interpretation_not_checked_source_evidence"}

    return host.store.transact(
        f"library-retain:{spec.work_id}", request, save, mission_id=spec.mission_id
    )


def _search_text(reader: Any, entry: dict) -> str:
    """Build index terms from saved metadata, without treating it as a grant."""
    copy_ref = entry["body"].get("copy_ref")
    copy = reader.get(**copy_ref) if _exact(copy_ref, "source_copy") else None
    publication_ref = copy["body"].get("publication") if copy else None
    publication = reader.get(**publication_ref) if _exact(publication_ref, "publication") else None
    titles = publication["body"].get("titles") if publication else None
    title = titles[0][:180] if isinstance(titles, list) and titles and isinstance(titles[0], str) else ""
    note = entry["body"].get("relevance_note")
    return " ".join((title, note if isinstance(note, str) else ""))


def _terms(value: str) -> set[str]:
    return set(re.findall(r"\w+", value.casefold()))


def _create_index_tx(tx: Any) -> bool:
    existed = tx.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_INDEX_TABLE,)
    ).fetchone() is not None
    tx.db.execute(
        "CREATE TABLE IF NOT EXISTS evidence_library_term_index ("
        "term TEXT NOT NULL, entry_id TEXT NOT NULL, entry_version INTEGER NOT NULL, "
        "PRIMARY KEY(term, entry_id)) WITHOUT ROWID"
    )
    return not existed


def _index_entry_tx(tx: Any, entry: dict) -> None:
    tx.db.execute("DELETE FROM evidence_library_term_index WHERE entry_id=?", (entry["id"],))
    tx.db.executemany(
        "INSERT INTO evidence_library_term_index(term,entry_id,entry_version) VALUES (?,?,?)",
        [(term, entry["id"], entry["version"]) for term in _terms(_search_text(tx, entry))],
    )


def _rebuild_index_tx(tx: Any) -> int:
    tx.db.execute("DROP TABLE IF EXISTS evidence_library_term_index")
    _create_index_tx(tx)
    rows = tx.db.execute(
        "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) "
        "WHERE r.kind=? ORDER BY r.id", (ENTRY_KIND,),
    ).fetchall()
    for row in rows:
        _index_entry_tx(tx, unpack(row))
    return len(rows)


def rebuild_evidence_library_index(host: Any) -> dict:
    """Recreate the disposable local term index from authoritative entry heads."""
    return host.store.transact(
        f"library-index-rebuild:{uuid.uuid4().hex}",
        {"schema": "ora.evidence_library.term_index.v1"},
        lambda tx: {"status": "rebuilt", "indexed_entries": _rebuild_index_tx(tx)},
    )


def _candidate_entries(host: Any, words: set[str]):
    db = host.store._connect()
    try:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_INDEX_TABLE,)
        ).fetchone() is not None
    finally:
        db.close()
    if not exists:
        rebuild_evidence_library_index(host)
    db = host.store._connect()
    try:
        placeholders = ",".join("?" for _ in words)
        rows = db.execute(
            "SELECT entry_id,entry_version FROM evidence_library_term_index "
            f"WHERE term IN ({placeholders}) GROUP BY entry_id,entry_version "
            "HAVING COUNT(*)=? ORDER BY entry_id",
            (*sorted(words), len(words)),
        ).fetchall()
    finally:
        db.close()
    for row in rows:
        entry = host.store.get(ENTRY_KIND, row["entry_id"])
        if entry is not None and entry["version"] == row["entry_version"]:
            yield entry


def _entry_metadata(host: Any, entry: dict) -> dict | None:
    body = entry["body"]
    if not _findings_current(host.store, body.get("finding_refs")):
        return None
    pointer = body.get("copy_ref")
    checked = _current_copy(host, host.store, pointer)
    if checked is None or checked[1] != body.get("source_use_ref"):
        return None
    copy = checked[0]
    if (body.get("content_sha256") != copy["body"].get("content_sha256")
            or body.get("quality_ref") != checked[2]):
        return None
    publication_ref = copy["body"].get("publication")
    publication = host.store.get(**publication_ref) if _exact(publication_ref, "publication") else None
    titles = publication["body"].get("titles") if publication else None
    title = titles[0] if isinstance(titles, list) and titles else None
    return {
        "entry_id": entry["id"], "entry_version": entry["version"],
        "source_ref": pointer,
        "source_version": body.get("source_version"),
        "title": title[:180] if isinstance(title, str) else None,
        "relevance_note": body.get("relevance_note", "")[:240],
        "note_authority": "peer_interpretation_not_checked_source_evidence",
        "finding_refs": body.get("finding_refs", []),
        "curated_at": body.get("curated_at"),
    }


def search_evidence_library(host: Any, spec: Any, args: dict) -> dict:
    """Search metadata or select one exact entry for this task's source reader."""
    _control(host, spec)
    query, entry_id = args.get("query"), args.get("entry_id")
    if ((query is None) == (entry_id is None)
            or query is not None and not (isinstance(query, str) and query.strip())
            or entry_id is not None and not (isinstance(entry_id, str) and entry_id.strip())):
        raise ValueError("Choose either a nonempty library query or one entry ID")
    cursor, limit = args.get("cursor", 0), args.get("limit", 5)
    if (type(cursor) is not int or cursor < 0 or type(limit) is not int
            or not 1 <= limit <= 20 or entry_id and cursor != 0):
        raise ValueError("Library paging needs cursor >= 0 and limit 1 to 20")
    if entry_id is not None:
        if not isinstance(entry_id, str) or len(entry_id) > 160:
            raise ValueError("Invalid library entry ID")
        entry = host.store.get(ENTRY_KIND, entry_id)
        metadata = _entry_metadata(host, entry) if entry else None
        if metadata is None or not _current_copy(host, host.store, metadata["source_ref"],
                                                  verify_blob=True):
            return {"status": "unavailable", "reason": "entry_or_source_use_unavailable"}
        control = _control(host, spec)
        binding_id = _binding_id(spec, metadata["source_ref"])
        request = {"entry_ref": ref(entry), "control_ref": ref(control)}

        def bind(tx):
            current_control = tx.get("mission_task_control", control["id"])
            current_entry = tx.get(ENTRY_KIND, entry_id)
            if (current_control is None or ref(current_control) != ref(control)
                    or current_entry is None or ref(current_entry) != ref(entry)):
                raise StateConflict("Library task or entry changed before selection")
            checked = _current_copy(host, tx, metadata["source_ref"])
            if (checked is None or checked[1] != entry["body"]["source_use_ref"]
                    or not _findings_current(tx, entry["body"].get("finding_refs"))):
                raise StateConflict("Library source use changed before selection")
            previous = tx.get(BINDING_KIND, binding_id)
            body = {
                "schema": "ora.evidence_library.task_binding.v1",
                "mission_id": spec.mission_id, "run_id": spec.run_id,
                "actor_id": spec.actor_id, "task_id": spec.proposal_ref["id"],
                "copy_ref": metadata["source_ref"], "entry_ref": ref(entry),
                "source_use_ref": checked[1], "status": "selected",
            }
            if previous and previous["body"] == body:
                saved = previous
            else:
                saved = tx.put(BINDING_KIND, binding_id, body,
                               expected_version=previous["version"] if previous else 0)
                tx.link(saved, tx.get(**metadata["source_ref"]), "depends_on")
                tx.link(saved, current_entry, "depends_on")
            return {"status": "selected_for_source_reader", "binding_ref": ref(saved),
                    "entry": metadata, "next_action": "Choose read_source_window for this exact source_ref"}

        return host.store.transact(
            f"library-select:{spec.work_id}", request, bind, mission_id=spec.mission_id
        )
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError("Library query must contain 1 to 500 characters")
    words = _terms(query)
    if not words:
        raise ValueError("Library query needs searchable terms")
    matches: dict[tuple[str, int], dict] = {}
    for entry in _candidate_entries(host, words):
        metadata = _entry_metadata(host, entry)
        if metadata is None:
            continue
        # A damaged index may propose false candidates; authoritative metadata
        # and current permission still decide what the caller can see.
        if words <= _terms(_search_text(host.store, entry)):
            key = metadata["source_ref"]["id"], metadata["source_ref"]["version"]
            prior = matches.get(key)
            if prior is None or (metadata["curated_at"], metadata["entry_id"]) > (
                    prior["curated_at"], prior["entry_id"]):
                matches[key] = metadata
    ordered = sorted(matches.values(), key=lambda item: (
        item["curated_at"] or "", item["entry_id"]
    ), reverse=True)
    page = ordered[cursor:cursor + limit]
    return {
        "status": "found" if page else "no_matches", "entries": page,
        "cursor": cursor, "next_cursor": cursor + len(page) if cursor + len(page) < len(ordered) else None,
        "matching_sources": len(ordered),
        "content_status": "metadata_only_select_one_entry_before_reading",
    }


def _binding_current(host: Any, reader: Any, spec: Any, copy_ref: dict) -> bool:
    if not _exact(copy_ref, "source_copy"):
        return False
    binding = reader.get(BINDING_KIND, _binding_id(spec, copy_ref))
    if binding is None or binding["body"].get("status") != "selected":
        return False
    body = binding["body"]
    if (body.get("mission_id") != spec.mission_id or body.get("run_id") != spec.run_id
            or body.get("actor_id") != spec.actor_id
            or body.get("task_id") != spec.proposal_ref["id"]
            or body.get("copy_ref") != copy_ref):
        return False
    entry_ref = body.get("entry_ref")
    entry = reader.get(**entry_ref) if _exact(entry_ref, ENTRY_KIND) else None
    current_entry = reader.get(ENTRY_KIND, entry_ref["id"]) if entry else None
    checked = _current_copy(host, reader, copy_ref)
    return bool(
        entry is not None and current_entry is not None and ref(current_entry) == entry_ref
        and entry["body"].get("copy_ref") == copy_ref
        and _findings_current(reader, entry["body"].get("finding_refs"))
        and checked is not None and checked[1] == body.get("source_use_ref")
        and checked[1] == entry["body"].get("source_use_ref")
    )


def task_bound_library_copy(host: Any, spec: Any, copy_ref: dict) -> bool:
    """Authorize a selected cross-mission copy for the normal checked reader."""
    return _binding_current(host, host.store, spec, copy_ref)


def sync_library_source_uses(host: Any) -> None:
    """Withdraw stale per-task selections; never modify original source grants."""
    after = ""
    while True:
        page = host.store.records(BINDING_KIND, limit=500, after_id=after)
        for binding in page:
            body = binding["body"]
            scope = SimpleNamespace(
                mission_id=body["mission_id"], run_id=body["run_id"],
                actor_id=body["actor_id"], proposal_ref={"id": body["task_id"]},
            )
            if body.get("status") != "selected" or _binding_current(
                host, host.store, scope, body["copy_ref"]
            ):
                continue

            def withdraw(tx):
                current = tx.get(BINDING_KIND, binding["id"])
                if current is None or current["body"].get("status") != "selected":
                    return {"status": "already_withdrawn"}
                saved = tx.put(BINDING_KIND, binding["id"],
                               {**current["body"], "status": "withdrawn"},
                               expected_version=current["version"])
                return {"status": "withdrawn", "binding_ref": ref(saved)}

            host.store.transact(
                f"library-withdraw:{binding['id']}:{binding['version']}",
                {"binding_ref": ref(binding)}, withdraw, mission_id=body["mission_id"]
            )
        if len(page) < 500:
            break
        after = page[-1]["id"]


def current_library_use_for_intent(host: Any, tx: Any, intent: dict) -> bool:
    """Recheck selected library sources within the final model-dispatch transaction."""
    actor_id = intent.get("instance_id")
    actor = tx.get("agent_instance", actor_id, 1) if isinstance(actor_id, str) else None
    if (actor is None or actor["body"].get("mission_id") != intent.get("mission_id")
            or not isinstance(actor["body"].get("run_id"), str)):
        return False
    scope = SimpleNamespace(
        mission_id=intent.get("mission_id"), run_id=actor["body"]["run_id"],
        actor_id=actor_id, proposal_ref={"id": intent.get("task_id")},
    )
    for pointer in [*intent.get("source_refs", []),
                    *intent.get("context_source_dependency_refs", [])]:
        if not _exact(pointer, "source_copy"):
            continue
        binding = tx.get(BINDING_KIND, _binding_id(scope, pointer))
        if binding is not None and not _binding_current(host, tx, scope, pointer):
            return False
    return True
