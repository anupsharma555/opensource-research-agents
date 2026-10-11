"""An accepted peer request leaves an exact, optional wait and review route."""

from __future__ import annotations

import json
import time

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.coordination.executor import WorkSpec
from ora.mission.observation_tools import record_wait_choice
from ora.state import StateStore
from tests.mission.test_observation_tools import fixture, peer_dependency
from tests.mission.test_service import answer, service, tool


def _drive_until(host, run_id: str, predicate) -> None:
    for _ in range(500):
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Peer wait handoff did not reach its saved boundary")


def _last_tool(request: dict) -> tuple[str, dict]:
    for message in reversed(request["messages"]):
        if message["role"] == "tool":
            return message["name"], json.loads(message["content"])
    raise AssertionError("Expected a saved tool turn in this model input")


def _disposition(request: dict) -> dict:
    index = json.loads(request["messages"][-1]["content"])
    return index["request_dispositions"][0]


def test_peer_handoff_does_not_claim_one_control_when_recipient_has_two_tasks(
    tmp_path, monkeypatch,
) -> None:
    def no_transport(_request, *, timeout):
        raise AssertionError("This handoff test must not call a provider")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="multi-control-handoff",
            question="Ask a synthetic peer question",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        requester = host._control(run.run_id, "agent-synthesis")
        recipient = host._control(run.run_id, "agent-critique")

        def add_second_task(tx):
            task = tx.put("board_proposal", "second-recipient-task", {
                "mission_id": requester["body"]["mission_id"], "status": "open",
                "objective": "Separate synthetic peer work",
            })
            extra = tx.put("mission_task_control", "second-recipient-control", {
                **recipient["body"], "task_id": task["id"],
                "proposal_ref": ref(task), "phase": "waiting",
            })
            return {"control_ref": ref(extra)}

        extra_ref = host.store.transact(
            "second-recipient-task", {}, add_second_task,
            mission_id=requester["body"]["mission_id"],
        )["control_ref"]
        spec = WorkSpec(
            work_id="multi-control-request-work",
            mission_id=requester["body"]["mission_id"], run_id=run.run_id,
            proposal_ref=requester["body"]["proposal_ref"],
            actor_id=requester["body"]["actor_id"], configuration_ref={},
            input_refs=(), handler_id="mission-tool", handler_version="1",
            kind="tool", lane="local",
        )
        result, output_refs = host._peer_request(spec, {
            "target_instance_id": recipient["body"]["actor_id"],
            "question": "Please check this synthetic claim independently.",
        })
        assert result["status"] == "request_saved"
        assert output_refs == (result["request_ref"],)
        assert "recipient_control_ref" not in result
        assert {item["control_ref"]["id"] for item in result["recipient_control_candidates"]} == {
            recipient["id"], extra_ref["id"],
        }
        assert "multiple saved tasks" in result["next_action"]
    finally:
        host.close()


def test_accepted_peer_request_can_wait_then_review_with_exact_current_refs(
    tmp_path, monkeypatch,
) -> None:
    host = None
    run = None
    turns = {"Sage": 0, "Mxra": 0}
    accepted = {}

    def transport(request, *, timeout):
        body = json.loads(request.data)
        role = "Sage" if body["messages"][0]["content"].startswith("You are Sage") else "Mxra"
        turns[role] += 1
        step = turns[role]
        if role == "Mxra":
            assert step == 1
            peer_request = host.store.records("mail_request")[0]
            return tool("publish_result", {
                "title": "Independent review",
                "text": "A bounded synthetic peer assessment.",
                "citations": [], "limitations": ["Synthetic fixture"],
                "answer_request_id": peer_request["id"],
            }, 20)
        if step == 1:
            assert "wait_for_dependencies" not in {
                item["function"]["name"] for item in body["tools"]
            }
            return tool("peer_request", {
                "target_instance_id": "agent-critique",
                "question": "Independently assess this synthetic claim.",
            }, 1)
        if step == 2:
            name, result = _last_tool(body)
            assert name == "peer_request" and result["status"] == "request_saved"
            accepted.update(result)
            request_ref = result["request_ref"]
            recipient_ref = result["recipient_control_ref"]
            assert request_ref == ref(host.store.get(**request_ref))
            assert host.store.get("mail_request", result["request_id"])["version"] >= (
                request_ref["version"]
            )
            assert recipient_ref == ref(host._control(run.run_id, "agent-critique"))
            assert "request_capability" in result["next_action"]
            assert "wait_for_dependencies" in result["next_action"]
            assert "review_peer_answer" in result["next_action"]
            assert _disposition(body)["status"] == "unanswered"
            return tool("request_capability", {"tool_id": "wait_for_dependencies"}, 2)
        if step == 3:
            assert "wait_for_dependencies" in {
                item["function"]["name"] for item in body["tools"]
            }
            current = _disposition(body)
            assert current["ref"]["id"] == accepted["request_ref"]["id"]
            assert current["status"] == "unanswered"
            assert current["recipient_control_ref"] == ref(
                host._control(run.run_id, "agent-critique")
            )
            # The peer advanced after the saved handoff. The wait handler
            # rechecks that same task identity and binds its current version.
            stale = accepted["recipient_control_ref"]
            return tool("wait_for_dependencies", {
                "dependencies": [{
                    "kind": "peer_request",
                    "request_id": current["ref"]["id"],
                    "request_version": current["ref"]["version"],
                    "target_control_id": stale["id"],
                    "target_control_version": stale["version"],
                }],
                "reason": "Await the independent review after other useful work.",
            }, 3)
        if step == 4:
            current = _disposition(body)
            assert current["status"] in {"awaiting_review", "partial_awaiting_review"}
            assert len(current["answer_refs"]) == 1
            assert "review_peer_answer" in {
                item["function"]["name"] for item in body["tools"]
            }
            return tool("review_peer_answer", {
                "request_id": current["ref"]["id"],
                "answer_id": current["answer_refs"][0]["id"],
                "disposition": "answered",
                "reason": "The saved independent assessment answers the request.",
            }, 4)
        return answer("The peer review was incorporated.", 5)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="peer-wait-handoff",
            question="Ask for an independent synthetic assessment",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        recipient = host._control(run.run_id, "agent-critique")
        host._update_control(
            recipient, phase="waiting", waiting_reason="Fixture holds peer work until wait choice",
        )
        _drive_until(host, run.run_id, lambda: (
            turns["Sage"] >= 2
            and "wait_for_dependencies" in host._control(
                run.run_id, "agent-synthesis"
            )["body"].get("requested_capabilities", [])
        ))
        recipient = host._control(run.run_id, "agent-critique")
        host._update_control(recipient, waiting_reason="Fixture advances target control version")
        assert ref(host._control(run.run_id, "agent-critique")) != accepted[
            "recipient_control_ref"
        ]
        _drive_until(host, run.run_id, lambda: (
            turns["Sage"] >= 3
            and host._control(run.run_id, "agent-synthesis")["body"].get("wait_for")
        ))
        requester = host._control(run.run_id, "agent-synthesis")
        assert requester["body"]["phase"] == "waiting"
        assert requester["body"]["wait_for"][0]["request_ref"]["id"] == accepted[
            "request_id"
        ]
        assert requester["body"]["wait_for"][0]["target_control_ref"] == ref(
            host._control(run.run_id, "agent-critique")
        )
        assert len(host.store.records("mission_wait_decision")) == 1
        recipient = host._control(run.run_id, "agent-critique")
        host._update_control(recipient, phase="ready_model", waiting_reason=None)
        host._drive_run(host._run(run.run_id))
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "waiting"
        assert turns["Sage"] == 3
        _drive_until(host, run.run_id, lambda: bool(
            host.store.get("mail_request", accepted["request_id"])["body"]["answer_refs"]
        ))
        _drive_until(host, run.run_id, lambda: (
            turns["Sage"] >= 4
            and any(
                item["body"].get("request_id") == accepted["request_id"]
                for item in host.store.records("mail_answer")
            )
            and host.store.get("mail_request", accepted["request_id"])["body"].get(
                "accepted_answer_ref"
            ) is not None
        ))
        reviewed = host.store.get("mail_request", accepted["request_id"])
        assert reviewed["body"]["accepted_answer_ref"] == reviewed["body"][
            "answer_refs"
        ][0]
        assert turns["Sage"] == 4
        assert turns["Mxra"] == 1
    finally:
        host.close()


def test_wait_rebind_is_idempotent_and_answer_race_returns_for_review(tmp_path) -> None:
    store = StateStore(tmp_path / "progress")
    saved = fixture(store)

    def progress(tx):
        target = tx.get(**saved["b"])
        return {"target_ref": ref(tx.put(
            "mission_task_control", target["id"],
            {**target["body"], "phase": "pending_model"},
            expected_version=target["version"],
        ))}

    current_target = store.transact(
        "peer-progress", {}, progress, mission_id="m",
    )["target_ref"]
    args = {
        "control_ref": saved["a"], "response_ref": saved["response"],
        "dependencies": [peer_dependency(saved)], "reason": "Await this peer answer",
    }
    result = record_wait_choice(store, "rebind-peer-wait", **args)
    assert result["status"] == "wait_decision_saved"
    assert result["control_delta"]["wait_for"][0]["target_control_ref"] == current_target
    assert store.get(**result["decision_ref"])["body"]["wait_for"] == result[
        "control_delta"
    ]["wait_for"]
    assert record_wait_choice(store, "rebind-peer-wait", **args) == result
    assert len(store.records("mission_wait_decision")) == 1

    answered = StateStore(tmp_path / "answered")
    saved = fixture(answered)

    def answer_before_wait(tx):
        request = tx.get(**saved["request"])
        return {"request_ref": ref(tx.put(
            "mail_request", request["id"],
            {**request["body"], "status": "awaiting_review"},
            expected_version=request["version"],
        ))}

    current_request = answered.transact(
        "answer-before-wait", {}, answer_before_wait, mission_id="m",
    )["request_ref"]
    raced = record_wait_choice(
        answered, "answered-before-wait", control_ref=saved["a"],
        response_ref=saved["response"], dependencies=[peer_dependency(saved)],
        reason="Await this peer answer",
    )
    assert raced["status"] == "dependency_available_for_review"
    assert raced["available"][0]["request_ref"] == current_request
    assert answered.records("mission_wait_decision") == []

    finished = StateStore(tmp_path / "finished")
    saved = fixture(finished)

    def finish_without_answer(tx):
        target = tx.get(**saved["b"])
        return {"target_ref": ref(tx.put(
            "mission_task_control", target["id"],
            {**target["body"], "phase": "done"},
            expected_version=target["version"],
        ))}

    finished.transact("peer-finished", {}, finish_without_answer, mission_id="m")
    terminal = record_wait_choice(
        finished, "finished-before-wait", control_ref=saved["a"],
        response_ref=saved["response"], dependencies=[peer_dependency(saved)],
        reason="Await this peer answer",
    )
    assert terminal["status"] == "dependency_available_for_review"
    assert terminal["available"][0]["phase"] == "done"
    assert finished.records("mission_wait_decision") == []
