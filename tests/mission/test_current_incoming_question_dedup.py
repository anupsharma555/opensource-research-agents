"""A complete current peer read can replace duplicate question navigation under pressure."""

import hashlib
import json

import pytest

from ora.coordination.board import ref
from ora.mission.context import WorkingContext
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack, fixture
from tests.mission.test_duplicate_report_read_compaction import _save_read
from tests.mission.test_incoming_question_delivery import _incoming


def _case(tmp_path, *, read_limit=4000, revise_request=False, alter_result=False):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    request = _incoming(store, task, "Check this synthetic comparison? " + "Q" * 460)
    context = WorkingContext(store)
    initial = context.assemble(call, task, allowance=20_000, estimate_tokens=len)
    inclusion = bind_no_source_pack(store, context, initial, "question-read-inclusion")
    window = context.read_omitted_record(
        mission_id="m", actor_id="peer", task_id=task["id"],
        inclusion_ref=inclusion, pointer=ref(request), offset=0, limit=read_limit,
    )
    if alter_result:
        projected = json.loads(window["text"])
        projected["question"] = "Changed synthetic question? " + "Q" * 460
        text = canonical(projected)
        window = {
            **window, "text": text, "returned_chars": len(text),
            "total_chars": len(text),
            "projection_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    current = _save_read(
        store, context, {"call_ref": call, "task_ref": task},
        inclusion, ref(request), window, label="question-read",
    )
    if revise_request:
        store.transact("revise-after-question-read", {}, lambda tx: tx.put(
            "mail_request", request["id"], {
                **request["body"], "expected_artifact": "Revised synthetic check",
            }, expected_version=request["version"],
        ), mission_id="m")
    return store, context, call, task, current


def _view(context, call, task, current, allowance):
    pack = context.assemble(
        call, task, tool_turn_refs=[current],
        allowance=allowance, estimate_tokens=len,
    )
    return pack, json.loads(pack.messages[-1]["content"])


def test_pressure_keeps_one_complete_current_question_and_exact_identity(tmp_path):
    store, context, call, task, current = _case(tmp_path)
    unpressured, ordinary = _view(context, call, task, current, 20_000)
    question = store.get("mail_request", "delivery-request")["body"]["question"]
    assert ordinary["incoming_request"]["question"] == question
    assert ordinary["incoming_request"]["question_delivery"] == "full_saved_text"

    compact, index = _view(context, call, task, current, 2500)
    incoming = index["incoming_request"]
    assert compact.estimated_tokens <= 2500
    assert incoming == {
        "owner": "peer", "ref": ref(store.get("mail_request", "delivery-request")),
        "requester_id": "sender",
        "question_delivery": "full_in_current_tool_result",
        "response_needed": True,
    }
    result = next(message for message in compact.messages
                  if message.get("name") == "read_context_record")
    window = json.loads(result["content"])
    assert window["offset"] == 0
    assert window["returned_chars"] == window["total_chars"] == len(window["text"])
    assert window["next_offset"] is None
    assert json.loads(window["text"])["question"] == question
    assert json.loads(window["text"])["status"] == "unanswered"
    assert "requests" not in index["record_counts"]
    assert "messages" not in index["record_counts"]
    assert store.get(**current) is not None

    original_call = store.get(**call)
    next_call = store.transact("next-question-call", {}, lambda tx: ref(tx.put(
        "agent_call", "next-question-call", original_call["body"],
    )), mission_id="m")
    prepared, prepared_index = _view(context, next_call, task, current, 2500)
    assert prepared_index["incoming_request"] == incoming
    inclusion = bind_no_source_pack(store, context, prepared, "question-read-next")
    assert inclusion["kind"] == "context_inclusion"


@pytest.mark.parametrize("read_limit,revise_request", [
    (100, False),
    (4000, True),
])
def test_partial_or_stale_read_does_not_claim_complete_current_question(
    tmp_path, read_limit, revise_request,
):
    store, context, call, task, current = _case(
        tmp_path, read_limit=read_limit, revise_request=revise_request,
    )
    allowance = 2700 if revise_request else 2500
    pack, index = _view(context, call, task, current, allowance)
    assert pack.estimated_tokens <= allowance
    assert index["incoming_request"]["question_delivery"] != (
        "full_in_current_tool_result"
    )
    if revise_request:
        assert index["incoming_request"]["ref"]["version"] == 3


def test_saved_result_with_different_question_cannot_suppress_current_question(tmp_path):
    _store, context, call, task, current = _case(tmp_path, alter_result=True)
    pack, index = _view(context, call, task, current, 2700)
    assert pack.estimated_tokens <= 2700
    assert index["incoming_request"]["question_delivery"] != (
        "full_in_current_tool_result"
    )


def test_complete_current_question_read_keeps_exact_callable_reply_under_pressure(tmp_path):
    store, context, call, task, current = _case(tmp_path)
    original = store.get(**call)["body"]
    call = store.transact("reply-after-question-read", {}, lambda tx: ref(tx.put(
        "agent_call", "reply-after-question-read", {
            **original,
            "exposed_tools": ["publish_result", "read_context_record"],
            "permitted_tools": ["publish_result", "read_context_record"],
        },
    )), mission_id="m")
    compact, index = _view(context, call, task, current, 2500)
    assert compact.estimated_tokens <= 2500
    incoming = index["incoming_request"]
    assert incoming["question_delivery"] == "full_in_current_tool_result"
    assert incoming["response_needed"] is True
    assert incoming["reply"] == {
        "tool": "publish_result", "answer_request_id": "delivery-request",
    }
    result = next(message for message in compact.messages
                  if message.get("name") == "read_context_record")
    window = json.loads(result["content"])
    assert json.loads(window["text"])["question"] == store.get(
        "mail_request", "delivery-request",
    )["body"]["question"]
