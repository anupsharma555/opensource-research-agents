"""Earlier checked note pages survive a later read only with current authorization."""

from __future__ import annotations

import hashlib
import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import ContextError, ContextOverflow, WorkingContext
from ora.mission.memory import WorkingNotes
from ora.models import ModelSession
from ora.models.token_estimate import estimate_request_tokens
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import fixture
from tests.mission.test_note_source_use import revoke, source_note
from tests.mission.test_service import ROOT, service


def note_turns(tmp_path, *, linked_source=False, duplicate_current=False,
               conflicting_tool_id=False):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    source = source_note(store, task) if linked_source else None
    note = WorkingNotes(store).write(
        "paged-working-note", mission_id="m", actor_id="peer", task_ref=task,
        note_id="paged-working-note", category="hypothesis",
        text=("FIRST-PAGE-FACT: 120 synthetic adults. " + "A" * 2200
              + " SECOND-PAGE-FACT: checked comparison. " + "B" * 2200
              + " THIRD-PAGE-FACT: remaining limitation. " + "C" * 1200),
        input_refs=[source["passage"]] if source else [],
    )
    inclusion = store.transact(
        "note-window-inclusion", {},
        lambda tx: ref(tx.put("context_inclusion", "note-window-inclusion", {
            "mission_id": "m", "call_ref": call,
            "configuration_ref": ref(tx.get("agent_configuration", "config")),
            "task_ref": task, "source_refs": [],
            "omitted": [{"type": "note", "ref": note, "fetch": note}],
        })), mission_id="m",
    )
    context = WorkingContext(store)

    def save_read(offset: int, suffix: str, tool_id: str):
        window = context.read_omitted_record(
            mission_id="m", actor_id="peer", task_id="task",
            inclusion_ref=inclusion, pointer=note, offset=offset, limit=2000,
        )
        arguments = {"inclusion_id": inclusion["id"], **note,
                     "offset": offset, "limit": 2000}
        blob = store.put_blob(canonical({"content": None, "tool_calls": [{
            "id": tool_id, "type": "function", "function": {
                "name": "read_context_record", "arguments": canonical(arguments),
            },
        }]}).encode())
        response = store.transact(
            "note-window-response-" + suffix, {},
            lambda tx: ref(tx.put("model_response_receipt", "note-window-response-" + suffix, {
                "mission_id": "m", "task_id": "task", "call_ref": call,
                "status": "tool_requested", "blob": blob,
            })), mission_id="m",
        )
        result = context.save_tool_result(
            "note-window-result-" + suffix, response_ref=response,
            tool_call_id=tool_id, name="read_context_record", content=canonical(window),
        )
        return response, result, window

    first = save_read(0, "first", "note-first")
    later = save_read(
        0 if duplicate_current else 2000, "later",
        "note-first" if conflicting_tool_id else "note-later",
    )
    return store, context, call, task, note, source, first, later


def assemble(saved, *, allowance=20000, context=None):
    _, original, call, task, _, _, first, later = saved
    return (context or original).assemble(
        call, task, tool_turn_refs=[later[0]], archived_tool_turn_refs=[first[0]],
        allowance=allowance, estimate_tokens=len,
    )


def note_receipt(pack):
    return next(item for item in pack.tool_result_compactions
                if item.get("kind") == "retained_working_note_window")


def test_older_exact_note_page_survives_newer_read_and_restart(tmp_path):
    saved = note_turns(tmp_path)
    store, _, _, _, note, _, first, later = saved
    original_blob = store.read_blob(store.get(**first[1])["body"]["blob"]["sha256"])
    pack = assemble(saved, context=WorkingContext(StateStore(tmp_path)))
    pages = [json.loads(message["content"])
             for message in pack.messages if message.get("name") == "read_context_record"]
    assert [page["offset"] for page in pages] == [0, 2000]
    assert pages[0]["text"] == first[2]["text"]
    assert pages[1]["text"] == later[2]["text"]
    assert "FIRST-PAGE-FACT" in pages[0]["text"]
    assert note_receipt(pack) == {
        "kind": "retained_working_note_window", "response_ref": first[0],
        "saved_tool_result_ref": first[1], "note_ref": note,
        "projection_sha256": first[2]["projection_sha256"],
        "offset": 0, "returned_chars": first[2]["returned_chars"],
        "original_content_sha256": hashlib.sha256(
            canonical(first[2]).encode()
        ).hexdigest(), "status": "included_exact_window",
    }
    assert first[0] in pack.included_refs and first[1] in pack.included_refs
    assert not any(item["ref"] == first[0] for item in pack.omitted)
    assert note in pack.note_text_refs
    assert store.read_blob(store.get(**first[1])["body"]["blob"]["sha256"]) == original_blob


def test_same_current_note_page_and_tool_id_collision_do_not_duplicate(tmp_path):
    duplicate = note_turns(tmp_path / "duplicate", duplicate_current=True)
    pack = assemble(duplicate)
    assert note_receipt(pack)["status"] == "same_exact_window_already_in_input"
    assert [message["tool_call_id"] for message in pack.messages
            if message.get("role") == "tool"] == ["note-later"]

    conflict = note_turns(tmp_path / "conflict", conflicting_tool_id=True)
    pack = assemble(conflict)
    assert note_receipt(pack)["status"] == "omitted_due_tool_call_id_conflict"
    assert any(item["ref"] == conflict[6][0]
               and item["reason"] == "working_note_window_tool_call_id_conflict"
               for item in pack.omitted)


def test_tight_allowance_exposes_exact_older_note_omission(tmp_path):
    saved = note_turns(tmp_path)
    for allowance in range(2500, 18000, 100):
        try:
            pack = assemble(saved, allowance=allowance)
        except ContextOverflow:
            continue
        if note_receipt(pack)["status"] == "omitted_for_input_allowance":
            break
    else:
        pytest.fail("Expected a fitting current page without the earlier note page")
    receipt = note_receipt(pack)
    assert pack.estimated_tokens <= allowance
    assert not any(message.get("tool_call_id") == "note-first" for message in pack.messages)
    assert receipt["note_ref"] == saved[4]
    assert any(item["ref"] == saved[6][0]
               and item["reason"] == "working_note_window_omitted_for_input_allowance"
               for item in pack.omitted)


def test_revocation_withholds_all_source_derived_note_pages(tmp_path):
    saved = note_turns(tmp_path, linked_source=True)
    store, context, _, _, note, source, first, _ = saved
    before = assemble(saved)
    assert note in before.note_text_refs
    assert context.context_source_dependency_refs(before) == (source["copy"],)
    revoke(store, source["use"])
    after = assemble(saved)
    assert "FIRST-PAGE-FACT" not in canonical(after.messages)
    assert "SECOND-PAGE-FACT" not in canonical(after.messages)
    assert note_receipt(after)["status"] == "omitted_due_source_use"
    assert any(item["ref"] == first[0]
               and item["reason"] == "working_note_window_source_use_unavailable"
               for item in after.omitted)
    assert after.note_text_refs == ()
    assert context.context_source_dependency_refs(after) == ()


def test_forged_or_other_task_note_window_cannot_enter_context(tmp_path, monkeypatch):
    saved = note_turns(tmp_path)
    store, context, _, _, _, _, first, _ = saved
    with pytest.raises(ContextError, match="outside this peer's task"):
        context._retained_context_window(first[0], scope={
            "mission_id": "m", "actor_id": "other-peer", "task_id": "task",
        })
    digest = store.get(**first[1])["body"]["blob"]["sha256"]
    original = store.read_blob

    def forged(pointer):
        raw = original(pointer)
        if pointer != digest:
            return raw
        payload = json.loads(raw)
        window = json.loads(payload["content"])
        window["text"] = "Forged note text"
        window["returned_chars"] = len(window["text"])
        payload["content"] = canonical(window)
        return canonical(payload).encode()

    monkeypatch.setattr(store, "read_blob", forged)
    with pytest.raises(ContextError, match="Saved working-note window differs"):
        assemble(saved)


def test_shared_host_prepares_and_reloads_all_checked_note_pages_with_hard_headroom(
    tmp_path, monkeypatch,
):
    def no_transport(*_args, **_kwargs):
        pytest.fail("Preparation must not call a provider")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="synthetic-note-page-continuity",
            question="Review the complete saved synthetic note.",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        mission_id = control["body"]["mission_id"]
        note = host.notes.write(
            "continuity-note", mission_id=mission_id,
            actor_id=control["body"]["actor_id"],
            task_ref=control["body"]["proposal_ref"],
            note_id="continuity-note", category="hypothesis",
            text=" ".join(
                f"candidate-{index:04d} comparison={index * 317 % 997} "
                f"limit={index * 409 % 991}."
                for index in range(190)
            ),
        )
        assert host._schedule_model(control)
        control = host._control(run.run_id, "agent-synthesis")
        origin_call = control["body"]["call_ref"]
        origin_intent = host.store.get(
            "model_request_intent", f"model-intent:{origin_call['id']}:{origin_call['version']}",
        )
        inclusion = host.store.transact(
            "continuity-note-inclusion", {},
            lambda tx: ref(tx.put("context_inclusion", "continuity-note-inclusion", {
                "mission_id": mission_id, "call_ref": origin_call,
                "configuration_ref": origin_intent["body"]["configuration_ref"],
                "task_ref": control["body"]["proposal_ref"], "source_refs": [],
                "omitted": [{"type": "note", "ref": note, "fetch": note}],
            })), mission_id=mission_id,
        )
        turns, texts = [], []
        for offset in (0, 2000, 4000):
            page = host.context.read_omitted_record(
                mission_id=mission_id, actor_id=control["body"]["actor_id"],
                task_id=control["body"]["task_id"], inclusion_ref=inclusion,
                pointer=note, offset=offset, limit=2000,
            )
            texts.append(page["text"])
            tool_id = f"note-page-{offset}"
            arguments = {"inclusion_id": inclusion["id"], **note,
                         "offset": offset, "limit": 2000}
            blob = host.store.put_blob(canonical({"content": None, "tool_calls": [{
                "id": tool_id, "type": "function", "function": {
                    "name": "read_context_record", "arguments": canonical(arguments),
                },
            }]}).encode())
            response = host.store.transact(
                f"note-response-{offset}", {},
                lambda tx: ref(tx.put("model_response_receipt", f"note-response-{offset}", {
                    "mission_id": mission_id, "task_id": control["body"]["task_id"],
                    "call_ref": origin_call, "status": "tool_requested", "blob": blob,
                    "usage": {"completion_tokens": 219},
                })), mission_id=mission_id,
            )
            host.context.save_tool_result(
                f"note-result-{offset}", response_ref=response,
                tool_call_id=tool_id, name="read_context_record", content=canonical(page),
            )
            turns.append(response)
        control = host._control(run.run_id, "agent-synthesis")
        host._update_control(
            control, phase="ready_model", sequence=3, work_id=None,
            call_ref=origin_call, response_ref=turns[-1], tool_turn_refs=turns,
            input_budget={"observed_limit_tokens": 8000, "target_fraction": .55},
        )
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        prepared_control = host._control(run.run_id, "agent-synthesis")
        assert prepared_control["body"]["phase"] == "pending_model"
        call_ref = prepared_control["body"]["call_ref"]
        intent_ref = {"kind": "model_request_intent",
                      "id": f"model-intent:{call_ref['id']}:{call_ref['version']}",
                      "version": 1}
        intent = host.store.get(**intent_ref)
        request = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        pages = [json.loads(message["content"])
                 for message in request["messages"]
                 if message.get("name") == "read_context_record"]
        assert {page["offset"] for page in pages} == {0, 2000, 4000}
        assert {page["text"] for page in pages} == set(texts)
        budget = host.store.get("mission_call_budget", call_ref["id"])["body"]
        assert budget["preview_admission_target_tokens"] == 8000 - 219
        estimate = estimate_request_tokens(canonical(request).encode())
        assert estimate.tokens > int(8000 * .55)
        assert estimate.tokens <= 8000 - 219
        assert estimate.tokens + 219 <= 8000
        assert sum(item.get("kind") == "retained_working_note_window"
                   and item.get("status") == "included_exact_window"
                   for item in host.store.get("context_inclusion", f"context:{intent['id']}")["body"][
                       "tool_result_compactions"
                   ]) == 2
        assert host.store.records("model_dispatch_attempt") == []
    finally:
        host.close()

    reopened = StateStore(tmp_path)
    prepared = ModelSession(reopened, ROOT).load(
        intent_ref, for_dispatch=True,
    )
    assert prepared.request.request_sha256 == intent["body"]["request_sha256"]
