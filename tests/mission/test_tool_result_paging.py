"""Recover exact saved result projections across restart and source-use changes."""

from __future__ import annotations

import json

import pytest

from ora.coordination.board import ref
from ora.mission.context import ContextError, WorkingContext
from ora.mission.memory import WorkingNotes
from ora.mission.tool_results import MAX_RESULT_PAGE_CHARS, ResultProjectionChanged
from ora.state import StateError, StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack, fixture
from tests.mission.test_note_source_use import PASSAGE, revoke, source_note


def save_result(store, call, name, content, *, arguments=None, suffix="original"):
    raw = store.put_blob(canonical({"content": None, "tool_calls": [{
        "id": f"tool-{suffix}", "type": "function",
        "function": {"name": name, "arguments": canonical(arguments or {})},
    }], "reasoning": "PRIVATE_REASONING_MUST_NOT_BE_PAGED"}).encode())
    body = store.get(**call)["body"]
    response = store.transact(f"response-{suffix}", {}, lambda tx: ref(tx.put(
        "model_response_receipt", f"response-{suffix}", {
            "mission_id": body["mission_id"], "task_id": body["task_id"],
            "call_ref": call, "status": "tool_requested", "blob": raw,
        },
    )), mission_id=body["mission_id"])
    result = WorkingContext(store).save_tool_result(
        f"result-{suffix}", response_ref=response, tool_call_id=f"tool-{suffix}",
        name=name, content=canonical(content),
    )
    return response, result


def read(context, result, **kwargs):
    return context.read_tool_result(
        mission_id="m", actor_id="peer", task_id="task", result_id=result["id"],
        offset=kwargs.get("offset", 0), limit=kwargs.get("limit", 1800),
        projection_sha256=kwargs.get("projection_sha256", ""),
    )


def collect(context, result, first=None):
    page = first or read(context, result)
    pieces, pages = [], []
    while True:
        assert len(canonical(page)) <= MAX_RESULT_PAGE_CHARS
        assert page["end_offset"] > page["offset"]
        assert "PRIVATE_REASONING" not in canonical(page)
        pages.append(page)
        pieces.append(page["text"])
        if page["next_request"] is None:
            break
        arguments = {key: value for key, value in page["next_request"].items() if key != "tool"}
        page = context.read_tool_result(mission_id="m", actor_id="peer", task_id="task", **arguments)
    return json.loads("".join(pieces)), pages


@pytest.mark.parametrize("name", ["read_note", "search_notes", "board_read", "list_optional_work"])
def test_saved_results_reconstruct_after_restart_without_rerunning_tools(tmp_path, name):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    if name in {"read_note", "search_notes"}:
        note = WorkingNotes(store).write(
            "large-note", mission_id="m", actor_id="peer", task_ref=task,
            note_id="large-note", category="note", text=('Unicode Ω and \\"\\n' * 900),
        )
        record = store.get(**note)
        content = {"status": "found", "note": record} if name == "read_note" else {"notes": [record]}
    else:
        items = [{
            "control_ref": {"kind": "mission_task_control", "id": f"optional-{i}", "version": 1},
            "proposal_ref": {"kind": "board_proposal", "id": f"proposal-{i}", "version": 1},
            "actor_id": "peer", "control_type": "optional", "scope_status": "open", "phase": "ready_model",
        } for i in range(60)]
        content = {"optional_work": items}
        if name == "board_read":
            content["open_work"] = [item["proposal_ref"] for item in items]
    response, result = save_result(store, call, name, content)
    original = store.read_blob(store.get(**result)["body"]["blob"]["sha256"])
    context = WorkingContext(store)
    exchange, receipt = context._tool_exchange(response)
    first = json.loads(exchange[1]["content"])
    assert first["schema"] == "ora.saved_tool_result_window.v1"
    assert receipt["saved_tool_result_ref"] == result
    reconstructed, pages = collect(WorkingContext(StateStore(tmp_path)), result, first)
    assert reconstructed == content
    assert len(pages) > 1
    assert store.read_blob(store.get(**result)["body"]["blob"]["sha256"]) == original
    assert len(store.records("context_tool_result")) == 1


@pytest.mark.parametrize("scope", [
    {"mission_id": "other", "actor_id": "peer", "task_id": "task"},
    {"mission_id": "m", "actor_id": "other", "task_id": "task"},
    {"mission_id": "m", "actor_id": "peer", "task_id": "other"},
])
def test_result_reader_rejects_cross_scope(tmp_path, scope):
    store = StateStore(tmp_path)
    call, _ = fixture(store)
    _, result = save_result(store, call, "board_read", {"open_work": [], "optional_work": []})
    with pytest.raises(ContextError):
        WorkingContext(store).read_tool_result(**scope, result_id=result["id"], offset=0,
                                              limit=100, projection_sha256="")


@pytest.mark.parametrize("name", ["read_source_window", "inspect_hit", "read_context_record"])
def test_reader_never_exposes_raw_source_or_arbitrary_tool_blobs(tmp_path, name):
    store = StateStore(tmp_path)
    call, _ = fixture(store)
    _, result = save_result(store, call, name, {"text": "RAW_SOURCE_OR_PRIVATE_PAYLOAD"})
    with pytest.raises(ContextError, match="authorized pageable projection"):
        read(WorkingContext(store), result)
    with pytest.raises(ContextError):
        read(WorkingContext(store), {"id": "source-copy-not-a-tool-result"})


def test_reader_rechecks_revocation_and_invalidates_saved_continuation_on_replay(tmp_path):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    saved = source_note(store, task)
    note = WorkingNotes(store).write(
        "large-source-note", mission_id="m", actor_id="peer", task_ref=task,
        note_id="large-source-note", category="note", text=PASSAGE * 100,
        input_refs=[saved["copy"]],
    )
    response, result = save_result(store, call, "read_note", {"status": "found", "note": store.get(**note)})
    context = WorkingContext(store)
    first = read(context, result)
    arguments = {key: value for key, value in first["next_request"].items() if key != "tool"}
    second = context.read_tool_result(mission_id="m", actor_id="peer", task_id="task", **arguments)
    page_response, _ = save_result(store, call, "read_tool_result", second,
                                   arguments=arguments, suffix="continuation")
    assert PASSAGE in second["text"]
    revoke(store, saved["use"])
    with pytest.raises(ResultProjectionChanged):
        context.read_tool_result(mission_id="m", actor_id="peer", task_id="task", **arguments)
    replay, _ = WorkingContext(StateStore(tmp_path))._tool_exchange(page_response)
    invalidated = json.loads(replay[1]["content"])
    assert invalidated["status"] == "projection_changed_restart_required"
    assert invalidated["next_request"]["offset"] == 0
    assert PASSAGE not in canonical(replay)
    refreshed = read(context, result)
    assert refreshed["projection_sha256"] != first["projection_sha256"]
    current, _ = collect(context, result, refreshed)
    assert "text" not in current["note"]["body"]
    assert current["note"]["body"]["unavailable_source_refs"] == [saved["copy"]]
    assert PASSAGE not in canonical(context._tool_exchange(response)[0])
    raw = json.loads(store.read_blob(store.get(**result)["body"]["blob"]["sha256"]))
    assert PASSAGE in raw["content"]


def test_visible_page_note_text_binds_sources_in_saved_prepared_request(tmp_path):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    saved = source_note(store, task)
    note = WorkingNotes(store).write(
        "large-linked-note", mission_id="m", actor_id="peer", task_ref=task,
        note_id="large-linked-note", category="note", text=PASSAGE * 100,
        input_refs=[saved["copy"]],
    )
    _, result = save_result(store, call, "search_notes", {"notes": [store.get(**note)]})
    context = WorkingContext(store)
    first = read(context, result)
    arguments = {key: value for key, value in first["next_request"].items() if key != "tool"}
    page = context.read_tool_result(mission_id="m", actor_id="peer", task_id="task", **arguments)
    response, _ = save_result(store, call, "read_tool_result", page,
                              arguments=arguments, suffix="visible-page")
    pack = context.assemble(call, task, tool_turn_refs=[response])
    assert pack.note_text_refs == (note,)
    assert context.context_source_dependency_refs(pack) == (saved["copy"],)
    inclusion = bind_no_source_pack(store, context, pack, "paged-note")
    assert store.get(**inclusion)["body"]["context_source_dependency_refs"] == [saved["copy"]]
    intent = store.get("model_request_intent", "model-intent:call:1")
    assert context.prepared_working_note_restrictions(ref(intent)) == []
    revoke(store, saved["use"])
    assert context.prepared_working_note_restrictions(ref(intent))[0]["note_ref"] == note
    with pytest.raises(ContextError, match="source use changed"):
        context.context_source_dependency_refs(pack)


@pytest.mark.parametrize("kwargs", [
    {"offset": -1}, {"offset": True}, {"offset": 1}, {"offset": 999999, "projection_sha256": "bad"},
    {"limit": 0}, {"limit": 1801}, {"limit": True}, {"projection_sha256": "wrong"},
])
def test_invalid_cursors_fail_without_returning_text(tmp_path, kwargs):
    store = StateStore(tmp_path)
    call, _ = fixture(store)
    _, result = save_result(store, call, "board_read", {"optional_work": [], "open_work": []})
    with pytest.raises(StateError):
        read(WorkingContext(store), result, **kwargs)


def test_host_page_delivery_then_revocation_blocks_prepared_dispatch(tmp_path, monkeypatch):
    import time

    from ora.contracts.models import LiveStartRequest
    from tests.mission.test_service import service, tool

    requests = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        step = len(requests)
        if step == 1:
            return tool("request_capability", {"tool_id": "read_note"}, step)
        if step == 2:
            return tool("read_note", {"note_id": "host-large-note"}, step)
        assert step == 3
        message = next(item for item in reversed(body["messages"]) if item["role"] == "tool")
        assert message["name"] == "read_note"
        first_page = json.loads(message["content"])
        assert first_page["schema"] == "ora.saved_tool_result_window.v1"
        assert len(canonical(first_page)) <= MAX_RESULT_PAGE_CHARS
        assert "read_tool_result" in {item["function"]["name"] for item in body["tools"]}
        arguments = {key: value for key, value in first_page["next_request"].items() if key != "tool"}
        return tool("read_tool_result", arguments, step)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="result-page-dispatch", question="Read a synthetic saved note",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        mission_id = control["body"]["mission_id"]
        blob = host.store.put_blob(PASSAGE.encode("utf-8"))

        def save_source(tx):
            publication = tx.put("publication", "page-source-publication", {
                "title": "Synthetic page source",
            })
            encounter = tx.put("source_encounter", "page-source-encounter", {
                "publication": ref(publication), "route": "user_supplied",
                "status": "accepted",
            })
            copy = tx.put("source_copy", "page-source", {
                "mission_id": mission_id, "rights_status": "permitted",
                "publication": ref(publication), "encounter": ref(encounter),
                "mime_type": "text/plain", "blob": blob,
                "content_sha256": blob["sha256"], "retain_bytes": True,
                "inspection_state": "checked", "validation_record": "synthetic-local-check",
                "passage_ids": [],
            })
            use = tx.put("source_use", "page-source-use", {
                "copy_ref": ref(copy), "allowed_uses": ["model_input"], "policy_status": "granted",
            })
            tx.bind("source_use.copy", "page-source:1", "source_use", use["id"])
            return {"copy": ref(copy), "use": ref(use)}

        source = host.store.transact("page-source", {}, save_source, mission_id=mission_id)
        note = host.notes.write(
            "host-large-note", mission_id=mission_id, actor_id=control["body"]["actor_id"],
            task_ref=control["body"]["proposal_ref"], note_id="host-large-note", category="note",
            text=(PASSAGE + ' Ω "quoted" \\ ') * 100, input_refs=[source["copy"]],
        )
        for _ in range(300):
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            if len(requests) == 3 and control["body"]["phase"] == "ready_model":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("Saved page was not ready for model continuation")
        assert host._schedule_model(control)
        pending = host._control(run.run_id, "agent-synthesis")
        spec = host.executor._spec_from(host._work_for(pending))
        intent = host.store.get("model_request_intent", f"model-intent:{spec.call_id}:1")
        request = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        page_message = next(item for item in reversed(request["messages"]) if item["role"] == "tool")
        assert page_message["name"] == "read_tool_result"
        page = json.loads(page_message["content"])
        assert page["note_text_refs"] == [note]
        assert source["copy"] in intent["body"]["context_source_dependency_refs"]

        def revoke_source(tx):
            previous = tx.get(**source["use"])
            return tx.put("source_use", previous["id"], {
                **previous["body"], "allowed_uses": [], "policy_status": "revoked",
            }, expected_version=1)

        host.store.transact("page-source-revoked", {}, revoke_source, mission_id=mission_id)
        blocked = host._model_work(spec)
        assert blocked.disposition == "terminal"
        assert blocked.payload["status"] == "working_note_source_use_restricted"
        assert blocked.payload["external_action"] == "none"
        assert len(requests) == 3
        assert host.store.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}") is None
    finally:
        host.close()


def test_note_text_dependencies_follow_exact_escaped_character_spans(tmp_path):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    saved = source_note(store, task)
    note = WorkingNotes(store).write(
        "quoted-note", mission_id="m", actor_id="peer", task_ref=task,
        note_id="quoted-note", category="note", text='Ω says "quoted" \\ next\nline',
        input_refs=[saved["copy"]],
    )
    content = {"status": "found", "note": store.get(**note)}
    _, result = save_result(store, call, "read_note", content)
    context = WorkingContext(store)
    first = read(context, result, limit=1)
    assert first["note_text_refs"] == []
    encoded = canonical(content)
    start = encoded.index('"text":') + len('"text":')
    for offset in (start + 1, encoded.index('\\"quoted'), encoded.index('\\\\ next')):
        page = read(context, result, offset=offset, limit=1,
                    projection_sha256=first["projection_sha256"])
        assert page["text"] == encoded[offset:offset + 1]
        assert page["note_text_refs"] == [note]
    after = start + len(canonical(store.get(**note)["body"]["text"]))
    page = read(context, result, offset=after, limit=1,
                projection_sha256=first["projection_sha256"])
    assert page["note_text_refs"] == []


def test_note_search_pages_many_small_records_and_ignores_unlisted_metadata(tmp_path):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    notes = []
    for i in range(20):
        pointer = WorkingNotes(store).write(
            f"note-{i}", mission_id="m", actor_id="peer", task_ref=task,
            note_id=f"note-{i}", category="note", text=f"Synthetic observation {i}.",
        )
        notes.append(store.get(**pointer))
    content = {"notes": notes, "private_diagnostic": "DO_NOT_EXPOSE_THIS_FIELD"}
    _, result = save_result(store, call, "search_notes", content)
    reconstructed, pages = collect(WorkingContext(store), result)
    assert reconstructed == {"notes": notes}
    assert len(pages) > 1
    assert "DO_NOT_EXPOSE_THIS_FIELD" not in canonical(pages)


def test_oversized_reference_fails_instead_of_returning_no_progress_cursor(tmp_path):
    store = StateStore(tmp_path)
    call, _ = fixture(store)
    _, result = save_result(store, call, "board_read", {"open_work": [], "optional_work": []},
                            suffix="x" * 4000)
    with pytest.raises(StateError, match="reply allowance"):
        read(WorkingContext(store), result)
