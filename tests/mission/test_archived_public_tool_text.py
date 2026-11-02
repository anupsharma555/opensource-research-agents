"""A completed tool turn keeps its ordinary public prose behind an exact reader."""

from __future__ import annotations

import hashlib
import json

import pytest

from ora.coordination.board import ref
from ora.mission.context import ContextError, WorkingContext
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack, fixture
from tests.mission.test_note_source_use import revoke, source_note

PUBLIC = "Synthetic public methods observation. " * 120
PRIVATE = "SYNTHETIC_PRIVATE_REASONING_OR_REJECTED_GENERATION"


def _saved_archived_turn(
    tmp_path, *, public: str | None = PUBLIC,
    response_request_sha256: str | None = None,
):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    source = source_note(store, task)
    request = canonical({"messages": [{"role": "user", "content": "Synthetic source"}]})
    request_blob = store.put_blob(request.encode())
    request_sha256 = hashlib.sha256(request.encode()).hexdigest()

    def save_intent(tx):
        return ref(tx.put("model_request_intent", "model-intent:call:1", {
            "mission_id": "m", "instance_id": "peer", "task_id": "task",
            "call_ref": call, "request_sha256": request_sha256,
            "source_refs": [source["copy"]], "context_source_dependency_refs": [],
            "blob": request_blob,
        }))

    intent = store.transact("public-producing-intent", {}, save_intent, mission_id="m")
    response_data = {
        "content": public,
        "tool_calls": [{"id": "synthetic-tool-call", "type": "function",
                        "function": {"name": "discover_capabilities", "arguments": "{}"}}],
        "reasoning": PRIVATE,
        "failed_generation": PRIVATE,
    }
    response_blob = store.put_blob(canonical(response_data).encode())

    def save_response(tx):
        return ref(tx.put("model_response_receipt", "public-response", {
            "mission_id": "m", "task_id": "task", "call_ref": call,
            "intent_ref": intent,
            "request_sha256": response_request_sha256 or request_sha256,
            "status": "tool_requested", "http_status": 200,
            "error_kind": None, "blob": response_blob,
        }))

    response = store.transact("public-response", {}, save_response, mission_id="m")
    context = WorkingContext(store)
    context.save_tool_result(
        "public-result", response_ref=response, tool_call_id="synthetic-tool-call",
        name="discover_capabilities", content=canonical({"status": "synthetic_catalog"}),
    )

    def next_call(tx):
        prior = tx.get(**call)
        return ref(tx.put("agent_call", "next-public-call", prior["body"]))

    later = store.transact("next-public-call", {}, next_call, mission_id="m")
    pack = context.assemble(later, task, archived_tool_turn_refs=[response])
    assert PUBLIC not in canonical(pack.messages)
    inclusion = bind_no_source_pack(store, context, pack, "public-archived-inclusion")
    return store, context, source, response, inclusion


def _read(context, response, inclusion, *, actor_id="peer", task_id="task",
          mission_id="m", offset=0, limit=1000):
    return context.read_omitted_record(
        mission_id=mission_id, actor_id=actor_id, task_id=task_id,
        inclusion_ref=inclusion, pointer=response, offset=offset, limit=limit,
    )


def test_exact_public_text_is_paged_but_not_in_archived_metadata(tmp_path):
    store, context, source, response, inclusion = _saved_archived_turn(tmp_path)
    saved_result_count = len(store.records("context_tool_result"))
    saved_receipt_count = len(store.records("model_response_receipt"))
    page = context.read_omitted_page(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion, cursor=0, limit=1,
    )
    cue = page["items"][0]["summary"]["public_assistant_text"]
    assert cue == {"status": "available", "total_chars": len(PUBLIC),
                   "reader_tool": "read_context_record"}
    assert PUBLIC not in canonical(page)
    assert PRIVATE not in canonical(page)

    chunks = []
    offset = 0
    while True:
        window = _read(context, response, inclusion, offset=offset)
        chunks.append(window["text"])
        if window["next_offset"] is None:
            break
        offset = window["next_offset"]
    projection = json.loads("".join(chunks))
    assert len(chunks) > 1
    assert projection["public_assistant_text"] == PUBLIC
    assert projection["public_assistant_text_status"] == "available"
    assert projection["public_assistant_text_chars"] == len(PUBLIC)
    assert projection["source_dependency_refs"] == [source["copy"]]
    assert projection["public_assistant_text_authority"] == (
        "saved_public_model_content_not_verified_evidence"
    )
    assert PRIVATE not in canonical(projection)
    assert len(store.records("context_tool_result")) == saved_result_count == 1
    assert len(store.records("model_response_receipt")) == saved_receipt_count == 1
    reopened = WorkingContext(StateStore(tmp_path))
    assert _read(reopened, response, inclusion, limit=1000)["text"] == chunks[0]


@pytest.mark.parametrize("scope", [
    {"mission_id": "other"}, {"actor_id": "sender"}, {"task_id": "other"},
])
def test_public_text_reader_rejects_foreign_scope(tmp_path, scope):
    _store, context, _source, response, inclusion = _saved_archived_turn(tmp_path)
    with pytest.raises(ContextError):
        _read(context, response, inclusion, **scope)


def test_public_text_reader_requires_its_exact_inclusion(tmp_path):
    store, context, _source, response, _inclusion = _saved_archived_turn(tmp_path)

    def unrelated(tx):
        return ref(tx.put("context_inclusion", "unrelated-public-inclusion", {
            "mission_id": "m", "omitted": [],
        }))

    other = store.transact("unrelated-public-inclusion", {}, unrelated, mission_id="m")
    with pytest.raises(ContextError, match="not omitted"):
        _read(context, response, other)


@pytest.mark.parametrize("change", ["revoked", "new_version"])
def test_source_change_withholds_archived_public_text(tmp_path, change):
    store, context, source, response, inclusion = _saved_archived_turn(tmp_path)
    if change == "revoked":
        revoke(store, source["use"])
    else:
        def revise(tx):
            prior = tx.get(**source["copy"])
            return ref(tx.put("source_copy", prior["id"], {
                **prior["body"], "revision": "different current version",
            }, expected_version=prior["version"]))

        store.transact("new-public-source-version", {}, revise, mission_id="m")
    page = context.read_omitted_page(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion, cursor=0, limit=1,
    )
    assert page["items"][0]["summary"]["public_assistant_text"]["status"] == (
        "withheld_due_source_use"
    )
    projection = json.loads(_read(context, response, inclusion, limit=2000)["text"])
    assert projection["public_assistant_text_status"] == "withheld_due_source_use"
    assert projection["unavailable_source_refs"] == [source["copy"]]
    assert "public_assistant_text" not in projection
    assert PUBLIC not in canonical(page) + canonical(projection)


def test_empty_public_content_has_no_readable_cue(tmp_path):
    _store, context, _source, response, inclusion = _saved_archived_turn(
        tmp_path, public=None,
    )
    page = context.read_omitted_page(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion, cursor=0, limit=1,
    )
    assert "public_assistant_text" not in page["items"][0]["summary"]
    projection = json.loads(_read(context, response, inclusion)["text"])
    assert projection["public_assistant_text_status"] == "empty"
    assert "public_assistant_text" not in projection


def test_mismatched_producing_request_makes_public_text_unavailable(tmp_path):
    _store, context, _source, response, inclusion = _saved_archived_turn(
        tmp_path, response_request_sha256="different-request-hash",
    )
    page = context.read_omitted_page(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion, cursor=0, limit=1,
    )
    assert page["items"][0]["summary"]["public_assistant_text"]["status"] == (
        "unavailable_lineage"
    )
    projection = json.loads(_read(context, response, inclusion)["text"])
    assert projection["public_assistant_text_status"] == "unavailable_lineage"
    assert "public_assistant_text" not in projection
