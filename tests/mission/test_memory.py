"""Synthetic working-note scope, provenance and process-reopen checks."""

from __future__ import annotations

import pytest

from ora.coordination.board import ref
from ora.mission.memory import MemoryError, WorkingNotes
from ora.state import StateConflict, StateStore


def seed(store: StateStore) -> tuple[dict, dict]:
    def save(tx):
        tx.put("agent_instance", "peer", {"mission_id": "m"})
        tx.put("agent_instance", "other", {"mission_id": "m"})
        task = tx.put("board_proposal", "task", {"mission_id": "m", "status": "open"})
        copy = tx.put("source_copy", "copy", {"mission_id": "m", "source_version": "synthetic"})
        evidence = tx.put("source_passage", "passage", {
            "mission_id": "m", "source_version": "synthetic", "copy_ref": ref(copy),
        })
        use = tx.put("source_use", "use", {
            "copy_ref": ref(copy), "allowed_uses": ["model_input"], "policy_status": "granted",
        })
        tx.bind("source_use.copy", "copy:1", "source_use", use["id"])
        return {"task": ref(task), "evidence": ref(evidence)}

    saved = store.transact("seed", {}, save, mission_id="m")
    return saved["task"], saved["evidence"]


def test_notes_survive_reopen_with_exact_inputs_and_revision(tmp_path) -> None:
    store = StateStore(tmp_path)
    task, evidence = seed(store)
    notes = WorkingNotes(store)
    first = notes.write(
        "note-1",
        mission_id="m",
        actor_id="peer",
        task_ref=task,
        note_id="idea",
        category="hypothesis",
        text="Synthetic candidate explanation",
        input_refs=[evidence],
    )
    second = notes.write(
        "note-2",
        mission_id="m",
        actor_id="peer",
        task_ref=task,
        note_id="idea",
        category="contradiction",
        text="Synthetic contrary observation",
        input_refs=[evidence],
        expected_version=1,
    )
    reopened = WorkingNotes(StateStore(tmp_path))
    assert (
        reopened.read(mission_id="m", actor_id="peer", task_id="task", note_id="idea")["version"]
        == 2
    )
    assert (
        reopened.read(mission_id="m", actor_id="peer", task_id="task", note_id="idea", version=1)[
            "body"
        ]["text"]
        == "Synthetic candidate explanation"
    )
    found = reopened.search(
        mission_id="m", actor_id="peer", task_id="task", query="contrary", category="contradiction"
    )
    assert [ref(item) for item in found] == [second]
    assert found[0]["body"]["input_refs"] == [evidence]
    assert found[0]["body"]["status"] == "peer_draft_not_accepted_evidence"
    assert any(link["target"] == evidence for link in store.links(store.get(**second)))
    assert first != second


def test_notes_are_actor_scoped_and_version_fenced(tmp_path) -> None:
    store = StateStore(tmp_path)
    task, evidence = seed(store)
    notes = WorkingNotes(store)
    notes.write(
        "first",
        mission_id="m",
        actor_id="peer",
        task_ref=task,
        note_id="private-note",
        category="next_action",
        text="Read more",
        input_refs=[evidence],
    )
    with pytest.raises(MemoryError, match="unavailable"):
        notes.read(mission_id="m", actor_id="other", task_id="task", note_id="private-note")
    assert notes.search(mission_id="m", actor_id="other", task_id="task") == []
    with pytest.raises(MemoryError, match="another peer"):
        notes.write(
            "hijack",
            mission_id="m",
            actor_id="other",
            task_ref=task,
            note_id="private-note",
            category="note",
            text="Override",
            expected_version=1,
        )
    with pytest.raises(StateConflict, match="version"):
        notes.write(
            "stale",
            mission_id="m",
            actor_id="peer",
            task_ref=task,
            note_id="private-note",
            category="note",
            text="Old update",
            expected_version=0,
        )


def test_search_recovers_related_terms_and_prefers_exact_recent_notes(tmp_path) -> None:
    store = StateStore(tmp_path)
    task, evidence = seed(store)
    notes = WorkingNotes(store)
    notes.write(
        "split-terms", mission_id="m", actor_id="peer", task_ref=task,
        note_id="split", category="hypothesis",
        text="The seed may be in an allocation appendix", input_refs=[evidence],
    )
    notes.write(
        "exact-phrase", mission_id="m", actor_id="peer", task_ref=task,
        note_id="exact", category="contradiction",
        text="Allocation seed detail is not reported here", input_refs=[evidence],
    )
    notes.write(
        "unrelated", mission_id="m", actor_id="peer", task_ref=task,
        note_id="other", category="checkpoint",
        text="Inspect the next source", input_refs=[evidence],
    )
    reopened = WorkingNotes(StateStore(tmp_path))
    matches = reopened.search(
        mission_id="m", actor_id="peer", task_id="task", query="ALLOCATION SEED"
    )
    assert [item["id"] for item in matches] == ["exact", "split"]
    by_category = reopened.search(
        mission_id="m", actor_id="peer", task_id="task", query="contradiction"
    )
    assert [item["id"] for item in by_category] == ["exact"]
    assert len(reopened.search(mission_id="m", actor_id="peer", task_id="task")) == 3
