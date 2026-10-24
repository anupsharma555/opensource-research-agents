"""An answer's checked report sidecar need not replay an identical earlier read."""

import json
from dataclasses import replace

import pytest

from ora.coordination.board import ref
from ora.mission.context import WorkingContext
from ora.mission.service import TOOL_SCHEMAS
from ora.models import GroqSelectedAdapter
from ora.models.token_estimate import estimate_request_tokens
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack
from tests.mission.test_peer_answer_preview import _fixture, _pack
from tests.models.test_groq import config


def _save_read(store, context, saved, inclusion, pointer, window, *, label,
               assistant_content=None):
    tool_call_id = f"{label}-call"
    arguments = canonical({
        "inclusion_id": inclusion["id"], **pointer,
        "offset": window["offset"], "limit": window["requested_limit"],
    })
    blob = store.put_blob(canonical({"content": assistant_content, "tool_calls": [{
        "id": tool_call_id, "type": "function", "function": {
            "name": "read_context_record", "arguments": arguments,
        },
    }]}).encode())
    response_ref = store.transact(f"{label}-response", {}, lambda tx: ref(tx.put(
        "model_response_receipt", f"{label}-response", {
            "mission_id": "m", "task_id": saved["task_ref"]["id"],
            "call_ref": saved["call_ref"], "status": "tool_requested", "blob": blob,
        },
    )), mission_id="m")
    context.save_tool_result(
        f"{label}-result", response_ref=response_ref, tool_call_id=tool_call_id,
        name="read_context_record", content=canonical(window),
    )
    return response_ref


def _case(tmp_path, *, direct_offset=0, direct_limit=None):
    store = StateStore(tmp_path)
    saved = _fixture(store, body="A short synthetic peer report with distinct words.")
    context, initial, _ = _pack(store, saved, allowance=1800)
    inclusion = bind_no_source_pack(store, context, initial, "duplicate-report-read")
    scope = {
        "mission_id": "m", "actor_id": "requester", "task_id": saved["task_ref"]["id"],
        "inclusion_ref": inclusion,
    }
    answer = context.read_omitted_record(
        **scope, pointer=saved["answer_ref"], offset=0, limit=2000,
    )
    sidecar = answer["related_report_window"]
    direct = context.read_omitted_record(
        **scope, pointer=saved["report_ref"], offset=direct_offset,
        limit=direct_limit or sidecar["returned_chars"],
    )
    older = _save_read(store, context, saved, inclusion, saved["report_ref"], direct,
                       label="older-report")
    current = _save_read(store, context, saved, inclusion, saved["answer_ref"], answer,
                         label="current-answer")
    return store, context, saved, direct, answer, older, current


def _assemble(store, context, saved, older, current, *, allowance=20000):
    return context.assemble(
        saved["call_ref"], saved["task_ref"],
        control_ref=ref(store.get("mission_task_control", "requester-control")),
        archived_tool_turn_refs=[older], tool_turn_refs=[current],
        allowance=allowance, estimate_tokens=len,
    )


def _tool_ids(pack):
    return [item["tool_call_id"] for item in pack.messages if item["role"] == "tool"]


def _receipt(pack):
    return next(item for item in pack.tool_result_compactions
                if item.get("kind") == "retained_report_window")


def _serialized_tokens(tmp_path, messages):
    adapter = GroqSelectedAdapter(tmp_path, transport=lambda *_a, **_kw: None)
    request = adapter.prepare(
        config(), call_id="duplicate-report-check", attempt_id="duplicate-report-attempt",
        task_category="collect", phase="collect", messages=messages,
        tools=({"type": "function", "function": {
            "name": "read_context_record", "parameters": TOOL_SCHEMAS["read_context_record"],
        }},), sources=(),
    )
    return estimate_request_tokens(canonical(request.body).encode()).tokens


def test_exact_prior_report_read_is_recovered_without_duplicate_serialized_text(tmp_path):
    store, context, saved, direct, answer, older, current = _case(tmp_path)
    sidecar = answer["related_report_window"]
    assert {key: direct[key] for key in (
        "ref", "projection_sha256", "offset", "returned_chars", "text",
    )} == {key: sidecar[key] for key in (
        "ref", "projection_sha256", "offset", "returned_chars", "text",
    )}
    pack = _assemble(store, context, saved, older, current)
    assert _tool_ids(pack) == ["current-answer-call"]
    assert _receipt(pack)["status"] == "same_exact_window_already_in_input"
    assert any(item["ref"] == older for item in pack.omitted)
    current_result = next(item for item in pack.messages if item.get("tool_call_id")
                          == "current-answer-call")
    checked_answer = json.loads(current_result["content"])
    assert checked_answer["related_report_window"] == sidecar
    assert checked_answer["peer_review"] == answer["peer_review"]
    assert checked_answer["text"] == answer["text"]
    assert checked_answer["related_report_window"]["text"] == direct["text"]
    assert "reader_feedback" not in json.loads(pack.messages[-1]["content"])

    # Reconstruct the former packet from its two checked, saved exchanges.
    old_exchange, _ = context._tool_exchange(older, scope={
        "mission_id": "m", "actor_id": "requester", "task_id": saved["task_ref"]["id"],
    })
    with_duplicate = replace(pack, messages=[
        *pack.messages[:-3], *old_exchange, *pack.messages[-3:],
    ])
    assert _tool_ids(with_duplicate) == ["older-report-call", "current-answer-call"]
    assert _serialized_tokens(tmp_path, pack.messages) < _serialized_tokens(
        tmp_path, with_duplicate.messages,
    )
    assert store.get(**older) is not None
    assert store.get("context_tool_result", f"tool-result:{older['id']}") is not None


def test_repeated_top_level_report_read_gets_optional_exact_feedback(tmp_path):
    store, context, saved, direct, _answer, older, _current = _case(tmp_path)
    old_response = store.get(**older)
    old_private = json.loads(store.read_blob(old_response["body"]["blob"]["sha256"]))
    old_args = json.loads(old_private["tool_calls"][0]["function"]["arguments"])
    current = _save_read(store, context, saved, {"id": old_args["inclusion_id"]}, saved["report_ref"],
                         direct, label="current-report")
    pack = _assemble(store, context, saved, older, current)
    assert _tool_ids(pack) == ["current-report-call"]
    assert json.loads(next(item["content"] for item in pack.messages
                           if item.get("tool_call_id") == "current-report-call"))["text"] == direct["text"]
    assert json.loads(pack.messages[-1]["content"])["reader_feedback"] == {
        "status": "same_exact_report_page_returned_by_earlier_saved_call",
        "previous_response_ref": older,
        "offset": 0,
        "current_report_text_in_input": True,
    }
    tight = _assemble(store, context, saved, older, current,
                      allowance=pack.estimated_tokens - 1)
    assert _tool_ids(tight) == ["current-report-call"]
    assert "reader_feedback" not in json.loads(tight.messages[-1]["content"])
    assert json.loads(next(item["content"] for item in tight.messages
                           if item.get("tool_call_id") == "current-report-call"))["text"] == direct["text"]


def test_sidecar_does_not_hide_earlier_assistant_explanation(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store, body="A short synthetic peer report with distinct words.")
    context, initial, _ = _pack(store, saved, allowance=1800)
    inclusion = bind_no_source_pack(store, context, initial, "explained-report-read")
    scope = {"mission_id": "m", "actor_id": "requester",
             "task_id": saved["task_ref"]["id"], "inclusion_ref": inclusion}
    answer = context.read_omitted_record(
        **scope, pointer=saved["answer_ref"], offset=0, limit=2000,
    )
    report = context.read_omitted_record(
        **scope, pointer=saved["report_ref"], offset=0,
        limit=answer["related_report_window"]["returned_chars"],
    )
    older = _save_read(store, context, saved, inclusion, saved["report_ref"], report,
                       label="older-report", assistant_content="I will compare this report with the answer.")
    current = _save_read(store, context, saved, inclusion, saved["answer_ref"], answer,
                         label="current-answer")
    pack = _assemble(store, context, saved, older, current)
    assert _tool_ids(pack) == ["older-report-call", "current-answer-call"]
    assert any(message.get("content") == "I will compare this report with the answer."
               for message in pack.messages)


@pytest.mark.parametrize("direct_offset,direct_limit", [(1, None), (0, 12)])
def test_distinct_report_window_remains_visible(tmp_path, direct_offset, direct_limit):
    store, context, saved, _direct, answer, older, current = _case(
        tmp_path, direct_offset=direct_offset, direct_limit=direct_limit,
    )
    pack = _assemble(store, context, saved, older, current)
    assert _tool_ids(pack) == ["older-report-call", "current-answer-call"]
    assert _receipt(pack)["status"] == "included_exact_window"
    assert json.loads(next(item["content"] for item in pack.messages
                           if item.get("tool_call_id") == "current-answer-call"))[
                               "related_report_window"] == answer["related_report_window"]


def test_reopen_and_current_access_loss_do_not_replay_old_report(tmp_path, monkeypatch):
    store, _context, saved, direct, _answer, older, current = _case(tmp_path)
    reopened = StateStore(tmp_path)
    context = WorkingContext(reopened)
    pack = _assemble(reopened, context, saved, older, current)
    assert _tool_ids(pack) == ["current-answer-call"]
    assert _receipt(pack)["status"] == "same_exact_window_already_in_input"

    monkeypatch.setattr("ora.mission.context.report_text_access", lambda _store, _report: {
        "status": "data_unavailable_due_source_use", "source_dependency_refs": [],
        "unavailable_source_refs": [], "dependency_lineage_complete": False,
    })
    restricted = _assemble(reopened, context, saved, older, current)
    assert _tool_ids(restricted) == ["current-answer-call"]
    current_result = next(item for item in restricted.messages
                          if item.get("tool_call_id") == "current-answer-call")
    assert "related_report_window" not in json.loads(current_result["content"])
    assert _receipt(restricted)["status"] == "omitted_due_source_use"
    assert direct["text"] not in canonical(restricted.messages)
