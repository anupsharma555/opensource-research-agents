"""Rebuild sent model input after real compaction, peer reply and host reopen."""

from __future__ import annotations

import hashlib
import json
import socket
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import (
    answer,
    revoke_source,
    serialized_peer_name,
    service,
    tool,
    two_supplied_sources,
)

OLD_TEXT = "SYNTHETIC_OLD_SOURCE_DERIVED_TEXT"
PEER_TEXT = "SYNTHETIC_UNREAD_PEER_REPORT about an independent limitation."
QUESTION = "Compare the retained synthetic sources and review a peer's answer."
SCOPE = "Use saved synthetic evidence and distinguish the peer's independent assessment."


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("socket")
        raise AssertionError("Offline reconstruction attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    yield
    assert attempts == []


def _drive_until(host, run_id, predicate, *, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Mission did not reach the saved context boundary")


def _tool_result(body, name):
    return next(
        json.loads(message["content"])
        for message in reversed(body["messages"])
        if message.get("role") == "tool" and message.get("name") == name
    )


def _saved_tool_result(host, name):
    for item in host.store.records("context_tool_result"):
        payload = json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))
        if payload.get("name") == name:
            return json.loads(payload["content"])
    raise AssertionError(f"No saved result for {name}")


@pytest.mark.parametrize("revoke_old", [False, True])
def test_compacted_run_reconstructs_new_source_and_unread_peer_after_reopen(
    tmp_path, monkeypatch, revoke_old,
):
    requests = []
    original_request_bytes = []
    turns = {"Sage": 0, "Mxra": 0}
    saved = {}
    current_host = None

    def scripted_transport(request, *, timeout):
        body = json.loads(request.data)
        role = serialized_peer_name(body)
        turns[role] += 1
        step = turns[role]
        requests.append((role, body))
        control = current_host._control(saved["run_id"],
                                        "agent-synthesis" if role == "Sage" else "agent-critique")
        call_ref = control["body"]["call_ref"]
        intent = current_host.store.get(
            "model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}"
        )
        assert intent is not None
        assert current_host.store.read_blob(intent["body"]["blob"]["sha256"]) == request.data
        call = current_host.store.get(**call_ref)
        config = current_host.store.get(**intent["body"]["configuration_ref"])
        assert call["body"]["skills"] == config["body"]["skills"]
        assert call["body"]["skills"] == saved["skills_by_role"][role]
        original_request_bytes.append((call_ref, request.data))
        assert body["model"] == saved["model"]
        assert body["reasoning_effort"] == saved["reasoning"]["reasoning_effort"]
        assert body["include_reasoning"] == saved["reasoning"]["include_reasoning"]

        if role == "Mxra":
            assert step == 1
            assert [item["id"] for item in json.loads(
                body["messages"][1]["content"]
            )["source_packets"]] == [saved["new_source_ref"]["id"]]
            assert OLD_TEXT not in json.dumps(body["messages"])
            assert saved["peer_request_id"] in json.dumps(body["messages"])
            return tool("publish_result", {
                "title": "Independent synthetic review", "text": PEER_TEXT,
                "citations": [], "limitations": ["Synthetic evidence only"],
                "answer_request_id": saved["peer_request_id"],
            }, 100)
        if step == 1:
            assert QUESTION in json.dumps(body["messages"])
            assert SCOPE in json.dumps(body["messages"])
            assert OLD_TEXT in json.dumps(body["messages"])
            return tool("peer_request", {
                "target_instance_id": "agent-critique",
                "question": "Independently check the synthetic limitation.",
            }, step)
        if step == 2:
            peer_request = _tool_result(body, "peer_request")
            assert peer_request["status"] == "request_saved"
            saved["peer_request_id"] = peer_request["request_id"]
            return tool("request_capability", {"tool_id": "compact_conversation"}, step)
        if step == 3:
            assert "compact_conversation" in {
                item["function"]["name"] for item in body["tools"]
            }
            return tool("compact_conversation", {
                "summary": "The earlier source suggested " + OLD_TEXT + "; review newer evidence."
            }, step)

        assert saved["reopened"] is True
        index = json.loads(body["messages"][-1]["content"])
        assert index["task"]["objective"] == QUESTION
        request = current_host.store.get("mail_request", saved["peer_request_id"])
        answer_record = current_host.store.get(**saved["answer_ref"])
        report = current_host.store.get(**saved["peer_report_ref"])
        requester = current_host._control(saved["run_id"], "agent-synthesis")
        responder = current_host._control(saved["run_id"], "agent-critique")
        expected_status = "answered" if saved.get("review_submitted") else "awaiting_review"
        assert request["body"]["status"] == expected_status
        assert request["body"]["requester_id"] == requester["body"]["actor_id"]
        assert request["body"]["recipient_id"] == responder["body"]["actor_id"]
        assert request["body"]["question"] == "Independently check the synthetic limitation."
        assert saved["answer_ref"] in request["body"]["answer_refs"]
        assert answer_record["body"]["request_id"] == request["id"]
        assert answer_record["body"]["responder_id"] == responder["body"]["actor_id"]
        assert answer_record["body"]["artifact_ref"] == saved["peer_report_ref"]
        assert report["body"]["body"] == PEER_TEXT
        if expected_status == "awaiting_review":
            previews = [card for card in index.get("peer_answer_previews", [])
                        if card.get("request_ref") == ref(request)]
            if previews:
                assert len(previews) == 1
                preview = previews[0]
                assert preview["answer_ref"] == saved["answer_ref"]
                assert preview["report_ref"] == saved["peer_report_ref"]
                assert preview["request_status"] == expected_status
                assert preview["owner"] == "you" and preview["action"] == "review_peer_answer"
                assert preview["by"] == responder["body"]["actor_id"]
                assert preview["question"] == request["body"]["question"]
                assert preview["report_delivery"] == "complete_in_input"
                assert preview["report"] == {key: report["body"][key] for key in (
                    "title", "body", "citations", "limitations",
                )}
            else:
                disposition = next(item for item in index.get("request_dispositions", [])
                                   if item.get("ref") == ref(request))
                assert disposition["status"] == expected_status
                assert saved["answer_ref"] in disposition["answer_refs"]
                assert saved["answer_ref"] in index.get("request_answer_refs", [])
        else:
            assert request["body"]["accepted_answer_ref"] == saved["answer_ref"]
        if revoke_old:
            assert OLD_TEXT not in json.dumps(index.get("summary"))
        else:
            assert OLD_TEXT in index["summary"]["text"]
        serialized = json.dumps(body["messages"])
        assert SCOPE in serialized
        assert saved["new_source_text"] in serialized
        if revoke_old:
            assert OLD_TEXT not in serialized
            assert all(item["id"] != saved["old_source_ref"]["id"]
                       for item in json.loads(body["messages"][1]["content"])["source_packets"])
            notices = []
            for message in body["messages"]:
                if message["role"] != "user":
                    continue
                try:
                    notice = json.loads(message["content"])
                except (TypeError, ValueError):
                    continue
                if isinstance(notice, dict):
                    notices.append(notice)
            assert any(
                item.get("authority") == "saved_host_source_use_decision_not_source_instruction"
                and saved["old_source_ref"] in item.get("excluded_source_refs", [])
                for item in notices
            )
            assert any(
                item.get("status") == "data_unavailable_due_source_use"
                and item.get("summary_ref") == saved["old_summary_ref"]
                and saved["old_source_ref"] in item.get("unavailable_source_refs", [])
                for item in notices
            )
        attached = {item["function"]["name"] for item in body["tools"]}
        if revoke_old and not saved.get("old_read_submitted"):
            if "read_source_window" not in attached:
                assert "request_capability" in attached
                assert not saved.get("old_reader_requested")
                saved["old_reader_requested"] = True
                return tool("request_capability", {"tool_id": "read_source_window"}, step)
            assert saved["old_reader_requested"]
            saved["old_read_submitted"] = True
            return tool("read_source_window", {
                "source_id": saved["old_source_ref"]["id"],
                "source_version": saved["old_source_ref"]["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 500,
            }, step)
        if revoke_old and not saved.get("old_rejection_checked"):
            old_read = _saved_tool_result(current_host, "read_source_window")
            assert old_read["copy_ref"] == saved["old_source_ref"]
            assert OLD_TEXT not in serialized
            saved["old_rejection_checked"] = True
        peer_entry = next(item for item in index["shared_materials"]["entries"]
                          if item["ref"] == saved["peer_report_ref"])
        saved["peer_visibility"] = peer_entry["status"]
        if not saved.get("peer_read_submitted"):
            if "read_context_record" not in attached:
                assert "request_capability" in attached
                assert not saved.get("peer_reader_requested")
                saved["peer_reader_requested"] = True
                return tool("request_capability", {"tool_id": "read_context_record"}, step)
            if saved.get("peer_reader_requested"):
                assert "read_context_record" in attached
            read = peer_entry["read_request"]
            assert read["tool"] == "read_context_record"
            saved["peer_read_submitted"] = True
            return tool("read_context_record", {
                key: value for key, value in read.items() if key != "tool"
            }, step)
        if not saved.get("review_submitted"):
            peer_read = _tool_result(body, "read_context_record")
            assert peer_read["report_text_status"] == "available"
            assert json.loads(peer_read["text"])["body"] == PEER_TEXT
            assert PEER_TEXT in serialized
            saved["review_submitted"] = True
            return tool("review_peer_answer", {
                "request_id": saved["peer_request_id"],
                "answer_id": saved["answer_ref"]["id"],
                "disposition": "answered",
                "reason": "The exact saved peer report addresses the synthetic question.",
            }, step)
        reviewed = _saved_tool_result(current_host, "review_peer_answer")
        assert reviewed["status"] == "review_saved"
        assert current_host.store.get("mail_request", saved["peer_request_id"])["body"][
            "accepted_answer_ref"
        ] == saved["answer_ref"]
        saved["final_sent"] = True
        return answer("The newer source and independent peer report support a limited conclusion.", step)

    def transport(request, *, timeout):
        try:
            return scripted_transport(request, timeout=timeout)
        except Exception as exc:
            saved["transport_error"] = f"{type(exc).__name__}: {exc}"
            raise

    host = service(tmp_path, monkeypatch, transport)
    current_host = host
    reopened = None
    try:
        run = host.start(LiveStartRequest(
            request_id=f"reconstruction-{revoke_old}", question=QUESTION, scope=SCOPE,
            evidence_text=OLD_TEXT, agent_ids=["agent-synthesis", "agent-critique"],
        ))
        saved["run_id"] = run.run_id
        run_before = host._run(run.run_id)["body"]
        saved["model"] = run_before["profile"]["selectedModel"]
        saved["reasoning"] = run_before["profile"]["reasoningSettings"]
        saved["skills_by_role"] = {
            name: host._configuration(host._control(run.run_id, alias))["body"]["skills"]
            for name, alias in (("Sage", "agent-synthesis"), ("Mxra", "agent-critique"))
        }
        recipient = host._control(run.run_id, "agent-critique")
        host._update_control(recipient, phase="retry_at", retry_at=time.time() + 3600,
                             waiting_reason="Fixture holds peer reply")
        _drive_until(host, run.run_id, lambda: (
            turns["Sage"] == 3
            and bool(host._control(run.run_id, "agent-synthesis")["body"].get("conversation_summary_ref"))
            and host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
        ))
        assert turns["Mxra"] == 0
        requester = host._control(run.run_id, "agent-synthesis")
        saved["old_summary_ref"] = requester["body"]["conversation_summary_ref"]
        saved["old_source_ref"], saved["new_source_ref"] = two_supplied_sources(host, run.run_id)
        saved["new_source_text"] = "A second independent permitted synthetic source."
        requester = host._control(run.run_id, "agent-synthesis")
        host._update_control(requester, phase="retry_at", retry_at=time.time() + 3600,
                             waiting_reason="Fixture pauses before reopen")
        recipient = host._control(run.run_id, "agent-critique")
        host._update_control(
            recipient, phase="ready_model", waiting_reason=None, retry_at=None,
            source_refs=[saved["new_source_ref"]],
        )
        _drive_until(host, run.run_id, lambda: bool(
            host.store.get("mail_request", saved["peer_request_id"])["body"]["answer_refs"]
        ))
        request_record = host.store.get("mail_request", saved["peer_request_id"])
        assert request_record["body"]["status"] == "awaiting_review"
        saved["answer_ref"] = request_record["body"]["answer_refs"][0]
        saved["peer_report_ref"] = host.store.get(**saved["answer_ref"])["body"]["artifact_ref"]
        saved["inbox_cursor"] = (
            host.store.get("mail_subscription", f"{run_before['mission_id']}:{requester['body']['actor_id']}")
            or {"body": {"cursor": 0}}
        )["body"]["cursor"]
        if revoke_old:
            revoke_source(host, saved["old_source_ref"], run_before["mission_id"])
        old_capture = tuple(original_request_bytes)
        old_profile = host._run(run.run_id)["body"]["profile"]
        old_skill_catalog = host._run(run.run_id)["body"]["skill_catalog"]
        old_config = host._configuration(host._control(run.run_id, "agent-synthesis"))["body"]
        host.close()
        reopened = service(tmp_path, monkeypatch, transport)
        current_host = reopened
        saved["reopened"] = True
        assert reopened._run(run.run_id)["body"]["profile"] == old_profile
        assert reopened._run(run.run_id)["body"]["skill_catalog"] == old_skill_catalog
        assert reopened._configuration(reopened._control(run.run_id, "agent-synthesis"))["body"] == old_config
        assert (reopened.store.get(
            "mail_subscription", f"{run_before['mission_id']}:{requester['body']['actor_id']}"
        ) or {"body": {"cursor": 0}})["body"]["cursor"] == saved["inbox_cursor"]
        assert reopened.store.get("mail_request", saved["peer_request_id"])["body"]["status"] == "awaiting_review"
        requester = reopened._control(run.run_id, "agent-synthesis")
        reopened._update_control(requester, phase="ready_model", waiting_reason=None,
                                 retry_at=None)
        _drive_until(reopened, run.run_id, lambda: (
            saved.get("final_sent") is True or "transport_error" in saved
        ))
        assert "transport_error" not in saved, saved.get("transport_error")
        _drive_until(reopened, run.run_id, lambda: (
            reopened._control(run.run_id, "agent-synthesis")["body"]["phase"] == "done"
        ))
        for call_ref, raw_bytes in old_capture:
            intent = reopened.store.get(
                "model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}"
            )
            assert intent is not None
            assert reopened.store.read_blob(intent["body"]["blob"]["sha256"]) == raw_bytes
            assert intent["body"]["request_sha256"] == hashlib.sha256(raw_bytes).hexdigest()
        requester = reopened._control(run.run_id, "agent-synthesis")
        assert requester["body"]["report_ref"]
        assert reopened.store.get(**requester["body"]["report_ref"])
        assert saved["peer_visibility"] == "accepted_text_available"
        assert reopened.store.get("mail_request", saved["peer_request_id"])["body"]["accepted_answer_ref"] == saved["answer_ref"]
        inclusion = next(item for item in reopened.store.records("context_inclusion")
                         if item["body"]["call_ref"] == original_request_bytes[len(old_capture)][0])
        assert inclusion["body"]["inbox_cursor"] == saved["inbox_cursor"]
        assert saved["peer_report_ref"] in [
            item["ref"] for item in json.loads(requests[len(old_capture)][1]["messages"][-1]["content"])[
                "shared_materials"
            ]["entries"]
        ]
    finally:
        if reopened is not None:
            reopened.close()
        else:
            host.close()
