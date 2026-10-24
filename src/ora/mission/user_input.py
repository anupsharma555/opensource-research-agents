"""Accept authenticated local user text for one continuing research task.

This module records command acceptance. Model preparation and scientific use are
separate facts owned by the mission loop and its context inclusion receipts.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from ora.contracts.models import LiveUserInputReceipt, LiveUserInputRequest
from ora.coordination.board import ref
from ora.state import StateConflict, StateStore


class UserInputError(ValueError):
    """A local command failed at its exact task or request identity boundary."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _record_id(request_id: str) -> str:
    return "user-input-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]


def accept_user_input(
    store: StateStore, command: LiveUserInputRequest,
) -> LiveUserInputReceipt:
    """Save one host-attributed input atomically, preserving idempotent replay."""
    if not isinstance(command, LiveUserInputRequest):
        command = LiveUserInputRequest.model_validate(command)
    requested = command.model_dump(mode="json", by_alias=True)
    input_id = _record_id(command.request_id)

    def save(tx):
        prior = tx.get("mission_user_input", input_id)
        if prior is not None:
            if prior["body"].get("request") != requested:
                raise UserInputError("Request ID already names different user input")
            body = prior["body"]
            return {
                "request_id": command.request_id,
                "run_id": body["run_id"],
                "task_id": body["task_id"],
                "target_agent_id": body["actor_id"],
                "status": "queued_for_next_boundary",
                "reused": True,
                "input_ref": ref(prior),
                "task_ref": body["task_ref"],
            }

        run = tx.get("mission_run", command.run_id)
        if run is None:
            raise UserInputError("Run is unavailable", 404)
        mission_id = run["body"].get("mission_id")
        task = tx.get("board_proposal", command.task_id)
        actor = tx.get("agent_instance", command.target_agent_id, 1)
        if (
            task is None or actor is None or not mission_id
            or task["body"].get("mission_id") != mission_id
            or actor["body"].get("mission_id") != mission_id
            or actor["body"].get("run_id") != command.run_id
            or task["body"].get("actor_ref") != ref(actor)
        ):
            raise UserInputError("Target task and agent are unavailable in this run", 404)
        if task["version"] != command.expected_task_version:
            raise UserInputError("Task version changed; reread before submitting input")
        if task["body"].get("status") != "open":
            raise UserInputError("Task has no continuing work for user input")

        rows = tx.db.execute(
            "SELECT r.body FROM records r JOIN heads h USING(kind,id,version) "
            "WHERE r.kind='mission_task_control' AND json_extract(r.body,'$.run_id')=? "
            "AND json_extract(r.body,'$.task_id')=? AND json_extract(r.body,'$.actor_id')=?",
            (command.run_id, command.task_id, command.target_agent_id),
        ).fetchall()
        controls = [json.loads(row["body"]) for row in rows]
        if len(controls) != 1 or controls[0].get("mission_id") != mission_id:
            raise UserInputError("Exact active task control is unavailable", 404)
        control = controls[0]
        if (
            control.get("phase") in {"done", "deferred", "cancelled"}
            or control.get("scope_status") in {"deferred", "cancelled"}
            or control.get("operator_credential_hold")
            or run["body"].get("status") == "cancelled"
            or run["body"].get("status") == "completed"
            and control.get("scope_status") != "continued"
            or run["body"].get("status") not in {"active", "waiting", "completed"}
        ):
            raise UserInputError("Target task is not accepting active-run input")

        saved = tx.put("mission_user_input", input_id, {
            "schema": "ora.mission_user_input.v1",
            "request_id": command.request_id,
            "request": requested,
            "mission_id": mission_id,
            "run_id": command.run_id,
            "task_id": command.task_id,
            "actor_id": command.target_agent_id,
            "run_ref": ref(run),
            "task_ref": ref(task),
            "actor_ref": ref(actor),
            "kind": command.kind,
            "text": command.text,
            "origin": "authenticated_local_user",
            "created_at": store.now(),
        }, expected_version=0)
        tx.link(saved, task, "about_task")
        tx.emit(mission_id, "mission_user_input_accepted", {
            "input_ref": ref(saved), "run_id": command.run_id,
            "task_id": command.task_id, "actor_id": command.target_agent_id,
            "subjects": [{"kind": "task", "id": command.task_id}],
        })
        return {
            "request_id": command.request_id,
            "run_id": command.run_id,
            "task_id": command.task_id,
            "target_agent_id": command.target_agent_id,
            "status": "queued_for_next_boundary",
            "reused": False,
            "input_ref": ref(saved),
            "task_ref": ref(task),
        }

    try:
        result = store.transact(
            f"mission-user-input:{input_id}:{uuid.uuid4().hex}",
            requested, save,
        )
    except StateConflict as exc:
        raise UserInputError(str(exc)) from exc
    return LiveUserInputReceipt.model_validate(result)
