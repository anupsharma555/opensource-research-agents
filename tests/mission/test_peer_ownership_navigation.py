"""Peer review navigation identifies direction and only credits visible report pages."""

from __future__ import annotations

import pytest

from ora.coordination.board import ref
from ora.coordination.mailbox import PeerMailbox
from ora.mission.context import _peer_report_delivery
from ora.models.token_estimate import estimate_context_tokens
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack
from tests.mission.test_peer_answer_preview import _fixture, _pack


@pytest.mark.parametrize("message_note", [None, "Distinct peer note"])
@pytest.mark.parametrize("peer_body", ["A bounded synthetic peer assessment.", "Long peer report. " * 140])
def test_two_way_answer_refs_only_name_answers_to_your_question(
    tmp_path, message_note, peer_body,
):
    store = StateStore(tmp_path)
    saved = _fixture(store, body=peer_body)
    mailbox = PeerMailbox(store)
    sent = mailbox.send_request(
        "ask-requester", mission_id="m", request_id="peer-question",
        message_id="peer-question-message", requester_id="responder",
        recipient_id="requester", question="A different synthetic question?",
        expected_artifact="A short answer", cause_ref=saved["peer_task_ref"],
        payload={"question": "A different synthetic question?", **(
            {"note": message_note} if message_note else {}
        )},
        task_ref=saved["peer_task_ref"],
    )
    assert sent["status"] == "accepted"
    mailbox.mark_delivered("deliver-peer-question", mission_id="m",
                           message_id="peer-question-message")
    _, _, pending = _pack(store, saved, allowance=2600)
    assert pending["incoming_request"]["ref"] == {
        "kind": "mail_request", "id": "peer-question", "version": 2,
    }
    assert pending["incoming_request"]["owner"] == "peer"
    assert pending["incoming_request"]["requester_id"] == "responder"
    def save_own_report(tx):
        report = tx.put("report", "my-report", {
            "mission_id": "m", "title": "My synthetic reply",
            "body": "A separate synthetic assessment.",
            "citations": [], "limitations": ["Synthetic fixture"],
        })
        own_outcome = tx.put("board_outcome", "direct-outcome-my-answer", {
            "mission_id": "m", "actor_ref": ref(store.get("agent_instance", "requester")),
            "proposal_ref": saved["task_ref"], "result_refs": [ref(report)],
            "completes_task": True,
        })
        artifact = tx.put("executor_artifact", "executor-artifact:my-publication", {
            "mission_id": "m", "work_id": "my-publication", "actor_id": "requester",
            "handler_id": "mission-tool", "output_refs": [ref(report)],
        })
        receipt = tx.put("executor_receipt", "executor-receipt:my-publication", {
            "mission_id": "m", "work_id": "my-publication",
            "artifact_ref": ref(artifact),
        })
        trace = tx.put("coordination_trace", "trace-my-publication", {
            "mission_id": "m", "instance_id": "requester",
            "proof": "recorded_local_callback", "output_refs": [ref(artifact)],
        })
        wrapper = tx.put("board_outcome", "executor-outcome:my-publication", {
            "mission_id": "m", "actor_ref": ref(store.get("agent_instance", "requester")),
            "proposal_ref": saved["task_ref"],
            "result_refs": [ref(artifact), ref(receipt), ref(trace)],
            "completes_task": False,
        })
        distinct = tx.put("board_outcome", "distinct-outcome", {
            "mission_id": "m", "actor_ref": ref(store.get("agent_instance", "requester")),
            "proposal_ref": saved["task_ref"], "result_refs": [saved["report_ref"]],
            "completes_task": False,
        })
        return {"report": ref(report), "own_outcome": ref(own_outcome),
                "wrapper": ref(wrapper),
                "distinct_outcome": ref(distinct)}

    saved_own = store.transact(
        "save-my-report", {}, save_own_report, mission_id="m",
    )
    own_report = saved_own["report"]
    own_outcome = saved_own["own_outcome"]
    wrapper = saved_own["wrapper"]
    distinct_outcome = saved_own["distinct_outcome"]
    own_answer = mailbox.record_answer(
        "answer-peer-question", mission_id="m", request_id="peer-question",
        answer_id="my-answer", responder_id="requester",
        artifact_ref=own_report,
    )
    assert own_answer["status"] == "accepted"
    context, pack, index = _pack(store, saved)
    card = (index.get("peer_answer_previews") or index["peer_answer_review_cues"])[0]
    assert card["owner"] == "you"
    assert card["by"] == "responder"
    assert card["request_ref"] == saved["request_ref"]
    assert card["answer_ref"] == saved["answer_ref"]
    if "report_ref" in card:
        assert card["report_ref"] == saved["report_ref"]
    else:
        assert {key: card["read_request"][key] for key in ("kind", "id", "version")} == (
            saved["report_ref"]
        )
    assert card["action"] == "review_peer_answer"
    if peer_body.startswith("A bounded"):
        assert card["report_delivery"] == "complete_in_input"
        assert "read_request" not in card
    assert own_answer["answer_ref"] not in index.get("request_answer_refs", [])
    own_reply_disposition = next(item for item in index["request_dispositions"]
                                 if item["ref"]["id"] == "peer-question")
    assert own_reply_disposition["owner"] == "peer"
    assert own_reply_disposition["answer_refs"] == [own_answer["answer_ref"]]
    assert not any(item["ref"]["id"] in {
        "peer-question", "my-answer", own_outcome["id"], wrapper["id"],
    } for item in index["details"])
    assert any(item["ref"] == distinct_outcome for item in index["details"])
    if message_note:
        assert any(item["ref"]["id"] == "peer-question-message"
                   for item in index["details"])
    else:
        assert not any(item["ref"]["id"] == "peer-question-message"
                       for item in index["details"])
    omitted = {item["ref"]["id"] for item in pack.omitted}
    assert {"peer-question", "my-answer", own_outcome["id"], wrapper["id"]} <= omitted
    if not message_note:
        assert "peer-question-message" in omitted
    inclusion = bind_no_source_pack(store, context, pack, "own-reply-compact")
    cursor, pages = 0, []
    while cursor is not None:
        page = context.read_omitted_page(
            mission_id="m", actor_id="requester", task_id="requester-task",
            inclusion_ref=inclusion, cursor=cursor, limit=20,
        )
        pages.extend(page["items"])
        cursor = page["next_cursor"]
    assert own_outcome in [item["ref"] for item in pages]
    assert pack.messages[-1]["content"] == canonical(index)


def test_visible_report_pages_advance_only_their_own_review_cue(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store, body="A synthetic peer report. " * 120)
    context, pack, index = _pack(store, saved, allowance=1800)
    cue = index["peer_answer_review_cues"][0]
    assert cue["owner"] == "you" and cue["by"] == "responder"
    inclusion = bind_no_source_pack(store, context, pack, "ownership-page-read")
    scope = {"mission_id": "m", "actor_id": "requester",
             "task_id": "requester-task", "inclusion_ref": inclusion,
             "pointer": saved["report_ref"]}
    first = context.read_omitted_record(**scope, offset=0, limit=2000)
    assert first["next_offset"] == 2000
    second = context.read_omitted_record(**scope, offset=2000, limit=2000)
    messages = [
        {"role": "tool", "name": "read_context_record", "content": canonical(page)}
        for page in (first, second)
    ]
    assert _peer_report_delivery(messages[:1], saved["report_ref"]) == (
        "partial_in_input", 2000,
    )
    partial = context._peer_answer_review_cue(
        store.get(**saved["request_ref"]), store.get(**saved["answer_ref"]),
        mission_id="m", actor_id="requester", task_id="requester-task",
        inclusion_id=inclusion["id"], visible_messages=messages[:1],
    )
    assert partial["read_request"]["offset"] == 2000
    assert _peer_report_delivery(messages[1:], saved["report_ref"]) == (
        "unread_in_input", 0,
    )
    assert _peer_report_delivery(messages, saved["report_ref"]) == (
        "complete_in_input", first["total_chars"],
    )
    complete = context._peer_answer_review_cue(
        store.get(**saved["request_ref"]), store.get(**saved["answer_ref"]),
        mission_id="m", actor_id="requester", task_id="requester-task",
        inclusion_id=inclusion["id"], visible_messages=messages,
    )
    assert complete["report_delivery"] == "complete_in_input"
    assert "read_request" not in complete
    assert _peer_report_delivery(messages, {**saved["report_ref"], "version": 2}) == (
        "unread_in_input", 0,
    )


def test_full_card_uses_fewer_estimated_tokens_than_repeated_starter_read(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store)
    _, pack, index = _pack(store, saved, allowance=2050)
    card = index["peer_answer_previews"][0]
    old_card = {key: value for key, value in card.items()
                if key not in {"owner", "by", "action", "report_delivery"}}
    old_card["read_request"] = {
        "tool": "read_context_record",
        "inclusion_id": index["older_records"]["inclusion_id"],
        **saved["report_ref"], "offset": 0, "limit": 2000,
    }
    counterfactual = {**index, "peer_answer_previews": [old_card]}
    current_tokens = estimate_context_tokens(canonical(pack.messages))
    old_tokens = estimate_context_tokens(canonical([
        *pack.messages[:-1], {"role": "user", "content": canonical(counterfactual)},
    ]))
    assert current_tokens < old_tokens
    assert index["peer_answer_previews"][0]["report"]["body"] == (
        "A bounded synthetic peer assessment."
    )
