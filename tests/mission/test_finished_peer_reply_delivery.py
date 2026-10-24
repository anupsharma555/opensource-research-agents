"""Scripted offline proof of a finished peer's reply reaching a required requester."""

from __future__ import annotations

import json
import socket
import time

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import answer, serialized_peer_name, service, tool

QUESTION = "Check the synthetic comparison after your first contribution."
PEER_TEXT = (
    "The independent check finds a limited synthetic comparison."
    + " Additional synthetic detail." * 75
)


def _drive_until(host, run_id: str, predicate) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Finished-peer reply did not reach its saved boundary")


def _last_tool(packet: dict) -> tuple[str, dict]:
    for message in reversed(packet["messages"]):
        if message["role"] == "tool":
            return message["name"], json.loads(message["content"])
    return "", {}


def test_finished_peer_explicit_reply_is_read_reviewed_and_both_required_finish(
    tmp_path, monkeypatch,
) -> None:
    def no_network(*_args, **_kwargs):
        raise AssertionError("Scripted peer mechanism must not reach the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    host = None
    run = None
    turns = {"Sage": 0, "Mxra": 0}
    seen = {"incoming": False, "read": False, "review": False}
    read_parts = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        role = serialized_peer_name(packet)
        turns[role] += 1
        index = turns[role]
        tool_id = index if role == "Sage" else 100 + index
        if role == "Mxra" and index == 1:
            return answer("My first independent synthetic contribution is complete.", tool_id)
        if role == "Mxra":
            saved = host.store.records("mail_request")[0]
            context = json.loads(packet["messages"][-1]["content"])
            incoming = context["incoming_request"]
            assert incoming["ref"] == ref(saved)
            assert incoming["recipient_id"] == host._control(
                run.run_id, "agent-critique",
            )["body"]["actor_id"]
            assert incoming["question"] == QUESTION
            assert "publish_result" in {
                item["function"]["name"] for item in packet["tools"]
            }
            seen["incoming"] = True
            return tool("publish_result", {
                "title": "Later independent check", "text": PEER_TEXT,
                "citations": [], "limitations": ["Synthetic evidence only"],
                "answer_request_id": saved["id"],
            }, tool_id)
        if index == 1:
            assert host._control(run.run_id, "agent-critique")["body"]["phase"] == "done"
            return tool("peer_request", {
                "target_instance_id": "agent-critique", "question": QUESTION,
            }, tool_id)

        saved = host.store.records("mail_request")[0]
        context = json.loads(packet["messages"][-1]["content"])
        disposition = next(
            item for item in context["request_dispositions"]
            if item["ref"]["id"] == saved["id"]
        )
        names = {item["function"]["name"] for item in packet["tools"]}
        if not saved["body"]["answer_refs"]:
            if "wait_for_dependencies" not in names:
                return tool("request_capability", {"tool_id": "wait_for_dependencies"}, tool_id)
            target = host._control(run.run_id, "agent-critique")
            return tool("wait_for_dependencies", {"dependencies": [{
                "kind": "peer_request", "request_id": disposition["ref"]["id"],
                "request_version": disposition["ref"]["version"],
                "target_control_id": target["id"],
                "target_control_version": target["version"],
            }], "reason": "Wait for the finished peer's later check."}, tool_id)

        answer_ref = saved["body"]["answer_refs"][0]
        peer_report_ref = host.store.get(**answer_ref)["body"]["artifact_ref"]
        name, result = _last_tool(packet)
        if name == "read_context_record":
            assert result["report_text_status"] == "available"
            assert result["ref"] == peer_report_ref
            read_parts.append(result["text"])
            if result["next_offset"] is not None:
                next_read = dict(result["next_request"])
                return tool(next_read.pop("tool"), next_read, tool_id)
            assert json.loads("".join(read_parts))["body"] == PEER_TEXT
            seen["read"] = True
            return tool("review_peer_answer", {
                "request_id": saved["id"], "answer_id": answer_ref["id"],
                "disposition": "answered",
                "reason": "I read the full later peer report and it addresses the request.",
            }, tool_id)
        if name == "review_peer_answer":
            assert result["status"] == "review_saved"
            assert saved["body"]["accepted_answer_ref"] == answer_ref
            seen["review"] = True
            return answer("The independent check supports only a limited synthetic conclusion.", tool_id)
        assert disposition["status"] in {"awaiting_review", "partial_awaiting_review"}
        assert answer_ref in context["request_answer_refs"]
        assert "peer_answer_previews" not in context
        assert PEER_TEXT not in json.dumps(packet["messages"])
        entry = next(
            item for item in context["shared_materials"]["entries"]
            if item["ref"] == peer_report_ref
        )
        read_request = entry["read_request"]
        assert read_request["tool"] == "read_context_record"
        assert "read_context_record" in names
        return tool("read_context_record", {
            key: value for key, value in read_request.items() if key != "tool"
        }, tool_id)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="finished-peer-explicit-reply",
            question="Obtain and review a later independent synthetic check.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        sage = host._control(run.run_id, "agent-synthesis")
        host._update_control(
            sage, phase="retry_at", retry_at=time.time() + 3600,
            waiting_reason="Fixture lets recipient finish first",
        )
        _drive_until(host, run.run_id, lambda: host._control(
            run.run_id, "agent-critique",
        )["body"]["phase"] == "done")
        assert turns == {"Sage": 0, "Mxra": 1}
        assert host._run(run.run_id)["body"]["status"] == "active"
        first_peer_report = host._control(run.run_id, "agent-critique")["body"]["report_ref"]
        host._update_control(
            host._control(run.run_id, "agent-synthesis"),
            phase="ready_model", waiting_reason=None, retry_at=None,
        )
        _drive_until(host, run.run_id, lambda: host._run(run.run_id)["body"]["status"] == "completed")
        saved = host.store.records("mail_request")
        assert len(saved) == 1
        assert saved[0]["body"]["status"] == "answered"
        assert len(saved[0]["body"]["answer_refs"]) == 1
        assert saved[0]["body"]["accepted_answer_ref"] == saved[0]["body"]["answer_refs"][0]
        assert seen == {"incoming": True, "read": True, "review": True}
        assert turns["Mxra"] == 2
        assert all(
            host._control(run.run_id, alias)["body"]["phase"] == "done"
            for alias in ("agent-synthesis", "agent-critique")
        )
        assert host._control(run.run_id, "agent-critique")["body"]["report_ref"] != first_peer_report
    finally:
        host.close()
