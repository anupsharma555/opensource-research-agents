"""Fresh peer input replaces only model requests that have not reached a provider."""

from __future__ import annotations

import asyncio
import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.executor import WorkSpec
from ora.mission.projection import build_snapshot
from ora.state import StateConflict
from tests.mission.test_service import service


def _spec(host, run_id: str, alias: str, work_id: str) -> WorkSpec:
    body = host._control(run_id, alias)["body"]
    return WorkSpec(
        work_id=work_id, mission_id=body["mission_id"], run_id=run_id,
        proposal_ref=body["proposal_ref"], actor_id=body["actor_id"],
        configuration_ref={}, input_refs=(), handler_id="mission-tool",
        handler_version="1", kind="tool", lane="local",
    )


def _start_queued(host, request_id: str):
    run = host.start(LiveStartRequest(
        request_id=request_id, question="Review a synthetic audit finding.",
        evidence_text="Synthetic checked evidence about an audit finding.",
        agent_ids=["agent-synthesis", "agent-critique"],
        required_agent_ids=["agent-synthesis", "agent-critique"],
    ))
    sage = host._control(run.run_id, "agent-synthesis")
    assert host._schedule_model(sage)
    pending = host._control(run.run_id, "agent-synthesis")
    intent_id = f"model-intent:{pending['body']['call_ref']['id']}:1"
    intent = host.store.get("model_request_intent", intent_id)
    work = host._work_for(pending)
    spec = host.executor._spec_from(work)
    host.executor._prepare(pending["body"]["mission_id"])
    cap_id = host.executor._capacity_id(spec)
    assert host.store.get("capacity_operation", cap_id)["body"]["phase"] in {
        "queued", "waiting",
    }
    return run, pending, intent, work, spec, cap_id


def _new_incoming_request(host, run_id: str, question: str, *, purpose: str | None = None):
    arguments = {"target_instance_id": "agent-synthesis", "question": question}
    if purpose is not None:
        arguments["purpose"] = purpose
    result, _ = host._peer_request(_spec(host, run_id, "agent-critique", question), arguments)
    host._deliver_pending(host._control(run_id, "agent-synthesis")["body"]["mission_id"])
    return result


def test_queued_request_refreshes_for_new_inbound_question_without_model_call(
    tmp_path, monkeypatch,
) -> None:
    def no_transport(*_args, **_kwargs):
        raise AssertionError("Queued refresh must not send the old model request")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run, pending, intent, work, spec, cap_id = _start_queued(
            host, "queued-peer-question-refresh",
        )
        question = "Can you check the second audit site independently?"
        purpose = "A second-site audit may expose an unchecked comparator."
        sent = _new_incoming_request(host, run.run_id, question, purpose=purpose)
        message = host.store.get("mail_message", host.store.get(
            "mail_request", sent["request_id"],
        )["body"]["message_ref"]["id"])
        assert message["body"]["payload"] == {"question": question}
        assert host._supersede_queued_peer_input(pending)
        revised = host._control(run.run_id, "agent-synthesis")
        assert revised["body"]["phase"] == "ready_model"
        assert revised["body"]["work_id"] is None
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "superseded"
        assert host.store.get("agent_attempt", spec.attempt_id)["body"]["status"] == "superseded"
        assert host.store.get("capacity_operation", cap_id)["body"]["phase"] == "canceled"
        assert host.store.get("model_request_intent", intent["id"]) == intent
        assert host.store.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}") is None
        assert host._supersede_queued_peer_input(revised) is False

        assert host._schedule_model(revised)
        successor = host._control(run.run_id, "agent-synthesis")["body"]["call_ref"]
        assert successor != pending["body"]["call_ref"]
        new_intent = host.store.get("model_request_intent", f"model-intent:{successor['id']}:1")
        packet = json.loads(host.store.read_blob(new_intent["body"]["blob"]["sha256"]))
        incoming = json.loads(packet["messages"][-1]["content"])["incoming_request"]
        assert incoming["ref"]["id"] == sent["request_id"]
        assert incoming["question"] == question
        assert purpose not in json.dumps(packet)
        assert new_intent["body"]["request_token_estimate"]["tokens"] > 0
    finally:
        host.close()


def test_outgoing_delivery_is_not_a_new_inbound_question(tmp_path, monkeypatch) -> None:
    def no_transport(*_args, **_kwargs):
        raise AssertionError("This test has no model transport")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run, pending, intent, work, _spec_model, cap_id = _start_queued(
            host, "queued-own-outgoing-ignore",
        )
        host._peer_request(_spec(host, run.run_id, "agent-synthesis", "own-outgoing"), {
            "target_instance_id": "agent-critique",
            "question": "Check the original synthetic finding.",
        })
        host._deliver_pending(pending["body"]["mission_id"])
        assert host._supersede_queued_peer_input(pending) is False
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "pending"
        assert host.store.get("capacity_operation", cap_id)["body"]["phase"] in {
            "queued", "waiting",
        }
        assert host.store.get("model_request_intent", intent["id"]) == intent
    finally:
        host.close()


def test_queued_peer_answer_refreshes_requester_without_replaying_old_call(
    tmp_path, monkeypatch,
) -> None:
    def no_transport(*_args, **_kwargs):
        raise AssertionError("A queued answer refresh must not send the old request")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run, pending, intent, work, _spec_model, cap_id = _start_queued(
            host, "queued-peer-answer-refresh",
        )
        sent, _ = host._peer_request(
            _spec(host, run.run_id, "agent-synthesis", "outgoing-before-answer"),
            {"target_instance_id": "agent-critique",
             "question": "Check this synthetic audit finding."},
        )
        host._deliver_pending(pending["body"]["mission_id"])
        report = host.store.transact(
            "synthetic-peer-answer-artifact", {},
            lambda tx: tx.put("report", "synthetic-peer-answer-artifact", {
                "mission_id": pending["body"]["mission_id"],
                "text": "The synthetic finding needs a second-site check.",
            }),
            mission_id=pending["body"]["mission_id"],
        )
        answered = host.mailbox.record_answer(
            "synthetic-peer-answer", mission_id=pending["body"]["mission_id"],
            request_id=sent["request_id"], answer_id="synthetic-peer-answer",
            responder_id=host._control(run.run_id, "agent-critique")["body"]["actor_id"],
            artifact_ref={key: report[key] for key in ("kind", "id", "version")},
        )
        assert answered["status"] == "accepted", answered
        assert host._supersede_queued_peer_input(pending)
        assert host.store.get("capacity_operation", cap_id)["body"]["phase"] == "canceled"
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "superseded"
        assert host.store.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}") is None
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        successor = host._control(run.run_id, "agent-synthesis")["body"]["call_ref"]
        new_intent = host.store.get("model_request_intent", f"model-intent:{successor['id']}:1")
        packet = json.loads(host.store.read_blob(new_intent["body"]["blob"]["sha256"]))
        assert answered["answer_ref"]["id"] in json.dumps(packet["messages"])
    finally:
        host.close()


def test_existing_dispatch_marker_prevents_queued_replacement(tmp_path, monkeypatch) -> None:
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run, pending, intent, work, _spec_model, cap_id = _start_queued(
            host, "queued-marker-is-not-replaced",
        )
        host.store.transact(
            "synthetic-existing-model-marker", {},
            lambda tx: tx.put(
                "model_dispatch_attempt", f"model-dispatch:{intent['id']}",
                {"mission_id": pending["body"]["mission_id"],
                 "intent_ref": {key: intent[key] for key in ("kind", "id", "version")}},
            ),
            mission_id=pending["body"]["mission_id"],
        )
        _new_incoming_request(
            host, run.run_id, "Assess the finding after the saved dispatch marker.",
        )
        assert host._supersede_queued_peer_input(pending) is False
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "pending"
        assert host.store.get("capacity_operation", cap_id)["body"]["phase"] in {
            "queued", "waiting",
        }
    finally:
        host.close()


def test_claimed_pre_dispatch_change_settles_zero_and_refreshes_after_restart(
    tmp_path, monkeypatch,
) -> None:
    calls = []

    def no_transport(request, *, timeout):
        calls.append(json.loads(request.data))
        raise AssertionError("Stale claimed input must not reach the model transport")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run, pending, intent, work, spec, cap_id = _start_queued(
            host, "claimed-peer-question-refresh",
        )
        original = host.session.invoke_capture
        question = "Please assess the revised audit claim."
        sent = {}

        def deliver_after_claim(intent_ref, **kwargs):
            sent.update(_new_incoming_request(host, run.run_id, question))
            return original(intent_ref, **kwargs)

        monkeypatch.setattr(host.session, "invoke_capture", deliver_after_claim)
        asyncio.run(host.executor.run_ready(pending["body"]["mission_id"]))
        cap = host.store.get("capacity_operation", cap_id)["body"]
        assert cap["phase"] == "failed"
        assert cap["attempts"][-1]["status"] == "terminal_no_effect"
        assert cap["attempts"][-1]["charged_tokens"] == 0
        assert cap["attempts"][-1]["usage_known"] is True
        assert host.store.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}") is None
        assert host.store.get("model_response_receipt", f"model-response:{intent['id']}") is None
        assert not calls
    finally:
        host.close()

    resumed = service(tmp_path, monkeypatch, no_transport)
    try:
        current = resumed._control(run.run_id, "agent-synthesis")
        assert resumed._supersede_claimed_peer_input(current)
        assert resumed.store.get("executor_work", work["id"])["body"]["phase"] == "superseded"
        assert resumed.store.get("agent_attempt", spec.attempt_id)["body"]["status"] == "superseded"
        assert resumed._schedule_model(resumed._control(run.run_id, "agent-synthesis"))
        successor = resumed._control(run.run_id, "agent-synthesis")["body"]["call_ref"]
        assert successor != pending["body"]["call_ref"]
        next_intent = resumed.store.get("model_request_intent", f"model-intent:{successor['id']}:1")
        packet = json.loads(resumed.store.read_blob(next_intent["body"]["blob"]["sha256"]))
        assert sent["request_id"] in json.dumps(packet["messages"])
        assert question in json.dumps(packet["messages"])
        assert not calls
    finally:
        resumed.close()


@pytest.mark.parametrize("marker_present,cancel_requested", [
    (False, False), (True, False), (False, True),
])
def test_crash_after_stale_diagnostic_recovers_only_without_provider_marker(
    tmp_path, monkeypatch, marker_present: bool, cancel_requested: bool,
) -> None:
    def no_transport(*_args, **_kwargs):
        raise AssertionError("A pre-dispatch stale stop cannot make an HTTP call")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run, pending, intent, work, spec, cap_id = _start_queued(
            host, f"crashed-peer-input-{marker_present}-{cancel_requested}",
        )
        owner = host.executor.owner
        admitted = host.capacity.admit(
            owner, "model", mission_id=pending["body"]["mission_id"],
            eligible_operation_ids=frozenset({cap_id}),
        )
        assert admitted and admitted["operation_id"] == cap_id
        generation = admitted["generation"]
        host.capacity.dispatched(cap_id, owner, generation)
        question = "Assess new peer evidence before the saved call goes out."
        sent = _new_incoming_request(host, run.run_id, question)
        stopped = host._model_work(spec)
        assert stopped.disposition == "terminal_no_effect"
        diagnostic = next(
            item for item in host.store.records("mission_host_diagnostic")
            if item["body"].get("work_id") == spec.work_id
            and item["body"].get("status") == "peer_input_changed_before_dispatch"
        )
        assert diagnostic["body"]["intent_ref"] == {
            key: intent[key] for key in ("kind", "id", "version")
        }
        # A known-zero stale-input stop is a normal refresh notice, not a
        # host-error record. Project it before recovery, including older records
        # that have no error_class/message/recorded_at fields.
        snapshot, *_ = build_snapshot(host.store, host._run(run.run_id),
                                      host._controls(run.run_id), snapshot_id="stale-input-view")
        visible = next(item for item in snapshot["diagnostics"] if item["id"] == diagnostic["id"])
        assert visible["kind"] == "peer_input_refresh"
        assert visible["status"] == "stopped_before_dispatch"
        # Simulate process death before the executor consumes the known-zero result.
        host.capacity.timeout(cap_id, owner, generation)
        if cancel_requested:
            host.capacity.cancel(cap_id)
        if marker_present:
            host.store.transact(
                "synthetic-crash-marker", {},
                lambda tx: tx.put(
                    "model_dispatch_attempt", f"model-dispatch:{intent['id']}",
                    {"mission_id": pending["body"]["mission_id"],
                     "intent_ref": {key: intent[key] for key in ("kind", "id", "version")}},
                ),
                mission_id=pending["body"]["mission_id"],
            )
    finally:
        host.close()

    resumed = service(tmp_path, monkeypatch, no_transport)
    try:
        current = resumed._control(run.run_id, "agent-synthesis")
        may_recover = not marker_present and not cancel_requested
        assert resumed._supersede_claimed_peer_input(current) is may_recover
        capacity = resumed.store.get("capacity_operation", cap_id)["body"]
        if not may_recover:
            assert capacity["phase"] == "uncertain"
            assert resumed.store.get("executor_work", work["id"])["body"]["phase"] == "pending"
        else:
            assert capacity["phase"] == "failed"
            assert capacity["attempts"][-1]["status"] == "terminal_no_effect"
            assert capacity["attempts"][-1]["charged_tokens"] == 0
            assert capacity["attempts"][-1]["usage_known"] is True
            with pytest.raises(StateConflict):
                resumed.capacity.transport_failure(
                    cap_id, owner, generation, classification="terminal_no_effect",
                )
            assert resumed._schedule_model(resumed._control(run.run_id, "agent-synthesis"))
            successor = resumed._control(run.run_id, "agent-synthesis")["body"]["call_ref"]
            next_intent = resumed.store.get(
                "model_request_intent", f"model-intent:{successor['id']}:1",
            )
            packet = json.loads(resumed.store.read_blob(next_intent["body"]["blob"]["sha256"]))
            assert sent["request_id"] in json.dumps(packet["messages"])
            assert question in json.dumps(packet["messages"])
    finally:
        resumed.close()
