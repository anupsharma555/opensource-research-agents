"""Current-use checks for source-linked draft notes across model and tool turns."""

from __future__ import annotations

import json

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import WorkingContext
from ora.mission.memory import WorkingNotes
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import fixture
from tests.mission.test_service import answer, service

PASSAGE = "Synthetic passage that must not reenter a model after source-use revocation."


def source_note(store: StateStore, task_ref: dict, *, actor_id: str = "peer") -> dict:
    def save(tx):
        copy = tx.put("source_copy", "note-copy", {"mission_id": "m", "rights_status": "permitted"})
        passage = tx.put("source_passage", "note-passage", {
            "mission_id": "m", "copy_ref": ref(copy), "text": PASSAGE,
        })
        window = tx.put("source_read_window", "note-window", {
            "mission_id": "m", "copy_ref": ref(copy), "passage_refs": [ref(passage)],
        })
        use = tx.put("source_use", "note-use", {
            "copy_ref": ref(copy), "allowed_uses": ["model_input"],
            "policy_status": "granted",
        })
        tx.bind("source_use.copy", "note-copy:1", "source_use", use["id"])
        return {"copy": ref(copy), "passage": ref(passage), "window": ref(window),
                "use": ref(use)}

    saved = store.transact("note-source", {}, save, mission_id="m")
    notes = WorkingNotes(store)
    saved["note"] = notes.write(
        "source-linked-note", mission_id="m", actor_id=actor_id, task_ref=task_ref,
        note_id="source-linked-note", category="hypothesis", text=PASSAGE,
        input_refs=[saved["passage"]],
    )
    saved["safe_note"] = notes.write(
        "source-free-note", mission_id="m", actor_id=actor_id, task_ref=task_ref,
        note_id="source-free-note", category="next_action",
        text="Ask a separate source-free question.",
    )
    return saved


def revoke(store: StateStore, use_ref: dict) -> None:
    def save(tx):
        prior = tx.get(**use_ref)
        return ref(tx.put("source_use", prior["id"], {
            **prior["body"], "allowed_uses": [], "policy_status": "revoked",
        }, expected_version=prior["version"]))

    store.transact("note-use-revoked", {}, save, mission_id="m")


def test_note_read_search_context_and_exact_omission_recheck_source_use(tmp_path) -> None:
    store = StateStore(tmp_path)
    call_ref, task_ref = fixture(store)
    saved = source_note(store, task_ref)
    context = WorkingContext(store)
    notes = WorkingNotes(store)
    allowed = context.assemble(call_ref, task_ref, note_refs=[saved["note"], saved["safe_note"]])
    assert PASSAGE in canonical(allowed.messages)
    assert {item["id"] for item in allowed.note_text_refs} == {
        saved["note"]["id"], saved["safe_note"]["id"],
    }
    assert notes.read(mission_id="m", actor_id="peer", task_id="task",
                      note_id="source-linked-note")["body"]["text"] == PASSAGE

    def inclusion(tx):
        item = tx.put("context_inclusion", "note-omission", {
            "mission_id": "m", "call_ref": call_ref,
            "configuration_ref": ref(store.get("agent_configuration", "config")),
            "task_ref": task_ref, "source_refs": [],
            "omitted": [{"type": "note", "ref": saved["note"], "fetch": saved["note"]}],
        })
        return ref(item)

    inclusion_ref = store.transact("note-omission", {}, inclusion, mission_id="m")
    revoke(store, saved["use"])
    current = context.assemble(call_ref, task_ref, note_refs=[saved["note"], saved["safe_note"]])
    assert PASSAGE not in canonical(current.messages)
    assert current.note_text_refs == (saved["safe_note"],)
    assert "data_unavailable_due_source_use" in canonical(current.messages)
    hidden = notes.read(mission_id="m", actor_id="peer", task_id="task",
                        note_id="source-linked-note")
    assert "text" not in hidden["body"]
    assert hidden["body"]["source_dependency_refs"] == [saved["copy"]]
    assert hidden["body"]["unavailable_source_refs"] == [saved["copy"]]
    assert notes.search(mission_id="m", actor_id="peer", task_id="task", query=PASSAGE) == []
    assert "text" not in notes.search(mission_id="m", actor_id="peer", task_id="task",
                                       query="source-linked-note")[0]["body"]
    assert notes.read(mission_id="m", actor_id="peer", task_id="task",
                      note_id="source-free-note")["body"]["text"] == "Ask a separate source-free question."
    page = context.read_omitted_page(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion_ref, cursor=0, limit=1,
    )
    window = context.read_omitted_record(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion_ref, pointer=saved["note"], offset=0, limit=2000,
    )
    assert PASSAGE not in canonical(page) + canonical(window)
    assert page["items"][0]["summary"]["status"] == "data_unavailable_due_source_use"
    assert json.loads(window["text"])["source_dependency_refs"] == [saved["copy"]]
    assert store.get(**saved["note"])["body"]["text"] == PASSAGE

    def revise(tx):
        copy = tx.put("source_copy", "note-copy", {
            "mission_id": "m", "rights_status": "permitted", "revision": "new permitted copy",
        }, expected_version=1)
        use = tx.put("source_use", "note-use-revised", {
            "copy_ref": ref(copy), "allowed_uses": ["model_input"],
            "policy_status": "granted",
        })
        tx.bind("source_use.copy", "note-copy:2", "source_use", use["id"])
        return ref(copy)

    revised_copy = store.transact("note-revised-copy", {}, revise, mission_id="m")
    revised_note = notes.write(
        "note-revised-from-current-copy", mission_id="m", actor_id="peer",
        task_ref=task_ref, note_id="source-linked-note", category="contradiction",
        text="A new permitted copy changes the candidate view.",
        input_refs=[revised_copy], expected_version=1,
    )
    assert notes.read(mission_id="m", actor_id="peer", task_id="task",
                      note_id="source-linked-note")["body"]["text"].startswith("A new permitted")
    assert "text" not in notes.read(
        mission_id="m", actor_id="peer", task_id="task",
        note_id="source-linked-note", version=1,
    )["body"]
    assert revised_note["version"] == 2


def test_shared_copy_ancestry_through_passage_and_window_is_not_a_cycle(tmp_path) -> None:
    store = StateStore(tmp_path)
    _, task_ref = fixture(store)
    saved = source_note(store, task_ref)
    note_ref = WorkingNotes(store).write(
        "note-with-shared-copy-ancestry", mission_id="m", actor_id="peer",
        task_ref=task_ref, note_id="note-with-shared-copy-ancestry",
        category="note", text="A bounded observation.",
        input_refs=[saved["passage"], saved["window"]],
    )
    access = WorkingContext(store).working_note_text_access(store.get(**note_ref))
    assert access == {
        "status": "available", "source_dependency_refs": [saved["copy"]],
        "unavailable_source_refs": [], "dependency_lineage_complete": True,
    }
    revoke(store, saved["use"])
    hidden = WorkingNotes(store).read(
        mission_id="m", actor_id="peer", task_id="task",
        note_id="note-with-shared-copy-ancestry",
    )
    assert "text" not in hidden["body"]
    assert hidden["body"]["unavailable_source_refs"] == [saved["copy"]]


def test_unresolved_passage_ancestry_and_saved_tool_result_hide_note_text(tmp_path) -> None:
    store = StateStore(tmp_path)
    call_ref, task_ref = fixture(store)
    saved = source_note(store, task_ref)
    context = WorkingContext(store)
    unresolved = store.transact("orphan-passage", {}, lambda tx: ref(tx.put(
        "source_passage", "orphan-passage", {"mission_id": "m", "text": "Unlinked text"},
    )), mission_id="m")
    orphan_note = WorkingNotes(store).write(
        "orphan-note", mission_id="m", actor_id="peer", task_ref=task_ref,
        note_id="orphan-note", category="note", text="Unlinked text",
        input_refs=[unresolved],
    )
    orphan = WorkingNotes(store).read(
        mission_id="m", actor_id="peer", task_id="task", note_id="orphan-note",
    )
    assert orphan["body"]["dependency_lineage_complete"] is False
    assert "text" not in orphan["body"]
    assert "Unlinked text" not in canonical(context.assemble(
        call_ref, task_ref, note_refs=[orphan_note],
    ).messages)

    prior = store.get(**saved["note"])
    response_blob = store.put_blob(canonical({"tool_calls": [{
        "id": "note-tool", "type": "function",
        "function": {"name": "read_note", "arguments": canonical({"note_id": prior["id"]})},
    }]}).encode())
    response_ref = store.transact("note-response", {}, lambda tx: ref(tx.put(
        "model_response_receipt", "note-response", {
            "mission_id": "m", "task_id": "task", "call_ref": call_ref,
            "status": "tool_requested", "blob": response_blob,
        },
    )), mission_id="m")
    context.save_tool_result(
        "note-result", response_ref=response_ref, tool_call_id="note-tool",
        name="read_note", content=canonical({"status": "found", "note": prior}),
    )
    assert PASSAGE in canonical(context._tool_exchange(response_ref)[0])
    revoke(store, saved["use"])
    exchange, receipt = context._tool_exchange(response_ref)
    assert PASSAGE not in canonical(exchange)
    assert receipt["status"] == "working_note_current_use_rechecked"
    assert json.loads(exchange[1]["content"])["note"]["body"]["unavailable_source_refs"] == [saved["copy"]]
    pack = context.assemble(call_ref, task_ref, tool_turn_refs=[response_ref])
    assert PASSAGE not in canonical(pack.messages)
    assert pack.note_text_refs == ()


def test_policy_change_after_inclusion_blocks_only_affected_dispatch(tmp_path, monkeypatch) -> None:
    sent = []

    def transport(request, *, timeout):
        sent.append(json.loads(request.data))
        return answer("A separate source-free contribution.", len(sent))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="note-pre-dispatch", question="Inspect a synthetic note",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        mission_id, actor_id = control["body"]["mission_id"], control["body"]["actor_id"]

        def save(tx):
            copy = tx.put("source_copy", "dispatch-note-copy", {
                "mission_id": mission_id, "rights_status": "permitted",
            })
            use = tx.put("source_use", "dispatch-note-use", {
                "copy_ref": ref(copy), "allowed_uses": ["model_input"],
                "policy_status": "granted",
            })
            tx.bind("source_use.copy", "dispatch-note-copy:1", "source_use", use["id"])
            return {"copy": ref(copy), "use": ref(use)}

        source = host.store.transact("dispatch-note-source", {}, save, mission_id=mission_id)
        note_ref = host.notes.write(
            "dispatch-note", mission_id=mission_id, actor_id=actor_id,
            task_ref=control["body"]["proposal_ref"], note_id="dispatch-note",
            category="hypothesis", text=PASSAGE, input_refs=[source["copy"]],
        )
        assert host._schedule_model(control)
        pending = host._control(run.run_id, "agent-synthesis")
        work = host._work_for(pending)
        spec = host.executor._spec_from(work)
        intent = host.store.get("model_request_intent", f"model-intent:{spec.call_id}:1")
        inclusion = host.store.get("context_inclusion", f"context:{intent['id']}")
        assert inclusion["body"]["note_text_refs"] == [note_ref]
        assert PASSAGE in host.store.read_blob(intent["body"]["blob"]["sha256"]).decode()
        assert sent == []

        def revoke_current(tx):
            prior = tx.get(**source["use"])
            return tx.put("source_use", prior["id"], {
                **prior["body"], "allowed_uses": [], "policy_status": "revoked",
            }, expected_version=prior["version"])

        host.store.transact("dispatch-note-revoke", {}, revoke_current, mission_id=mission_id)
        blocked = host._model_work(spec)
        assert blocked.disposition == "terminal"
        assert blocked.payload["status"] == "working_note_source_use_restricted"
        assert blocked.payload["external_action"] == "none"
        assert blocked.payload["working_note_diagnostics"][0]["unavailable_source_refs"] == [source["copy"]]
        assert sent == []
        assert host.store.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}") is None
        assert host.store.records("mission_source_block")[0]["body"]["external_effect"] == "not_dispatched"

        other = host.start(LiveStartRequest(
            request_id="unrelated-source-free-note", question="Continue an unrelated task",
            agent_ids=["agent-synthesis"],
        ))
        assert host._schedule_model(host._control(other.run_id, "agent-synthesis"))
        other_work = host._work_for(host._control(other.run_id, "agent-synthesis"))
        assert host._model_work(host.executor._spec_from(other_work)).disposition == "continue_task"
        assert len(sent) == 1
    finally:
        host.close()


def test_note_use_revoked_between_preflight_and_dispatch_mark_has_no_external_effect(
    tmp_path, monkeypatch,
) -> None:
    sent = []

    def transport(request, *, timeout):
        sent.append(json.loads(request.data))
        return answer("Unexpected provider response.", len(sent))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="note-policy-race", question="Inspect a synthetic note",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        mission_id = control["body"]["mission_id"]

        def save(tx):
            copy = tx.put("source_copy", "race-note-copy", {
                "mission_id": mission_id, "rights_status": "permitted",
            })
            use = tx.put("source_use", "race-note-use", {
                "copy_ref": ref(copy), "allowed_uses": ["model_input"],
                "policy_status": "granted",
            })
            tx.bind("source_use.copy", "race-note-copy:1", "source_use", use["id"])
            return {"copy": ref(copy), "use": ref(use)}

        source = host.store.transact("race-note-source", {}, save, mission_id=mission_id)
        host.notes.write(
            "race-note", mission_id=mission_id, actor_id=control["body"]["actor_id"],
            task_ref=control["body"]["proposal_ref"], note_id="race-note",
            category="hypothesis", text=PASSAGE, input_refs=[source["copy"]],
        )
        assert host._schedule_model(control)
        work = host._work_for(host._control(run.run_id, "agent-synthesis"))
        spec = host.executor._spec_from(work)
        intent = host.store.get("model_request_intent", f"model-intent:{spec.call_id}:1")
        assert intent["body"]["context_source_dependency_refs"] == [source["copy"]]

        original_transact = host.session.store.transact
        revoked_at_mark = False

        def mark_with_policy_change(operation_id, request, apply, *, mission_id):
            nonlocal revoked_at_mark
            if operation_id.startswith("model-dispatch-mark:") and not revoked_at_mark:
                revoked_at_mark = True

                def revoke_now(tx):
                    prior = tx.get(**source["use"])
                    return tx.put("source_use", prior["id"], {
                        **prior["body"], "allowed_uses": [], "policy_status": "revoked",
                    }, expected_version=prior["version"])

                original_transact("race-note-revoke", {}, revoke_now, mission_id=mission_id)
            return original_transact(operation_id, request, apply, mission_id=mission_id)

        monkeypatch.setattr(host.session.store, "transact", mark_with_policy_change)
        blocked = host._model_work(spec)
        assert revoked_at_mark
        assert blocked.disposition == "terminal"
        assert blocked.payload["status"] == "working_note_source_use_restricted"
        assert blocked.payload["external_action"] == "none"
        assert blocked.payload["working_note_diagnostics"][0]["unavailable_source_refs"] == [
            source["copy"]
        ]
        assert sent == []
        assert host.store.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}") is None
        assert host.store.records("mission_source_block")[0]["body"]["external_effect"] == "not_dispatched"
    finally:
        host.close()
