"""Private offline probe for a sibling request racing cancelled-scope closure."""

from __future__ import annotations

import asyncio
import socket

import pytest

from ora.contracts.models import LiveBranchCancelRequest, LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import answer, service


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("socket connection")
        raise AssertionError("Peer closure probe attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    yield
    assert attempts == []


def test_sibling_request_saved_at_cancelled_closure_boundary_stays_actionable(
    tmp_path, monkeypatch,
):
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        return answer("Independent synthetic contribution.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    try:
        aliases = ["agent-synthesis", "agent-critique", "agent-literature-acquisition"]
        run = host.start(LiveStartRequest(
            request_id="sibling-mail-closure-race",
            question="Three independent synthetic contributions",
            agent_ids=aliases,
            required_agent_ids=aliases,
        ))
        stopped = host._control(run.run_id, aliases[0])
        task = host.store.get("board_proposal", stopped["body"]["task_id"])
        host.cancel_branch(LiveBranchCancelRequest(
            request_id="cancel-first-required", run_id=run.run_id,
            task_id=task["id"], expected_task_version=task["version"],
            reason="Stop one exact synthetic task",
        ))
        mission_id = stopped["body"]["mission_id"]
        for alias in aliases[1:]:
            assert host._schedule_model(host._control(run.run_id, alias))
        result = asyncio.run(host.executor.run_ready(mission_id))
        assert sum(value == "completed" for value in result["work"].values()) == 2
        for alias in aliases[1:]:
            assert host._advance(host._control(run.run_id, alias))
            assert host._control(run.run_id, alias)["body"]["phase"] == "done"
        assert host._run(run.run_id)["body"]["status"] == "active"
        requester = host._control(run.run_id, aliases[1])
        recipient = host._control(run.run_id, aliases[2])
        original = host.store.transact
        inserted = []

        def before_close(operation_id, request, action, **kwargs):
            if operation_id.startswith("mission-cancelled-scope:") and not inserted:
                sent = host.mailbox.send_request(
                    "synthetic-sibling-mail-race", mission_id=mission_id,
                    request_id="sibling-mail-race", message_id="sibling-message-race",
                    requester_id=requester["body"]["actor_id"],
                    recipient_id=recipient["body"]["actor_id"],
                    question="Review the sibling's saved result",
                    expected_artifact="Synthetic peer review",
                    cause_ref=ref(host.store.get("board_proposal", requester["body"]["task_id"])),
                    payload={"text": "Review the sibling's saved result"},
                )
                inserted.append(sent)
            return original(operation_id, request, action, **kwargs)

        monkeypatch.setattr(host.store, "transact", before_close)
        assert host._maybe_complete(host._run(run.run_id)) is False
        assert inserted and inserted[0]["status"] == "accepted"
        assert host._run(run.run_id)["body"]["status"] == "active"
        request = host.store.get("mail_request", "sibling-mail-race")
        assert request["body"]["status"] == "unanswered"
        assert host._activate_peer_obligations(host._run(run.run_id))
        assert host._control(run.run_id, aliases[2])["body"]["phase"] == "ready_model"
        assert len(calls) == 2
    finally:
        host.close()
