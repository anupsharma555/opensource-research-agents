"""Recipient-authored request decline through the offline mission loop."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from ora.agents.capabilities import CapabilityError, validate_arguments
from ora.contracts.models import LiveBranchCancelRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.mission.tool_catalog import TOOL_SCHEMAS
from tests.mission.test_service import service, tool


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("socket connection")
        raise AssertionError("Offline peer decline attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    yield
    assert attempts == []


def _role(body: dict) -> str:
    identities = [part for part in body["messages"][0]["content"].split("\n\n")
                  if part.startswith("You are ")]
    assert len(identities) == 1
    return next(name for name in ("Sage", "Mxra", "Remy")
                if identities[0].startswith(f"You are {name},"))


def _last_tool(body: dict) -> tuple[str, dict]:
    for message in reversed(body["messages"]):
        if message["role"] == "tool":
            return message["name"], json.loads(message["content"])
    return "", {}


def _drive_until(host, run_id: str, predicate) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"Peer decline did not reach the saved checkpoint: "
                         f"{[(item['body']['alias'], item['body']['phase'], item['body'].get('waiting_reason')) for item in host._controls(run_id)]}")


def test_model_decline_wakes_done_requester_and_preserves_independent_report(
    tmp_path, monkeypatch,
) -> None:
    host = None
    run_id = None
    turns = {"Sage": 0, "Mxra": 0, "Remy": 0}
    request_id = None
    seen = {"decline_in_request": False}
    reason = "The synthetic comparison lacks an independent critique source."

    def transport(request, *, timeout):
        nonlocal request_id
        body = json.loads(request.data)
        role = _role(body)
        turns[role] += 1
        step = turns[role]
        attached = {item["function"]["name"] for item in body["tools"]}
        if role == "Remy":
            assert step == 1
            return tool("publish_result", {
                "title": "Independent synthetic data check",
                "text": "The synthetic data peer completed a bounded check.",
                "citations": [], "limitations": ["Synthetic input only"],
            }, 100)
        if role == "Mxra":
            context = json.loads(body["messages"][-1]["content"])
            incoming = context["incoming_request"]
            assert incoming["ref"]["id"] == request_id
            if step == 1:
                assert "decline_peer_request" not in attached
                catalog = body["messages"][0]["content"]
                assert "decline_peer_request" in catalog
                return tool("request_capability", {
                    "tool_id": "decline_peer_request",
                }, 200)
            if step == 2:
                assert "decline_peer_request" in attached
                schema = next(item["function"]["parameters"] for item in body["tools"]
                              if item["function"]["name"] == "decline_peer_request")
                assert schema["required"] == ["request_id", "request_version", "reason"]
                assert host.store.get("mail_request", request_id)["version"] == (
                    incoming["ref"]["version"]
                )
                return tool("decline_peer_request", {
                    "request_id": request_id,
                    "request_version": incoming["ref"]["version"],
                    "reason": reason,
                }, 201)
            assert step == 3
            name, result = _last_tool(body)
            assert name == "decline_peer_request"
            assert result["status"] == "decline_saved"
            assert result["request_ref"]["id"] == request_id
            return tool("publish_result", {
                "title": "Scoped synthetic critique",
                "text": "I could not provide the requested independent critique.",
                "citations": [], "limitations": [reason],
            }, 202)

        if step == 1:
            assert "peer_request" in attached
            return tool("peer_request", {
                "target_instance_id": "agent-critique",
                "question": "Check the synthetic comparison independently.",
            }, 1)
        if step == 2:
            name, result = _last_tool(body)
            assert name == "peer_request" and result["status"] == "request_saved"
            request_id = result["request_id"]
            return tool("publish_result", {
                "title": "Provisional synthetic synthesis",
                "text": "The comparison remains provisional pending peer critique.",
                "citations": [], "limitations": ["Peer critique is pending"],
            }, 2)
        assert step == 3
        context = json.loads(body["messages"][-1]["content"])
        disposition = next(item for item in context["request_dispositions"]
                           if item["ref"]["id"] == request_id)
        assert disposition["status"] == "declined"
        assert disposition["answer_refs"] == []
        assert disposition["accepted_answer_ref"] is None
        assert disposition["peer_decline"] == {
            "recipient_id": host._control(run_id, "agent-critique")["body"]["actor_id"],
            "reason": reason,
            "authority": "recipient_authored_peer_data_not_verified_evidence",
        }
        assert host._control(run_id, "agent-data-management")["body"]["phase"] == "done"
        seen["decline_in_request"] = True
        return tool("publish_result", {
            "title": "Qualified synthetic synthesis",
            "text": "The data check was retained; the critique peer declined.",
            "citations": [], "limitations": [reason],
            "scope_completion": {
                "reason": "Record the declined optional critique and retain the supported data check.",
                "decisions": [],
            },
        }, 3)

    host = service(tmp_path, monkeypatch, transport)
    try:
        aliases = ["agent-synthesis", "agent-critique", "agent-data-management"]
        run = host.start(LiveStartRequest(
            request_id="model-directed-request-decline",
            question="Compare two synthetic research checks.",
            evidence_text="Synthetic source for a bounded comparison.",
            agent_ids=aliases,
            required_agent_ids=["agent-synthesis", "agent-data-management"],
        ))
        run_id = run.run_id
        for alias in aliases[1:]:
            host._update_control(host._control(run_id, alias), phase="retry_at",
                                 waiting_reason="Fixture holds peer until requester is done",
                                 retry_at=time.time() + 3600)
        _drive_until(host, run_id, lambda: (
            host._control(run_id, "agent-synthesis")["body"]["phase"] == "done"
            and request_id is not None
        ))
        provisional = host._control(run_id, "agent-synthesis")["body"]["report_ref"]
        assert provisional and host.store.get(**provisional)
        assert host.store.get("mail_request", request_id)["body"]["status"] == "unanswered"
        assert host._run(run_id)["body"]["status"] == "active"

        host._update_control(host._control(run_id, "agent-data-management"),
                             phase="ready_model", waiting_reason=None, retry_at=None)
        _drive_until(host, run_id, lambda: host._control(
            run_id, "agent-data-management"
        )["body"]["phase"] == "done")
        data_report = host._control(run_id, "agent-data-management")["body"]["report_ref"]
        assert data_report and host.store.get(**data_report)
        assert host._run(run_id)["body"]["status"] == "active"
        assert host._control(run_id, "agent-synthesis")["body"]["report_ref"] == provisional

        host._update_control(host._control(run_id, "agent-critique"),
                             phase="ready_model", waiting_reason=None, retry_at=None)
        _drive_until(host, run_id, lambda: host._run(run_id)["body"]["status"] == "completed")
        assert seen["decline_in_request"] is True
        assert turns == {"Sage": 3, "Mxra": 3, "Remy": 1}
        declined = host.store.get("mail_request", request_id)
        assert declined["body"]["status"] == "declined"
        assert declined["body"]["requester_wake_ref"]
        assert declined["body"]["reason"] == reason
        assert declined["body"]["answer_refs"] == []
        assert declined["body"]["accepted_answer_ref"] is None
        assert len(host.store.records("mail_answer")) == 0
        mission_id = host._run(run_id)["body"]["mission_id"]
        assert len([event for event in host.store.events(mission_id)
                    if event["type"] == "peer_request_declined_wake"]) == 1
        assert host._control(run_id, "agent-data-management")["body"]["report_ref"] == data_report
        assert host._control(run_id, "agent-synthesis")["body"]["report_ref"] != provisional
        assert host.store.get(**provisional)["body"]["body"].startswith("The comparison")
    finally:
        host.close()


def test_decline_version_owner_mission_and_replay_are_transactional(tmp_path, monkeypatch) -> None:
    schema = TOOL_SCHEMAS["decline_peer_request"]
    for invalid_version in (True, 0):
        with pytest.raises(CapabilityError):
            validate_arguments(schema, {
                "request_id": "synthetic-request",
                "request_version": invalid_version,
                "reason": "Cannot complete this synthetic check",
            })

    def unexpected(_request, *, timeout):
        raise AssertionError("This transaction case must not call a model")

    host = service(tmp_path, monkeypatch, unexpected)
    try:
        run = host.start(LiveStartRequest(
            request_id="decline-boundaries",
            question="Check a synthetic peer request.",
            agent_ids=["agent-synthesis", "agent-critique", "agent-data-management"],
        ))
        controls = {alias: host._control(run.run_id, alias) for alias in
                    ("agent-synthesis", "agent-critique", "agent-data-management")}
        mission_id = controls["agent-synthesis"]["body"]["mission_id"]
        sent = host.mailbox.send_request(
            "decline-boundary-send", mission_id=mission_id,
            request_id="decline-boundary-request", message_id="decline-boundary-message",
            requester_id=controls["agent-synthesis"]["body"]["actor_id"],
            recipient_id=controls["agent-critique"]["body"]["actor_id"],
            question="Check synthetic evidence.", expected_artifact="A bounded note",
            cause_ref=controls["agent-synthesis"]["body"]["proposal_ref"],
            payload={"synthetic": True},
        )
        assert sent["status"] == "accepted"
        original = host.store.get("mail_request", "decline-boundary-request")
        host.mailbox.mark_delivered(
            "decline-boundary-deliver", mission_id=mission_id,
            message_id="decline-boundary-message",
        )
        current = host.store.get("mail_request", original["id"])
        assert current["version"] > original["version"]
        common = dict(mission_id=mission_id, request_id=original["id"],
                      recipient_id=controls["agent-critique"]["body"]["actor_id"],
                      reason="No independent source available")
        assert host.mailbox.decline("decline-stale", **common,
                                    expected_version=original["version"])["status"] == "rejected"
        assert host.mailbox.decline("decline-wrong-actor", **{
            **common, "recipient_id": controls["agent-data-management"]["body"]["actor_id"],
        }, expected_version=current["version"])["status"] == "rejected"
        assert host.mailbox.decline("decline-cross-mission", **{
            **common, "mission_id": "different-synthetic-mission",
        }, expected_version=current["version"])["status"] == "rejected"
        assert host.mailbox.decline("decline-bool-version", **common,
                                    expected_version=True)["status"] == "rejected"
        assert host.mailbox.decline("decline-zero-version", **common,
                                    expected_version=0)["status"] == "rejected"
        assert host.store.get("mail_request", original["id"]) == current
        accepted = host.mailbox.decline("decline-exact", **common,
                                        expected_version=current["version"])
        assert accepted["status"] == "accepted"
        assert host.store.get(**accepted["request_ref"])["body"]["status"] == "declined"

        legacy_opened = host.mailbox.send_request(
            "legacy-decline-send", mission_id=mission_id,
            request_id="legacy-decline-request", message_id="legacy-decline-message",
            requester_id=controls["agent-synthesis"]["body"]["actor_id"],
            recipient_id=controls["agent-critique"]["body"]["actor_id"],
            question="Can you inspect this separate synthetic check?",
            expected_artifact="A bounded note",
            cause_ref=controls["agent-synthesis"]["body"]["proposal_ref"],
            payload={"synthetic": True},
        )
        assert legacy_opened["status"] == "accepted"
        legacy_args = {**common, "request_id": "legacy-decline-request"}
        legacy_accepted = host.mailbox.decline("legacy-decline", **legacy_args)
        assert legacy_accepted["status"] == "accepted"

        for partial in (False, True):
            label = "partial" if partial else "full"
            pending_id = f"review-pending-{label}"
            opened = host.mailbox.send_request(
                f"open-{label}", mission_id=mission_id, request_id=pending_id,
                message_id=f"message-{label}",
                requester_id=controls["agent-synthesis"]["body"]["actor_id"],
                recipient_id=controls["agent-critique"]["body"]["actor_id"],
                question="Check another synthetic point.", expected_artifact="A bounded note",
                cause_ref=controls["agent-synthesis"]["body"]["proposal_ref"],
                payload={"synthetic": True},
            )
            assert opened["status"] == "accepted"
            artifact = host.store.transact(
                f"artifact-{label}", {},
                lambda tx: ref(tx.put("artifact", f"artifact-{label}", {
                    "mission_id": mission_id, "synthetic": True,
                })), mission_id=mission_id,
            )
            answered = host.mailbox.record_answer(
                f"answer-{label}", mission_id=mission_id, request_id=pending_id,
                answer_id=f"answer-{label}",
                responder_id=controls["agent-critique"]["body"]["actor_id"],
                artifact_ref=artifact, partial=partial,
            )
            assert answered["status"] == "accepted"
            pending_review = host.store.get("mail_request", pending_id)
            assert pending_review["body"]["status"] == (
                "partial_awaiting_review" if partial else "awaiting_review"
            )
            blocked = host.mailbox.decline(
                f"decline-pending-{label}", mission_id=mission_id,
                request_id=pending_id,
                recipient_id=controls["agent-critique"]["body"]["actor_id"],
                reason="Do not replace an unreviewed answer",
                expected_version=pending_review["version"],
            )
            assert blocked["status"] == "rejected"
            assert host.store.get("mail_request", pending_id) == pending_review

        cancellation = host.mailbox.send_request(
            "open-before-cancel", mission_id=mission_id,
            request_id="request-before-cancel", message_id="message-before-cancel",
            requester_id=controls["agent-synthesis"]["body"]["actor_id"],
            recipient_id=controls["agent-critique"]["body"]["actor_id"],
            question="Check a synthetic point before cancellation.",
            expected_artifact="A bounded note",
            cause_ref=controls["agent-synthesis"]["body"]["proposal_ref"],
            payload={"synthetic": True},
        )
        assert cancellation["status"] == "accepted"
        target = controls["agent-critique"]
        task = host.store.get("board_proposal", target["body"]["task_id"])
        receipt = host.cancel_branch(LiveBranchCancelRequest(
            request_id="cancel-recipient-after-request", run_id=run.run_id,
            task_id=task["id"], expected_task_version=task["version"],
            reason="Stop the exact synthetic recipient task.",
        ))
        assert receipt.status == "cancelled"
        cancelled_request = host.store.get("mail_request", "request-before-cancel")
        assert cancelled_request["body"]["status"] == "recipient_unavailable_due_branch_cancel"
        assert host.mailbox.decline(
            "decline-after-cancel", mission_id=mission_id,
            request_id="request-before-cancel",
            recipient_id=target["body"]["actor_id"], reason="Too late to decline",
            expected_version=cancellation["request_ref"]["version"],
        )["status"] == "rejected"
        assert host.store.get("mail_request", "request-before-cancel") == cancelled_request
    finally:
        host.close()

    reopened = service(tmp_path, monkeypatch, unexpected)
    try:
        assert reopened.mailbox.decline("decline-exact", **common,
                                        expected_version=current["version"]) == accepted
        assert reopened.mailbox.decline("legacy-decline", **legacy_args) == legacy_accepted
        assert reopened.mailbox.decline("decline-after-final", **common,
                                        expected_version=accepted["request_ref"]["version"])["status"] == "rejected"
        assert len([event for event in reopened.store.events(mission_id)
                    if event["type"] == "peer_request_declined"]) == 2
    finally:
        reopened.close()


def test_decline_during_inflight_requester_call_waits_for_fresh_input(tmp_path, monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    turns = {"Sage": 0, "Mxra": 0}
    captured = {}
    request_id = None
    host = None
    run_id = None
    reason = "No independent synthetic source is available."

    def transport(request, *, timeout):
        nonlocal request_id
        body = json.loads(request.data)
        role = _role(body)
        turns[role] += 1
        step = turns[role]
        if role == "Sage":
            if step == 1:
                return tool("peer_request", {
                    "target_instance_id": "agent-critique",
                    "question": "Check the synthetic comparison independently.",
                }, 1)
            if step == 2:
                name, result = _last_tool(body)
                assert name == "peer_request" and result["status"] == "request_saved"
                request_id = result["request_id"]
                captured["old_input"] = body
                entered.set()
                assert release.wait(8), "Held requester call was not released"
                return tool("publish_result", {
                    "title": "Provisional synthetic note",
                    "text": "This provisional note predates the peer decline.",
                    "citations": [], "limitations": ["Peer status was pending"],
                }, 2)
            assert step == 3
            index = json.loads(body["messages"][-1]["content"])
            declined = next(item for item in index["request_dispositions"]
                            if item["ref"]["id"] == request_id)
            assert declined["status"] == "declined"
            assert declined["peer_decline"]["reason"] == reason
            captured["fresh_input"] = body
            return tool("publish_result", {
                "title": "Final synthetic note",
                "text": "The provisional note remains; the peer declined the requested check.",
                "citations": [], "limitations": [reason],
            }, 3)

        if step in {1, 2}:
            incoming = json.loads(body["messages"][-1]["content"])["incoming_request"]
            assert incoming["ref"]["id"] == request_id
        if step == 1:
            return tool("request_capability", {"tool_id": "decline_peer_request"}, 100)
        if step == 2:
            assert "decline_peer_request" in {item["function"]["name"]
                                              for item in body["tools"]}
            return tool("decline_peer_request", {
                "request_id": request_id,
                "request_version": incoming["ref"]["version"],
                "reason": reason,
            }, 101)
        assert step == 3
        return tool("publish_result", {
            "title": "Independent synthetic note",
            "text": "I declined the requested check due to missing independent material.",
            "citations": [], "limitations": [reason],
        }, 102)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="decline-while-requester-inflight",
            question="Review a synthetic comparison.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        run_id = run.run_id
        host._update_control(host._control(run_id, "agent-critique"),
                             phase="retry_at", waiting_reason="Fixture holds recipient",
                             retry_at=time.time() + 3600)
        _drive_until(host, run_id, entered.is_set)
        requester = host._control(run_id, "agent-synthesis")
        assert requester["body"]["phase"] == "pending_model"
        old_call_ref = requester["body"]["call_ref"]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{old_call_ref['id']}:{old_call_ref['version']}"
        )
        assert json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"])) == (
            captured["old_input"]
        )
        host._update_control(host._control(run_id, "agent-critique"),
                             phase="ready_model", waiting_reason=None, retry_at=None)
        _drive_until(host, run_id, lambda: host.store.get(
            "mail_request", request_id
        )["body"]["status"] == "declined")
        assert not release.is_set()
        assert host._control(run_id, "agent-synthesis")["body"]["phase"] == "pending_model"
        assert host.store.get("mail_request", request_id)["body"].get("requester_wake_ref") is None
        assert host._run(run_id)["body"]["status"] == "active"
        release.set()
        _drive_until(host, run_id, lambda: host._run(run_id)["body"]["status"] == "completed")
        assert turns == {"Sage": 3, "Mxra": 3}
        assert captured["fresh_input"] != captured["old_input"]
        assert json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"])) == (
            captured["old_input"]
        )
        provisional = next(item for item in host.store.records("report")
                           if item["body"]["body"] == "This provisional note predates the peer decline.")
        assert provisional["body"]["title"] == "Provisional synthetic note"
        assert host.store.get("mail_request", request_id)["body"]["requester_wake_ref"]
        assert len([event for event in host.store.events(host._run(run_id)["body"]["mission_id"])
                    if event["type"] == "peer_request_declined_wake"]) == 1
    finally:
        release.set()
        host.close()


def test_decline_wakes_only_the_requesters_originating_task(tmp_path, monkeypatch) -> None:
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: pytest.fail(
        "Task-routing check must not dispatch a model"
    ))
    try:
        run = host.start(LiveStartRequest(
            request_id="decline-two-requester-tasks",
            question="Keep directed feedback on its originating task.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        requester = host._control(run.run_id, "agent-synthesis")
        recipient = host._control(run.run_id, "agent-critique")
        mission_id = requester["body"]["mission_id"]
        unrelated = host.board.propose(
            "decline-unrelated-proposal", mission_id=mission_id,
            proposal_id="aaa-unrelated-proposal",
            actor_id=requester["body"]["actor_id"],
            objective="Separate synthetic task", expected_artifact="Separate note",
            next_action="Keep this task independent",
        )
        assert unrelated["status"] == "accepted"
        other = host._save("mission_task_control", "aaa-unrelated-control", {
            **requester["body"],
            "task_id": unrelated["proposal_ref"]["id"],
            "proposal_ref": unrelated["proposal_ref"],
            "phase": "done", "report_ref": None,
        }, mission_id=mission_id)
        host._update_control(requester, phase="done")
        sent = host.mailbox.send_request(
            "decline-exact-task-send", mission_id=mission_id,
            request_id="decline-exact-task-request",
            message_id="decline-exact-task-message",
            requester_id=requester["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"],
            question="Check this task's synthetic conclusion.",
            expected_artifact="Independent note",
            cause_ref=requester["body"]["proposal_ref"],
            payload={"synthetic": True},
        )
        assert sent["status"] == "accepted"
        declined = host.mailbox.decline(
            "decline-exact-task", mission_id=mission_id,
            request_id="decline-exact-task-request",
            recipient_id=recipient["body"]["actor_id"],
            reason="No independent material for this task",
            expected_version=sent["request_ref"]["version"],
        )
        assert declined["status"] == "accepted"
        assert host._activate_peer_obligations(host._run(run.run_id)) is True
        current = host.store.get("mail_request", "decline-exact-task-request")
        assert current["body"]["requester_wake_ref"]["id"] == requester["id"]
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
        assert host.store.get("mission_task_control", other["id"])["body"]["phase"] == "done"
        assert host._activate_peer_obligations(host._run(run.run_id)) is False
        assert len([event for event in host.store.events(mission_id)
                    if event["type"] == "peer_request_declined_wake"]) == 1
    finally:
        host.close()


def test_decline_wake_retries_after_requester_control_changes_before_commit(
    tmp_path, monkeypatch,
) -> None:
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: pytest.fail(
        "Wake retry check must not dispatch a model"
    ))
    try:
        run = host.start(LiveStartRequest(
            request_id="decline-wake-head-race",
            question="Preserve an exact declined-request wake.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        requester = host._control(run.run_id, "agent-synthesis")
        recipient = host._control(run.run_id, "agent-critique")
        mission_id = requester["body"]["mission_id"]
        sent = host.mailbox.send_request(
            "decline-wake-race-send", mission_id=mission_id,
            request_id="decline-wake-race-request",
            message_id="decline-wake-race-message",
            requester_id=requester["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"],
            question="Check this synthetic point.", expected_artifact="A bounded note",
            cause_ref=requester["body"]["proposal_ref"],
            payload={"synthetic": True},
        )
        assert sent["status"] == "accepted"
        declined = host.mailbox.decline(
            "decline-wake-race-save", mission_id=mission_id,
            request_id="decline-wake-race-request",
            recipient_id=recipient["body"]["actor_id"],
            reason="No independent synthetic material",
            expected_version=sent["request_ref"]["version"],
        )
        assert declined["status"] == "accepted"
        original_transact = host.store.transact
        injected = []

        def change_head_at_wake(operation_id, *args, **kwargs):
            if operation_id.startswith("mission-declined-wake:") and not injected:
                current = host._control(run.run_id, "agent-synthesis")
                host._update_control(current, waiting_reason="Injected head change")
                injected.append(current["version"])
            return original_transact(operation_id, *args, **kwargs)

        monkeypatch.setattr(host.store, "transact", change_head_at_wake)
        assert host._activate_peer_obligations(host._run(run.run_id)) is False
        assert injected
        assert host.store.get("mail_request", "decline-wake-race-request")["body"].get(
            "requester_wake_ref"
        ) is None
        assert host._activate_peer_obligations(host._run(run.run_id)) is True
        current = host.store.get("mail_request", "decline-wake-race-request")
        assert current["body"]["requester_wake_ref"] == ref(
            host._control(run.run_id, "agent-synthesis")
        )
        assert host._activate_peer_obligations(host._run(run.run_id)) is False
        assert len([event for event in host.store.events(mission_id)
                    if event["type"] == "peer_request_declined_wake"]) == 1
    finally:
        host.close()
