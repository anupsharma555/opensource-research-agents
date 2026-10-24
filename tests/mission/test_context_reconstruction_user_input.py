"""Rebuild sent model input after real compaction, peer reply and host reopen."""

from __future__ import annotations

import hashlib
import json
import socket
import time
import traceback

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.retrieval.receipts import record_delivery
from tests.mission.test_service import (
    answer,
    revoke_source,
    serialized_peer_name,
    service,
    tool,
)

OLD_TEXT = "Synthetic draft counted benefit in 12 of 20 records."
LATE_TEXT = (
    "Synthetic later audit counted benefit in 3 of 20 records; "
    "the draft included records from another group."
)
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


def _saved_tool_result(host, name, *, source_id=None):
    for item in host.store.records("context_tool_result"):
        payload = json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))
        if payload.get("name") == name:
            result = json.loads(payload["content"])
            if source_id is None or result.get("copy_ref", {}).get("id") == source_id:
                return result
    raise AssertionError(f"No saved result for {name}")


@pytest.mark.parametrize("revoke_old", [False, True])
def test_repeated_compaction_late_audit_and_pending_peer_reconstruct_after_reopen(
    tmp_path, monkeypatch, revoke_old,
):
    requests = []
    original_request_bytes = []
    turns = {"Sage": 0, "Mxra": 0}
    saved = {}
    current_host = None

    def _transport(request, *, timeout):
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
            assert saved["reader_done"] is True
            assert LATE_TEXT not in json.dumps(body["messages"])
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
                "summary": "The earlier source suggested " + OLD_TEXT + "; seek later evidence."
            }, step)
        if step == 4:
            index = json.loads(body["messages"][-1]["content"])
            assert index["summary"]["text"].endswith("seek later evidence.")
            assert saved["peer_request_id"] in json.dumps(index["request_dispositions"])
            assert "compact_conversation" in {
                item["function"]["name"] for item in body["tools"]
            }
            return tool("compact_conversation", {
                "summary": "Draft count remains unverified: " + OLD_TEXT
            }, step)

        assert saved["reopened"] is True
        index = json.loads(body["messages"][-1]["content"])
        assert index["task"]["objective"] == QUESTION
        dispositions = index.get("request_dispositions", [])
        if dispositions:
            assert any(
                item["ref"]["id"] == saved["peer_request_id"]
                and (
                    item["status"] in {
                        "unanswered", "awaiting_review", "partial_awaiting_review",
                    }
                    or item["status"] == "answered"
                    and item.get("accepted_answer_ref") == saved["answer_ref"]
                    and saved["answer_ref"] in item.get("answer_refs", [])
                )
                for item in dispositions
            )
        else:
            assert any(
                card["request_ref"]["id"] == saved["peer_request_id"]
                and card["request_status"] == "awaiting_review"
                and card["answer_ref"] == saved["answer_ref"]
                and card["report_ref"] == saved["peer_report_ref"]
                and card["report"]["body"] == PEER_TEXT
                and card["action"] == "review_peer_answer"
                and card["report_delivery"] == "complete_in_input"
                for card in index.get("peer_answer_previews", [])
            )
        if revoke_old:
            assert OLD_TEXT not in json.dumps(index.get("summary"))
        else:
            assert OLD_TEXT in index["summary"]["text"]
        serialized = json.dumps(body["messages"])
        assert SCOPE in serialized
        assert saved["message_ref"]["id"] in serialized
        assert saved["hit_ref"]["id"] in serialized
        if revoke_old:
            assert OLD_TEXT not in serialized
            assert all(item["id"] != saved["old_source_ref"]["id"]
                       for item in json.loads(body["messages"][1]["content"]).get(
                           "source_packets", []
                       ))
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
        if not saved.get("hit_inspected"):
            assert LATE_TEXT not in serialized
            assert not index.get("request_answer_refs", [])
            assert current_host.store.get("mail_request", saved["peer_request_id"])[
                "body"
            ]["status"] == "unanswered"
            assert "inspect_hit" in attached
            saved["hit_inspected"] = True
            return tool("inspect_hit", {"hit_id": saved["hit_ref"]["id"]}, step)
        if not saved.get("reader_done"):
            visible_source = None
            try:
                inspected = _tool_result(body, "inspect_hit")
            except StopIteration:
                # Revoking the old source withholds the whole earlier tool exchange;
                # the saved inspection still has an exact new-copy ref and the
                # permitted new source is independently visible in this input.
                assert revoke_old
                inspected = _saved_tool_result(current_host, "inspect_hit")
                candidates = json.loads(body["messages"][1]["content"])["source_packets"]
                visible_source = next(
                    item for item in candidates
                    if item["id"] == inspected["source_id"]
                    and LATE_TEXT in item["text"]
                    and item["text_basis"] == "checked_passages"
                )
                saved["inspect_withheld"] = True
            assert inspected["status"] == "inspected"
            saved["new_source_ref"] = {
                "kind": "source_copy", "id": (
                    visible_source["id"] if visible_source else inspected["source_id"]
                ), "version": (
                    visible_source["version"] if visible_source else inspected["source_version"]
                ),
            }
            assert saved["new_source_ref"]["id"] == inspected["source_id"]
            assert saved["new_source_ref"]["version"] == inspected["source_version"]
            assert LATE_TEXT in serialized
            source_packets = json.loads(body["messages"][1]["content"]).get(
                "source_packets", []
            )
            assert any(item["id"] == inspected["source_id"] for item in source_packets)
            assert saved["new_source_ref"] in current_host._control(
                saved["run_id"], "agent-synthesis"
            )["body"]["source_refs"]
            assert not index.get("request_answer_refs", [])
            assert "read_source_window" in attached
            saved["reader_done"] = True
            return tool("read_source_window", {
                "source_id": saved["new_source_ref"]["id"],
                "source_version": saved["new_source_ref"]["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": len(LATE_TEXT),
                "selection_mode": "replace",
            }, step)
        if not saved.get("late_window_checked"):
            window_visible = True
            try:
                window = _tool_result(body, "read_source_window")
            except StopIteration:
                assert revoke_old
                window_visible = False
                window = _saved_tool_result(
                    current_host, "read_source_window",
                    source_id=saved["new_source_ref"]["id"],
                )
                assert "data_unavailable_due_source_use" in serialized
                source_packets = json.loads(body["messages"][1]["content"])[
                    "source_packets"
                ]
                assert any(
                    item["id"] == saved["new_source_ref"]["id"]
                    and LATE_TEXT in item["text"]
                    and item["text_basis"] == "checked_passages"
                    for item in source_packets
                )
                saved["read_withheld"] = True
            assert window["copy_ref"] == saved["new_source_ref"]
            if window_visible:
                assert window["source_text_status"] == "exact_checked_passages_in_this_model_input"
            assert current_host.store.get(**window["passages"][0]["passage_ref"])[
                "body"
            ]["text"] == LATE_TEXT
            saved["late_window_checked"] = True
        assert LATE_TEXT in serialized
        assert (
            saved["answer_ref"] in index.get("request_answer_refs", [])
            or any(
                card["request_ref"]["id"] == saved["peer_request_id"]
                and card["answer_ref"] == saved["answer_ref"]
                and card["report_ref"] == saved["peer_report_ref"]
                and card["report"]["body"] == PEER_TEXT
                and card["action"] == "review_peer_answer"
                and card["report_delivery"] == "complete_in_input"
                for card in index.get("peer_answer_previews", [])
            )
        )
        if revoke_old and not saved.get("old_read_submitted"):
            if "read_source_window" not in attached:
                assert "request_capability" in attached
                assert not saved.get("old_reader_requested")
                saved["old_reader_requested"] = True
                return tool("request_capability", {"tool_id": "read_source_window"}, step)
            saved["old_read_submitted"] = True
            return tool("read_source_window", {
                "source_id": saved["old_source_ref"]["id"],
                "source_version": saved["old_source_ref"]["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 500,
            }, step)
        if revoke_old and not saved.get("old_rejection_checked"):
            old_read = _saved_tool_result(
                current_host, "read_source_window",
                source_id=saved["old_source_ref"]["id"],
            )
            assert old_read["copy_ref"] == saved["old_source_ref"]
            assert old_read["passages"]
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
        return answer("The later audit contradicts the draft; the peer report limits the conclusion.", step)

    def transport(request, *, timeout):
        try:
            return _transport(request, timeout=timeout)
        except BaseException as exc:
            saved.setdefault("transport_errors", []).append("".join(
                traceback.format_exception(exc)
            ))
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
            turns["Sage"] == 4
            and bool(host._control(run.run_id, "agent-synthesis")["body"].get("conversation_summary_ref"))
            and host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
        ))
        assert turns["Mxra"] == 0
        requester = host._control(run.run_id, "agent-synthesis")
        saved["old_summary_ref"] = requester["body"]["conversation_summary_ref"]
        summaries = host.store.records("context_summary")
        assert len(summaries) == 2
        assert all(host.store.get(**item["body"]["archive_ref"]) for item in summaries)
        assert len({item["body"]["archive_ref"]["id"] for item in summaries}) == 2
        saved["old_source_ref"] = requester["body"]["source_refs"][0]
        mission_id = run_before["mission_id"]
        actor_id = requester["body"]["actor_id"]
        task = host.store.get("board_proposal", requester["body"]["task_id"])
        delivery = record_delivery(host.store, {
            "mission_id": mission_id, "task_id": task["id"],
            "agent_instance_id": actor_id, "request_id": "synthetic-late-audit",
            "attempt_id": "synthetic-late-audit-1", "route": "synthetic-saved-index",
            "provider": "synthetic-fixture", "operation": "search",
            "requested_at": "2026-01-02T00:00:00Z",
            "requested_window": {"offset": 0, "limit": 1},
            "query": "synthetic later audit",
        }, {
            "status": "ok", "count": 1, "offset": 0, "items": [{
                "id": "synthetic-audit-item", "url": "https://example.org/synthetic-audit",
                "title": "Synthetic later audit", "content": LATE_TEXT,
                "surface": "abstract", "origin": "original_source",
                "rights_status": "permitted", "access_status": "available",
            }],
        }, delivery_id="synthetic-late-audit-delivery", adapter="count_offset",
            retrieved_at="2026-01-02T00:01:00Z")
        saved["hit_ref"] = delivery["accepted_hit_refs"][0]
        message = host.mailbox.send_message(
            "synthetic-late-audit-send", mission_id=mission_id,
            message_id="synthetic-late-audit-notice", sender_id=actor_id,
            recipient_id=actor_id, correlation_id="synthetic-late-audit",
            payload={"type": "evidence_update", "status": "new_candidate_for_inspection"},
            task_ref=ref(task), evidence_refs=[saved["hit_ref"]],
        )
        saved["message_ref"] = message["message_ref"]
        requester = host._control(run.run_id, "agent-synthesis")
        host._update_control(requester, phase="retry_at", retry_at=time.time() + 3600,
                             waiting_reason="Fixture pauses before reopen")
        request_record = host.store.get("mail_request", saved["peer_request_id"])
        assert request_record["body"]["status"] == "unanswered"
        assert request_record["body"]["answer_refs"] == []
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
        assert reopened.store.get("mail_request", saved["peer_request_id"])["body"]["status"] == "unanswered"
        requester = reopened._control(run.run_id, "agent-synthesis")
        reopened._update_control(requester, phase="ready_model", waiting_reason=None,
                                 retry_at=None)
        try:
            _drive_until(reopened, run.run_id, lambda: (
                saved.get("reader_done") is True
                and any(item["body"].get("response_ref") for item in reopened.store.records(
                    "context_tool_result") if json.loads(reopened.store.read_blob(
                        item["body"]["blob"]["sha256"]
                    )).get("name") == "read_source_window")
            ))
        except AssertionError as exc:
            raise AssertionError(
                f"{exc}; transport_errors={saved.get('transport_errors')}; "
                f"phase={reopened._control(run.run_id, 'agent-synthesis')['body']['phase']}"
            ) from exc
        requester = reopened._control(run.run_id, "agent-synthesis")
        reopened._update_control(requester, phase="retry_at", retry_at=time.time() + 3600,
                                 waiting_reason="Fixture waits for independent peer")
        recipient = reopened._control(run.run_id, "agent-critique")
        reopened._update_control(recipient, phase="ready_model", waiting_reason=None,
                                 retry_at=None, source_refs=[])
        _drive_until(reopened, run.run_id, lambda: bool(
            reopened.store.get("mail_request", saved["peer_request_id"])["body"]["answer_refs"]
        ))
        request_record = reopened.store.get("mail_request", saved["peer_request_id"])
        assert request_record["body"]["status"] == "awaiting_review"
        saved["answer_ref"] = request_record["body"]["answer_refs"][0]
        saved["peer_report_ref"] = reopened.store.get(**saved["answer_ref"])["body"]["artifact_ref"]
        requester = reopened._control(run.run_id, "agent-synthesis")
        reopened._update_control(requester, phase="ready_model", waiting_reason=None,
                                 retry_at=None)
        try:
            _drive_until(reopened, run.run_id, lambda: saved.get("final_sent") is True)
        except AssertionError as exc:
            raise AssertionError(
                f"{exc}; first_transport_error={saved.get('transport_errors', [None])[0]}; "
                f"phase={reopened._control(run.run_id, 'agent-synthesis')['body']['phase']}"
            ) from exc
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
        assert bool(saved.get("inspect_withheld")) is revoke_old
        assert bool(saved.get("read_withheld")) is revoke_old
        assert not saved.get("transport_errors")
        receipts = reopened.store.records("model_response_receipt")
        assert len(receipts) == len(requests)
        assert {item["body"]["status"] for item in receipts} == {
            "complete", "tool_requested",
        }
        assert sum(item["body"]["usage"]["total_tokens"] for item in receipts) == (
            11 * len(requests)
        )  # Synthetic fixture usage, not provider capacity or a live estimate.
        assert len(reopened.store.records("mail_request")) == 1
        inclusion = next(item for item in reopened.store.records("context_inclusion")
                         if item["body"]["call_ref"] == original_request_bytes[len(old_capture)][0])
        assert inclusion["body"]["inbox_cursor"] == saved["inbox_cursor"]
        assert any(
            saved["peer_report_ref"] in [
                item["ref"] for item in json.loads(body["messages"][-1]["content"])
                .get("shared_materials", {}).get("entries", [])
            ]
            for role, body in requests[len(old_capture):] if role == "Sage"
        )
    finally:
        if reopened is not None:
            reopened.close()
        else:
            host.close()
