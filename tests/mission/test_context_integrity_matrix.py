"""Check exact peer input across budgets, restart, and separate mission runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import ContextError, WorkingContext
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack
from tests.mission.test_peer_answer_preview import _fixture
from tests.mission.test_service import MODEL, Response, serialized_peer_name, service, tool


def test_wide_and_tight_peer_input_replays_exactly_after_reopen(tmp_path) -> None:
    marker = "Synthetic answer marker: BLUE-WIDE-TIGHT. " * 10
    store = StateStore(tmp_path)
    saved = _fixture(store, body=marker)
    context = WorkingContext(store)
    control_ref = ref(store.get("mission_task_control", "requester-control"))

    wide = context.assemble(
        saved["call_ref"], saved["task_ref"], control_ref=control_ref,
        allowance=3000, estimate_tokens=len,
    )
    wide_index = json.loads(wide.messages[-1]["content"])
    assert wide_index["peer_answer_previews"][0]["question"] == (
        "Please review this synthetic claim."
    )
    assert wide_index["peer_answer_previews"][0]["report"]["body"] == marker
    assert wide_index["peer_answer_previews"][0]["request_ref"] == saved["request_ref"]
    assert wide_index["peer_answer_previews"][0]["answer_ref"] == saved["answer_ref"]
    assert wide_index["peer_answer_previews"][0]["request_status"] == "awaiting_review"
    wide_inclusion = bind_no_source_pack(store, context, wide, "matrix-wide")
    wide_intent = store.get("model_request_intent", "model-intent:requester-call:1")
    wide_bytes = store.read_blob(wide_intent["body"]["blob"]["sha256"])

    old_call = store.get(**saved["call_ref"])
    tight_call = store.transact("matrix-tight-call", {}, lambda tx: ref(tx.put(
        "agent_call", "requester-tight-call", old_call["body"],
    )), mission_id="m")
    tight = context.assemble(
        tight_call, saved["task_ref"], control_ref=control_ref,
        allowance=1500, estimate_tokens=len,
    )
    tight_index = json.loads(tight.messages[-1]["content"])
    cue = tight_index["peer_answer_review_cues"][0]
    assert cue["question"] == wide_index["peer_answer_previews"][0]["question"]
    assert cue["request_ref"] == saved["request_ref"]
    assert cue["answer_ref"] == saved["answer_ref"]
    assert cue["partial"] is False
    assert cue["owner"] == "you" and cue["action"] == "review_peer_answer"
    assert {key: cue["read_request"][key] for key in ("kind", "id", "version")} == (
        saved["report_ref"]
    )
    assert "peer_answer_previews" not in tight_index
    assert marker not in canonical(tight_index)
    assert saved["report_ref"] not in tight.included_refs
    tight_inclusion = bind_no_source_pack(store, context, tight, "matrix-tight")
    tight_intent = store.get("model_request_intent", "model-intent:requester-tight-call:1")
    tight_bytes = store.read_blob(tight_intent["body"]["blob"]["sha256"])
    assert hashlib.sha256(wide_bytes).hexdigest() == wide_intent["body"]["request_sha256"]
    assert hashlib.sha256(tight_bytes).hexdigest() == tight_intent["body"]["request_sha256"]

    reopened = StateStore(tmp_path)
    assert reopened.read_blob(wide_intent["body"]["blob"]["sha256"]) == wide_bytes
    assert reopened.read_blob(tight_intent["body"]["blob"]["sha256"]) == tight_bytes
    assert reopened.get(**wide_inclusion)["body"]["intent_ref"] == ref(wide_intent)
    assert reopened.get(**tight_inclusion)["body"]["intent_ref"] == ref(tight_intent)
    reader = WorkingContext(reopened)
    assert reader.context_source_dependency_refs(tight) == ()
    assert reopened.get(**saved["request_ref"])["body"]["status"] == "awaiting_review"
    parts, offset = [], 0
    while True:
        page = reader.read_omitted_record(
            mission_id="m", actor_id="requester", task_id=saved["task_ref"]["id"],
            inclusion_ref=tight_inclusion, pointer=saved["report_ref"],
            offset=offset, limit=2000,
        )
        assert page["report_text_status"] == "available"
        parts.append(page["text"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert json.loads("".join(parts))["body"] == marker
    with pytest.raises(ContextError):
        reader.read_omitted_record(
            mission_id="m", actor_id="responder", task_id=saved["task_ref"]["id"],
            inclusion_ref=tight_inclusion, pointer=saved["report_ref"],
            offset=0, limit=2000,
        )
    with pytest.raises(ContextError):
        reader.read_omitted_record(
            mission_id="m", actor_id="requester", task_id=saved["peer_task_ref"]["id"],
            inclusion_ref=tight_inclusion, pointer=saved["report_ref"],
            offset=0, limit=2000,
        )


@pytest.mark.parametrize("page_limit", [1000, 2000])
def test_repeated_report_page_after_reopen_has_no_new_peer_effect(
    tmp_path, page_limit,
) -> None:
    body = "Synthetic long peer assessment. " * 150
    store = StateStore(tmp_path)
    saved = _fixture(store, body=body)
    context = WorkingContext(store)
    control_ref = ref(store.get("mission_task_control", "requester-control"))
    pack = context.assemble(
        saved["call_ref"], saved["task_ref"], control_ref=control_ref,
        allowance=1500, estimate_tokens=len,
    )
    index = json.loads(pack.messages[-1]["content"])
    assert "peer_answer_previews" not in index
    assert body not in canonical(index)
    inclusion = bind_no_source_pack(store, context, pack, f"repeat-page-{page_limit}")
    scope = {
        "mission_id": "m", "actor_id": "requester", "task_id": "requester-task",
        "inclusion_ref": inclusion, "pointer": saved["report_ref"],
    }
    first = context.read_omitted_record(**scope, offset=0, limit=page_limit)
    assert first["report_text_status"] == "available"
    assert first["next_offset"] == page_limit
    assert body not in first["text"]
    before = {
        kind: len(store.records(kind))
        for kind in ("report", "mail_answer", "context_tool_result", "source_copy")
    }
    request_before = store.get(**saved["request_ref"])

    reopened = StateStore(tmp_path)
    reader = WorkingContext(reopened)
    assert reader.read_omitted_record(**scope, offset=0, limit=page_limit) == first
    chunks = [first["text"]]
    offset = first["next_offset"]
    while offset is not None:
        page = reader.read_omitted_record(**scope, offset=offset, limit=page_limit)
        chunks.append(page["text"])
        offset = page["next_offset"]
    assert json.loads("".join(chunks))["body"] == body
    assert reopened.get(**saved["request_ref"]) == request_before
    assert reopened.get(**saved["request_ref"])["body"]["accepted_answer_ref"] is None
    assert {
        kind: len(reopened.records(kind)) for kind in before
    } == before


def test_independent_runs_keep_answer_and_source_in_their_own_serialized_input(
    tmp_path, monkeypatch,
) -> None:
    def no_socket(*_args, **_kwargs):
        raise AssertionError("Offline context integrity test opened a socket")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(socket.socket, "connect_ex", no_socket)
    source_a = "RUN_A_SOURCE_MARKER: synthetic observations from one project."
    source_b = "RUN_B_SOURCE_MARKER: independent synthetic observations."
    reply_a = "RUN_A_PEER_ANSWER_MARKER: a bounded provisional interpretation."
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        assert serialized_peer_name(packet) == "Sage"
        assert source_a in canonical(packet["messages"])
        assert source_b not in canonical(packet["messages"])
        return tool("publish_result", {
            "title": "Run A provisional answer", "text": reply_a,
            "citations": [], "limitations": ["Synthetic source only"],
            "answer_request_id": "run-a-peer-request",
        }, 1)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run_a = host.start(LiveStartRequest(
            request_id="integrity-run-a", question="Assess run A only.",
            evidence_text=source_a,
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        run_b = host.start(LiveStartRequest(
            request_id="integrity-run-b", question="Assess run B independently.",
            evidence_text=source_b,
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        sage_a = host._control(run_a.run_id, "agent-synthesis")
        mxra_a = host._control(run_a.run_id, "agent-critique")
        result = host.mailbox.send_request(
            "send-run-a-peer-request", mission_id=sage_a["body"]["mission_id"],
            request_id="run-a-peer-request", message_id="run-a-peer-message",
            requester_id=mxra_a["body"]["actor_id"],
            recipient_id=sage_a["body"]["actor_id"],
            question="What does run A's synthetic source support?",
            expected_artifact="A short provisional report",
            cause_ref=mxra_a["body"]["proposal_ref"],
            payload={"question": "What does run A's synthetic source support?"},
            task_ref=mxra_a["body"]["proposal_ref"],
        )
        assert result["status"] == "accepted"
        host.mailbox.mark_delivered(
            "deliver-run-a-peer-message", mission_id=sage_a["body"]["mission_id"],
            message_id="run-a-peer-message",
        )
        assert host._schedule_model(host._control(run_a.run_id, "agent-synthesis"))
        asyncio.run(host.executor.run_ready(sage_a["body"]["mission_id"]))
        assert host._advance(host._control(run_a.run_id, "agent-synthesis"))
        assert host._schedule_tool(host._control(run_a.run_id, "agent-synthesis"))
        asyncio.run(host.executor.run_ready(sage_a["body"]["mission_id"]))
        assert host._advance(host._control(run_a.run_id, "agent-synthesis"))
        request_a = host.store.get("mail_request", "run-a-peer-request")
        assert len(request_a["body"]["answer_refs"]) == 1
        answer_ref = request_a["body"]["answer_refs"][0]
        report_ref = host.store.get(**answer_ref)["body"]["artifact_ref"]

        def prepared(run_id: str, alias: str) -> dict:
            assert host._schedule_model(host._control(run_id, alias))
            call_ref = host._control(run_id, alias)["body"]["call_ref"]
            intent = host.store.get(
                "model_request_intent",
                f"model-intent:{call_ref['id']}:{call_ref['version']}",
            )
            raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
            assert hashlib.sha256(raw).hexdigest() == intent["body"]["request_sha256"]
            return json.loads(raw)

        packet_a = prepared(run_a.run_id, "agent-critique")
        packet_b_sage = prepared(run_b.run_id, "agent-synthesis")
        packet_b_mxra = prepared(run_b.run_id, "agent-critique")
        index_a = json.loads(packet_a["messages"][-1]["content"])
        cards = index_a.get("peer_answer_previews", [])
        assert len(cards) == 1
        assert cards[0]["answer_ref"] == answer_ref
        assert cards[0]["report_ref"] == report_ref
        assert cards[0]["question"] == "What does run A's synthetic source support?"
        assert cards[0]["report"]["body"] == reply_a
        assert source_a in canonical(packet_a["messages"])
        for packet_b in (packet_b_sage, packet_b_mxra):
            serialized = canonical(packet_b["messages"])
            context = json.loads(packet_b["messages"][-1]["content"])
            assert "Assess run B independently." in serialized
            assert source_b in serialized
            assert source_a not in serialized
            assert reply_a not in serialized
            assert answer_ref["id"] not in serialized
            assert "run-a-peer-request" not in serialized
            assert not context.get("peer_answer_previews")
            assert not context.get("peer_answer_review_cues")
        assert len(packets) == 1  # Only the synthetic run-A reply was dispatched.
    finally:
        host.close()


@pytest.mark.parametrize("visible_text", [None, "An apparent answer, still unfinished."])
def test_cutoff_peer_turn_does_not_create_an_answer(
    tmp_path, monkeypatch, visible_text,
) -> None:
    def no_socket(*_args, **_kwargs):
        raise AssertionError("Offline cutoff test opened a socket")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(socket.socket, "connect_ex", no_socket)
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        assert serialized_peer_name(packet) == "Sage"
        assert "What is the bounded synthetic interpretation?" in canonical(packet["messages"])
        return Response({
            "id": "synthetic-peer-cutoff", "model": MODEL,
            "choices": [{"message": {"role": "assistant", "content": visible_text},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
        })

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"peer-cutoff-{visible_text is not None}",
            question="Assess only the synthetic evidence.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        sage = host._control(run.run_id, "agent-synthesis")
        requester = host._control(run.run_id, "agent-critique")
        sent = host.mailbox.send_request(
            "send-peer-cutoff-request", mission_id=sage["body"]["mission_id"],
            request_id="cutoff-peer-request", message_id="cutoff-peer-message",
            requester_id=requester["body"]["actor_id"],
            recipient_id=sage["body"]["actor_id"],
            question="What is the bounded synthetic interpretation?",
            expected_artifact="A short report",
            cause_ref=requester["body"]["proposal_ref"],
            payload={"question": "What is the bounded synthetic interpretation?"},
            task_ref=requester["body"]["proposal_ref"],
        )
        assert sent["status"] == "accepted"
        host.mailbox.mark_delivered(
            "deliver-peer-cutoff-request", mission_id=sage["body"]["mission_id"],
            message_id="cutoff-peer-message",
        )
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        asyncio.run(host.executor.run_ready(sage["body"]["mission_id"]))
        assert host._advance(host._control(run.run_id, "agent-synthesis"))
        receipts = host.store.records("model_response_receipt")
        assert len(packets) == len(receipts) == 1
        assert receipts[0]["body"]["status"] == "partial"
        assert receipts[0]["body"]["finish_reason"] == "length"
        pending = host.store.get("mail_request", "cutoff-peer-request")["body"]
        assert pending["status"] == "unanswered"
        assert pending["answer_refs"] == []
        assert host.store.records("mail_answer") == []
        assert host.store.records("report") == []
        assert host._run(run.run_id)["body"]["status"] == "active"
    finally:
        host.close()
