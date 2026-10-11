"""Task-scoped, peer-authored working notes over versioned mission state.

Notes are private aids to continuation. They never become accepted evidence merely
because they were saved, and exact input references remain available for review.
"""

from __future__ import annotations

from ora.coordination.board import ref, require_actor, require_ref
from ora.state import StateError, StateStore, Transaction
from ora.state.store import identifier

from .context import WorkingContext


class MemoryError(StateError):
    """A working-note operation needs a local repair."""


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
            task = self._scope(tx, mission_id, actor_id, task_ref)
            current_task = tx.get("board_proposal", task["id"])
            if current_task is None or current_task["version"] != task["version"]:
                raise MemoryError("Task changed; reread before writing a note")
            inputs = [require_ref(tx, mission_id, item) for item in input_refs or []]
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
            saved = tx.put("working_note", note_id, body, expected_version=expected_version)
            tx.link(saved, task, "about_task")
            for item in inputs:
                tx.link(saved, item, "input")
            if previous:
                tx.link(saved, previous, "derived_from")
            return ref(saved)

        return self.store.transact(operation_id, request, save, mission_id=mission_id)

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
