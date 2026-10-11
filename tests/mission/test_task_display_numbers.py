"""Task labels follow durable creation order, including work not yet controlled."""

from pathlib import Path

from ora.agents import AgentRegistry
from ora.contracts.models import LiveStartRequest, ResearchSnapshot
from ora.coordination.board import ProposalBoard
from ora.mission.projection import _task_display_numbers, build_snapshot
from ora.state import StateStore
from ora.state.inspection import load_snapshot
from tests.mission.test_service import answer, service

ROOT = Path(__file__).resolve().parents[2]


def test_creation_sequence_survives_earlier_uuid_late_control_and_restart(tmp_path):
    mission_id = "synthetic-numbering-mission"
    store = StateStore(tmp_path, clock=lambda: 1_800_000_000)
    actor = AgentRegistry(store, ROOT).register(
        "register-numbering-actor", mission_id=mission_id, run_id="numbering-run",
        role_id="literature_acquisition", display_name="Synthetic Numbering Peer",
    )
    board = ProposalBoard(store)

    def propose(task_id):
        result = board.propose(
            "propose-" + task_id, mission_id=mission_id, proposal_id=task_id,
            actor_id=actor["id"], objective="Synthetic task " + task_id,
            expected_artifact="Synthetic artifact", next_action="Inspect source",
        )
        assert result["status"] == "accepted"

    root = "root-task"
    propose("task-z")
    first = _task_display_numbers(store, mission_id, root)
    assert first == {root: 1, "task-z": 2}

    # The second proposal is not yet controlled or visible. It still reserves
    # its creation-order number before a later UUID that sorts earlier.
    propose("task-m")
    propose("task-a")
    created = [event for event in store.events(mission_id)
               if event["type"] == "proposal_created"]
    assert len({event["created_at"] for event in created}) == 1
    assert [event["sequence"] for event in created] == sorted(
        event["sequence"] for event in created
    )
    expected = {root: 1, "task-z": 2, "task-m": 3, "task-a": 4}
    assert _task_display_numbers(store, mission_id, root) == expected
    assert _task_display_numbers(StateStore(tmp_path), mission_id, root) == expected
    assert _task_display_numbers(store, "another-run-mission", "another-root") == {
        "another-root": 1
    }


def test_missing_legacy_creation_event_has_no_invented_number(tmp_path):
    store = StateStore(tmp_path)
    mission_id = "synthetic-legacy-mission"
    store.transact(
        "legacy-proposal", {"task": "legacy"},
        lambda tx: tx.put("board_proposal", "legacy-task", {"mission_id": mission_id}),
        mission_id=mission_id,
    )
    assert _task_display_numbers(store, mission_id, "root-task") == {"root-task": 1}


def test_snapshot_numbers_are_run_scoped_and_optional_for_legacy_clients(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("unused", 1))
    try:
        run = host.start(LiveStartRequest(
            request_id="display-numbers-run", question="Inspect synthetic evidence",
            agent_ids=["agent-synthesis"],
        ))
        controls = host._controls(run.run_id)
        snapshot, *_ = build_snapshot(
            host.store, host._run(run.run_id), [controls[0], controls[0]],
            snapshot_id="synthetic-duplicate-control-view",
        )
        root, first, second = snapshot["tasks"]
        assert root["id"] == run.task_id and root["displayNumber"] == 1
        assert first["id"] == second["id"] and first["displayNumber"] == second["displayNumber"] == 2

        saved = host.state(run.run_id).snapshot
        loaded = load_snapshot(StateStore(tmp_path), saved.snapshot_id)
        assert {task["id"]: task["displayNumber"] for task in loaded["tasks"]} == {
            run.task_id: 1, first["id"]: 2,
        }

        historical = saved.model_dump(mode="json", by_alias=True)
        for task in historical["tasks"]:
            task.pop("displayNumber", None)
        reopened = ResearchSnapshot.model_validate(historical)
        assert all(task.display_number is None for task in reopened.tasks)
    finally:
        host.close()
