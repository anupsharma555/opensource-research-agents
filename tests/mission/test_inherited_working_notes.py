"""Accepted public-source follow-ups can keep scoped, source-linked draft notes."""

from __future__ import annotations

import hashlib
import json

import pytest

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import WorkingContext
from ora.mission.memory import MemoryError, WorkingNotes
from ora.state import StateStore
from ora.state.source_lineage import scoped_note_source_input
from ora.state.store import canonical
from tests.mission.test_service import MODEL, Response, drain, service, tool

URL = "https://example.org/synthetic-note-guide"
HTML = (b"<html><title>Synthetic guide</title><main><h1>Synthetic guide</h1>"
        b"<p>A checked sentence supports a private draft note.</p></main></html>")
NOTE_TEXT = "SYNTHETIC_INHERITED_NOTE A checked sentence supports a private draft note."


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
    raise AssertionError(f"{key} missing from model input")


def test_inherited_checked_copy_passage_and_window_notes_recheck_current_use(
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
                "choices": [{"message": {"role": "assistant", "content": URL},
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
        if turn == 7:
            return tool("request_capability", {"tool_id": "write_note"}, turn)
        if turn == 8:
            assert "write_note" in {
                item["function"]["name"] for item in packet["tools"]
            }
            return tool("write_note", {
                "note_id": "inherited-loop-note", "category": "note",
                "text": f"{NOTE_TEXT} loop", "expected_version": 0,
            }, turn)
        if turn == 9:
            saved_result = next(
                json.loads(item["content"]) for item in packet["messages"]
                if item.get("role") == "tool" and item.get("name") == "write_note"
            )
            assert saved_result["status"] == "saved"
            assert f"{NOTE_TEXT} loop" in canonical(packet["messages"])
            return tool("request_capability", {"tool_id": "read_note"}, turn)
        if turn == 10:
            assert "read_note" in {
                item["function"]["name"] for item in packet["tools"]
            }
            return tool("read_note", {"note_id": "inherited-loop-note"}, turn)
        if turn == 11:
            read_result = next(
                json.loads(item["content"]) for item in packet["messages"]
                if item.get("role") == "tool" and item.get("name") == "read_note"
            )
            assert read_result["status"] == "found"
            assert read_result["note"]["body"]["text"] == f"{NOTE_TEXT} loop"
        else:
            assert turn == 5
        passage = next(
            item for item in host.store.records("source_passage")
            if item["body"].get("copy_ref") == selected["copy_ref"]
            and item["body"].get("inspection_state") == "checked"
        )
        return tool("publish_result", {
            "title": f"Synthetic report {turn}",
            "text": "The checked page supports this offline sentence.",
            "citations": [{"source_id": selected["copy_ref"]["id"],
                           "span_id": passage["body"]["span_id"]}],
            "limitations": ["Synthetic offline source"],
        }, turn)

    monkeypatch.setattr(service_module, "fetch_public_source", fetch)
    host = service(tmp_path, monkeypatch, transport)
    try:
        parent = host.start(LiveStartRequest(
            request_id="note-public-parent", question="Read the synthetic guide.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, parent.run_id, limit=40)
        view = host.state(parent.run_id).snapshot
        assert view.mission.status == "completed" and view.artifacts
        artifact = view.artifacts[0]
        child = host.followup(LiveFollowupRequest(
            request_id="note-public-child", parent_run_id=parent.run_id,
            parent_snapshot_id=view.snapshot_id, target_id=artifact.id,
            target_version=artifact.version,
            question="Keep a checked private note from this source.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, child.run_id, limit=40)
        assert len(requests) == 11
        control = host._control(child.run_id, "agent-synthesis")
        body = control["body"]
        copy_ref = selected["copy_ref"]
        window_ref = body["read_window_refs"][0]
        passage_ref = host.store.get(**window_ref)["body"]["passage_refs"][0]
        assert host.store.get(**copy_ref)["body"]["mission_id"] != body["mission_id"]
        assert host.store.get(**window_ref)["body"].get("mission_id") is None
        assert scoped_note_source_input(
            host.store, mission_id=body["mission_id"], run_id=child.run_id,
            actor_id=body["actor_id"], task_id=body["task_id"], pointer=window_ref,
        ) is not None
        loop_note = host.store.get("working_note", "inherited-loop-note")
        assert loop_note is not None and loop_note["body"]["input_refs"] == [copy_ref]
        loop_result = next(
            item for item in host.store.records("context_tool_result")
            if item["body"].get("mission_id") == body["mission_id"]
            and json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))
            .get("name") == "write_note"
        )
        assert json.loads(json.loads(host.store.read_blob(
            loop_result["body"]["blob"]["sha256"]
        ))["content"])["status"] == "saved"

        notes = WorkingNotes(host.store)
        note_refs = []
        task_ref = ref(host.store.get("board_proposal", body["task_id"]))
        for name, pointer in (
            ("copy", copy_ref), ("passage", passage_ref), ("window", window_ref),
        ):
            saved = notes.write(
                f"inherited-note-{name}", mission_id=body["mission_id"],
                actor_id=body["actor_id"], task_ref=task_ref,
                note_id=f"inherited-note-{name}", category="note",
                text=f"{NOTE_TEXT} {name}", input_refs=[pointer],
            )
            note_refs.append(saved)
            assert notes.read(
                mission_id=body["mission_id"], actor_id=body["actor_id"],
                task_id=body["task_id"], note_id=saved["id"],
            )["body"]["text"].endswith(name)
        assert len(notes.search(
            mission_id=body["mission_id"], actor_id=body["actor_id"],
            task_id=body["task_id"], query="SYNTHETIC_INHERITED_NOTE",
        )) == 4
        restarted = WorkingNotes(StateStore(tmp_path))
        assert restarted.read(
            mission_id=body["mission_id"], actor_id=body["actor_id"],
            task_id=body["task_id"], note_id=note_refs[2]["id"],
        )["body"]["text"].endswith("window")

        call = next(
            item for item in host.store.records("agent_call")
            if item["body"].get("mission_id") == body["mission_id"]
        )
        pack = host.context.assemble(
            ref(call), task_ref, note_refs=note_refs,
        )
        assert NOTE_TEXT in canonical(pack.messages)
        assert {canonical(item) for item in pack.note_text_refs} == {
            canonical(item) for item in note_refs
        }

        def change_selection(tx):
            current = tx.get("mission_task_control", control["id"])
            return ref(tx.put("mission_task_control", current["id"], {
                **current["body"], "selected_passage_refs": {},
            }, expected_version=current["version"]))

        host.store.transact(
            "note-change-selection", {}, change_selection,
            mission_id=body["mission_id"],
        )
        assert notes.read(
            mission_id=body["mission_id"], actor_id=body["actor_id"],
            task_id=body["task_id"], note_id=note_refs[1]["id"],
        )["body"]["text"].endswith("passage")

        with pytest.raises(MemoryError):
            notes.read(mission_id=body["mission_id"], actor_id="wrong-actor",
                       task_id=body["task_id"], note_id=note_refs[0]["id"])
        assert scoped_note_source_input(
            host.store, mission_id=body["mission_id"], run_id=child.run_id,
            actor_id="wrong-actor", task_id=body["task_id"], pointer=window_ref,
        ) is None
        assert scoped_note_source_input(
            host.store, mission_id=body["mission_id"], run_id=child.run_id,
            actor_id=body["actor_id"], task_id="wrong-task", pointer=window_ref,
        ) is None

        def forged_sources(tx):
            original = tx.get(**copy_ref)
            foreign = tx.put("source_copy", "unaccepted-note-copy", original["body"])
            orphan = tx.put("source_passage", "unlinked-inherited-passage", {
                "copy_ref": copy_ref, "inspection_state": "checked",
                "text": "Unlinked synthetic passage",
            })
            altered = tx.put("source_read_window", "mismatched-note-window", {
                **tx.get(**window_ref)["body"], "content_sha256": "0" * 64,
            })
            saved_window = tx.get(**window_ref)
            tx.link(altered, tx.get(**copy_ref), "depends_on")
            tx.link(altered, tx.get(**saved_window["body"]["source_use_ref"]), "depends_on")
            tx.link(altered, tx.get(**saved_window["body"]["quality_ref"]), "depends_on")
            for included in saved_window["body"]["passage_refs"]:
                tx.link(altered, tx.get(**included), "includes_passage")
            return {"foreign": ref(foreign), "orphan": ref(orphan),
                    "altered": ref(altered)}

        forged = host.store.transact(
            "note-forged-sources", {}, forged_sources,
            mission_id=body["mission_id"],
        )
        foreign_ref, orphan_ref, altered_ref = (
            forged["foreign"], forged["orphan"], forged["altered"]
        )

        def add_altered_window(tx):
            current = tx.get("mission_task_control", control["id"])
            return ref(tx.put("mission_task_control", current["id"], {
                **current["body"],
                "read_window_refs": [*current["body"]["read_window_refs"], altered_ref],
            }, expected_version=current["version"]))

        host.store.transact(
            "note-add-altered-window", {}, add_altered_window,
            mission_id=body["mission_id"],
        )
        for name, pointer in (
            ("foreign", foreign_ref), ("orphan", orphan_ref),
            ("altered", altered_ref),
        ):
            with pytest.raises(MemoryError, match="checked task sources"):
                notes.write(
                    f"reject-note-{name}", mission_id=body["mission_id"],
                    actor_id=body["actor_id"], task_ref=task_ref,
                    note_id=f"reject-note-{name}", category="note",
                    text="Unsupported synthetic input", input_refs=[pointer],
                )

        use = host.store.resolve(
            "source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}",
        )

        def revoke(tx):
            return ref(tx.put("source_use", use["id"], {
                **use["body"], "policy_status": "revoked", "allowed_uses": [],
            }, expected_version=use["version"]))

        host.store.transact(
            "note-revoke-source", {}, revoke,
            mission_id=host._run(parent.run_id)["body"]["mission_id"],
        )
        for pointer in note_refs:
            hidden = restarted.read(
                mission_id=body["mission_id"], actor_id=body["actor_id"],
                task_id=body["task_id"], note_id=pointer["id"],
            )
            assert "text" not in hidden["body"]
            assert hidden["body"]["unavailable_source_refs"] == [copy_ref]
        assert notes.search(
            mission_id=body["mission_id"], actor_id=body["actor_id"],
            task_id=body["task_id"], query="SYNTHETIC_INHERITED_NOTE",
        ) == []
        assert host.context.restricted_working_notes(note_refs)
        prepared = next(
            item for item in host.store.records("model_request_intent")
            if item["body"].get("mission_id") == body["mission_id"]
            and NOTE_TEXT.encode() in host.store.read_blob(
                item["body"]["blob"]["sha256"]
            )
        )
        restrictions = host.context.prepared_working_note_restrictions(ref(prepared))
        assert restrictions and restrictions[0]["unavailable_source_refs"] == [copy_ref]
        read_response = next(
            item for item in host.store.records("model_response_receipt")
            if item["body"].get("mission_id") == body["mission_id"]
            and (result := host.store.get(
                "context_tool_result", f"tool-result:{item['id']}"
            )) is not None
            and json.loads(host.store.read_blob(result["body"]["blob"]["sha256"]))
            .get("name") == "read_note"
        )
        replayed_read, replay_receipt = host.context._tool_exchange(ref(read_response))
        assert NOTE_TEXT not in canonical(replayed_read)
        assert replay_receipt["status"] == "working_note_current_use_rechecked"
        replay = host.context.assemble(
            ref(call), task_ref, note_refs=note_refs,
        )
        assert NOTE_TEXT not in canonical(replay.messages)

        def remove_source(tx):
            current = tx.get("mission_task_control", control["id"])
            return ref(tx.put("mission_task_control", current["id"], {
                **current["body"], "source_refs": [],
            }, expected_version=current["version"]))

        host.store.transact(
            "note-remove-task-source", {}, remove_source,
            mission_id=body["mission_id"],
        )
        assert not WorkingContext(host.store).working_note_text_access(
            host.store.get(**note_refs[2])
        )["dependency_lineage_complete"]
    finally:
        host.close()
