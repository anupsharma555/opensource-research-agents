"""Accepted public-document follow-ups retain scoped conversation source history."""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import ContextError, WorkingContext
from ora.mission.conversation import read_history, save_compaction
from tests.mission.test_service import MODEL, Response, drain, service, tool

URL = "https://example.org/synthetic-history-guide"
HTML = (b"<html><title>Synthetic guide</title><main><h1>Synthetic guide</h1>"
        b"<p>One checked sentence supports the retained report.</p></main></html>")


def _user_value(packet: dict, key: str) -> dict:
    for message in reversed(packet["messages"]):
        if message.get("role") != "user":
            continue
        try:
            value = json.loads(message["content"])
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and key in value:
            return value[key]
    raise AssertionError(f"{key} missing from model request")


def test_inherited_checked_document_can_compact_and_page_history(
    tmp_path, monkeypatch,
):
    import ora.mission.service as service_module

    requests = []
    selected = {}
    host = None

    def fetch(url, **_kwargs):
        assert url == URL
        return {
            "status": 200, "content": HTML,
            "content_sha256": hashlib.sha256(HTML).hexdigest(),
            "byte_length": len(HTML), "content_type": "text/html",
            "requested_url": url, "final_url": url, "redirect_chain": [],
            "access_basis": "public_https_unauthenticated", "authenticated": False,
            "retrieved_at": "2026-10-13T00:00:00Z",
        }

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        requests.append(packet)
        turn = len(requests)
        if turn == 1:
            return tool("request_capability", {"tool_id": "browser_search"}, turn)
        if turn == 2:
            return Response({
                "id": "synthetic-browser", "model": MODEL,
                "choices": [{"message": {"role": "assistant",
                                         "content": f"Uninspected lead: {URL}"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6,
                          "total_tokens": 11},
            })
        if turn == 3:
            hit_id = _user_value(packet, "browser_search")["hits"][0]["hit_id"]
            return tool("fetch_source", {"hit_id": hit_id}, turn)
        if turn in {4, 6}:
            item = _user_value(packet, "inventory")["items"][0]
            selected["copy_ref"] = item["ref"]
            return tool("read_source_window", {
                "source_id": item["ref"]["id"],
                "source_version": item["ref"]["version"],
                "block_offset": 1, "char_offset": 0, "char_limit": 200,
            }, turn)
        if turn == 5:
            window = host.store.records("source_read_window")[0]
            passage = host.store.get(**window["body"]["passage_refs"][0])
            selected["span_id"] = passage["body"]["span_id"]
            return tool("publish_result", {
                "title": "Synthetic parent report",
                "text": "The retained page contains one checked sentence.",
                "citations": [{"source_id": selected["copy_ref"]["id"],
                               "span_id": selected["span_id"]}],
                "limitations": ["Synthetic offline source"],
            }, turn)
        if turn == 7:
            return tool("request_capability", {"tool_id": "compact_conversation"}, turn)
        if turn == 8:
            return tool("compact_conversation", {
                "summary": "Revisit the exact checked sentence and retained citation.",
            }, turn)
        if turn == 9:
            control = host._control(selected["child_run_id"], "agent-synthesis")
            summary = host.store.get(**control["body"]["conversation_summary_ref"])
            selected["archive_id"] = summary["body"]["archive_ref"]["id"]
            return tool("read_conversation_history", {
                "archive_id": selected["archive_id"], "cursor": 0,
                "limit": 1800, "projection_sha256": "",
            }, turn)
        assert turn == 10
        page = next(
            json.loads(item["content"])
            for item in packet["messages"]
            if item.get("role") == "tool"
            and item.get("name") == "read_conversation_history"
        )
        assert page["schema"] == "ora.conversation_history_page.v1"
        assert page["archive_ref"]["id"] == selected["archive_id"]
        return tool("publish_result", {
            "title": "Synthetic follow-up report",
            "text": "The exact retained page remains available for review.",
            "citations": [{"source_id": selected["copy_ref"]["id"],
                           "span_id": selected["span_id"]}],
            "limitations": ["Synthetic offline source"],
        }, turn)

    monkeypatch.setattr(service_module, "fetch_public_source", fetch)
    host = service(tmp_path, monkeypatch, transport)
    try:
        parent = host.start(LiveStartRequest(
            request_id="history-public-parent", question="Read the synthetic guide.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, parent.run_id, limit=40)
        view = host.state(parent.run_id).snapshot
        assert view.mission.status == "completed" and view.artifacts
        artifact = view.artifacts[0]
        child = host.followup(LiveFollowupRequest(
            request_id="history-public-child", parent_run_id=parent.run_id,
            parent_snapshot_id=view.snapshot_id, target_id=artifact.id,
            target_version=artifact.version,
            question="Review the retained source and summarize reading history.",
            agent_ids=["agent-synthesis"],
        ))
        selected["child_run_id"] = child.run_id
        compacted = None
        for _ in range(500):
            host._drive_run(host._run(child.run_id))
            compacted = next(
                (item for item in host.store.records("context_tool_result")
                 if item["body"].get("mission_id")
                 == host._run(child.run_id)["body"]["mission_id"]
                 and json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))
                 .get("name") == "compact_conversation"),
                None,
            )
            if compacted:
                break
            time.sleep(.005)
        assert compacted is not None
        envelope = json.loads(host.store.read_blob(compacted["body"]["blob"]["sha256"]))
        result = json.loads(envelope["content"])
        assert result["status"] == "saved", result
        drain(host, child.run_id, limit=40)
        assert len(requests) == 10
        control = host._control(child.run_id, "agent-synthesis")
        assert control["body"]["phase"] == "done"
        summary = host.store.get(**control["body"]["conversation_summary_ref"])
        archive = host.store.get(**summary["body"]["archive_ref"])
        assert selected["copy_ref"] in archive["body"]["source_dependency_refs"]
        page = read_history(
            WorkingContext(host.store), archive_id=archive["id"],
            mission_id=control["body"]["mission_id"],
            actor_id=control["body"]["actor_id"],
            task_id=control["body"]["task_id"], cursor=0,
            limit=1800, projection_sha256="",
        )
        assert page["schema"] == "ora.conversation_history_page.v1"

        def unrelated_archive(tx):
            foreign = tx.put("source_copy", "unrelated-conversation-copy", {
                "mission_id": "unrelated-mission", "rights_status": "permitted",
            })
            saved = tx.put("conversation_archive", "unrelated-conversation-archive", {
                **archive["body"],
                "source_dependency_refs": [
                    *archive["body"]["source_dependency_refs"], ref(foreign),
                ],
            })
            return ref(saved)

        unrelated = host.store.transact(
            "unrelated-conversation-archive", {}, unrelated_archive,
            mission_id=control["body"]["mission_id"],
        )
        with pytest.raises(ContextError, match="source use"):
            read_history(
                WorkingContext(host.store), archive_id=unrelated["id"],
                mission_id=control["body"]["mission_id"],
                actor_id=control["body"]["actor_id"],
                task_id=control["body"]["task_id"], cursor=0,
                limit=1800, projection_sha256="",
            )
        use = host.store.resolve(
            "source_use.copy", f"{selected['copy_ref']['id']}:{selected['copy_ref']['version']}",
        )

        def revoke(tx):
            return ref(tx.put("source_use", use["id"], {
                **use["body"], "policy_status": "revoked", "allowed_uses": [],
            }, expected_version=use["version"]))

        host.store.transact("history-public-revoke", {}, revoke,
                            mission_id=host._run(parent.run_id)["body"]["mission_id"])
        with pytest.raises(ContextError, match="source use"):
            read_history(
                WorkingContext(host.store), archive_id=archive["id"],
                mission_id=control["body"]["mission_id"],
                actor_id=control["body"]["actor_id"],
                task_id=control["body"]["task_id"], cursor=0,
                limit=1800, projection_sha256="",
            )
        with pytest.raises(ContextError, match="source use"):
            save_compaction(
                WorkingContext(host.store), control=control,
                response_ref=compacted["body"]["response_ref"],
                summary="Revisit the exact checked sentence and retained citation.",
                operation_id="history-public-replay-after-revoke",
            )

        def remove_inherited_source(tx):
            current = tx.get("mission_task_control", control["id"])
            return ref(tx.put("mission_task_control", current["id"], {
                **current["body"], "source_refs": [],
            }, expected_version=current["version"]))

        host.store.transact(
            "history-public-remove-task-source", {}, remove_inherited_source,
            mission_id=control["body"]["mission_id"],
        )
        with pytest.raises(ContextError, match="source lineage"):
            read_history(
                WorkingContext(host.store), archive_id=archive["id"],
                mission_id=control["body"]["mission_id"],
                actor_id=control["body"]["actor_id"],
                task_id=control["body"]["task_id"], cursor=0,
                limit=1800, projection_sha256="",
            )
    finally:
        host.close()
