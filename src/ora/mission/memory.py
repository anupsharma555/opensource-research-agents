"""Task-scoped, peer-authored working notes over versioned mission state.

Notes are private aids to continuation. They never become accepted evidence merely
because they were saved, and exact input references remain available for review.
"""

from __future__ import annotations

from ora.coordination.board import ref, require_actor, require_ref
from ora.state import StateError, StateStore, Transaction
from ora.state.source_lineage import scoped_note_source_input
from ora.state.store import identifier

from .context import WorkingContext


class MemoryError(StateError):
    """A working-note operation needs a local repair."""


PLAN_NOTE_PREFIX = "runtime-plan-"

NOTE_KINDS = frozenset({"note", "hypothesis", "contradiction", "next_action", "checkpoint"})


class WorkingNotes:
    """Read, revise and search one peer's durable notes for one task."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def _scope(tx: Transaction, mission_id: str, actor_id: str, task_ref: dict) -> dict:
        require_actor(tx, mission_id, actor_id)
        task = require_ref(tx, mission_id, task_ref)
        if task["kind"] != "board_proposal" or task["body"]["mission_id"] != mission_id:
            raise MemoryError("Working notes require a task in this mission")
        return task

    def write(
        self,
        operation_id: str,
        *,
        mission_id: str,
        actor_id: str,
        task_ref: dict,
        note_id: str,
        category: str,
        text: str,
        input_refs: list[dict] | None = None,
        expected_version: int = 0,
    ) -> dict:
        identifier(note_id)
        if note_id.startswith(PLAN_NOTE_PREFIX):
            raise MemoryError("Runtime planning note IDs are reserved for save_plan")
        if category not in NOTE_KINDS or not isinstance(text, str) or not text.strip():
            raise MemoryError("Note needs a known category and nonempty text")
        if type(expected_version) is not int or expected_version < 0:
            raise MemoryError("Expected note version must be nonnegative")
        request = {
            "mission_id": mission_id,
            "actor_id": actor_id,
            "task_ref": task_ref,
            "note_id": note_id,
            "category": category,
            "text": text,
            "input_refs": input_refs or [],
            "expected_version": expected_version,
        }

        def save(tx: Transaction) -> dict:
            return self.write_in_transaction(
                tx, mission_id=mission_id, actor_id=actor_id, task_ref=task_ref,
                note_id=note_id, category=category, text=text,
                input_refs=input_refs, expected_version=expected_version,
            )

        return self.store.transact(operation_id, request, save, mission_id=mission_id)

    def write_in_transaction(
        self, tx: Transaction, *, mission_id: str, actor_id: str,
        task_ref: dict, note_id: str, category: str, text: str,
        input_refs: list[dict] | None = None, expected_version: int = 0,
        planning_metadata: dict | None = None,
    ) -> dict:
        """Reuse note acceptance inside a caller's atomic lifecycle boundary.

        Reserved planning IDs require host-supplied metadata. The ordinary write
        API never accepts those IDs, including before their first creation.
        """
        identifier(note_id)
        if note_id.startswith(PLAN_NOTE_PREFIX) != (planning_metadata is not None):
            raise MemoryError("Reserved planning note needs its host planning boundary")
        if category not in NOTE_KINDS or not isinstance(text, str) or not text.strip():
            raise MemoryError("Note needs a known category and nonempty text")
        if type(expected_version) is not int or expected_version < 0:
            raise MemoryError("Expected note version must be nonnegative")
        task = self._scope(tx, mission_id, actor_id, task_ref)
        current_task = tx.get("board_proposal", task["id"])
        if current_task is None or current_task["version"] != task["version"]:
            raise MemoryError("Task changed; reread before writing a note")
        actor = tx.get("agent_instance", actor_id, 1)
        run_id = actor["body"].get("run_id") if actor else None

        def note_input(item: dict) -> dict:
            if (type(item) is not dict or set(item) != {"kind", "id", "version"}
                    or type(item.get("id")) is not str
                    or type(item.get("version")) is not int
                    or item["version"] < 1 or item.get("kind") not in {
                        "source_copy", "source_passage", "source_read_window"
                    }):
                return require_ref(tx, mission_id, item)
            saved = tx.get(**item)
            if saved is None:
                return require_ref(tx, mission_id, item)
            copy_ref = (item if item["kind"] == "source_copy"
                        else saved["body"].get("copy_ref"))
            copy = tx.get(**copy_ref) if (
                type(copy_ref) is dict and set(copy_ref) == {"kind", "id", "version"}
                and copy_ref.get("kind") == "source_copy"
                and type(copy_ref.get("id")) is str
                and type(copy_ref.get("version")) is int
                and copy_ref["version"] > 0
            ) else None
            inherited = copy is not None and copy["body"].get("mission_id") not in (
                None, mission_id,
            )
            if (inherited or item["kind"] == "source_read_window"
                    and saved["body"].get("mission_id") != mission_id):
                checked = scoped_note_source_input(
                    tx, mission_id=mission_id, run_id=run_id,
                    actor_id=actor_id, task_id=task["id"], pointer=item,
                ) if isinstance(run_id, str) else None
                if checked is None:
                    raise MemoryError("Note source input is outside this peer's checked task sources")
                return checked
            return require_ref(tx, mission_id, item)

        inputs = [note_input(item) for item in input_refs or []]
        previous = tx.get("working_note", note_id)
        if previous and (
            previous["body"]["mission_id"] != mission_id
            or previous["body"]["actor_id"] != actor_id
            or previous["body"]["task_id"] != task["id"]
        ):
            raise MemoryError("Note ID belongs to another peer or task")
        body = {
            "schema": "ora.working_note.v1",
            "mission_id": mission_id,
            "actor_id": actor_id,
            "task_id": task["id"],
            "task_ref": ref(task),
            "category": category,
            "text": text,
            "input_refs": [ref(item) for item in inputs],
            "status": "peer_draft_not_accepted_evidence",
            "updated_at": self.store.now(),
        }
        if planning_metadata is not None:
            body["planning"] = planning_metadata
        saved = tx.put("working_note", note_id, body, expected_version=expected_version)
        tx.link(saved, task, "about_task")
        for item in inputs:
            tx.link(saved, item, "input")
        if previous:
            tx.link(saved, previous, "derived_from")
        return ref(saved)

    def read(
        self,
        *,
        mission_id: str,
        actor_id: str,
        task_id: str,
        note_id: str,
        version: int | None = None,
    ) -> dict:
        note = self.store.get("working_note", note_id, version)
        if note is None or any(
            note["body"].get(key) != value
            for key, value in (
                ("mission_id", mission_id),
                ("actor_id", actor_id),
                ("task_id", task_id),
            )
        ):
            raise MemoryError("Working note is unavailable in this peer task")
        return WorkingContext(self.store).working_note_projection(note)

    def search(
        self,
        *,
        mission_id: str,
        actor_id: str,
        task_id: str,
        query: str = "",
        category: str | None = None,
    ) -> list[dict]:
        if not isinstance(query, str) or (category is not None and category not in NOTE_KINDS):
            raise MemoryError("Search needs text and a known optional category")
        folded = query.casefold().strip()
        terms = folded.split()
        results, after = [], ""
        while True:
            page = self.store.records("working_note", after_id=after)
            for note in page:
                body = note["body"]
                projected = WorkingContext(self.store).working_note_projection(note)
                visible = projected["body"]
                searchable = f"{note['id']} {visible.get('category', '')} {visible.get('text', '')}".casefold()
                if (
                    body.get("mission_id") == mission_id
                    and body.get("actor_id") == actor_id
                    and body.get("task_id") == task_id
                    and (category is None or body.get("category") == category)
                    and all(term in searchable for term in terms)
                ):
                    results.append(projected)
            if len(page) < 1000:
                # Exact phrases first, then current revisions before older
                # notes. Every match remains available; no relevance cutoff.
                return sorted(
                    results,
                    key=lambda item: (
                        folded in item["body"].get("text", "").casefold(),
                        item["body"].get("updated_at", ""),
                        item["id"],
                    ),
                    reverse=True,
                )
            after = page[-1]["id"]
