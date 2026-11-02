"""Peer question cues describe saved-text delivery, not the sender's wording quality."""

import json

import pytest

from ora.coordination.board import ref
from ora.coordination.mailbox import PeerMailbox
from ora.mission.context import WorkingContext
from ora.mission.tool_catalog import TOOL_DESCRIPTIONS
from ora.state import StateStore
from tests.mission.test_context import bind_no_source_pack, fixture


def _incoming(store, task, question):
    mailbox = PeerMailbox(store)
    sent = mailbox.send_request(
        "question-delivery-request", mission_id="m", request_id="delivery-request",
        message_id="delivery-message", requester_id="sender", recipient_id="peer",
        question=question, expected_artifact="A synthetic answer", cause_ref=task,
        payload={"question": question}, task_ref=task,
    )
    assert sent["status"] == "accepted"
    assert mailbox.mark_delivered(
        "question-delivery-ack", mission_id="m", message_id="delivery-message",
    )["status"] == "accepted"
    return store.get("mail_request", "delivery-request")


@pytest.mark.parametrize("question,delivery", [
    ("What is the design/measur", "full_saved_text"),
    ("¿Qué se midió? 研究結果", "full_saved_text"),
    ("研究" * 301, "prefix_only"),
    ("研究" * 1200, "prefix_only"),
])
def test_incoming_question_marks_exact_full_or_prefix_delivery(
    tmp_path, question, delivery,
):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    request = _incoming(store, task, question)
    context = WorkingContext(store)
    pack = context.assemble(call, task, allowance=20_000, estimate_tokens=len)
    index = json.loads(pack.messages[-1]["content"])
    incoming = index["incoming_request"]
    assert incoming["ref"] == ref(request)
    assert incoming["question_delivery"] == delivery
    assert index["task"]["objective"] == "Compare synthetic reports"
    if delivery == "full_saved_text":
        assert incoming["question"] == question
        assert incoming["saved_question_has_no_hidden_suffix"] is True
        assert "question_window" not in incoming
        assert "read_full_question" not in incoming
    else:
        assert "saved_question_has_no_hidden_suffix" not in incoming
        assert incoming["question_status"] == "prefix_window_only"
        assert incoming["question_window"] == question[:600]
        assert incoming["question_chars"] == len(question)
        assert "read_context_record" in incoming["read_full_question"]

    inclusion = bind_no_source_pack(store, context, pack, "delivery-inclusion")
    parts = []
    offset = 0
    first_next_offset = None
    while True:
        saved = context.read_omitted_record(
            mission_id="m", actor_id="peer", task_id=task["id"],
            inclusion_ref=inclusion, pointer=ref(request), offset=offset, limit=2000,
        )
        parts.append(saved["text"])
        if offset == 0:
            first_next_offset = saved["next_offset"]
        if saved["next_offset"] is None:
            break
        assert saved["next_offset"] > offset
        offset = saved["next_offset"]
    assert json.loads("".join(parts))["question"] == question
    assert saved["returned_chars"] + saved["offset"] == saved["total_chars"]
    if len(question) > 2000:
        assert first_next_offset is not None
    else:
        assert first_next_offset is None


def test_latest_request_version_keeps_full_delivery_marker_and_exact_ref(tmp_path):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    request = _incoming(store, task, "A clear synthetic question?")
    updated = store.transact("revise-request-metadata", {}, lambda tx: ref(tx.put(
        "mail_request", request["id"], {
            **request["body"], "expected_artifact": "A revised synthetic answer",
        }, expected_version=request["version"],
    )), mission_id="m")
    pack = WorkingContext(store).assemble(
        call, task, allowance=20_000, estimate_tokens=len,
    )
    incoming = json.loads(pack.messages[-1]["content"])["incoming_request"]
    assert incoming["ref"] == updated
    assert incoming["question"] == "A clear synthetic question?"
    assert incoming["question_delivery"] == "full_saved_text"
    assert "offset=0" in TOOL_DESCRIPTIONS["read_context_record"]
    assert "saved version" in TOOL_DESCRIPTIONS["read_context_record"]
    assert "not source coverage" in TOOL_DESCRIPTIONS["read_context_record"]
    assert "Original source text uses read_source_window" in (
        TOOL_DESCRIPTIONS["read_context_record"]
    )


@pytest.mark.parametrize("question", [
    "Which synthetic comparison is supported?",
    "What about the compar",
    "Explain the synthetic comparison. " + "Q" * 1200,
])
def test_reply_route_uses_exact_saved_request_only_with_full_question(
    tmp_path, question,
):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    request = _incoming(store, task, question)
    original = store.get(**call)["body"]
    exposed = ["publish_result", "request_capability", "read_context_record"]
    call = ref(store.transact(
        "synthetic-reply-route-call", {},
        lambda tx: tx.put("agent_call", "synthetic-reply-route-call", {
            **original, "exposed_tools": exposed, "permitted_tools": exposed,
        }), mission_id="m",
    ))
    pack = WorkingContext(store).assemble(
        call, task, allowance=20_000, estimate_tokens=len,
    )
    incoming = json.loads(pack.messages[-1]["content"])["incoming_request"]
    assert incoming["ref"] == ref(request)
    if incoming["question_delivery"] == "full_saved_text":
        assert incoming["question"] == question
        assert incoming["saved_question_has_no_hidden_suffix"] is True
        assert incoming["reply"] == {
            "tool": "publish_result", "answer_request_id": request["id"],
        }
    else:
        assert incoming["question_delivery"] == "prefix_only"
        assert "saved_question_has_no_hidden_suffix" not in incoming
        assert "reply" not in incoming
        assert "read_context_record" in incoming["read_full_question"]
    assert "incoming_request.reply.answer_request_id" in (
        TOOL_DESCRIPTIONS["publish_result"]
    )


@pytest.mark.parametrize("route", ["attached", "deferred", "unavailable"])
@pytest.mark.parametrize("long_question", [False, True])
def test_incoming_response_need_and_usable_reply_route_share_question_input(
    tmp_path, route, long_question,
):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    question = "Which synthetic finding is supported?" + (" Detail." * 120 if long_question else "")
    request = _incoming(store, task, question)
    original = store.get(**call)["body"]
    exposed = ["read_context_record", "request_capability"]
    permitted = [*exposed]
    if route in {"attached", "deferred"}:
        permitted.append("publish_result")
    if route == "attached":
        exposed.append("publish_result")
    call = ref(store.transact(
        "response-need-call", {}, lambda tx: tx.put("agent_call", "response-need-call", {
            **original, "exposed_tools": exposed, "permitted_tools": permitted,
        }), mission_id="m",
    ))
    packed = WorkingContext(store).assemble(
        call, task, allowance=20_000, estimate_tokens=len,
    )
    incoming = json.loads(packed.messages[-1]["content"])["incoming_request"]
    assert incoming["ref"] == ref(request)
    assert incoming["response_needed"] is True
    if long_question:
        assert incoming["question_delivery"] == "prefix_only"
        assert "read_context_record" in incoming["read_full_question"]
        assert "reply" not in incoming
        assert "reply_setup" not in incoming
    else:
        assert incoming["question"] == question
        if route == "attached":
            assert incoming["reply"] == {
                "tool": "publish_result", "answer_request_id": request["id"],
            }
            assert "reply_setup" not in incoming
        elif route == "deferred":
            assert incoming["reply"] == {
                "tool": "publish_result", "answer_request_id": request["id"],
            }
            assert incoming["reply_setup"] == {
                "tool": "request_capability", "tool_id": "publish_result",
            }
        else:
            assert "reply" not in incoming
            assert "reply_setup" not in incoming


@pytest.mark.parametrize("status", [
    "partly_answered", "awaiting_review", "answered", "cancelled", "declined",
])
def test_response_need_follows_current_request_status(tmp_path, status):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    request = _incoming(store, task, "Which synthetic finding is supported?")
    current = store.transact(
        "response-need-status", {}, lambda tx: ref(tx.put("mail_request", request["id"], {
            **request["body"], "status": status,
        }, expected_version=request["version"])), mission_id="m",
    )
    packed = WorkingContext(store).assemble(
        call, task, allowance=20_000, estimate_tokens=len,
    )
    index = json.loads(packed.messages[-1]["content"])
    if status == "partly_answered":
        assert index["incoming_request"]["ref"] == current
        assert index["incoming_request"]["response_needed"] is True
    else:
        assert "incoming_request" not in index


def test_answered_first_request_promotes_next_exact_question_and_reply_id(tmp_path):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    first = _incoming(store, task, "Check the first synthetic finding.")
    mailbox = PeerMailbox(store)
    assert mailbox.send_request(
        "second-ask", mission_id="m", request_id="second-request",
        message_id="second-message", requester_id="sender", recipient_id="peer",
        question="Check the second synthetic finding.", expected_artifact="Synthetic answer",
        cause_ref=task, payload={"question": "Check the second synthetic finding."}, task_ref=task,
    )["status"] == "accepted"
    assert mailbox.mark_delivered(
        "second-delivery", mission_id="m", message_id="second-message",
    )["status"] == "accepted"
    original = store.get(**call)["body"]
    call = store.transact("two-ask-call", {}, lambda tx: ref(tx.put(
        "agent_call", "two-ask-call", {
            **original, "exposed_tools": ["publish_result"],
            "permitted_tools": ["publish_result"],
        },
    )), mission_id="m")
    context = WorkingContext(store)

    def incoming():
        pack = context.assemble(call, task, allowance=20_000, estimate_tokens=len)
        return json.loads(pack.messages[-1]["content"])["incoming_request"]

    assert incoming()["reply"]["answer_request_id"] == first["id"]
    artifact = store.transact("synthetic-answer-artifact", {}, lambda tx: ref(tx.put(
        "source_passage", "synthetic-answer-passage", {"text": "Synthetic answer"},
    )), mission_id="m")
    assert mailbox.record_answer(
        "first-answer", mission_id="m", request_id=first["id"],
        answer_id="first-answer", responder_id="peer", artifact_ref=artifact,
    )["status"] == "accepted"
    assert store.get("mail_request", first["id"])["body"]["status"] == "awaiting_review"
    next_card = incoming()
    assert next_card["ref"] == ref(store.get("mail_request", "second-request"))
    assert next_card["question"] == "Check the second synthetic finding."
    assert next_card["response_needed"] is True
    assert next_card["reply"]["answer_request_id"] == "second-request"
    assert len(store.records("mail_answer")) == 1
