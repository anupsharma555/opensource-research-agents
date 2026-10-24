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
from tests.mission.test_service import answer, serialized_peer_name, service, tool


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
    reviewed_input = {}
    source = {}

    def transport(request, *, timeout):
        body = json.loads(request.data)
        role = serialized_peer_name(body)
        turns[role] += 1
        step = turns[role]
        if role == "Mxra":
            assert step == 1
            peer_request = host.store.records("mail_request")[0]
            assert source["ref"] in host._control(run.run_id, "agent-critique")["body"][
                "source_refs"
            ]
            assert "Synthetic source: the comparison has an unresolved limitation." in json.dumps(
                body["messages"]
            )
            context = json.loads(body["messages"][-1]["content"])
            assert context["request_dispositions"][0]["ref"]["id"] == peer_request["id"]
            incoming = context["incoming_request"]
            assert incoming["ref"] == ref(peer_request)
            assert incoming["recipient_id"] == peer_request["body"]["recipient_id"]
            assert incoming["question"] == "Independently assess this synthetic claim."
            return tool("publish_result", {
                "title": "Independent review",
                "text": "The supplied comparison has an unresolved limitation.",
                "citations": [source["citation"]],
                "limitations": ["Synthetic fixture"],
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
            answer_ref = current["answer_refs"][0]
            peer_answer = host.store.get(**answer_ref)
            peer_report = host.store.get(**peer_answer["body"]["artifact_ref"])
            assert peer_answer["body"]["request_id"] == accepted["request_id"]
            assert peer_report["body"]["body"] == (
                "The supplied comparison has an unresolved limitation."
            )
            assert peer_report["body"]["citations"] == [
                {"sourceId": source["citation"]["source_id"],
                 "spanId": source["citation"]["span_id"]}
            ]
            assert peer_report["body"]["citation_input_provenance"][0][
                "input_status"
            ] == "exact_passage_in_producing_call"
            assert peer_report["body"]["producer_attempt_ref"]["kind"] == "agent_attempt"
            context = json.loads(body["messages"][-1]["content"])
            assert answer_ref in context["request_answer_refs"]
            preview = context["peer_answer_previews"][0]
            assert preview["request_ref"]["id"] == accepted["request_id"]
            assert preview["answer_ref"] == answer_ref
            assert preview["report_ref"] == ref(peer_report)
            assert preview["report"] == {
                key: peer_report["body"][key]
                for key in ("title", "body", "citations", "limitations")
            }
            assert preview["authority"] == "model_authored_peer_data_not_verified_evidence"
            assert preview["question"] == "Independently assess this synthetic claim."
            assert not any(
                detail["type"] in {"request", "request_answer"}
                and detail["ref"] in (preview["request_ref"], preview["answer_ref"])
                for detail in context["details"]
            )
            shared = context["shared_materials"]
            peer_material = next(
                entry for entry in shared["entries"]
                if entry["ref"] == ref(peer_report)
            )
            read_request = peer_material["read_request"]
            assert read_request["tool"] == "read_context_record"
            assert "read_context_record" in {
                item["function"]["name"] for item in body["tools"]
            }
            requester = host._control(run.run_id, "agent-synthesis")
            call_ref = requester["body"]["call_ref"]
            inclusion = host.store.get(
                "context_inclusion",
                f"context:model-intent:{call_ref['id']}:{call_ref['version']}",
            )
            assert inclusion is not None
            assert answer_ref in inclusion["body"]["included_refs"]
            reviewed_input.update(answer_ref=answer_ref, report_ref=ref(peer_report))
            return tool("review_peer_answer", {
                "request_id": current["ref"]["id"],
                "answer_id": answer_ref["id"],
                "disposition": "answered",
                "reason": "The complete saved peer assessment answers the request.",
            }, 4)
        if step == 5:
            name, result = _last_tool(body)
            assert name == "review_peer_answer" and result["status"] == "review_saved"
            current = _disposition(body)
            assert current["status"] == "answered"
            assert current["accepted_answer_ref"] == reviewed_input["answer_ref"]
            return answer(
                "The peer confirmed the source's unresolved limitation "
                f"【{source['ref']['id']}::{source['citation']['span_id']}】; "
                "the fixture limits any broader conclusion.", 5,
            )
        raise AssertionError("Unexpected requester turn after peer review")

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="peer-wait-handoff",
            question="Ask for an independent synthetic assessment",
            evidence_text="Synthetic source: the comparison has an unresolved limitation.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        source_copy = host.store.get(**host._control(run.run_id, "agent-synthesis")[
            "body"
        ]["source_refs"][0])
        passage = host.store.get("source_passage", source_copy["body"]["passage_ids"][0])
        source.update(ref=ref(source_copy), citation={
            "source_id": source_copy["id"], "span_id": passage["body"]["span_id"],
        })
        recipient = host._control(run.run_id, "agent-critique")
        host._update_control(
            recipient, phase="retry_at", retry_at=time.time() + 3600,
            waiting_reason="Fixture holds peer work until wait choice",
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
        host._update_control(recipient, phase="ready_model", waiting_reason=None, retry_at=None)
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
        assert reviewed_input["answer_ref"] == reviewed["body"]["accepted_answer_ref"]
        _drive_until(host, run.run_id, lambda: host._run(run.run_id)["body"]["status"] == "completed")
        requester = host._control(run.run_id, "agent-synthesis")
        report_ref = requester["body"]["report_ref"]
        assert requester["body"]["phase"] == "done"
        report = host.store.get(**report_ref)
        assert report["body"]["body"].startswith(
            "The peer confirmed the source's unresolved limitation"
        )
        assert report["body"]["citations"] == [{
            "sourceId": source["citation"]["source_id"],
            "spanId": source["citation"]["span_id"],
        }]
        assert report["body"]["citation_input_provenance"][0]["input_status"] == (
            "exact_passage_in_producing_call"
        )
        proposal = host.store.get("board_proposal", requester["body"]["task_id"])
        assert proposal["body"]["status"] == "completed"
        outcome = host.store.get(**proposal["body"]["outcome_ref"])
        assert report_ref in outcome["body"]["result_refs"]
        assert reviewed_input["report_ref"] != report_ref
        assert turns["Sage"] == 5
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
