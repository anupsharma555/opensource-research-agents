"""Metadata notices wake only relevant active peers before model dispatch."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ora.coordination.board import ref
from ora.mission.peer_freshness import changed_peer_input
from ora.mission.research_notifications import record_research_notice
from ora.state import StateError, StateStore, Transaction
from tests.mission.test_peer_queued_freshness import _spec, _start_queued
from tests.mission.test_service import service


def _fixture(tmp_path):
    store = StateStore(tmp_path)
    actors = ("producer", "recipient", "done-peer", "other-run")

    def seed(tx):
        for actor in actors:
            run_id = "other" if actor == "other-run" else "run"
            tx.put("agent_instance", actor, {"mission_id": "mission", "run_id": run_id})
            proposal = tx.put(
                "board_proposal",
                f"task-{actor}",
                {
                    "mission_id": "mission",
                    "actor_ref": {"kind": "agent_instance", "id": actor, "version": 1},
                    "status": "open",
                },
            )
            tx.put(
                "mission_task_control",
                f"control-{actor}",
                {
                    "mission_id": "mission",
                    "run_id": run_id,
                    "actor_id": actor,
                    "task_id": proposal["id"],
                    "phase": "done" if actor == "done-peer" else "ready_model",
                },
            )
        return tx.get("board_proposal", "task-producer")

    proposal = store.transact("seed-notice", {}, seed, mission_id="mission")
    wakes = []
    host = SimpleNamespace(
        store=store,
        executor=SimpleNamespace(notify=lambda mission_id: wakes.append(mission_id)),
        _controls=lambda run_id: [
            control
            for control in store.records("mission_task_control")
            if control["body"]["run_id"] == run_id
        ],
    )
    spec = SimpleNamespace(
        mission_id="mission",
        run_id="run",
        actor_id="producer",
        proposal_ref=ref(proposal),
    )
    return store, host, spec, wakes


def _changed(store, *, task_id="task-recipient", actor_id="recipient", through=0):
    db = store._connect()
    try:
        return changed_peer_input(
            Transaction(store, db),
            db,
            mission_id="mission",
            run_id="run",
            task_id=task_id,
            actor_id=actor_id,
            event_through=through,
        )
    finally:
        db.close()


def test_notice_is_coalesced_scoped_and_contains_no_source_text(tmp_path):
    store, host, spec, wakes = _fixture(tmp_path)
    first = record_research_notice(
        host, spec, kind="responsibility_change", subject_ref=spec.proposal_ref
    )
    assert first["status"] == "recorded"
    assert len(first["notice_refs"]) == 1
    notice = store.get(**first["notice_refs"][0])
    assert notice["body"]["recipient_task_id"] == "task-recipient"
    assert "text" not in notice["body"] and "payload" not in notice["body"]
    assert wakes == ["mission"]
    event = _changed(store)
    assert event["event_type"] == "research_peer_notice"
    assert event["request_ref"] == ref(notice)
    assert _changed(store, task_id="task-producer", actor_id="producer") is None
    assert _changed(store, task_id="task-done-peer", actor_id="done-peer") is None
    with pytest.raises(StateError, match="outside this run"):
        _changed(store, task_id="task-other-run", actor_id="other-run")
    notice_sequence = next(
        event["sequence"]
        for event in store.events("mission")
        if event["type"] == "research_peer_notice"
    )
    assert _changed(store, through=notice_sequence) is None
    repeated = record_research_notice(
        host, spec, kind="responsibility_change", subject_ref=spec.proposal_ref
    )
    assert repeated == first  # Transaction replay is durable and emits no new event.
    assert len([e for e in store.events("mission") if e["type"] == "research_peer_notice"]) == 1
    assert wakes == ["mission", "mission"]  # A replay may nudge local readers without a new event.


def test_gap_notice_updates_same_recipient_slot_and_rejects_unchecked_copy(tmp_path):
    store, host, spec, _wakes = _fixture(tmp_path)

    def add_note(tx, name):
        return tx.put(
            "working_note",
            name,
            {
                "mission_id": "mission",
                "run_id": "run",
                "actor_id": "producer",
                "task_id": "task-producer",
                "text": "Synthetic gap retained in its source record, not the notice",
            },
        )

    first_note = store.transact(
        "first-gap", {}, lambda tx: add_note(tx, "gap-one"), mission_id="mission"
    )
    second_note = store.transact(
        "second-gap", {}, lambda tx: add_note(tx, "gap-two"), mission_id="mission"
    )
    first = record_research_notice(host, spec, kind="new_gap", subject_ref=ref(first_note))
    second = record_research_notice(host, spec, kind="new_gap", subject_ref=ref(second_note))
    assert first["notice_refs"][0]["id"] == second["notice_refs"][0]["id"]
    assert second["notice_refs"][0]["version"] == 2
    assert "Synthetic gap" not in str(store.get(**second["notice_refs"][0])["body"])
    copy = store.transact(
        "unchecked-copy",
        {},
        lambda tx: tx.put("source_copy", "unchecked", {"mission_id": "mission"}),
        mission_id="mission",
    )
    with pytest.raises(StateError, match="currently permitted"):
        record_research_notice(host, spec, kind="checked_evidence", subject_ref=ref(copy))


def test_notice_replaces_only_unsubmitted_model_input_without_provider_call(
    tmp_path, monkeypatch
):
    host = service(
        tmp_path,
        monkeypatch,
        lambda *_args, **_kwargs: pytest.fail("Notice refresh called model provider"),
    )
    try:
        run, pending, intent, work, spec, cap_id = _start_queued(host, "notice-queued")
        origin = _spec(host, run.run_id, "agent-critique", "emit-substantive-notice")
        notice = record_research_notice(
            host, origin, kind="responsibility_change", subject_ref=origin.proposal_ref
        )
        assert notice["status"] == "recorded"
        assert host._supersede_queued_peer_input(pending)
        refreshed = host._control(run.run_id, "agent-synthesis")
        assert refreshed["body"]["phase"] == "ready_model"
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "superseded"
        assert host.store.get("capacity_operation", cap_id)["body"]["phase"] == "canceled"
        assert host.store.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}") is None
    finally:
        host.close()
