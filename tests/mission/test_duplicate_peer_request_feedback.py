"""Exact open duplicate requests get bounded feedback without merging work."""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import replace

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_peer_target_feedback import _request_spec
from tests.mission.test_service import answer, serialized_peer_name, service, tool


def _host(tmp_path, monkeypatch):
    def no_transport(*_args, **_kwargs):
        raise AssertionError("Offline peer request feedback must not call a model")

    host = service(tmp_path, monkeypatch, no_transport)
    third = next(alias for alias in sorted(host.role_choices)
                 if alias not in {"agent-synthesis", "agent-critique"})
    run = host.start(LiveStartRequest(
        request_id="exact-duplicate-peer-feedback",
        question="Compare synthetic interpretations.",
        agent_ids=["agent-synthesis", "agent-critique", third],
    ))
    spec = _request_spec(host, run.run_id, "agent-synthesis")
    return host, spec, third


def _send(host, spec, work_id, *, question="Check this synthetic interpretation.",
          recipient="agent-critique"):
    return host._peer_request(replace(spec, work_id=work_id), {
        "target_instance_id": recipient, "question": question,
    })[0]


def test_exact_open_duplicate_feedback_is_saved_with_distinct_requests(
    tmp_path, monkeypatch,
):
    host, spec, _third = _host(tmp_path, monkeypatch)
    try:
        first = _send(host, spec, "work-a")
        second = _send(host, spec, "work-b")
        assert first["request_ref"] != second["request_ref"]
        assert "same_question_observed_open" not in first
        hint = second["same_question_observed_open"]
        assert hint == {
            "scope": "exact_question_same_requester_recipient_and_task",
            "prior_requests": [{"request_ref": first["request_ref"],
                                "observed_status": "unanswered"}],
            "additional_count": 0,
        }
        assert "Both remain saved" in second["next_action"]
        assert len(host.store.records("mail_request")) == 2
        assert len(host.store.records("mail_message")) == 2
        message_ref = host.store.get(**second["request_ref"])["body"]["message_ref"]
        message = host.store.get(**message_ref)
        assert message["body"]["payload"] == {
            "question": "Check this synthetic interpretation.",
            "same_question_observed_open": hint,
        }
    finally:
        host.close()


def test_same_work_replay_retains_first_send_hint_after_state_changes(
    tmp_path, monkeypatch,
):
    host, spec, _third = _host(tmp_path, monkeypatch)
    try:
        first = _send(host, spec, "work-a")
        second = _send(host, spec, "work-b")
        prior = host.store.get("mail_request", first["request_id"])
        host.store.transact("synthetic-close-prior", {}, lambda tx: tx.put(
            "mail_request", prior["id"], {**prior["body"], "status": "answered"},
            expected_version=prior["version"],
        ), mission_id=spec.mission_id)
        _send(host, spec, "work-c")
        replay = _send(host, spec, "work-b")
        assert replay == second
        assert len(host.store.records("mail_request")) == 3
        assert replay["same_question_observed_open"]["prior_requests"] == [{
            "request_ref": first["request_ref"], "observed_status": "unanswered",
        }]
    finally:
        host.close()


def test_feedback_requires_exact_open_question_recipient_and_task(tmp_path, monkeypatch):
    host, spec, third = _host(tmp_path, monkeypatch)
    try:
        first = _send(host, spec, "work-a")
        changed_question = _send(host, spec, "work-b", question="A different question.")
        changed_recipient = _send(host, spec, "work-c", recipient=third)
        another = host.store.transact("synthetic-other-task", {}, lambda tx: ref(tx.put(
            "board_proposal", "synthetic-other-task", {
                "mission_id": spec.mission_id, "status": "open",
            },
        )), mission_id=spec.mission_id)
        changed_task = _send(host, replace(spec, proposal_ref=another), "work-d")
        assert all("same_question_observed_open" not in item for item in (
            first, changed_question, changed_recipient, changed_task,
        ))
        prior = host.store.get("mail_request", first["request_id"])
        host.store.transact("synthetic-close-prior", {}, lambda tx: tx.put(
            "mail_request", prior["id"], {**prior["body"], "status": "answered"},
            expected_version=prior["version"],
        ), mission_id=spec.mission_id)
        after_closed = _send(host, spec, "work-e")
        assert "same_question_observed_open" not in after_closed
    finally:
        host.close()


def test_duplicate_metadata_is_bounded_to_three_exact_refs(tmp_path, monkeypatch):
    host, spec, _third = _host(tmp_path, monkeypatch)
    try:
        requests = [_send(host, spec, f"work-{number}") for number in range(6)]
        hint = requests[-1]["same_question_observed_open"]
        assert len(hint["prior_requests"]) == 3
        assert hint["additional_count"] == 2
        assert [item["request_ref"] for item in hint["prior_requests"]] == [
            item["request_ref"] for item in sorted(
                requests[:-1], key=lambda item: item["request_id"],
            )[:3]
        ]
        assert all("question" not in item for item in hint["prior_requests"])
        assert len(host.store.records("mail_request")) == 6
    finally:
        host.close()


def test_hint_records_pre_send_observation_when_prior_changes_during_send(
    tmp_path, monkeypatch,
):
    host, spec, _third = _host(tmp_path, monkeypatch)
    try:
        first = _send(host, spec, "work-a")
        original_send = host.mailbox.send_request

        def close_prior_before_commit(*args, **kwargs):
            prior = host.store.get("mail_request", first["request_id"])
            host.store.transact("synthetic-racing-prior-close", {}, lambda tx: tx.put(
                "mail_request", prior["id"], {**prior["body"], "status": "answered"},
                expected_version=prior["version"],
            ), mission_id=spec.mission_id)
            return original_send(*args, **kwargs)

        monkeypatch.setattr(host.mailbox, "send_request", close_prior_before_commit)
        second = _send(host, spec, "work-b")
        assert second["same_question_observed_open"]["prior_requests"] == [{
            "request_ref": first["request_ref"], "observed_status": "unanswered",
        }]
        assert host.store.get("mail_request", first["request_id"])["body"][
            "status"
        ] == "answered"
        assert "check the prior request's current status" in second["next_action"]
    finally:
        host.close()


def test_duplicate_hint_reaches_next_serialized_tool_result(tmp_path, monkeypatch):
    def no_network(*_args, **_kwargs):
        raise AssertionError("Offline duplicate feedback attempted a socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    seen = {}
    turns = {"Sage": 0, "Mxra": 0}

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        peer = serialized_peer_name(packet)
        assert peer == "Sage"
        turns[peer] += 1
        if turns[peer] == 1:
            return tool("peer_request", {
                "target_instance_id": "agent-critique",
                "question": "Check this synthetic interpretation.",
            }, 1)
        result = next(json.loads(message["content"])
                      for message in reversed(packet["messages"])
                      if message.get("role") == "tool"
                      and message.get("name") == "peer_request")
        assert result["status"] == "request_saved"
        assert result["same_question_observed_open"]["prior_requests"][0][
            "request_ref"
        ] == seen["first_ref"]
        seen["delivered"] = True
        return answer("The exact duplicate warning was received.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="duplicate-result-delivery",
            question="Compare synthetic interpretations.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis"],
        ))
        spec = _request_spec(host, run.run_id, "agent-synthesis")
        seen["first_ref"] = _send(host, spec, "seed-prior-request")["request_ref"]
        requester = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(requester)
        asyncio.run(host.executor.run_ready(spec.mission_id))
        assert host._advance(host._control(run.run_id, "agent-synthesis"))
        assert host._schedule_tool(host._control(run.run_id, "agent-synthesis"))
        asyncio.run(host.executor.run_ready(spec.mission_id))
        assert host._advance(host._control(run.run_id, "agent-synthesis"))
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        asyncio.run(host.executor.run_ready(spec.mission_id))
        assert seen.get("delivered") is True
        assert len(host.store.records("mail_request")) == 2
    finally:
        host.close()
