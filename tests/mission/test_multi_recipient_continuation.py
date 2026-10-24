"""Independent peer requests and branch-local repair through saved mission state."""

from __future__ import annotations

import json
import socket
import time

import pytest

from ora.agents import AgentRegistry
from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ProposalBoard, ref
from ora.coordination.mailbox import PeerMailbox
from ora.state import StateStore
from tests.mission.test_service import ROOT, answer, serialized_peer_name, service, tool


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("socket")
        raise AssertionError("Offline multi-recipient case opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    yield
    assert attempts == []


def _drive_until(host, run_id: str, predicate, *, timeout: float = 10, errors=()) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if errors:
            raise errors[0]
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Multi-recipient mission did not reach the saved checkpoint")


def _last_tool(body: dict) -> tuple[str, dict]:
    for message in reversed(body["messages"]):
        if message["role"] == "tool":
            return message["name"], json.loads(message["content"])
    return "", {}


def test_mailbox_independent_partial_full_decline_and_linked_late_evidence(tmp_path):
    """Backend diagnostic: dispositions are independent, not a group vote."""
    store = StateStore(tmp_path)
    registry = AgentRegistry(store, ROOT)
    peers = [registry.register(
        f"synthetic-peer-{index}", mission_id="mission", run_id="run",
        role_id="evidence_preparation_synthesis", display_name=f"Peer {index}",
    )["id"] for index in range(4)]
    proposal = ProposalBoard(store).propose(
        "multi-proposal", mission_id="mission", proposal_id="synthetic-task",
        actor_id=peers[0], objective="Compare synthetic limits",
        expected_artifact="Qualified methods note", next_action="Ask independent peers",
    )["proposal_ref"]
    source = store.transact("synthetic-source", {}, lambda tx: tx.put(
        "source_copy", "synthetic-source", {
            "mission_id": "mission", "synthetic": True, "text": "A bounded fixture passage.",
        }), mission_id="mission")
    mailbox = PeerMailbox(store)
    shared_question = "Independently assess the synthetic comparator."
    requests = {}
    for index, label in enumerate(("partial", "full", "declined"), start=1):
        opened = mailbox.send_request(
            f"send-{label}", mission_id="mission", request_id=f"request-{label}",
            message_id=f"message-{label}", requester_id=peers[0],
            recipient_id=peers[index], question=shared_question,
            expected_artifact="Independent synthetic methods note",
            cause_ref=proposal, task_ref=proposal,
            evidence_refs=[ref(source)], payload={"synthetic": True, "branch": label},
        )
        assert opened["status"] == "accepted"
        requests[label] = opened["request_ref"]
    assert len(store.records("mail_request")) == len(store.records("mail_message")) == 3
    assert {store.get(**pointer)["body"]["recipient_id"] for pointer in requests.values()} == set(peers[1:])
    assert all(store.get(**pointer)["body"]["question"] == shared_question
               for pointer in requests.values())

    # Transport delivery and recipient acknowledgment do not claim a scientific answer.
    assert mailbox.mark_delivered("deliver-full", mission_id="mission",
                                  message_id="message-full")["status"] == "accepted"
    assert mailbox.acknowledge("ack-full", mission_id="mission",
                               message_id="message-full", recipient_id=peers[2])["status"] == "accepted"
    full_message = store.get("mail_message", "message-full")
    assert full_message["body"]["delivered_at"] and full_message["body"]["acknowledged_at"]
    assert mailbox.request_view("request-full")["status"] == "unanswered"
    assert store.get("mail_message", "message-partial")["body"]["delivered_at"] is None

    def artifact(name: str, text: str) -> dict:
        return ref(store.transact(f"artifact-{name}", {}, lambda tx: tx.put(
            "artifact", name, {"mission_id": "mission", "synthetic": True,
                               "body": text, "source_ref": ref(source)}),
            mission_id="mission"))

    partial = mailbox.record_answer(
        "partial-answer", mission_id="mission", request_id="request-partial",
        answer_id="answer-partial", responder_id=peers[1],
        artifact_ref=artifact("partial-note", "Comparator not resolved"), partial=True,
    )
    assert partial["status"] == "accepted"
    assert store.get(**store.get(**partial["answer_ref"])["body"]["artifact_ref"])["body"][
        "body"
    ] == "Comparator not resolved"
    assert mailbox.review_answer(
        "partial-review", mission_id="mission", request_id="request-partial",
        reviewer_id=peers[0], answer_id="answer-partial",
        disposition="partly_answered", reason="Comparator still missing",
    )["status"] == "accepted"
    full = mailbox.record_answer(
        "full-answer", mission_id="mission", request_id="request-full",
        answer_id="answer-full", responder_id=peers[2],
        artifact_ref=artifact("full-note", "Comparator checked with the fixture"),
    )
    assert full["status"] == "accepted"
    assert mailbox.review_answer(
        "full-review", mission_id="mission", request_id="request-full",
        reviewer_id=peers[0], answer_id="answer-full", disposition="answered",
        reason="Exact synthetic note addresses this recipient request",
    )["status"] == "accepted"
    assert mailbox.decline(
        "decline-third", mission_id="mission", request_id="request-declined",
        recipient_id=peers[3], reason="No independent comparator available",
    )["status"] == "accepted"
    assert store.get("mail_request", "request-declined")["body"]["reason"] == (
        "No independent comparator available"
    )
    assert [mailbox.request_view(f"request-{label}")["status"]
            for label in ("partial", "full", "declined")] == [
                "partly_answered", "answered", "declined",
            ]
    assert store.get(**store.get(**full["answer_ref"])["body"]["artifact_ref"])["body"][
        "body"
    ] == "Comparator checked with the fixture"
    assert store.get("mail_request", "request-full")["body"]["accepted_answer_ref"] == full[
        "answer_ref"
    ]
    assert store.get("mail_request", "request-partial")["body"]["accepted_answer_ref"] is None

    # Later evidence creates a linked follow-up; original decisions remain exact history.
    original_full = store.get("mail_request", "request-full")
    later = store.transact("later-synthetic-source", {}, lambda tx: tx.put(
        "source_copy", "later-synthetic-source", {
            "mission_id": "mission", "synthetic": True,
            "text": "Later contradictory fixture passage.",
        }), mission_id="mission")
    follow_args = dict(
        mission_id="mission", request_id="request-followup",
        message_id="message-followup", requester_id=peers[0], recipient_id=peers[1],
        question="Recheck the comparator after later synthetic evidence.",
        expected_artifact="Updated independent note", cause_ref=ref(original_full),
        task_ref=proposal, evidence_refs=[ref(later)],
        payload={"prior_request_ref": ref(original_full), "synthetic": True},
    )
    follow = mailbox.send_request("linked-followup", **follow_args)
    assert follow["status"] == "accepted"
    reopened = PeerMailbox(StateStore(tmp_path))
    assert reopened.send_request("linked-followup", **follow_args) == follow
    assert len(store.records("mail_request")) == 4
    assert store.get("mail_request", "request-full") == original_full
    assert store.get(**follow["request_ref"])["body"]["cause_ref"] == ref(original_full)
    assert store.get(**store.get(**follow["request_ref"])["body"]["message_ref"])[
        "body"
    ]["evidence_refs"] == [ref(later)]


def test_two_scripted_recipients_are_read_and_reviewed_after_reopen(tmp_path, monkeypatch):
    """The actual mission loop delivers each reply to a separate requester turn."""
    turns = {"Sage": 0, "Mxra": 0, "Remy": 0}
    request_ids = {}
    seen = {"read": [], "review": []}
    peer_text = {
        "Mxra": "Mxra finds the synthetic comparator uncertain.",
        "Remy": "Remy finds a different synthetic data limitation.",
    }
    active_host = None
    run_id = None
    transport_errors = []

    def transport(request, *, timeout):
        try:
            return scripted_transport(request, timeout=timeout)
        except Exception as exc:
            transport_errors.append(exc)
            raise

    def scripted_transport(request, *, timeout):
        body = json.loads(request.data)
        role = serialized_peer_name(body, tuple(turns))
        turns[role] += 1
        step = turns[role]
        if role != "Sage":
            assert step == 1
            incoming = json.loads(body["messages"][-1]["content"])["incoming_request"]
            assert incoming["ref"]["id"] == request_ids[role]
            assert incoming["recipient_id"] == active_host._control(
                run_id, "agent-critique" if role == "Mxra" else "agent-data-management"
            )["body"]["actor_id"]
            return tool("publish_result", {
                "title": f"{role}'s independent synthetic note",
                "text": peer_text[role], "citations": [],
                "limitations": ["Synthetic input only"],
                "answer_request_id": incoming["ref"]["id"],
            }, 100 + step if role == "Mxra" else 200 + step)

        name, result = _last_tool(body)
        if step == 1:
            return tool("peer_request", {
                "target_instance_id": "agent-critique",
                "question": "Check the synthetic comparator independently.",
            }, 1)
        if step == 2:
            assert name == "peer_request" and result["status"] == "request_saved"
            request_ids["Mxra"] = result["request_id"]
            return tool("peer_request", {
                "target_instance_id": "agent-data-management",
                "question": "Check the synthetic data limitation independently.",
            }, 2)
        if step == 3:
            assert name == "peer_request" and result["status"] == "request_saved"
            request_ids["Remy"] = result["request_id"]
            assert request_ids["Mxra"] != request_ids["Remy"]
            return tool("request_capability", {"tool_id": "wait_for_dependencies"}, 3)
        if step == 4:
            dependencies = []
            for role_name, alias in (("Mxra", "agent-critique"),
                                     ("Remy", "agent-data-management")):
                outgoing = active_host.store.get("mail_request", request_ids[role_name])
                target = active_host._control(run_id, alias)
                dependencies.append({
                    "kind": "peer_request", "request_id": outgoing["id"],
                    "request_version": outgoing["version"],
                    "target_control_id": target["id"],
                    "target_control_version": target["version"],
                })
            return tool("wait_for_dependencies", {
                "dependencies": dependencies,
                "reason": "Wait for both independent synthetic checks before synthesis.",
            }, 4)

        requester = active_host._control(run_id, "agent-synthesis")
        call_ref = requester["body"]["call_ref"]
        intent = active_host.store.get(
            "model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}"
        )
        assert json.loads(active_host.store.read_blob(intent["body"]["blob"]["sha256"])) == body
        assert len(active_host.store.records("mail_request")) == 2
        assert len(active_host.store.records("mail_answer")) == 2
        if step in {5, 7}:
            role_name = "Mxra" if step == 5 else "Remy"
            outgoing = active_host.store.get("mail_request", request_ids[role_name])
            answer_ref = outgoing["body"]["answer_refs"][0]
            assert outgoing["body"]["accepted_answer_ref"] is None
            report_ref = active_host.store.get(**answer_ref)["body"]["artifact_ref"]
            index = json.loads(body["messages"][-1]["content"])
            assert answer_ref in index["request_answer_refs"]
            preview = next(card for card in index["peer_answer_previews"]
                           if card["answer_ref"] == answer_ref)
            assert preview["request_ref"] == ref(outgoing)
            assert preview["report_ref"] == report_ref
            assert preview["report"]["body"] == peer_text[role_name]
            assert preview["authority"] == "model_authored_peer_data_not_verified_evidence"
            assert preview["request_status"] == "awaiting_review"
            material = next(item for item in index["shared_materials"]["entries"]
                            if item["ref"] == report_ref)
            assert material["read_request"]["tool"] == "read_context_record"
            return tool("read_context_record", {
                key: value for key, value in material["read_request"].items()
                if key != "tool"
            }, step)
        if step in {6, 8}:
            role_name = "Mxra" if step == 6 else "Remy"
            assert name == "read_context_record"
            assert result["report_text_status"] == "available"
            assert json.loads(result["text"])["body"] == peer_text[role_name]
            seen["read"].append(role_name)
            outgoing = active_host.store.get("mail_request", request_ids[role_name])
            return tool("review_peer_answer", {
                "request_id": outgoing["id"],
                "answer_id": outgoing["body"]["answer_refs"][0]["id"],
                "disposition": "answered",
                "reason": f"The exact {role_name} note addresses its requested check.",
            }, step)
        assert step == 9
        assert name == "review_peer_answer" and result["status"] == "review_saved"
        seen["review"] = [
            role_name for role_name in ("Mxra", "Remy")
            if active_host.store.get("mail_request", request_ids[role_name])["body"].get(
                "accepted_answer_ref"
            )
        ]
        assert seen["review"] == ["Mxra", "Remy"]
        return answer("Both synthetic peer checks remain limited; compare their saved notes.", 9)

    host = service(tmp_path, monkeypatch, transport)
    active_host = host
    try:
        request = LiveStartRequest(
            request_id="two-synthetic-recipients",
            question="Compare two independent synthetic limitations.",
            evidence_text="A synthetic source passage shared with all peers.",
            agent_ids=["agent-synthesis", "agent-critique", "agent-data-management"],
            required_agent_ids=["agent-synthesis", "agent-critique", "agent-data-management"],
        )
        started = host.start(request)
        run_id = started.run_id
        original = host._run(run_id)
        original_controls = {alias: host._control(run_id, alias) for alias in request.agent_ids}
        requester_id = original_controls["agent-synthesis"]["body"]["actor_id"]
        for alias in ("agent-critique", "agent-data-management"):
            host._update_control(host._control(run_id, alias), phase="retry_at",
                                 retry_at=time.time() + 3600,
                                 waiting_reason="Fixture holds recipient until both requests exist")
        original_advance = host._advance
        original_update = host._update_control
        original_schedule = host._schedule_model
        hold = {"requester": False}

        def hold_requester(control):
            if hold["requester"] and control["body"]["actor_id"] == requester_id:
                return False
            return original_advance(control)

        def hold_after_wait(control, **changes):
            saved = original_update(control, **changes)
            if (control["body"]["actor_id"] == requester_id
                    and changes.get("phase") == "waiting" and changes.get("wait_for")):
                hold["requester"] = True
            return saved

        def hold_requester_schedule(control, **kwargs):
            if hold["requester"] and control["body"]["actor_id"] == requester_id:
                return False
            return original_schedule(control, **kwargs)

        monkeypatch.setattr(host, "_advance", hold_requester)
        monkeypatch.setattr(host, "_update_control", hold_after_wait)
        monkeypatch.setattr(host, "_schedule_model", hold_requester_schedule)
        _drive_until(host, run_id, lambda: len(request_ids) == 2 and bool(
            host._control(run_id, "agent-synthesis")["body"].get("wait_for")
        ), errors=transport_errors)
        assert hold["requester"] is True
        assert len(host.store.records("mail_request")) == 2
        assert len(host.store.records("mail_answer")) == 0
        host._update_control(host._control(run_id, "agent-critique"),
                             phase="ready_model", waiting_reason=None, retry_at=None)
        _drive_until(host, run_id, lambda: bool(
            host.store.get("mail_request", request_ids["Mxra"])["body"]["answer_refs"]
        ), errors=transport_errors)
        assert host.store.get("mail_request", request_ids["Remy"])["body"]["status"] == "unanswered"
        assert host._control(run_id, "agent-synthesis")["body"]["report_ref"] is None
        host._update_control(host._control(run_id, "agent-data-management"),
                             phase="ready_model", waiting_reason=None, retry_at=None)
        _drive_until(host, run_id, lambda: bool(
            host.store.get("mail_request", request_ids["Remy"])["body"]["answer_refs"]
        ), errors=transport_errors)
        assert turns == {"Sage": 4, "Mxra": 1, "Remy": 1}
        assert all(host.store.get("mail_request", request_ids[role_name])["body"].get(
            "accepted_answer_ref"
        ) is None for role_name in ("Mxra", "Remy"))
        captured = {role_name: host.store.get("mail_request", request_ids[role_name])
                    for role_name in ("Mxra", "Remy")}
    finally:
        host.close()

    resumed = service(tmp_path, monkeypatch, transport)
    active_host = resumed
    try:
        assert resumed._run(run_id)["body"]["profile"] == original["body"]["profile"]
        assert resumed._run(run_id)["body"]["source_refs"] == original["body"]["source_refs"]
        assert all(resumed._control(run_id, alias)["body"]["source_refs"] == original_controls[
            alias
        ]["body"]["source_refs"] for alias in request.agent_ids)
        assert all(resumed.store.get("mail_request", request_ids[role_name]) == captured[
            role_name
        ] for role_name in ("Mxra", "Remy"))
        _drive_until(resumed, run_id, lambda: resumed._run(run_id)["body"]["status"] == "completed",
                     timeout=30, errors=transport_errors)
        assert turns == {"Sage": 9, "Mxra": 1, "Remy": 1}
        assert seen == {"read": ["Mxra", "Remy"], "review": ["Mxra", "Remy"]}
        assert len(resumed.store.records("mail_request")) == 2
        assert len(resumed.store.records("mail_answer")) == 2
        for role_name in ("Mxra", "Remy"):
            reviewed = resumed.store.get("mail_request", request_ids[role_name])
            assert reviewed["body"]["reviewer_id"] == requester_id
            assert reviewed["body"]["accepted_answer_ref"] == reviewed["body"][
                "answer_refs"
            ][0]
        assert all(resumed._control(run_id, alias)["body"]["phase"] == "done"
                   for alias in request.agent_ids)
    finally:
        resumed.close()


def test_malformed_resumed_peer_action_does_not_stop_healthy_branch(tmp_path, monkeypatch):
    """A saved model work item can fail locally while its sibling still contributes."""
    original = service(
        tmp_path, monkeypatch,
        lambda *_args, **_kwargs: pytest.fail("Initial host must not dispatch"),
    )
    try:
        request = LiveStartRequest(
            request_id="malformed-resumed-peer-action",
            question="Assess two synthetic branches despite a bad peer target.",
            evidence_text="Synthetic evidence shared with both branches.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        )
        run = original.start(request)
        original._update_control(original._control(run.run_id, "agent-critique"),
                                 phase="retry_at", retry_at=time.time() + 3600,
                                 waiting_reason="Fixture holds healthy sibling")
        assert original._schedule_model(original._control(run.run_id, "agent-synthesis"))
        sage = original._control(run.run_id, "agent-synthesis")
        saved_call_ref = sage["body"]["call_ref"]
        saved_intent = original.store.get(
            "model_request_intent",
            f"model-intent:{saved_call_ref['id']}:{saved_call_ref['version']}",
        )
        assert saved_intent is not None
        source_refs = original._run(run.run_id)["body"]["source_refs"]
        profile = original._run(run.run_id)["body"]["profile"]
    finally:
        original.close()

    turns = {"Sage": 0, "Mxra": 0}
    observed = {"rejection": False, "healthy": False}
    active_host = None

    def transport(request, *, timeout):
        body = json.loads(request.data)
        role = serialized_peer_name(body)
        turns[role] += 1
        if role == "Mxra":
            assert turns[role] == 1
            observed["healthy"] = True
            return tool("publish_result", {
                "title": "Independent synthetic branch",
                "text": "The healthy branch produced a bounded synthetic note.",
                "citations": [], "limitations": ["Synthetic input only"],
            }, 90)
        if turns[role] == 1:
            assert json.loads(active_host.store.read_blob(
                saved_intent["body"]["blob"]["sha256"
            ])) == body
            assert active_host._control(run.run_id, "agent-synthesis")["body"][
                "call_ref"
            ] == saved_call_ref
            return tool("peer_request", {
                "target_instance_id": "agent-not-in-this-run",
                "question": "Check this synthetic claim.",
            }, 1)
        assert turns[role] == 2
        assert any(message["role"] == "user"
                   and '"status":"failed_peer_request_still_unresolved"' in message["content"]
                   for message in body["messages"])
        observed["rejection"] = True
        return tool("publish_result", {
            "title": "Qualified synthetic branch after rejected target",
            "text": "The requested peer target was unavailable; this branch retains a limited result.",
            "citations": [], "limitations": ["The peer check was unavailable"],
            "scope_completion": {
                "reason": "The unavailable peer route did not provide evidence; finish with a limit.",
                "decisions": [],
            },
        }, 2)

    resumed = service(tmp_path, monkeypatch, transport)
    active_host = resumed
    try:
        assert resumed._run(run.run_id)["body"]["profile"] == profile
        assert resumed._run(run.run_id)["body"]["source_refs"] == source_refs
        sage_actor = resumed._control(run.run_id, "agent-synthesis")["body"]["actor_id"]
        original_schedule = resumed._schedule_model
        hold = {"repair": True}

        def hold_failed_branch(control, **kwargs):
            if (hold["repair"] and control["body"]["actor_id"] == sage_actor
                    and any(item.get("request_ref") is None for item in control[
                        "body"
                    ].get("peer_action_obligations", []))):
                return False
            return original_schedule(control, **kwargs)

        monkeypatch.setattr(resumed, "_schedule_model", hold_failed_branch)
        _drive_until(resumed, run.run_id, lambda: any(
            item.get("request_ref") is None
            for item in resumed._control(run.run_id, "agent-synthesis")["body"].get(
                "peer_action_obligations", []
            )
        ))
        assert turns["Sage"] == 1
        assert resumed.store.records("mail_request") == []
        resumed._update_control(resumed._control(run.run_id, "agent-critique"),
                                phase="ready_model", waiting_reason=None, retry_at=None)
        _drive_until(resumed, run.run_id, lambda: bool(
            resumed._control(run.run_id, "agent-critique")["body"].get("report_ref")
        ))
        assert turns == {"Sage": 1, "Mxra": 1}
        assert resumed._control(run.run_id, "agent-synthesis")["body"]["report_ref"] is None
        assert any(item.get("request_ref") is None for item in resumed._control(
            run.run_id, "agent-synthesis"
        )["body"].get("peer_action_obligations", []))
        hold["repair"] = False
        _drive_until(resumed, run.run_id, lambda: (
            resumed._run(run.run_id)["body"]["status"] == "completed"
        ))
        assert turns == {"Sage": 2, "Mxra": 1}
        assert observed == {"rejection": True, "healthy": True}
        assert resumed.store.records("mail_request") == []
        assert resumed.store.records("board_invitation") == []
        assert all(resumed._control(run.run_id, alias)["body"]["phase"] == "done"
                   for alias in request.agent_ids)
        assert all(resumed._control(run.run_id, alias)["body"]["report_ref"]
                   for alias in request.agent_ids)
        assert len(resumed.store.records("model_request_intent")) == 3
        repeat = resumed.start(request)
        assert repeat.reused and repeat.run_id == run.run_id
        assert len(resumed.store.records("mission_run")) == 1
    finally:
        resumed.close()
