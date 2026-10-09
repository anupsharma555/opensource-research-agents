"""Exercise identity through the shared transactional state store."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ora.agents import AgentIdentityError, AgentRegistry
from ora.state import StateConflict, StateStore

ROOT = Path(__file__).resolve().parents[2]


def test_nine_roles_lanes_temporary_restart_and_replacement(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    registry = AgentRegistry(store, ROOT)
    assert len(registry.role_ids) == 9
    instances = [
        registry.register(
            f"create-{role}",
            mission_id="mission-one",
            run_id="run-one",
            role_id=role,
            display_name=role.replace("_", " "),
        )
        for role in sorted(registry.role_ids)
    ]
    lanes = [
        registry.register(
            f"lane-{lane}",
            mission_id="mission-one",
            run_id="run-one",
            role_id="literature_acquisition",
            kind="lane",
            lane_id=lane,
            display_name=f"Acquisition {lane}",
        )
        for lane in ("database", "preprint", "citation", "registry", "repository", "dataset")
    ]
    temporary = registry.register(
        "temp-one",
        mission_id="mission-one",
        run_id="run-one",
        role_id="temporary_specialist",
        kind="temporary",
        display_name="Methods specialist",
    )
    ids = {item["id"] for item in [*instances, *lanes, temporary]}
    assert len(ids) == 16
    assert registry.by_name("mission-one", "  METHODS   specialist  ")["id"] == temporary["id"]
    assert (
        registry.register(
            "temp-one",
            mission_id="mission-one",
            run_id="run-one",
            role_id="temporary_specialist",
            kind="temporary",
            display_name="Methods specialist",
        )["id"]
        == temporary["id"]
    )
    replacement = registry.register(
        "temp-two",
        mission_id="mission-one",
        run_id="run-two",
        role_id="temporary_specialist",
        kind="temporary",
        display_name="Methods specialist 2",
        predecessor_id=temporary["id"],
    )
    assert replacement["id"] != temporary["id"]
    assert registry.get(temporary["id"])["body"]["display_name"] == "Methods specialist"
    assert registry.by_name("mission-one", "Methods specialist")["id"] == temporary["id"]
    assert any(link["relation"] == "replaces" for link in store.links(replacement))
    reopened = AgentRegistry(StateStore(tmp_path), ROOT)
    assert reopened.get(temporary["id"])["id"] == temporary["id"]
    assert (
        reopened.register(
            "temp-one",
            mission_id="mission-one",
            run_id="run-one",
            role_id="temporary_specialist",
            kind="temporary",
            display_name="Methods specialist",
        )["id"]
        == temporary["id"]
    )
    assert reopened.get(replacement["id"])["body"]["predecessor_id"] == temporary["id"]


def test_concurrent_name_collision_and_scoped_activity(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    registry = AgentRegistry(store, ROOT)

    def create(number: int):
        return registry.register(
            f"r-{number}",
            mission_id="m",
            run_id="r",
            role_id="analysis",
            display_name="Same Analyst",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create, index) for index in (1, 2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except Exception as exc:
                outcomes.append(exc)
    accepted = [item for item in outcomes if isinstance(item, dict)]
    assert len(accepted) == 1
    assert len([item for item in outcomes if isinstance(item, StateConflict)]) == 1
    actor = accepted[0]
    assert registry.by_name("m", "same analyst")["id"] == actor["id"]
    assert (
        registry.register(
            "other-mission",
            mission_id="m2",
            run_id="r2",
            role_id="analysis",
            display_name="Same Analyst",
        )["id"]
        != actor["id"]
    )

    owned_message = {
        "mission_id": "m",
        "task_id": "task-one",
        "attempt_id": "attempt-one",
        "actor_ref": {"kind": "agent_instance", "id": actor["id"], "version": 1},
    }
    message = store.transact(
        "message-op",
        {"id": "message-one"},
        lambda tx: tx.put("message", "message-one", owned_message, expected_version=0),
        mission_id="m",
    )
    activity = registry.record_activity(
        "activity-op",
        mission_id="m",
        instance_id=actor["id"],
        target_kind="message",
        target_id=message["id"],
        target_version=message["version"],
        task_id="task-one",
        attempt_id="attempt-one",
    )
    assert (
        registry.record_activity(
            "activity-op",
            mission_id="m",
            instance_id=actor["id"],
            target_kind="message",
            target_id=message["id"],
            target_version=message["version"],
            task_id="task-one",
            attempt_id="attempt-one",
        )["id"]
        == activity["id"]
    )
    assert {link["relation"] for link in store.links(activity)} == {"performed_by", "describes"}
    with pytest.raises(AgentIdentityError, match="call lineage"):
        registry.record_activity(
            "invented-call",
            mission_id="m",
            instance_id=actor["id"],
            target_kind="message",
            target_id=message["id"],
            target_version=1,
            task_id="task-one",
            attempt_id="attempt-one",
            call_id="unrelated-call",
        )
    with pytest.raises(AgentIdentityError):
        registry.record_activity(
            "missing",
            mission_id="m",
            instance_id=actor["id"],
            target_kind="message",
            target_id="missing",
            target_version=1,
            task_id="task-one",
            attempt_id="attempt-one",
        )
    foreign = store.transact(
        "foreign-message",
        {"id": "foreign-message"},
        lambda tx: tx.put("message", "foreign-message", {"mission_id": "m2"}, expected_version=0),
        mission_id="m2",
    )
    with pytest.raises(AgentIdentityError, match="mission ownership"):
        registry.record_activity(
            "foreign-activity",
            mission_id="m",
            instance_id=actor["id"],
            target_kind="message",
            target_id=foreign["id"],
            target_version=1,
            task_id="task-one",
            attempt_id="attempt-one",
        )
    for suffix, changed in (
        ("missing-owner", {"actor_ref": None}),
        ("wrong-actor", {"actor_ref": {"kind": "agent_instance", "id": "other", "version": 1}}),
        ("wrong-task", {"task_id": "other-task"}),
        ("wrong-attempt", {"attempt_id": "other-attempt"}),
        ("missing-mission", {"mission_id": None}),
    ):
        body = {**owned_message, **changed}
        target = store.transact(
            f"target-{suffix}",
            {"id": suffix},
            lambda tx, value=body, name=suffix: tx.put("message", name, value),
            mission_id="m",
        )
        with pytest.raises(AgentIdentityError):
            registry.record_activity(
                f"activity-{suffix}",
                mission_id="m",
                instance_id=actor["id"],
                target_kind="message",
                target_id=target["id"],
                target_version=1,
                task_id="task-one",
                attempt_id="attempt-one",
            )


def test_activity_can_verify_exact_call_lineage(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    registry = AgentRegistry(store, ROOT)
    actor = registry.register(
        "actor", mission_id="m", run_id="r", role_id="analysis", display_name="Analyst"
    )
    store.transact(
        "call-op",
        {"call": "c1"},
        lambda tx: tx.put(
            "agent_call",
            "c1",
            {
                "mission_id": "m",
                "instance_id": actor["id"],
                "task_id": "task",
                "attempt_id": "attempt",
            },
        ),
        mission_id="m",
    )
    receipt = store.transact(
        "receipt-op",
        {"receipt": "tool-1"},
        lambda tx: tx.put(
            "capability_invocation",
            "tool-1",
            {"mission_id": "m", "call_id": "c1", "manifest_version": 1},
        ),
        mission_id="m",
    )
    activity = registry.record_activity(
        "activity-op",
        mission_id="m",
        instance_id=actor["id"],
        target_kind="capability_invocation",
        target_id=receipt["id"],
        target_version=1,
        task_id="task",
        attempt_id="attempt",
        call_id="c1",
    )
    assert activity["body"]["verified_call_id"] == "c1"
    with pytest.raises(AgentIdentityError, match="call lineage"):
        registry.record_activity(
            "wrong-call-work",
            mission_id="m",
            instance_id=actor["id"],
            target_kind="capability_invocation",
            target_id=receipt["id"],
            target_version=1,
            task_id="task",
            attempt_id="wrong-attempt",
            call_id="c1",
        )


def test_invalid_identity_requests_are_scoped(tmp_path: Path) -> None:
    registry = AgentRegistry(StateStore(tmp_path), ROOT)
    with pytest.raises(AgentIdentityError):
        registry.register("bad", mission_id="m", run_id="r", role_id="analysis", display_name="   ")
    with pytest.raises(AgentIdentityError):
        registry.register(
            "bad-lane",
            mission_id="m",
            run_id="r",
            role_id="analysis",
            display_name="Lane",
            kind="lane",
            lane_id="citations",
        )
