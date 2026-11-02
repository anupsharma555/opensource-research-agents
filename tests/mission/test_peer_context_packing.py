"""Tight peer inputs retain questions and exact request navigation."""

from __future__ import annotations

import hashlib
import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.coordination.mailbox import PeerMailbox
from ora.mission.context import ContextError, WorkingContext
from ora.models.token_estimate import estimate_context_tokens
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.availability_assertions import request_first_names
from tests.mission.test_context import bind_no_source_pack, fixture
from tests.mission.test_service import service


def _question_message_pair(
    store: StateStore, task: dict, *, incoming_question: str,
    incoming_payload: dict, outgoing_question: str,
    outgoing_payload: dict, incoming_evidence_refs: list[dict] | None = None,
) -> tuple[dict, dict, dict, dict]:
    mailbox = PeerMailbox(store)
    incoming = mailbox.send_request(
        "send-exact-incoming", mission_id="m", request_id="exact-incoming",
        message_id="exact-incoming-message", requester_id="sender",
        recipient_id="peer", question=incoming_question,
        expected_artifact="Synthetic check", cause_ref=task,
        payload=incoming_payload, task_ref=task,
        evidence_refs=incoming_evidence_refs,
    )
    outgoing = mailbox.send_request(
        "send-exact-outgoing", mission_id="m", request_id="exact-outgoing",
        message_id="exact-outgoing-message", requester_id="peer",
        recipient_id="sender", question=outgoing_question,
        expected_artifact="Synthetic check", cause_ref=task,
        payload=outgoing_payload, task_ref=task,
    )
    assert incoming["status"] == outgoing["status"] == "accepted"
    for message_id in ("exact-incoming-message", "exact-outgoing-message"):
        delivered = mailbox.mark_delivered(
            f"deliver-{message_id}", mission_id="m", message_id=message_id,
        )
        assert delivered["status"] == "accepted"
    return (
        store.get("mail_request", "exact-incoming"),
        store.get("mail_message", "exact-incoming-message"),
        store.get("mail_request", "exact-outgoing"),
        store.get("mail_message", "exact-outgoing-message"),
    )


def test_exact_duplicate_peer_messages_page_but_full_questions_remain(tmp_path):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    incoming_question = "Check the first synthetic tie exactly."
    outgoing_question = "Compare the second synthetic tie exactly."
    incoming, incoming_message, outgoing, outgoing_message = _question_message_pair(
        store, task,
        incoming_question=incoming_question,
        incoming_payload={"question": incoming_question},
        outgoing_question=outgoing_question,
        outgoing_payload={"question": outgoing_question},
    )
    context = WorkingContext(store)
    pack = context.assemble(
        call, task, allowance=20_000, estimate_tokens=estimate_context_tokens,
    )
    view = json.loads(pack.messages[-1]["content"])
    assert view["incoming_request"]["ref"] == ref(incoming)
    assert view["incoming_request"]["question"] == incoming_question
    assert view["incoming_request"]["question_delivery"] == "full_saved_text"
    assert any(
        detail["type"] == "request" and detail["ref"] == ref(outgoing)
        and detail["body"]["question"] == outgoing_question
        for detail in view["details"]
    )
    assert not any(
        detail["ref"] in (ref(incoming_message), ref(outgoing_message))
        for detail in view["details"]
    )
    assert ref(incoming_message) in view["message_refs"]
    assert ref(outgoing_message) in view["outgoing_request_message_refs"]
    omitted = {item["ref"]["id"] for item in pack.omitted}
    assert {incoming_message["id"], outgoing_message["id"]} <= omitted
    assert view["omitted"]["count"] == len(pack.omitted)

    inclusion = bind_no_source_pack(store, context, pack, "duplicate-peer-messages")
    reopened = WorkingContext(StateStore(store.root))
    page = reopened.read_omitted_page(
        mission_id="m", actor_id="peer", task_id=task["id"],
        inclusion_ref=inclusion, cursor=0, limit=20,
    )
    assert {incoming_message["id"], outgoing_message["id"]} <= {
        item["ref"]["id"] for item in page["items"]
    }
    for pointer in (ref(incoming_message), ref(outgoing_message)):
        listed = next(item for item in page["items"] if item["ref"] == pointer)
        assert listed["summary"]["request_id"] in {incoming["id"], outgoing["id"]}
        assert listed["summary"]["delivered"] is True
        assert listed["read_request"]["tool"] == "read_context_record"
        message_record = reopened.read_omitted_record(
            mission_id="m", actor_id="peer", task_id=task["id"],
            inclusion_ref=inclusion, pointer=pointer, offset=0, limit=2000,
        )
        expected = reopened.store.get(**pointer)["body"]["payload"]
        assert json.loads(message_record["text"])["payload"] == expected
    request_record = reopened.read_omitted_record(
        mission_id="m", actor_id="peer", task_id=task["id"],
        inclusion_ref=inclusion, pointer=ref(incoming), offset=0, limit=2000,
    )
    assert json.loads(request_record["text"])["question"] == incoming_question
    with pytest.raises(ContextError):
        reopened.read_omitted_record(
            mission_id="m", actor_id="sender", task_id=task["id"],
            inclusion_ref=inclusion, pointer=ref(incoming_message), offset=0,
            limit=2000,
        )


@pytest.mark.parametrize("reader_mode", ("attached", "unavailable", "legacy_no_tool_fields"))
def test_peer_question_pressure_preserves_exact_messages_without_a_reader(
    tmp_path, reader_mode,
):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    mailbox = PeerMailbox(store)
    question = ("Check this synthetic comparison and explain limitations. " + "Q" * 600)[:548]
    for number in range(8):
        sender, recipient = (("sender", "peer") if number % 2 == 0
                             else ("peer", "sender"))
        text = question if number == 0 else (
            f"Synthetic question {number} " + "X" * (100 + number * 30)
        )
        sent = mailbox.send_request(
            f"pressure-ask-{number}", mission_id="m", request_id=f"pressure-req-{number}",
            message_id=f"pressure-msg-{number}", requester_id=sender,
            recipient_id=recipient, question=text, expected_artifact="Synthetic note",
            cause_ref=task, payload={}, task_ref=task,
        )
        assert sent["status"] == "accepted"
        assert mailbox.mark_delivered(
            f"pressure-deliver-{number}", mission_id="m",
            message_id=f"pressure-msg-{number}",
        )["status"] == "accepted"

    if reader_mode != "legacy_no_tool_fields":
        original = store.get(**call)["body"]
        exposed = (["request_capability", "read_context_page", "read_context_record"]
                   if reader_mode == "attached" else ["request_capability"])
        call = ref(store.transact(
            f"pressure-call-{reader_mode}", {},
            lambda tx: tx.put("agent_call", f"pressure-call-{reader_mode}", {
                **original, "exposed_tools": exposed, "permitted_tools": exposed,
            }), mission_id="m",
        ))

    context = WorkingContext(store)
    pack = context.assemble(
        call, task, allowance=500, estimate_tokens=estimate_context_tokens,
    )
    index = json.loads(pack.messages[-1]["content"])
    incoming = index["incoming_request"]
    assert incoming["ref"] == ref(store.get("mail_request", "pressure-req-0"))
    assert pack.estimated_tokens <= 500
    redundant = [
        ref(store.get("mail_message", f"pressure-msg-{number}"))
        for number in (4, 6)
    ]
    # These two full message records are omitted from the packet in every mode.
    assert all(pointer in [item["ref"] for item in pack.omitted] for pointer in redundant)
    if reader_mode == "attached":
        assert incoming["question"] == question
        assert incoming["question_delivery"] == "full_saved_text"
        assert all(pointer not in index.get("message_refs", []) for pointer in redundant)
        inclusion = bind_no_source_pack(store, context, pack, "pressure-attached")
        reopened = WorkingContext(StateStore(tmp_path))
        listed = []
        cursor = 0
        while True:
            page = reopened.read_omitted_page(
                mission_id="m", actor_id="peer", task_id=task["id"],
                inclusion_ref=inclusion, cursor=cursor, limit=30,
            )
            listed.extend(item["ref"] for item in page["items"])
            if page["next_cursor"] is None:
                break
            assert page["next_cursor"] > cursor
            cursor = page["next_cursor"]
        assert all(pointer in listed for pointer in redundant)
        message = reopened.read_omitted_record(
            mission_id="m", actor_id="peer", task_id=task["id"],
            inclusion_ref=inclusion,
            pointer=ref(store.get("mail_message", "pressure-msg-6")),
            offset=0, limit=2000,
        )
        assert json.loads(message["text"])["request_id"] == "pressure-req-6"
    else:
        assert incoming["question_status"] == "prefix_window_only"
        assert incoming["question_delivery"] == "prefix_only"
        assert question.startswith(incoming["question_window"])
        assert len(incoming["question_window"]) < len(question)
        assert all(pointer in index["message_refs"] for pointer in redundant)


@pytest.mark.parametrize("reason", [
    "different_payload", "partial_card", "extra_payload", "evidence_ref",
])
def test_distinct_or_partial_peer_message_body_remains_visible(tmp_path, reason):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    question = (
        "Review the full synthetic row sequence: " + "Q" * 700
        if reason == "partial_card" else "Review the first synthetic row."
    )
    payload = (
        {"question": "Review a different synthetic row."}
        if reason == "different_payload" else
        {"question": question, "note": "Distinct message annotation"}
        if reason == "extra_payload" else {"question": question}
    )
    incoming, incoming_message, _, _ = _question_message_pair(
        store, task, incoming_question=question, incoming_payload=payload,
        outgoing_question="Check the other synthetic row.",
        outgoing_payload={"question": "Check the other synthetic row."},
        incoming_evidence_refs=(
            [ref(store.get("source_copy", "copy"))]
            if reason == "evidence_ref" else None
        ),
    )
    if reason == "evidence_ref":
        copy_ref = ref(store.get("source_copy", "copy"))

        def grant_attachment(tx):
            use = tx.put("source_use", "peer-packing-use", {
                "copy_ref": copy_ref, "allowed_uses": ["model_input"],
                "policy_status": "granted",
            })
            tx.bind("source_use.copy", "copy:1", "source_use", use["id"])
            return ref(use)

        store.transact("grant-peer-packing-source", {}, grant_attachment, mission_id="m")
    pack = WorkingContext(store).assemble(
        call, task, allowance=20_000, estimate_tokens=estimate_context_tokens,
    )
    view = json.loads(pack.messages[-1]["content"])
    card = view["incoming_request"]
    assert card["ref"] == ref(incoming)
    if reason == "partial_card":
        assert card["question_status"] == "prefix_window_only"
        assert "question" not in card
    else:
        assert card["question"] == question
    assert any(
        detail["type"] == "message" and detail["ref"] == ref(incoming_message)
        and detail["body"]["payload"] == payload
        for detail in view["details"]
    )
    if reason == "evidence_ref":
        assert incoming_message["body"]["evidence_refs"]


def _two_outgoing_requests(
    store: StateStore, *, question_suffix: str = "", separate_recipients: bool = False,
) -> tuple:
    call, task = fixture(store)
    if separate_recipients:
        store.transact("alternate-peer", {}, lambda tx: ref(tx.put(
            "agent_instance", "alternate", {"mission_id": "m"},
        )), mission_id="m")
    sent = []
    for number in (1, 2):
        question = f"Check the current evidence and tie policy {number}. {question_suffix}"
        recipient_id = "alternate" if separate_recipients and number == 2 else "sender"
        saved = PeerMailbox(store).send_request(
            f"ask-{number}", mission_id="m", request_id=f"request-{number}",
            message_id=f"message-{number}", requester_id="peer", recipient_id=recipient_id,
            question=question, expected_artifact="Independent review",
            cause_ref=task, payload={}, task_ref=task,
        )
        sent.append((saved["request_ref"], question, recipient_id))

    def controls(tx):
        tx.put("mission_run", "peer-run", {
            "mission_id": "m", "question": "Compare synthetic reports",
            "scope": "Use only supplied evidence", "source_refs": [],
        })
        own = tx.put("mission_task_control", "requester-control", {
            "mission_id": "m", "run_id": "peer-run", "actor_id": "peer",
            "task_id": task["id"], "source_refs": [],
        })
        recipient = tx.put("mission_task_control", "recipient-control", {
            "mission_id": "m", "run_id": "peer-run", "actor_id": "sender",
            "task_id": "recipient-task", "source_refs": [],
        })
        recipients = {"sender": ref(recipient)}
        if separate_recipients:
            alternate = tx.put("mission_task_control", "alternate-control", {
                "mission_id": "m", "run_id": "peer-run", "actor_id": "alternate",
                "task_id": "alternate-task", "source_refs": [],
            })
            recipients["alternate"] = ref(alternate)
        return {"own": ref(own), "recipients": recipients}

    controls_saved = store.transact("peer-controls", {}, controls, mission_id="m")
    return call, task, controls_saved["own"], controls_saved["recipients"], sent


def test_large_unanswered_request_keeps_question_and_pages_full_record(tmp_path):
    store = StateStore(tmp_path)
    call, task, control, recipients, sent = _two_outgoing_requests(
        store, question_suffix="Q" * 180,
    )
    context = WorkingContext(store)
    pack = context.assemble(call, task, control_ref=control,
                            allowance=4000, estimate_tokens=len)
    view = json.loads(pack.messages[-1]["content"])
    previews = [entry for entry in view["details"]
                if entry.get("detail_status") == "selected_fields_full_record_in_older_records"]
    assert len(previews) == 1
    preview = previews[0]
    assert preview["ref"] in [pointer for pointer, _question, _recipient in sent]
    assert preview["body"]["question"] in [question for _pointer, question, _recipient in sent]
    assert preview["body"]["status"] == "unanswered"
    assert preview["body"]["recipient_id"] == "sender"
    assert "created_at" not in preview["body"]
    assert {item["ref"]["id"] for item in view["request_dispositions"]} == {
        pointer["id"] for pointer, _question, _recipient in sent
    }
    assert all(item["recipient_control_ref"] == recipients["sender"]
               for item in view["request_dispositions"])
    assert any(item["ref"] == preview["ref"] for item in pack.omitted)
    assert pack.estimated_tokens <= 4000

    inclusion = bind_no_source_pack(store, context, pack, "peer-preview")
    page = context.read_omitted_page(
        mission_id="m", actor_id="peer", task_id=task["id"],
        inclusion_ref=inclusion, cursor=0, limit=20,
    )
    assert any(item["ref"] == preview["ref"] for item in page["items"])
    full = context.read_omitted_record(
        mission_id="m", actor_id="peer", task_id=task["id"],
        inclusion_ref=inclusion, pointer=preview["ref"], offset=0, limit=2000,
    )
    assert json.loads(full["text"])["question"] == preview["body"]["question"]


def test_repeated_open_request_guidance_is_shared_only_under_pressure(tmp_path):
    store = StateStore(tmp_path)
    call, task, control, recipients, sent = _two_outgoing_requests(
        store, separate_recipients=True,
    )
    context = WorkingContext(store)
    ordinary = json.loads(context.assemble(call, task, control_ref=control).messages[-1]["content"])
    assert all("next_action" in item for item in ordinary["request_dispositions"])

    packed = context.assemble(call, task, control_ref=control,
                              allowance=1900, estimate_tokens=len)
    view = json.loads(packed.messages[-1]["content"])
    assert packed.estimated_tokens <= 1900
    assert "wait_for_dependencies" in view["open_request_next_action"]
    assert "review_peer_answer" in view["open_request_next_action"]
    assert {item["ref"]["id"] for item in view["request_dispositions"]} == {
        pointer["id"] for pointer, _question, _recipient in sent
    }
    expected_recipients = {
        pointer["id"]: recipients[recipient]
        for pointer, _question, recipient in sent
    }
    assert all(item["status"] == "unanswered"
               and item["recipient_control_ref"] == expected_recipients[item["ref"]["id"]]
               and "next_action" not in item
               for item in view["request_dispositions"])
    assert {item["ref"]["id"] for item in packed.omitted
            if item["type"] == "request"} == {
        pointer["id"] for pointer, _question, _recipient in sent
    }


def test_selected_source_and_latest_tool_result_survive_peer_pressure(tmp_path):
    from tests.mission.test_context_window_compaction import _window_turn

    store, context, task, copy_ref, window, refs, _result = _window_turn(tmp_path)
    sent = PeerMailbox(store).send_request(
        "selected-source-peer-request", mission_id="m", request_id="selected-request",
        message_id="selected-message", requester_id="peer", recipient_id="sender",
        question="Check the selected passage and tie policy: " + "Q" * 180,
        expected_artifact="Independent review", cause_ref=task, payload={}, task_ref=task,
    )
    pack = context.assemble(
        refs["next_call"], task, source_refs=[copy_ref], control_ref=refs["control"],
        tool_turn_refs=[refs["response"]], allowance=7000, estimate_tokens=len,
    )
    index = json.loads(pack.messages[-1]["content"])
    tool_result = json.loads(pack.messages[-2]["content"])
    assert pack.estimated_tokens <= 7000
    assert pack.source_refs == (copy_ref,)
    assert index["source_retrieval"][0]["ref"] == copy_ref
    assert index["source_retrieval"][0]["status"] == "permitted"
    assert sent["request_ref"] in [item["ref"] for item in index["request_dispositions"]]
    assert sent["request_ref"] in [item["ref"] for item in pack.omitted]
    assert tool_result["window_ref"] == window["window_ref"]
    assert tool_result["status"] == "saved_window_text_in_selected_source_packet"


def test_older_delivered_incoming_question_survives_real_token_pressure_and_reopens(
    tmp_path,
):
    from tests.mission.test_context_window_compaction import _window_turn

    store, context, task, copy_ref, window, refs, _result = _window_turn(tmp_path)
    mailbox = PeerMailbox(store)
    questions = {}
    for number, (requester, recipient, length) in enumerate((
        ("sender", "peer", 1400), ("peer", "sender", 548),
        ("sender", "peer", 306), ("peer", "sender", 462),
    ), 1):
        question = (
            f"Check the synthetic comparison {number}: "
            + " ".join(f"row-{i:03d} value={i * 37 % 991}" for i in range(100))
        )[:length]
        questions[f"request-{number}"] = question
        mailbox.send_request(
            f"send-{number}", mission_id="m", request_id=f"request-{number}",
            message_id=f"message-{number}", requester_id=requester,
            recipient_id=recipient, question=question,
            expected_artifact="Independent synthetic review", cause_ref=task,
            payload={}, task_ref=task,
        )
        mailbox.mark_delivered(
            f"deliver-{number}", mission_id="m", message_id=f"message-{number}",
        )

    draft = store.transact("seed-long-peer-draft", {}, lambda tx: ref(tx.put(
        "working_note", "peer-long-draft", {
            "mission_id": "m", "actor_id": "peer", "task_id": task["id"],
            "category": "draft", "status": "draft",
            "text": "Synthetic working assessment. " * 100,
            "input_refs": [],
        },
    )), mission_id="m")

    pack = context.assemble(
        refs["next_call"], task, source_refs=[copy_ref], control_ref=refs["control"],
        tool_turn_refs=[refs["response"]], note_refs=[draft], allowance=1300,
        estimate_tokens=estimate_context_tokens,
    )
    index = json.loads(pack.messages[-1]["content"])
    incoming = index["incoming_request"]
    first = store.get("mail_request", "request-1")
    assert incoming["ref"] == ref(first)  # Older inbound work survives later outbound asks.
    assert incoming["recipient_id"] == "peer"
    assert incoming["status"] == "unanswered"
    assert incoming["question_status"] == "prefix_window_only"
    assert incoming["question_delivery"] == "prefix_only"
    assert 120 <= len(incoming["question_window"]) <= 600
    assert questions[first["id"]].startswith(incoming["question_window"])
    assert incoming["question_chars"] == len(questions[first["id"]])
    assert "read_context_record" in incoming["read_full_question"]
    assert first["id"] in {item["ref"]["id"] for item in pack.omitted}
    assert pack.estimated_tokens <= 1300
    assert pack.source_refs == (copy_ref,)
    assert draft in [*index.get("note_refs", []), *(item["ref"] for item in pack.omitted)]
    assert json.loads(pack.messages[-2]["content"])["status"] == (
        "saved_window_text_in_selected_source_packet"
    )
    assert index["source_retrieval"][0]["ref"] == copy_ref

    request = {"messages": [
        {"role": "developer", "content": "Synthetic instructions"},
        {"role": "user", "content": canonical({"source_packets": [{
            "id": copy_ref["id"], "version": copy_ref["version"],
        }]})},
        *pack.messages,
    ]}
    encoded = canonical(request).encode()
    blob = store.put_blob(encoded)

    def save_intent(tx):
        return ref(tx.put("model_request_intent", "model-intent:window-next-call:1", {
            "mission_id": "m", "call_ref": pack.call_ref,
            "configuration_ref": pack.configuration_ref,
            "source_refs": list(pack.source_refs),
            "context_source_dependency_refs": list(context.context_source_dependency_refs(pack)),
            "source_decisions": [{
                "copy_ref": copy_ref,
                "passage_refs": store.get(**refs["control"])["body"]["selected_passage_refs"][
                    copy_ref["id"]
                ],
            }],
            "request_sha256": hashlib.sha256(encoded).hexdigest(), "blob": blob,
        }))

    intent = store.transact("bind-incoming-intent", {}, save_intent, mission_id="m")
    inclusion = context.save_inclusion("bind-incoming-context", pack, intent)
    reopened = WorkingContext(StateStore(store.root))
    cursor = 0
    paged_requests = set()
    while cursor is not None:
        page = reopened.read_omitted_page(
            mission_id="m", actor_id="peer", task_id=task["id"],
            inclusion_ref=inclusion, cursor=cursor, limit=20,
        )
        paged_requests.update(
            item["ref"]["id"] for item in page["items"]
            if item["ref"]["kind"] == "mail_request"
        )
        cursor = page["next_cursor"]
    assert {"request-1", "request-2", "request-3", "request-4"} <= paged_requests
    windows = []
    offset = 0
    while True:
        saved = reopened.read_omitted_record(
            mission_id="m", actor_id="peer", task_id=task["id"],
            inclusion_ref=inclusion, pointer=incoming["ref"], offset=offset, limit=400,
        )
        windows.append(saved["text"])
        if saved["next_offset"] is None:
            break
        offset = saved["next_offset"]
    assert json.loads("".join(windows))["question"] == questions["request-1"]
    with pytest.raises(ContextError):
        reopened.read_omitted_record(
            mission_id="m", actor_id="sender", task_id=task["id"],
            inclusion_ref=inclusion, pointer=incoming["ref"], offset=0, limit=400,
        )


@pytest.mark.parametrize("first_length", (548, 1400))
def test_prepared_model_packet_carries_incoming_question_and_callable_answer(
    tmp_path, monkeypatch, first_length,
):
    def no_transport(*_args, **_kwargs):
        raise AssertionError("Preparing a model packet must not call a provider")

    source_text = "Synthetic source: tie policy is not specified. " + (
        "site alpha score 7; site beta score 7; review the comparator. " * 20
    )
    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="incoming-question-packet", question="Assess the synthetic tie policy",
            evidence_text=source_text, agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        sage = host._control(run.run_id, "agent-synthesis")
        mxra = host._control(run.run_id, "agent-critique")
        mission_id = sage["body"]["mission_id"]
        first_question = ("Can you evaluate this synthetic tie? " + " ".join(
            f"row-{i:03d} has score {i * 41 % 997}" for i in range(70)
        ))[:first_length]
        def varied(label: str, length: int) -> str:
            return (label + " " + " ".join(
                f"row-{i:03d} score={i * 73 % 991} site={i * 19 % 13}"
                for i in range(100)
            ))[:length]

        for number, (requester, recipient, cause, question) in enumerate((
            (mxra, sage, mxra, first_question),
            (sage, mxra, sage, varied("Check comparator ordering.", 548)),
            (mxra, sage, mxra, varied("Check the second claim.", 320)),
            (sage, mxra, sage, varied("Validate a separate tie rule.", 462)),
            (mxra, sage, mxra, varied("Check the fifth claim.", 1100)),
            (sage, mxra, sage, varied("Review a separate limitation.", 1000)),
            (mxra, sage, mxra, varied("Check the seventh claim.", 1200)),
            (sage, mxra, sage, varied("Review another limitation.", 900)),
        ), 1):
            host.mailbox.send_request(
                f"packet-send-{number}", mission_id=mission_id,
                request_id=f"packet-request-{number}",
                message_id=f"packet-message-{number}",
                requester_id=requester["body"]["actor_id"],
                recipient_id=recipient["body"]["actor_id"],
                question=question, expected_artifact="Independent synthetic review",
                cause_ref=cause["body"]["proposal_ref"], payload={},
                task_ref=cause["body"]["proposal_ref"],
            )
            host.mailbox.mark_delivered(
                f"packet-deliver-{number}", mission_id=mission_id,
                message_id=f"packet-message-{number}",
            )
        # A saved response from the same selected model is what the host uses
        # to learn account input headroom; a bare capacity snapshot is not.
        assert host._schedule_model(mxra)
        prior = host._control(run.run_id, "agent-critique")["body"]["call_ref"]
        prior_intent = ref(host.store.get(
            "model_request_intent", f"model-intent:{prior['id']}:{prior['version']}",
        ))
        quota_domain = host._selected_credential(host._run(run.run_id))["quota_domain"]
        host.store.transact("synthetic-8k-response-headroom", {}, lambda tx: tx.put(
            "model_response_receipt", "synthetic-8k-response-headroom", {
                "mission_id": mission_id, "call_ref": prior, "intent_ref": prior_intent,
                "http_status": 200, "quota_domain": quota_domain,
                "rate_headers": {"x-ratelimit-limit-tokens": "8000"},
            },
        ), mission_id=mission_id)
        assert host._schedule_model(sage)
        prepared = host._control(run.run_id, "agent-synthesis")
        call_ref = prepared["body"]["call_ref"]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}",
        )
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        context = json.loads(packet["messages"][-1]["content"])
        incoming = context["incoming_request"]
        assert incoming["ref"] == ref(host.store.get("mail_request", "packet-request-1"))
        assert incoming["recipient_id"] == sage["body"]["actor_id"]
        if first_length == 548:
            assert incoming["question"] == first_question
        else:
            assert incoming["question_status"] == "prefix_window_only"
            assert first_question.startswith(incoming["question_window"])
            assert incoming["question_chars"] == len(first_question)
        assert (context["omitted"] or {})["count"] >= 1
        assert incoming["ref"]["id"] in packet["messages"][-1]["content"]
        assert "site alpha score 7" in packet["messages"][1]["content"]
        functions = {item["function"]["name"]: item["function"]
                     for item in packet["tools"]}
        assert {"publish_result", "read_context_page", "request_capability"} <= functions.keys()
        if "peer_request" not in functions:
            availability = next(
                json.loads(message["content"])
                for message in packet["messages"]
                if "trusted_host_attached_tool_availability" in message.get("content", "")
            )
            assert "peer_request" in request_first_names(packet, availability)
        assert "answer_request_id" in functions["publish_result"]["parameters"]["properties"]
        if first_length > 600:
            assert "read_context_record" in incoming["read_full_question"]
            assert (
                "read_context_record" in functions
                or ("request_capability" in functions
                    and "request_capability" in incoming["read_full_question"])
            )
        budget = host.store.get("mission_call_budget", call_ref["id"])["body"]
        assert budget["observed_account_limit"] == 8000
        assert budget["exact_complete_request_estimate_tokens"] <= 8000
        assert not host.store.records("model_dispatch_attempt")
    finally:
        host.close()
