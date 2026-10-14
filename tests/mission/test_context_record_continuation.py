"""Executable continuation for bounded, authorized omitted context records."""

from __future__ import annotations

import json

import pytest

from ora.coordination.board import ref
from ora.mission.context import ContextError, WorkingContext
from ora.mission.memory import WorkingNotes
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import fixture
from tests.mission.test_note_source_use import revoke, source_note
from tests.mission.test_service import service


def test_omitted_working_note_next_request_pages_complete_projection(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    note = WorkingNotes(store).write(
        "candidate-note", mission_id="m", actor_id="peer", task_ref=task,
        note_id="candidate-note", category="hypothesis",
        text=("Candidate A: " + "a" * 1400 + "\nCandidate B: " + "b" * 600
              + "\nCandidate C: 29 of 84 synthetic records meet the stated criterion."
              + "\nLimitations: " + "c" * 2500),
    )
    context = WorkingContext(store)
    full = json.loads(context.assemble(call, task, note_refs=[note]).messages[-1]["content"])
    core = {**full, "details": [], "omitted": {"count": 1, "fetch": full["older_records"]}}
    allowance = len(canonical([{"role": "user", "content": canonical(core)}])) + 50
    pack = context.assemble(call, task, note_refs=[note], allowance=allowance,
                            estimate_tokens=len)
    assert [item["fetch"] for item in pack.omitted] == [note]
    inclusion = store.transact(
        "candidate-note-window", {},
        lambda tx: ref(tx.put("context_inclusion", "candidate-note-window", {
            "mission_id": "m", "call_ref": call,
            "configuration_ref": ref(tx.get("agent_configuration", "config")),
            "task_ref": task, "source_refs": [], "omitted": list(pack.omitted),
        })),
        mission_id="m",
    )

    scope = {"mission_id": "m", "actor_id": "peer", "task_id": "task",
             "inclusion_ref": inclusion, "pointer": note}
    first = context.read_omitted_record(**scope, offset=0, limit=2000)
    assert first["next_offset"] == 2000
    assert "Candidate C" not in first["text"]
    assert first["next_request"] == {
        "tool": "read_context_record", "inclusion_id": inclusion["id"],
        **note, "offset": 2000, "limit": 2000,
    }
    chunks = [first["text"]]
    page = first
    while "next_request" in page:
        request = page["next_request"]
        page = context.read_omitted_record(
            **scope, offset=request["offset"], limit=request["limit"],
        )
        chunks.append(page["text"])
    assert page["next_offset"] is None
    assert "next_request" not in page
    assert len("".join(chunks)) == first["total_chars"]
    projection = json.loads("".join(chunks))
    assert "Candidate C: 29 of 84" in projection["text"]
    assert projection["input_refs"] == []

    for actor_id, task_id in (("sender", "task"), ("peer", "other-task")):
        with pytest.raises(ContextError, match="another peer or task"):
            context.read_omitted_record(
                **{**scope, "actor_id": actor_id, "task_id": task_id},
                offset=2000, limit=2000,
            )


def test_stale_continuation_rechecks_revocation_and_raw_source_stays_unavailable(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    saved = source_note(store, task)
    note = WorkingNotes(store).write(
        "long-source-note", mission_id="m", actor_id="peer", task_ref=task,
        note_id="long-source-note", category="hypothesis",
        text="Synthetic source-linked text " + "restricted " * 250,
        input_refs=[saved["passage"]],
    )

    def save(tx):
        inclusion = tx.put("context_inclusion", "revocable-note-window", {
            "mission_id": "m", "call_ref": call,
            "configuration_ref": ref(tx.get("agent_configuration", "config")),
            "task_ref": task, "source_refs": [],
            "omitted": [
                {"type": "note", "ref": note, "fetch": note},
                {"type": "source_copy", "ref": saved["copy"],
                 "fetch": saved["copy"]},
            ],
        })
        return ref(inclusion)

    inclusion = store.transact("revocable-note-window", {}, save, mission_id="m")
    context = WorkingContext(store)
    scope = {"mission_id": "m", "actor_id": "peer", "task_id": "task",
             "inclusion_ref": inclusion}
    before = context.read_omitted_record(
        **scope, pointer=note, offset=0, limit=2000,
    )
    assert before["next_request"]["offset"] == 2000
    with pytest.raises(ContextError, match="Raw source"):
        context.read_omitted_record(
            **scope, pointer=saved["copy"], offset=0, limit=2000,
        )

    revoke(store, saved["use"])
    request = before["next_request"]
    with pytest.raises(ContextError, match="cursor exceeds"):
        context.read_omitted_record(
            **scope, pointer=note, offset=request["offset"], limit=request["limit"],
        )
    withheld = context.read_omitted_record(
        **scope, pointer=note, offset=0, limit=2000,
    )
    assert "Synthetic source-linked text" not in canonical(withheld)
    assert json.loads(withheld["text"])["status"] == "data_unavailable_due_source_use"


def test_saved_old_window_derives_current_continuation_without_rewriting_receipt(
    tmp_path, monkeypatch,
) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    note = WorkingNotes(store).write(
        "historical-note", mission_id="m", actor_id="peer", task_ref=task,
        note_id="historical-note", category="hypothesis", text="Synthetic " + "x" * 3500,
    )

    def save_inclusion(tx):
        return ref(tx.put("context_inclusion", "historical-note-inclusion", {
            "mission_id": "m", "call_ref": call,
            "configuration_ref": ref(tx.get("agent_configuration", "config")),
            "task_ref": task, "source_refs": [],
            "omitted": [{"type": "note", "ref": note, "fetch": note}],
        }))

    inclusion = store.transact("historical-note-inclusion", {}, save_inclusion, mission_id="m")
    context = WorkingContext(store)
    current = context.read_omitted_record(
        mission_id="m", actor_id="peer", task_id="task", inclusion_ref=inclusion,
        pointer=note, offset=0, limit=2000,
    )
    old = {key: value for key, value in current.items() if key != "next_request"}
    arguments = {"inclusion_id": inclusion["id"], **note, "offset": 0, "limit": 2000}

    response_blob = store.put_blob(canonical({
        "content": None,
        "tool_calls": [{"id": "old-read", "type": "function", "function": {
            "name": "read_context_record", "arguments": canonical(arguments),
        }}],
    }).encode())

    def save_response(tx):
        return ref(tx.put("model_response_receipt", "old-note-read", {
            "mission_id": "m", "task_id": "task", "call_ref": call,
            "status": "tool_requested", "blob": response_blob,
        }))

    response = store.transact("old-note-read", {}, save_response, mission_id="m")
    result = context.save_tool_result(
        "old-note-tool-result", response_ref=response, tool_call_id="old-read",
        name="read_context_record", content=canonical(old),
    )
    exchange, derived = context._tool_exchange(response)
    assert json.loads(exchange[-1]["content"]) == current
    assert derived["status"] == "working_note_continuation_rechecked"
    assert derived["saved_tool_result_ref"] == result
    saved = json.loads(store.read_blob(store.get(**result)["body"]["blob"]["sha256"]))
    assert json.loads(saved["content"]) == old
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        assert host._current_context_read_turn(response, {"body": {
            "mission_id": "m", "actor_id": "peer", "task_id": "task",
        }})
    finally:
        host.close()

    corrupted = {**current, "next_request": {**current["next_request"], "offset": 999}}
    bad_response = store.transact(
        "bad-note-read", {},
        lambda tx: ref(tx.put("model_response_receipt", "bad-note-read", {
            "mission_id": "m", "task_id": "task", "call_ref": call,
            "status": "tool_requested", "blob": response_blob,
        })), mission_id="m",
    )
    context.save_tool_result(
        "bad-note-tool-result", response_ref=bad_response, tool_call_id="old-read",
        name="read_context_record", content=canonical(corrupted),
    )
    with pytest.raises(ContextError, match="differs from its current exact read"):
        context._tool_exchange(bad_response)
