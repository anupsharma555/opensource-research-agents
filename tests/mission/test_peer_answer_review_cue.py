"""A pending peer review retains its question when the report needs a read."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ora.coordination.board import ref
from ora.coordination.mailbox import PeerMailbox
from ora.mission.context import ContextError, WorkingContext
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack
from tests.mission.test_peer_answer_preview import _fixture, _other_call, _pack


def test_long_report_keeps_exact_question_and_reader_without_delivery_credit(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store, body="Synthetic peer answer. " * 100)
    context, pack, index = _pack(store, saved, allowance=2000)

    assert "peer_answer_previews" not in index
    cue = index["peer_answer_review_cues"][0]
    assert cue["question"] == "Please review this synthetic claim."
    assert cue["request_ref"] == saved["request_ref"]
    assert cue["answer_ref"] == saved["answer_ref"]
    assert cue["owner"] == "you"
    assert cue["action"] == "review_peer_answer"
    assert cue["read_request"]["tool"] == "read_context_record"
    assert {key: cue["read_request"][key] for key in ("kind", "id", "version")} == (
        saved["report_ref"]
    )
    assert "report_ref" not in cue and "report" not in cue
    assert saved["answer_ref"] not in index.get("request_answer_refs", [])
    assert not any(item.get("ref") == saved["request_ref"]
                   for item in index.get("request_dispositions", []))
    assert "Synthetic peer answer." not in canonical(index)
    assert saved["report_ref"] not in pack.included_refs
    assert context.context_source_dependency_refs(pack) == ()
    inclusion_ref = bind_no_source_pack(store, context, pack, "bounded-review-cue")
    pages, offset = [], 0
    while True:
        page = context.read_omitted_record(
            mission_id="m", actor_id="requester", task_id="requester-task",
            inclusion_ref=inclusion_ref, pointer=saved["report_ref"],
            offset=offset, limit=2000,
        )
        assert page["report_text_status"] == "available"
        pages.append(page["text"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert json.loads("".join(pages))["body"] == "Synthetic peer answer. " * 100

    tampered = json.loads(pack.messages[-1]["content"])
    tampered["peer_answer_review_cues"][0]["question"] = "A different question"
    changed = replace(pack, messages=[*pack.messages[:-1], {
        **pack.messages[-1], "content": canonical(tampered),
    }])
    with pytest.raises(ContextError, match="cue changed"):
        context.context_source_dependency_refs(changed)
    wrong_reader = json.loads(pack.messages[-1]["content"])
    wrong_reader["peer_answer_review_cues"][0]["read_request"]["id"] = "other-report"
    changed_reader = replace(pack, messages=[*pack.messages[:-1], {
        **pack.messages[-1], "content": canonical(wrong_reader),
    }])
    with pytest.raises(ContextError, match="cue changed"):
        context.context_source_dependency_refs(changed_reader)


def test_review_cue_denies_wrong_requester_task_and_changed_report_access(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store, body="x" * 2100)
    context, pack, index = _pack(store, saved)
    assert {key: index["peer_answer_review_cues"][0]["read_request"][key]
            for key in ("kind", "id", "version")} == saved["report_ref"]
    request = store.get(**saved["request_ref"])
    answer = store.get(**saved["answer_ref"])
    kwargs = {"mission_id": "m", "actor_id": "requester",
              "task_id": saved["task_ref"]["id"], "inclusion_id": "synthetic-inclusion"}
    assert context._peer_answer_review_cue(
        request, answer, **{**kwargs, "actor_id": "other"},
    ) is None
    assert context._peer_answer_review_cue(
        request, answer, **{**kwargs, "task_id": saved["peer_task_ref"]["id"]},
    ) is None
    third_call = _other_call(store, "other", saved["peer_task_ref"])
    third_pack = WorkingContext(store).assemble(third_call, saved["peer_task_ref"])
    assert "peer_answer_review_cues" not in json.loads(third_pack.messages[-1]["content"])

    control = store.get("mission_task_control", "responder-control")
    store.transact("change-peer-report-producer", {}, lambda tx: tx.put(
        "mission_task_control", control["id"], {
            **control["body"], "actor_id": "other",
        }, expected_version=control["version"]), mission_id="m")
    with pytest.raises(ContextError, match="cue changed"):
        context.context_source_dependency_refs(pack)


def test_oversize_question_has_exact_reader_without_a_prefix(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store, body="x" * 2100)
    request = store.get(**saved["request_ref"])
    store.transact("long-review-question", {}, lambda tx: tx.put(
        "mail_request", request["id"], {
            **request["body"], "question": "q" * 601,
        }, expected_version=request["version"]), mission_id="m")
    _context, _pack_result, index = _pack(store, saved)
    cue = index["peer_answer_review_cues"][0]
    assert "question" not in cue
    assert cue["question_status"] == "not_in_input_question_exceeds_window"
    assert cue["question_chars"] == 601
    assert cue["read_full_question"]["tool"] == "read_context_record"
    assert cue["read_full_question"]["kind"] == "mail_request"
    assert "q" * 100 not in canonical(cue)


def test_cue_survives_required_index_compaction_before_optional_detail(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store, body="Synthetic peer answer. " * 100)
    context = WorkingContext(store)
    control = store.get("mission_task_control", "requester-control")
    pack = context.assemble(
        saved["call_ref"], saved["task_ref"], control_ref=ref(control),
        allowance=1500, estimate_tokens=len,
    )
    index = json.loads(pack.messages[-1]["content"])
    # Earlier duplicate-field removal may make the first attempt fit. The
    # observable requirement is the same usable cue within the allowance.
    assert len(pack.messages[-1]["content"]) <= 1500
    cue = index["peer_answer_review_cues"][0]
    assert cue["question"] == "Please review this synthetic claim."
    assert cue["request_ref"] == saved["request_ref"]
    assert cue["answer_ref"] == saved["answer_ref"]
    assert cue["read_request"]["tool"] == "read_context_record"
    assert {key: cue["read_request"][key] for key in ("kind", "id", "version")} == (
        saved["report_ref"]
    )
    assert saved["report_ref"] not in pack.included_refs
    assert context.context_source_dependency_refs(pack) == ()


def test_review_cue_survives_delivered_message_version_and_tight_allowance(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store, body="Synthetic peer answer. " * 100)
    request = store.get(**saved["request_ref"])
    original_message_ref = request["body"]["message_ref"]
    current_message = store.get("mail_message", original_message_ref["id"])
    assert current_message["version"] > original_message_ref["version"]

    context, pack, index = _pack(store, saved, allowance=1400)
    cue = index["peer_answer_review_cues"][0]
    assert cue["question"] == "Please review this synthetic claim."
    assert cue["owner"] == "you"
    assert cue["answer_ref"] == saved["answer_ref"]
    assert {key: cue["read_request"][key] for key in ("kind", "id", "version")} == (
        saved["report_ref"]
    )
    assert not any(item["id"] == original_message_ref["id"]
                   for item in index.get("outgoing_request_message_refs", []))
    assert saved["report_ref"] not in pack.included_refs
    assert store.get(**original_message_ref)["body"]["payload"] == {
        "question": "Please review this synthetic claim.",
    }
    inclusion_ref = bind_no_source_pack(store, context, pack, "advanced-message-cue")
    omission_page = context.read_omitted_page(
        mission_id="m", actor_id="requester", task_id="requester-task",
        inclusion_ref=inclusion_ref, cursor=0, limit=10,
    )
    assert any(item["ref"] == ref(current_message)
               for item in omission_page["items"])

    other = PeerMailbox(store).send_request(
        "send-separate-request", mission_id="m", request_id="separate-request",
        message_id="separate-message", requester_id="requester",
        recipient_id="responder", question="A separate question?",
        expected_artifact="A separate report", cause_ref=saved["task_ref"],
        payload={"question": "A separate question?"}, task_ref=saved["task_ref"],
    )
    assert other["status"] == "accepted"
    PeerMailbox(store).mark_delivered(
        "deliver-separate-request", mission_id="m", message_id="separate-message",
    )
    _context, _pack_result, with_other = _pack(store, saved, allowance=1950)
    assert with_other["peer_answer_review_cues"][0]["answer_ref"] == saved["answer_ref"]
    assert [item["id"] for item in with_other["outgoing_request_message_refs"]] == [
        "separate-message",
    ]
