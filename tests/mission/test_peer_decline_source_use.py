"""Keep source-derived decline prose out of model input after permission changes."""

from __future__ import annotations

import asyncio
import json
import socket
import time

from ora.contracts.models import LiveStartRequest
from tests.mission.test_service import revoke_source, serialized_peer_name, service, tool

REVOKED_SENTINEL = "SYNTHETIC_REVOKED_SOURCE_ONLY_TEXT"


def test_decline_reason_does_not_copy_revoked_source_into_requester_input(
    tmp_path, monkeypatch,
):
    def no_network(*_args, **_kwargs):
        raise AssertionError("Source-use probe attempted network access")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    host = service(tmp_path, monkeypatch, no_network)
    try:
        run = host.start(LiveStartRequest(
            request_id="revoked-decline-reason", question="Assess a synthetic source",
            evidence_text=REVOKED_SENTINEL,
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        requester = host._control(run.run_id, "agent-synthesis")
        recipient = host._control(run.run_id, "agent-critique")
        mission_id = requester["body"]["mission_id"]
        source_ref = requester["body"]["source_refs"][0]
        sent = host.mailbox.send_request(
            "revoked-decline-send", mission_id=mission_id,
            request_id="revoked-decline-request",
            message_id="revoked-decline-message",
            requester_id=requester["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"],
            question="Check the synthetic source", expected_artifact="Peer check",
            cause_ref=requester["body"]["proposal_ref"],
            payload={"synthetic": True},
        )
        assert sent["status"] == "accepted"
        host.mailbox.mark_delivered(
            "revoked-decline-deliver", mission_id=mission_id,
            message_id="revoked-decline-message",
        )
        request = host.store.get("mail_request", "revoked-decline-request")
        declined = host.mailbox.decline(
            "revoked-decline-save", mission_id=mission_id,
            request_id=request["id"],
            recipient_id=recipient["body"]["actor_id"],
            reason=f"Could not verify {REVOKED_SENTINEL}",
            expected_version=request["version"],
        )
        assert declined["status"] == "accepted"
        revoke_source(host, source_ref, mission_id)
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        active = host._control(run.run_id, "agent-synthesis")
        call_ref = active["body"]["call_ref"]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}",
        )
        request_bytes = host.store.read_blob(intent["body"]["blob"]["sha256"])
        assert REVOKED_SENTINEL.encode() not in request_bytes
        assert json.loads(request_bytes)["model"]
    finally:
        host.close()


PRODUCER_SENTINEL = "SYNTHETIC_DECLINE_PRODUCER_SOURCE_TEXT"


def test_revoked_decline_dependency_blocks_prepared_request_before_dispatch(
    tmp_path, monkeypatch,
):
    def no_network(*_args, **_kwargs):
        raise AssertionError("Offline dispatch probe attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    seen = {"Sage": 0, "Mxra": 0}
    request_id = "decline-revocation-request"
    host = None

    def transport(request, *, timeout):
        nonlocal host
        body = json.loads(request.data)
        role = serialized_peer_name(body)
        seen[role] += 1
        if role == "Sage":
            raise AssertionError("Stale prepared requester input reached the model adapter")
        if seen[role] == 1:
            return tool("request_capability", {"tool_id": "decline_peer_request"}, 1)
        if seen[role] == 2:
            current = host.store.get("mail_request", request_id)
            return tool("decline_peer_request", {
                "request_id": request_id,
                "request_version": current["version"],
                "reason": f"Cannot independently verify {PRODUCER_SENTINEL}",
            }, 2)
        raise AssertionError("Unexpected additional recipient call")

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="decline-revocation-dispatch",
            question="Assess a synthetic publication",
            evidence_text=PRODUCER_SENTINEL,
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        mission_id = host._run(run.run_id)["body"]["mission_id"]
        requester = host._control(run.run_id, "agent-synthesis")
        recipient = host._control(run.run_id, "agent-critique")
        source_ref = recipient["body"]["source_refs"][0]
        sent = host.mailbox.send_request(
            "decline-revocation-send", mission_id=mission_id,
            request_id=request_id, message_id="decline-revocation-message",
            requester_id=requester["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"],
            question="Check this synthetic finding", expected_artifact="Peer note",
            cause_ref=requester["body"]["proposal_ref"],
            payload={"synthetic": True},
        )
        assert sent["status"] == "accepted"
        host.mailbox.mark_delivered(
            "decline-revocation-deliver", mission_id=mission_id,
            message_id="decline-revocation-message",
        )
        sage_actor = requester["body"]["actor_id"]
        schedule = host._schedule_model

        def hold_sage(control, **kwargs):
            return False if control["body"]["actor_id"] == sage_actor else schedule(
                control, **kwargs
            )

        monkeypatch.setattr(host, "_schedule_model", hold_sage)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            host._drive_run(host._run(run.run_id))
            if host.store.get("mail_request", request_id)["body"]["status"] == "declined":
                break
            time.sleep(0.01)
        assert host.store.get("mail_request", request_id)["body"]["status"] == "declined"
        assert seen == {"Sage": 0, "Mxra": 2}

        sage = host._control(run.run_id, "agent-synthesis")
        host._update_control(sage, source_refs=[], phase="ready_model", waiting_reason=None)
        sage = host._control(run.run_id, "agent-synthesis")
        assert schedule(sage)
        pending = host._control(run.run_id, "agent-synthesis")
        call_ref = pending["body"]["call_ref"]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}",
        )
        assert intent["body"]["source_refs"] == []
        assert source_ref in intent["body"]["context_source_dependency_refs"]
        prepared = host.store.read_blob(intent["body"]["blob"]["sha256"])
        assert PRODUCER_SENTINEL.encode() in prepared  # The peer reason was legitimately visible then.

        revoke_source(host, source_ref, mission_id)
        asyncio.run(host.executor.run_ready(mission_id))
        assert seen == {"Sage": 0, "Mxra": 2}
        assert not any(item["body"].get("status") == "captured"
                       and item["body"].get("call_ref") == call_ref
                       for item in host.store.records("model_dispatch_attempt"))
    finally:
        host.close()
