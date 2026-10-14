"""The model's saved continuation summary replaces prior visible context, with scoped recovery."""

from __future__ import annotations

import json
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import ContextError, WorkingContext
from ora.mission.conversation import read_history, save_compaction
from ora.mission.memory import WorkingNotes
from ora.state import StateStore
from tests.mission.test_context import fixture
from tests.mission.test_note_source_use import revoke, source_note
from tests.mission.test_service import drain, service, tool
from tests.mission.test_tool_result_paging import save_result


def test_model_compaction_changes_next_input_and_pages_original_turns(tmp_path, monkeypatch):
    requests = []
    phrase = "A visible old tool request that must remain recoverable"
    note_text = "SYNTHETIC_ARCHIVED_NOTE " + "n" * 6000
    archive_id = None

    def transport(request, *, timeout):
        nonlocal archive_id
        body = json.loads(request.data)
        requests.append(body)
        number = len(requests)
        if number == 1:
            return tool("request_capability", {"tool_id": "compact_conversation"}, number)
        if number == 2:
            assert any(item["function"]["name"] == "compact_conversation" for item in body["tools"])
            assert note_text in json.dumps(body["messages"])
            return tool("compact_conversation", {
                "summary": f"Continue the task; retain contrary evidence and source IDs. {phrase}"
            }, number)
        if number == 3:
            index = json.loads(body["messages"][-1]["content"])
            assert index["summary"]["text"].endswith(phrase)
            assert index["task"]["objective"]
            assert note_text not in json.dumps(body["messages"])
            assert len(json.dumps(body["messages"])) < len(json.dumps(requests[1]["messages"])) - 3000
            archive_id = index["summary"]["archive_ref"]["id"]
            assert not any(
                item.get("role") == "assistant"
                and "request_capability" in json.dumps(item)
                for item in body["messages"]
            )
            return tool("read_conversation_history", {
                "archive_id": archive_id, "cursor": 0, "limit": 1800,
                "projection_sha256": "",
            }, number)
        assert number == 4
        page = next(json.loads(item["content"]) for item in body["messages"]
                    if item.get("role") == "tool"
                    and item.get("name") == "read_conversation_history")
        assert "request_capability" in page["content"]
        assert "PRIVATE_REASONING" not in json.dumps(body["messages"])
        return tool("publish_result", {"title": "Synthetic result", "text": "Scoped result",
                                      "citations": [], "limitations": []}, number)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="conversation-compaction", question="Assess a synthetic task",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        task = host.store.get("board_proposal", control["body"]["task_id"])
        WorkingNotes(host.store).write(
            "synthetic-conversation-note", mission_id=control["body"]["mission_id"],
            actor_id=control["body"]["actor_id"], task_ref=ref(task),
            note_id="synthetic-conversation-note", category="note", text=note_text,
        )
        drain(host, run.run_id)
        assert len(requests) == 4  # No hidden provider call.
        control = host._control(run.run_id, "agent-synthesis")
        summary = host.store.get(**control["body"]["conversation_summary_ref"])
        assert summary["body"]["text"].endswith(phrase)
        assert host.store.get("conversation_archive", archive_id, 1)
        restarted = WorkingContext(StateStore(tmp_path))
        page = read_history(restarted, archive_id=archive_id,
                            mission_id=control["body"]["mission_id"],
                            actor_id=control["body"]["actor_id"],
                            task_id=control["body"]["task_id"], cursor=0,
                            limit=1800, projection_sha256="")
        assert "request_capability" in page["content"]
        with pytest.raises(ContextError):
            read_history(restarted, archive_id=archive_id,
                         mission_id=control["body"]["mission_id"],
                         actor_id="foreign", task_id=control["body"]["task_id"],
                         cursor=0, limit=100, projection_sha256="")
    finally:
        host.close()


def test_source_free_window_archive_rechecks_revocation_and_scope(tmp_path):
    store = StateStore(tmp_path)
    call_ref, task_ref = fixture(store)
    saved = source_note(store, task_ref)
    window_response, _ = save_result(
        store, call_ref, "read_source_window",
        {"window_ref": saved["window"], "copy_ref": saved["copy"],
         "passage_refs": [saved["passage"]], "next_cursor": {}},
        suffix="source-free-window",
    )
    compact_response, _ = save_result(
        store, call_ref, "compact_conversation", {"status": "saved"},
        arguments={"summary": "Synthetic passage was inspected."}, suffix="compact",
    )

    def setup(tx):
        tx.put("model_request_intent", f"model-intent:{call_ref['id']}:1", {
            "mission_id": "m", "call_ref": call_ref,
            "source_refs": [], "context_source_dependency_refs": [],
        })
        control = tx.put("mission_task_control", "compaction-control", {
            "mission_id": "m", "actor_id": "peer", "task_id": "task",
            "response_ref": compact_response, "tool_turn_refs": [window_response],
        })
        return ref(control)

    control_ref = store.transact("compaction-setup", {}, setup, mission_id="m")
    context = WorkingContext(store)
    compacted = save_compaction(
        context, control=store.get(**control_ref), response_ref=compact_response,
        summary="Synthetic passage was inspected.", operation_id="compact-synthetic",
    )
    assert saved["copy"] in store.get(**compacted["archive_ref"])["body"]["source_dependency_refs"]
    page = read_history(context, archive_id=compacted["archive_ref"]["id"],
                        mission_id="m", actor_id="peer", task_id="task", cursor=0,
                        limit=1800, projection_sha256="")
    assert "read_source_window" in page["content"]
    later_note = WorkingNotes(store).write(
        "later-note", mission_id="m", actor_id="peer", task_ref=task_ref,
        note_id="later-note", category="note", text="NEW_NOTE_AFTER_COMPACTION",
    )
    later_pack = context.assemble(call_ref, task_ref,
                                  summary_ref=compacted["summary_ref"],
                                  note_refs=[later_note])
    assert "NEW_NOTE_AFTER_COMPACTION" in json.dumps(later_pack.messages)
    assert save_compaction(
        WorkingContext(StateStore(tmp_path)), control=store.get(**control_ref),
        response_ref=compact_response, summary="Synthetic passage was inspected.",
        operation_id="compact-synthetic",
    ) == compacted
    with pytest.raises(ContextError):
        read_history(context, archive_id=compacted["archive_ref"]["id"],
                     mission_id="m", actor_id="other", task_id="task", cursor=0,
                     limit=1800, projection_sha256="")
    revoke(store, saved["use"])
    with pytest.raises(ContextError):
        read_history(context, archive_id=compacted["archive_ref"]["id"],
                     mission_id="m", actor_id="peer", task_id="task", cursor=0,
                     limit=1800, projection_sha256="")
    with pytest.raises(ContextError):
        save_compaction(context, control=store.get(**control_ref),
                        response_ref=compact_response,
                        summary="Synthetic passage was inspected.",
                        operation_id="compact-after-revoke")


def test_two_compactions_keep_earlier_archive_and_rejected_read_replays(tmp_path):
    store = StateStore(tmp_path)
    call_ref, task_ref = fixture(store)
    first_response, _ = save_result(store, call_ref, "request_capability",
                                    {"status": "selected_for_next_turn"}, suffix="first")
    compact_one_response, _ = save_result(store, call_ref, "compact_conversation",
                                          {"status": "saved"}, suffix="compact-one")

    def setup(tx):
        intent = tx.put("model_request_intent", f"model-intent:{call_ref['id']}:1", {
            "mission_id": "m", "call_ref": call_ref,
            "source_refs": [], "context_source_dependency_refs": [],
        })
        return ref(intent)

    store.transact("two-compaction-intent", {}, setup, mission_id="m")
    context = WorkingContext(store)
    scope = {"mission_id": "m", "actor_id": "peer", "task_id": "task"}
    control = {"body": {**scope, "response_ref": compact_one_response,
                        "tool_turn_refs": [first_response]}}
    first = save_compaction(context, control=control, response_ref=compact_one_response,
                            summary="First summary", operation_id="first-compaction")
    first_page = read_history(context, archive_id=first["archive_ref"]["id"],
                              **scope, cursor=0, limit=1800, projection_sha256="")
    history_response, _ = save_result(
        store, call_ref, "read_conversation_history", first_page,
        arguments={"archive_id": first["archive_ref"]["id"], "cursor": 0,
                   "limit": 1800, "projection_sha256": ""}, suffix="history",
    )
    compact_two_response, _ = save_result(store, call_ref, "compact_conversation",
                                          {"status": "saved"}, suffix="compact-two")
    control["body"].update({
        "response_ref": compact_two_response,
        "tool_turn_refs": [first_response, compact_one_response, history_response],
        "conversation_summary_ref": first["summary_ref"],
    })
    second = save_compaction(context, control=control, response_ref=compact_two_response,
                             summary="Second summary", operation_id="second-compaction")
    assert store.get(**first["archive_ref"])
    page = read_history(WorkingContext(StateStore(tmp_path)),
                        archive_id=second["archive_ref"]["id"], **scope,
                        cursor=0, limit=100, projection_sha256="")
    with pytest.raises(ContextError, match="projection changed"):
        read_history(context, archive_id=second["archive_ref"]["id"], **scope,
                     cursor=100, limit=100, projection_sha256="0" * 64)
    chunks = [page["content"]]
    while page["next_request"]:
        args = {key: value for key, value in page["next_request"].items() if key != "tool"}
        page = read_history(context, **scope, **args)
        chunks.append(page["content"])
    assert "request_capability" in "".join(chunks)
    assert "read_conversation_history" in "".join(chunks)
    assert "PRIVATE_REASONING" not in "".join(chunks)

    rejected_response, _ = save_result(
        store, call_ref, "read_conversation_history", {"status": "rejected"},
        arguments={"archive_id": "invalid", "cursor": 0, "limit": 100,
                   "projection_sha256": ""}, suffix="rejected-history",
    )
    exchange, _ = context._tool_exchange(rejected_response)
    assert json.loads(exchange[1]["content"])["status"] == "rejected"
    rejected_pack = context.assemble(call_ref, task_ref, tool_turn_refs=[rejected_response])
    assert context.context_source_dependency_refs(rejected_pack) == ()


def test_rejected_compaction_never_replays_revoked_summary_argument(tmp_path):
    store = StateStore(tmp_path)
    call_ref, task_ref = fixture(store)
    saved = source_note(store, task_ref)
    secret = "SYNTHETIC_SOURCE_DERIVED_SUMMARY"
    response_ref, _ = save_result(
        store, call_ref, "compact_conversation", {"status": "rejected"},
        arguments={"summary": secret}, suffix="rejected-compact",
    )

    def intent(tx):
        record = tx.put("model_request_intent", f"model-intent:{call_ref['id']}:1", {
            "mission_id": "m", "call_ref": call_ref, "source_refs": [],
            "context_source_dependency_refs": [saved["copy"]],
        })
        return ref(record)

    store.transact("rejected-compact-intent", {}, intent, mission_id="m")
    context = WorkingContext(store)
    prepared = context.assemble(call_ref, task_ref, tool_turn_refs=[response_ref])
    assert secret in json.dumps(prepared.messages)
    assert saved["copy"] in context.context_source_dependency_refs(prepared)
    revoke(store, saved["use"])
    with pytest.raises(ContextError):
        context.context_source_dependency_refs(prepared)  # Prepared before revocation.
    replayed = context.assemble(call_ref, task_ref, tool_turn_refs=[response_ref])
    assert secret not in json.dumps(replayed.messages)
    assert "data_unavailable_due_source_use" in json.dumps(replayed.messages)


def test_source_free_window_then_revoked_compaction_replays_no_summary(tmp_path):
    store = StateStore(tmp_path)
    call_ref, task_ref = fixture(store)
    saved = source_note(store, task_ref)
    window_response, _ = save_result(
        store, call_ref, "read_source_window",
        {"window_ref": saved["window"], "copy_ref": saved["copy"],
         "passage_refs": [saved["passage"]], "next_cursor": {}},
        suffix="source-free-before-compact",
    )
    context = WorkingContext(store)
    delivered = context.assemble(call_ref, task_ref, tool_turn_refs=[window_response])
    dependencies = context.context_source_dependency_refs(delivered)
    assert saved["copy"] in dependencies
    secret = "SUMMARY_FROM_SOURCE_FREE_WINDOW"
    compact_response, _ = save_result(
        store, call_ref, "compact_conversation", {"status": "rejected"},
        arguments={"summary": secret}, suffix="compact-after-window",
    )

    def save(tx):
        tx.put("model_request_intent", f"model-intent:{call_ref['id']}:1", {
            "mission_id": "m", "call_ref": call_ref, "source_refs": [],
            "context_source_dependency_refs": list(dependencies),
        })
        control = tx.put("mission_task_control", "window-compact-control", {
            "mission_id": "m", "actor_id": "peer", "task_id": "task",
            "response_ref": compact_response, "tool_turn_refs": [window_response],
        })
        return ref(control)

    control_ref = store.transact("window-compact-intent", {}, save, mission_id="m")
    revoke(store, saved["use"])
    with pytest.raises(ContextError):
        save_compaction(context, control=store.get(**control_ref),
                        response_ref=compact_response, summary=secret,
                        operation_id="window-compact-revoked")
    replayed = context.assemble(call_ref, task_ref, tool_turn_refs=[compact_response])
    assert secret not in json.dumps(replayed.messages)
    assert "data_unavailable_due_source_use" in json.dumps(replayed.messages)


def test_summary_dependency_rechecked_after_prepare_before_dispatch(tmp_path, monkeypatch):
    requests = []
    summary_text = "SYNTHETIC_NOTE_DERIVED_SUMMARY"

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        if len(requests) == 1:
            return tool("request_capability", {"tool_id": "compact_conversation"}, 1)
        if len(requests) == 2:
            return tool("compact_conversation", {"summary": summary_text}, 2)
        pytest.fail("Revoked summary must not reach provider dispatch")

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="summary-pre-dispatch", question="Assess synthetic evidence",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        mission_id, actor_id = control["body"]["mission_id"], control["body"]["actor_id"]
        source_text = "Source-derived synthetic note"
        blob = host.store.put_blob(source_text.encode("utf-8"))

        def source(tx):
            publication = tx.put("publication", "summary-dispatch-publication", {
                "title": "Synthetic summary source",
            })
            encounter = tx.put("source_encounter", "summary-dispatch-encounter", {
                "publication": ref(publication), "route": "user_supplied",
                "status": "accepted",
            })
            copy = tx.put("source_copy", "summary-dispatch-copy", {
                "mission_id": mission_id, "rights_status": "permitted",
                "publication": ref(publication), "encounter": ref(encounter),
                "mime_type": "text/plain", "blob": blob,
                "content_sha256": blob["sha256"], "retain_bytes": True,
                "inspection_state": "checked", "validation_record": "synthetic-local-check",
                "passage_ids": [],
            })
            use = tx.put("source_use", "summary-dispatch-use", {
                "copy_ref": ref(copy), "allowed_uses": ["model_input"],
                "policy_status": "granted",
            })
            tx.bind("source_use.copy", "summary-dispatch-copy:1", "source_use", use["id"])
            return {"copy": ref(copy), "use": ref(use)}

        saved = host.store.transact("summary-dispatch-source", {}, source,
                                   mission_id=mission_id)
        host.notes.write(
            "summary-dispatch-note", mission_id=mission_id, actor_id=actor_id,
            task_ref=control["body"]["proposal_ref"], note_id="summary-dispatch-note",
            category="note", text=source_text,
            input_refs=[saved["copy"]],
        )
        for _ in range(400):
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            if (control["body"].get("conversation_summary_ref")
                    and control["body"]["phase"] == "ready_model"):
                break
            time.sleep(.01)
        else:
            pytest.fail("Compaction did not settle before next dispatch")
        assert len(requests) == 2
        assert host._schedule_model(control)
        pending = host._control(run.run_id, "agent-synthesis")
        work = host._work_for(pending)
        spec = host.executor._spec_from(work)
        intent = host.store.get("model_request_intent", f"model-intent:{spec.call_id}:1")
        assert saved["copy"] in intent["body"]["context_source_dependency_refs"]
        assert summary_text in host.store.read_blob(intent["body"]["blob"]["sha256"]).decode()

        def revoke_now(tx):
            prior = tx.get(**saved["use"])
            record = tx.put("source_use", prior["id"], {
                **prior["body"], "allowed_uses": [], "policy_status": "revoked",
            }, expected_version=prior["version"])
            return ref(record)

        host.store.transact("summary-dispatch-revoke", {}, revoke_now,
                            mission_id=mission_id)
        blocked = host._model_work(spec)
        assert blocked.disposition == "terminal"
        assert blocked.payload["status"] == "source_use_restricted"
        assert len(requests) == 2
    finally:
        host.close()
