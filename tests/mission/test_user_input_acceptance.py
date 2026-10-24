"""Durable, task-scoped acceptance of authenticated local user input."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from ora.contracts.models import (
    LiveBranchCancelRequest,
    LiveRunCancelRequest,
    LiveStartRequest,
    LiveUserInputRequest,
)
from ora.coordination.board import ref
from ora.mission.user_input import UserInputError, accept_user_input
from tests.mission.test_service import service


def _command(host, run_id, request_id="user-input-1", **updates):
    control = host._control(run_id, "agent-synthesis")
    task = host.store.get("board_proposal", control["body"]["task_id"])
    fields = dict(
        request_id=request_id, run_id=run_id, task_id=task["id"],
        target_agent_id=control["body"]["actor_id"],
        expected_task_version=task["version"], kind="direction",
        text="Please check the synthetic count before concluding.",
    )
    return LiveUserInputRequest(**{**fields, **updates})


def _start(host, request_id="user-input-run"):
    return host.start(LiveStartRequest(
        request_id=request_id, question="Inspect a synthetic count",
        agent_ids=["agent-synthesis"],
    ))


def test_acceptance_is_exact_host_attributed_and_replays_across_restart(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    run = _start(host)
    command = _command(host, run.run_id)
    receipt = accept_user_input(host.store, command)
    try:
        assert receipt.status == "queued_for_next_boundary"
        assert receipt.reused is False
        saved = host.store.get(**receipt.input_ref.model_dump())
        assert saved["body"]["text"] == command.text
        assert saved["body"]["kind"] == "direction"
        assert saved["body"]["origin"] == "authenticated_local_user"
        assert saved["body"]["task_ref"] == receipt.task_ref.model_dump()
        assert saved["body"]["actor_id"] == command.target_agent_id
        assert saved["body"]["request"] == command.model_dump(by_alias=True)
        assert accept_user_input(host.store, command).model_dump() == {
            **receipt.model_dump(), "reused": True,
        }
        events = [item for item in host.store.events(saved["body"]["mission_id"])
                  if item["type"] == "mission_user_input_accepted"]
        assert len(events) == 1
        assert events[0]["body"]["input_ref"] == ref(saved)
        assert command.text not in json.dumps(events[0])
        with pytest.raises(UserInputError) as conflict:
            accept_user_input(host.store, command.model_copy(update={"text": "Changed text"}))
        assert conflict.value.status == 409
        control = host._control(run.run_id, "agent-synthesis")
        host._update_control(control, phase="done")
        assert accept_user_input(host.store, command).reused is True
        with pytest.raises(UserInputError) as closed:
            accept_user_input(host.store, command.model_copy(update={"request_id": "after-close"}))
        assert closed.value.status == 409
        assert len(host.store.records("mission_user_input")) == 1
    finally:
        host.close()

    reopened = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        assert accept_user_input(reopened.store, command).reused is True
        with pytest.raises(UserInputError) as changed:
            accept_user_input(reopened.store, command.model_copy(update={"kind": "unverified_pasted_evidence"}))
        assert changed.value.status == 409
        assert len(reopened.store.records("mission_user_input")) == 1
    finally:
        reopened.close()


def test_foreign_target_stale_version_and_closed_control_do_not_record_input(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        first = _start(host, "user-target-a")
        other = _start(host, "user-target-b")
        command = _command(host, first.run_id)
        other_control = host._control(other.run_id, "agent-synthesis")
        for changed in (
            command.model_copy(update={"run_id": "missing-run", "request_id": "missing-run"}),
            command.model_copy(update={"target_agent_id": other_control["body"]["actor_id"],
                                       "request_id": "foreign-actor"}),
            command.model_copy(update={"expected_task_version": 2,
                                       "request_id": "stale-version"}),
        ):
            with pytest.raises(UserInputError):
                accept_user_input(host.store, changed)
        control = host._control(first.run_id, "agent-synthesis")
        host._update_control(control, phase="deferred", scope_status="deferred")
        with pytest.raises(UserInputError):
            accept_user_input(host.store, command)
        assert host.store.records("mission_user_input") == []
    finally:
        host.close()


def test_operator_hold_does_not_create_or_wake_input(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run = _start(host, "user-held-run")
        command = _command(host, run.run_id)
        control = host._control(run.run_id, "agent-synthesis")
        host._update_control(control, phase="waiting", operator_credential_hold={
            "phase": "ready_model", "waiting_reason": None,
        })
        with pytest.raises(UserInputError) as held:
            accept_user_input(host.store, command)
        assert held.value.status == 409
        assert host.store.records("mission_user_input") == []
    finally:
        host.close()


def test_racing_same_request_id_keeps_one_original_text(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run = _start(host, "user-input-race")
        first = _command(host, run.run_id, "racing-input", text="First synthetic update")
        second = first.model_copy(update={"text": "Second synthetic update"})

        def submit(command):
            try:
                return accept_user_input(host.store, command)
            except UserInputError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, [first, second]))
        assert len([item for item in results if not isinstance(item, UserInputError)]) == 1
        assert len([item for item in results if isinstance(item, UserInputError)
                    and item.status == 409]) == 1
        assert len(host.store.records("mission_user_input")) == 1
        saved = host.store.records("mission_user_input")[0]
        assert saved["body"]["text"] in {first.text, second.text}
        assert len([item for item in host.store.events(saved["body"]["mission_id"])
                    if item["type"] == "mission_user_input_accepted"]) == 1
    finally:
        host.close()


def test_completed_run_accepts_only_its_continuing_branch(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run = _start(host, "post-scope-input")
        command = _command(host, run.run_id, "continued-input")
        current = host._run(run.run_id)

        def mark_completed(tx):
            return ref(tx.put("mission_run", run.run_id, {
                **current["body"], "status": "completed",
            }, expected_version=current["version"]))

        host.store.transact("test-completed-with-continuing", {}, mark_completed,
                            mission_id=current["body"]["mission_id"])
        assert accept_user_input(host.store, command).status == "queued_for_next_boundary"

        other = _start(host, "post-scope-required-input")
        required_command = _command(host, other.run_id, "required-input")
        required_control = host._control(other.run_id, "agent-synthesis")
        host._update_control(required_control, scope_status="required")
        required_run = host._run(other.run_id)

        def mark_other_completed(tx):
            return ref(tx.put("mission_run", other.run_id, {
                **required_run["body"], "status": "completed",
            }, expected_version=required_run["version"]))

        host.store.transact("test-completed-with-required", {}, mark_other_completed,
                            mission_id=required_run["body"]["mission_id"])
        with pytest.raises(UserInputError) as closed:
            accept_user_input(host.store, required_command)
        assert closed.value.status == 409
    finally:
        host.close()


@pytest.mark.parametrize("cancel_kind", ["branch", "run"])
def test_cancelled_work_rejects_new_input_across_reopen(
    tmp_path, monkeypatch, cancel_kind,
):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    run = _start(host, f"user-input-{cancel_kind}-cancel")
    command = _command(host, run.run_id, f"after-{cancel_kind}-cancel")
    try:
        if cancel_kind == "branch":
            host.cancel_branch(LiveBranchCancelRequest(
                request_id="stop-user-input-branch", run_id=run.run_id,
                task_id=command.task_id,
                expected_task_version=command.expected_task_version,
                reason="Stop synthetic task",
            ))
        else:
            host.cancel_run(LiveRunCancelRequest(
                request_id="stop-user-input-run", run_id=run.run_id,
                expected_run_version=host._run(run.run_id)["version"],
                reason="Stop synthetic run",
            ))
        with pytest.raises(UserInputError) as stopped:
            accept_user_input(host.store, command)
        assert stopped.value.status == 409
        assert host.store.records("mission_user_input") == []
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "cancelled"
    finally:
        host.close()

    reopened = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        with pytest.raises(UserInputError) as still_stopped:
            accept_user_input(reopened.store, command)
        assert still_stopped.value.status == 409
        assert reopened.store.records("mission_user_input") == []
        assert reopened._control(run.run_id, "agent-synthesis")["body"]["phase"] == "cancelled"
    finally:
        reopened.close()
