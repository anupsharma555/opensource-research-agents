"""An explicit child can page its producer's working aid, subject to source use."""

from __future__ import annotations

import json

import pytest

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.mission.continuation import (
    ContinuationError,
    followup_handoff_view,
    read_followup_handoff,
    save_followup_handoff,
)
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_service import answer, drain, service


def _child(host, parent_run_id: str, snapshot, *, request_id: str):
    target = snapshot.artifacts[0]
    child = host.followup(LiveFollowupRequest(
        request_id=request_id, parent_run_id=parent_run_id,
        parent_snapshot_id=snapshot.snapshot_id,
        target_id=target.id, target_version=target.version,
        question="Continue the explicit comparison", scope="Recheck the open point",
        agent_ids=["agent-synthesis", "agent-critique"],
    ))
    child_ref = ref(host._run(child.run_id))
    handoff_ref = save_followup_handoff(
        host.store, f"handoff-{request_id}", child_run_ref=child_ref,
        parent_snapshot_ref={"kind": "research_snapshot",
                             "id": snapshot.snapshot_id, "version": 1},
        target_ref={"kind": "report", "id": target.id,
                    "version": target.version},
    )
    return child, child_ref, handoff_ref


def test_explicit_followup_pages_only_producers_snapshot_note(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch,
                   lambda request, *, timeout: answer("Synthetic parent answer", 1))
    try:
        parent = host.start(LiveStartRequest(
            request_id="parent-handoff", question="Review a synthetic comparison",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(parent.run_id, "agent-synthesis")
        task = host.store.get("board_proposal", control["body"]["task_id"])
        note_ref = host.notes.write(
            "parent-open-point", mission_id=control["body"]["mission_id"],
            actor_id=control["body"]["actor_id"], task_ref=ref(task),
            note_id="parent-open-point", category="next_action",
            text="Check the unresolved synthetic comparator.",
        )
        drain(host, parent.run_id)
        snapshot = host.state(parent.run_id).snapshot
        # A later private revision cannot rewrite the selected parent snapshot.
        host.store.transact("later-parent-note", {}, lambda tx: ref(tx.put(
            "working_note", note_ref["id"],
            {**tx.get(**note_ref)["body"], "text": "A later different note."},
            expected_version=note_ref["version"],
        )), mission_id=control["body"]["mission_id"])
        child, child_ref, handoff_ref = _child(
            host, parent.run_id, snapshot, request_id="child-handoff")
        producer = host._control(child.run_id, "agent-synthesis")["body"]
        other = host._control(child.run_id, "agent-critique")["body"]
        view = followup_handoff_view(
            host.store, handoff_ref=handoff_ref, child_run_ref=child_ref,
            actor_id=producer["actor_id"], task_id=producer["task_id"],
            inclusion_id="context:child-handoff-test",
        )
        assert view["status"] == "available_subject_to_current_source_use"
        assert view["note_count"] == 1
        assert len(canonical(view)) <= 700
        page = read_followup_handoff(
            host.store, handoff_ref=handoff_ref, child_run_ref=child_ref,
            actor_id=producer["actor_id"], task_id=producer["task_id"],
            inclusion_id="context:child-handoff-test", offset=0, limit=1200,
        )
        assert "Check the unresolved synthetic comparator." in page["content"]
        assert "A later different note." not in page["content"]
        assert page["source_dependency_refs"] == []
        assert view["read_request"] == {"tool": "read_context_record",
                                        "inclusion_id": "context:child-handoff-test",
                                        **handoff_ref, "offset": 0, "limit": 1200}
        first = read_followup_handoff(
            host.store, handoff_ref=handoff_ref, child_run_ref=child_ref,
            actor_id=producer["actor_id"], task_id=producer["task_id"],
            inclusion_id="context:child-handoff-test", offset=0, limit=80,
        )
        assert first["next_request"]["offset"] == 80
        second = read_followup_handoff(
            host.store, handoff_ref=handoff_ref, child_run_ref=child_ref,
            actor_id=producer["actor_id"], task_id=producer["task_id"],
            inclusion_id="context:child-handoff-test", offset=80, limit=80,
            projection_sha256=first["projection_sha256"],
        )
        assert first["content"] + second["content"] == page["content"][:160]
        with pytest.raises(ContinuationError, match="projection changed"):
            read_followup_handoff(
                host.store, handoff_ref=handoff_ref, child_run_ref=child_ref,
                actor_id=producer["actor_id"], task_id=producer["task_id"],
                inclusion_id="context:child-handoff-test", offset=80, limit=80,
                projection_sha256="0" * 64,
            )
        other_page = read_followup_handoff(
            host.store, handoff_ref=handoff_ref, child_run_ref=child_ref,
            actor_id=other["actor_id"], task_id=other["task_id"],
            inclusion_id="context:child-handoff-test", offset=0, limit=1200,
        )
        assert json.loads(other_page["content"])["notes"] == []
        with pytest.raises(ContinuationError, match="outside this child"):
            read_followup_handoff(
                host.store, handoff_ref=handoff_ref, child_run_ref=child_ref,
                actor_id="foreign", task_id=producer["task_id"],
                inclusion_id="context:child-handoff-test", offset=0, limit=1200,
            )
        reopened = StateStore(tmp_path, read_only=True)
        assert read_followup_handoff(
            reopened, handoff_ref=handoff_ref, child_run_ref=child_ref,
            actor_id=producer["actor_id"], task_id=producer["task_id"],
            inclusion_id="context:child-handoff-test", offset=0, limit=1200,
        )["content"] == page["content"]
    finally:
        host.close()


def test_parent_source_note_does_not_become_child_source_free_text(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch,
                   lambda request, *, timeout: answer("Synthetic source answer", 1))
    try:
        parent = host.start(LiveStartRequest(
            request_id="parent-supplied-note", question="Review supplied text",
            evidence_text="Synthetic user supplied evidence text.",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(parent.run_id, "agent-synthesis")
        task = host.store.get("board_proposal", control["body"]["task_id"])
        host.notes.write(
            "source-linked-private-note",
            mission_id=control["body"]["mission_id"],
            actor_id=control["body"]["actor_id"], task_ref=ref(task),
            note_id="source-linked-private-note", category="hypothesis",
            text="A source-derived private interpretation.",
            input_refs=control["body"]["source_refs"],
        )
        drain(host, parent.run_id)
        snapshot = host.state(parent.run_id).snapshot
        child, child_ref, handoff_ref = _child(
            host, parent.run_id, snapshot, request_id="child-supplied-note")
        producer = host._control(child.run_id, "agent-synthesis")["body"]
        page = read_followup_handoff(
            host.store, handoff_ref=handoff_ref, child_run_ref=child_ref,
            actor_id=producer["actor_id"], task_id=producer["task_id"],
            inclusion_id="context:child-supplied-note-test", offset=0, limit=1200,
        )
        assert "A source-derived private interpretation." not in page["content"]
        assert "data_unavailable_due_source_use" in page["content"]
        assert page["source_dependency_refs"] == []
    finally:
        host.close()
