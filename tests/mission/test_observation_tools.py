"""Model-authored observation tools leave leased controls to the runtime."""

from __future__ import annotations

import pytest

from ora.agents.capabilities import CapabilityError, validate_arguments
from ora.agents.json_shapes import validate_schema
from ora.coordination.board import ref
from ora.mission.observation_tools import (
    OBSERVATION_TOOL_SCHEMAS,
    record_latency_choice,
    record_wait_choice,
    wait_replan_notification,
)
from ora.state import StateStore


def fixture(store: StateStore, *, dependency: bool = False) -> dict:
    a_ref = {"kind": "mission_task_control", "id": "control-a", "version": 1}
    b_ref = {"kind": "mission_task_control", "id": "control-b", "version": 1}

    def save(tx):
        tx.put("mission_run", "run", {"mission_id": "m", "run_id": "run"})
        a = tx.put("agent_instance", "actor-a", {"mission_id": "m", "run_id": "run"})
        tx.put("agent_instance", "actor-b", {"mission_id": "m", "run_id": "run"})
        b_task = tx.put("board_proposal", "task-b", {"mission_id": "m", "status": "open"})
        a_task = tx.put(
            "board_proposal",
            "task-a",
            {
                "mission_id": "m",
                "status": "open",
                "depends_on": [ref(b_task)] if dependency else [],
            },
        )
        call = tx.put(
            "agent_call",
            "call-a",
            {
                "mission_id": "m",
                "instance_id": "actor-a",
                "task_id": "task-a",
            },
        )
        response = tx.put(
            "model_response_receipt",
            "response-a",
            {
                "mission_id": "m",
                "task_id": "task-a",
                "status": "tool_requested",
                "call_ref": ref(call),
            },
        )
        request = tx.put(
            "mail_request",
            "request-ab",
            {
                "mission_id": "m",
                "requester_id": "actor-a",
                "recipient_id": "actor-b",
                "status": "unanswered",
                "answer_refs": [],
            },
        )
        tx.put(
            "mission_task_control",
            "control-b",
            {
                "mission_id": "m",
                "run_id": "run",
                "task_id": "task-b",
                "proposal_ref": ref(b_task),
                "actor_id": "actor-b",
                "phase": "ready_model",
                "work_id": None,
            },
        )
        tx.put(
            "mission_task_control",
            "control-a",
            {
                "mission_id": "m",
                "run_id": "run",
                "task_id": "task-a",
                "proposal_ref": ref(a_task),
                "actor_id": "actor-a",
                "response_ref": ref(response),
                "phase": "pending_tool",
                "work_id": "tool-work",
            },
        )
        return {
            "a": a_ref,
            "b": b_ref,
            "response": ref(response),
            "request": ref(request),
            "a_task": ref(a_task),
            "b_task": ref(b_task),
            "actor": ref(a),
        }

    return store.transact("fixture", {}, save, mission_id="m")


def peer_dependency(saved: dict) -> dict:
    return {
        "kind": "peer_request",
        "request_id": saved["request"]["id"],
        "request_version": saved["request"]["version"],
        "target_control_id": saved["b"]["id"],
        "target_control_version": saved["b"]["version"],
    }


def proposal_dependency(saved: dict) -> dict:
    return {
        "kind": "proposal_dependency",
        "proposal_id": saved["b_task"]["id"],
        "proposal_version": saved["b_task"]["version"],
        "target_control_id": saved["b"]["id"],
        "target_control_version": saved["b"]["version"],
    }


def test_schemas_are_strict_and_latency_choice_is_revisable(tmp_path) -> None:
    store = StateStore(tmp_path)
    saved = fixture(store)
    for schema in OBSERVATION_TOOL_SCHEMAS.values():
        validate_schema(schema)
    assert (
        validate_arguments(
            OBSERVATION_TOOL_SCHEMAS["estimate_latency"],
            {
                "category": "medium",
                "basis": "A focused review is needed",
                "expected_version": 0,
            },
        )
        is None
    )
    first = record_latency_choice(
        store,
        "estimate-1",
        control_ref=saved["a"],
        response_ref=saved["response"],
        category="medium",
        basis="A focused review is needed",
        expected_version=0,
    )
    assert first["status"] == "latency_expectation_saved"
    assert first["expectation_ref"]["version"] == 1
    assert first["control_delta"] == {}
    assert store.get(**saved["a"])["body"]["phase"] == "pending_tool"

    def new_turn(tx):
        call = tx.put(
            "agent_call",
            "call-a-2",
            {
                "mission_id": "m",
                "instance_id": "actor-a",
                "task_id": "task-a",
            },
        )
        response = tx.put(
            "model_response_receipt",
            "response-a-2",
            {
                "mission_id": "m",
                "task_id": "task-a",
                "status": "tool_requested",
                "call_ref": ref(call),
            },
        )
        old = tx.get("mission_task_control", "control-a")
        control = tx.put(
            "mission_task_control",
            "control-a",
            {
                **old["body"],
                "response_ref": ref(response),
            },
            expected_version=1,
        )
        return {"control": ref(control), "response": ref(response)}

    second = store.transact("new-turn", {}, new_turn, mission_id="m")
    assert (
        record_latency_choice(
            store,
            "estimate-1",
            control_ref=saved["a"],
            response_ref=saved["response"],
            category="medium",
            basis="A focused review is needed",
            expected_version=0,
        )
        == first
    )
    revised = record_latency_choice(
        store,
        "estimate-2",
        control_ref=second["control"],
        response_ref=second["response"],
        category="long",
        basis="Independent checking remains",
        expected_version=1,
    )
    assert revised["expectation_ref"]["version"] == 2
    assert store.get(**first["expectation_ref"])["body"]["category"] == "medium"


def test_unresolved_wait_saves_decision_but_not_control_and_replays(tmp_path) -> None:
    store = StateStore(tmp_path)
    saved = fixture(store)
    args = {"dependencies": [peer_dependency(saved)], "reason": "Await this peer's source check"}
    assert validate_arguments(OBSERVATION_TOOL_SCHEMAS["wait_for_dependencies"], args) is None
    result = record_wait_choice(
        store,
        "wait-choice",
        control_ref=saved["a"],
        response_ref=saved["response"],
        **args,
    )
    assert result["status"] == "wait_decision_saved"
    assert result["control_delta"]["wait_for"] == [
        {
            "kind": "peer_request",
            "request_ref": saved["request"],
            "target_control_ref": saved["b"],
        }
    ]
    assert result["control_delta"]["phase"] == "waiting"
    assert store.get(**saved["a"])["body"]["phase"] == "pending_tool"
    assert store.get(**result["decision_ref"])["body"]["response_ref"] == saved["response"]

    def apply_after_acceptance(tx):
        control = tx.get("mission_task_control", "control-a")
        return ref(
            tx.put(
                "mission_task_control",
                "control-a",
                {
                    **control["body"],
                    **result["control_delta"],
                },
                expected_version=1,
            )
        )

    updated = store.transact("accepted-tool-apply", {}, apply_after_acceptance, mission_id="m")
    assert updated["version"] == 2
    assert (
        record_wait_choice(
            store,
            "wait-choice",
            control_ref=saved["a"],
            response_ref=saved["response"],
            **args,
        )
        == result
    )
    assert len(store.records("mission_wait_decision")) == 1
    assert wait_replan_notification(store, control_ref=updated) is None

    def answer_after_wait(tx):
        prior = tx.get("mail_request", "request-ab")
        tx.put(
            "mail_request",
            "request-ab",
            {
                **prior["body"],
                "status": "awaiting_review",
            },
            expected_version=1,
        )
        return {"answered": True}

    store.transact("answer-after-wait", {}, answer_after_wait, mission_id="m")
    notice = wait_replan_notification(store, control_ref=updated)
    assert notice["status"] == "replan_notification_available"
    assert notice["feedback"]["stranded_waits"][0]["reason"] == "no_recorded_future_wake"
    assert store.get(**updated)["body"]["phase"] == "waiting"


@pytest.mark.parametrize("is_peer", [True, False])
def test_wait_schema_requires_exact_branch_reference_and_handler_accepts_it(
    tmp_path, is_peer,
) -> None:
    store = StateStore(tmp_path)
    saved = fixture(store, dependency=not is_peer)
    item = peer_dependency(saved) if is_peer else proposal_dependency(saved)
    schema = OBSERVATION_TOOL_SCHEMAS["wait_for_dependencies"]
    args = {"dependencies": [item], "reason": "Wait for the selected exact dependency"}
    validate_arguments(schema, args)
    for name in item:
        incomplete = {key: value for key, value in item.items() if key != name}
        with pytest.raises(CapabilityError):
            validate_arguments(schema, {**args, "dependencies": [incomplete]})
    prefix = "request" if is_peer else "proposal"
    for invalid in (
        {**item, f"{prefix}_version": 0},
        {**item, "target_control_version": True},
        {**item, "proposal_id" if is_peer else "request_id": "wrong-kind"},
    ):
        with pytest.raises(CapabilityError):
            validate_arguments(schema, {**args, "dependencies": [invalid]})
    with pytest.raises(CapabilityError):
        validate_arguments(schema, {**args, "dependencies": []})
    result = record_wait_choice(
        store, "schema-handler-parity", control_ref=saved["a"],
        response_ref=saved["response"], **args,
    )
    assert result["status"] == "wait_decision_saved"
    assert result["control_delta"]["wait_for"][0]["kind"] == item["kind"]


def test_answered_request_and_completed_prerequisite_return_feedback(tmp_path) -> None:
    peer_store = StateStore(tmp_path / "peer")
    saved = fixture(peer_store)

    def answer_and_progress(tx):
        request = tx.get("mail_request", "request-ab")
        answer = tx.put("mail_answer", "answer", {"mission_id": "m", "request_id": "request-ab"})
        tx.put(
            "mail_request",
            "request-ab",
            {
                **request["body"],
                "status": "awaiting_review",
                "answer_refs": [ref(answer)],
            },
            expected_version=1,
        )
        target = tx.get("mission_task_control", "control-b")
        tx.put(
            "mission_task_control",
            "control-b",
            {
                **target["body"],
                "phase": "done",
            },
            expected_version=1,
        )
        return {"answered": True}

    peer_store.transact("answer", {}, answer_and_progress, mission_id="m")
    result = record_wait_choice(
        peer_store,
        "wait-answered",
        control_ref=saved["a"],
        response_ref=saved["response"],
        dependencies=[peer_dependency(saved)],
        reason="Wait for answer",
    )
    assert result["status"] == "dependency_available_for_review"
    assert result["available"][0]["status"] == "awaiting_review"
    assert result["available"][0]["answer_refs"][0]["id"] == "answer"
    assert result["control_delta"] == {}
    assert peer_store.records("mission_wait_decision") == []

    proposal_store = StateStore(tmp_path / "proposal")
    saved = fixture(proposal_store, dependency=True)

    def complete(tx):
        prior = tx.get("board_proposal", "task-b")
        outcome = tx.put("board_outcome", "outcome-b", {"mission_id": "m"})
        tx.put(
            "board_proposal",
            "task-b",
            {
                **prior["body"],
                "status": "completed",
                "outcome_ref": ref(outcome),
            },
            expected_version=1,
        )
        return {"completed": True}

    proposal_store.transact("complete", {}, complete, mission_id="m")
    result = record_wait_choice(
        proposal_store,
        "wait-completed",
        control_ref=saved["a"],
        response_ref=saved["response"],
        dependencies=[proposal_dependency(saved)],
        reason="Wait for prerequisite",
    )
    assert result["status"] == "dependency_available_for_review"
    assert result["available"][0]["status"] == "completed"
    assert result["available"][0]["outcome_ref"]["id"] == "outcome-b"
    assert proposal_store.records("mission_wait_decision") == []

    deferred_store = StateStore(tmp_path / "deferred")
    saved = fixture(deferred_store, dependency=True)

    def defer(tx):
        prior = tx.get("board_proposal", "task-b")
        tx.put(
            "board_proposal", "task-b", {**prior["body"], "status": "deferred"}, expected_version=1
        )
        return {"deferred": True}

    deferred_store.transact("defer", {}, defer, mission_id="m")
    result = record_wait_choice(
        deferred_store,
        "wait-deferred",
        control_ref=saved["a"],
        response_ref=saved["response"],
        dependencies=[proposal_dependency(saved)],
        reason="Wait for prerequisite",
    )
    assert result["status"] == "dependency_available_for_review"
    assert result["available"][0]["status"] == "deferred"
    assert deferred_store.records("mission_wait_decision") == []


def test_open_prerequisite_saves_exact_wait_without_changing_control(tmp_path) -> None:
    store = StateStore(tmp_path)
    saved = fixture(store, dependency=True)
    result = record_wait_choice(
        store,
        "wait-open-prerequisite",
        control_ref=saved["a"],
        response_ref=saved["response"],
        dependencies=[proposal_dependency(saved)],
        reason="Need the prerequisite source check",
    )
    assert result["status"] == "wait_decision_saved"
    assert result["control_delta"]["wait_for"] == [
        {
            "kind": "proposal_dependency",
            "proposal_ref": saved["b_task"],
            "target_control_ref": saved["b"],
        }
    ]
    assert store.get(**saved["a"])["body"]["phase"] == "pending_tool"


def test_cycle_notification_reopens_reasoning_once_without_choosing_action(tmp_path) -> None:
    store = StateStore(tmp_path)
    a = {"kind": "mission_task_control", "id": "a", "version": 1}
    b = {"kind": "mission_task_control", "id": "b", "version": 1}
    ab = {"kind": "mail_request", "id": "ab", "version": 1}
    ba = {"kind": "mail_request", "id": "ba", "version": 1}

    def save(tx):
        tx.put(
            "mail_request",
            "ab",
            {
                "mission_id": "m",
                "status": "unanswered",
                "requester_id": "actor-a",
                "recipient_id": "actor-b",
            },
        )
        tx.put(
            "mail_request",
            "ba",
            {
                "mission_id": "m",
                "status": "unanswered",
                "requester_id": "actor-b",
                "recipient_id": "actor-a",
            },
        )
        tx.put(
            "mission_task_control",
            "a",
            {
                "mission_id": "m",
                "actor_id": "actor-a",
                "phase": "waiting",
                "work_id": None,
                "wait_for": [{"kind": "peer_request", "request_ref": ab, "target_control_ref": b}],
            },
        )
        tx.put(
            "mission_task_control",
            "b",
            {
                "mission_id": "m",
                "actor_id": "actor-b",
                "phase": "waiting",
                "work_id": None,
                "wait_for": [{"kind": "peer_request", "request_ref": ba, "target_control_ref": a}],
            },
        )
        return {"saved": True}

    store.transact("cycle", {}, save, mission_id="m")
    notice = wait_replan_notification(store, control_ref=a)
    assert notice["status"] == "replan_notification_available"
    assert len(notice["feedback"]["cycles"]) == 1
    assert notice["control_delta"]["phase"] == "ready_model"
    assert notice["control_delta"]["wait_for"] == []
    assert store.get(**a)["body"]["phase"] == "waiting"
    assert (
        wait_replan_notification(store, control_ref=a, last_fingerprint=notice["fingerprint"])
        is None
    )
