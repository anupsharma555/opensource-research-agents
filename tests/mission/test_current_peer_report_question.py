"""A completed peer-report read carries its exact question beside checked source text."""

import json

from ora.coordination.board import ref
from ora.mission.context import _peer_report_delivery
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack
from tests.mission.test_duplicate_report_read_compaction import _save_read
from tests.mission.test_peer_answer_preview import _fixture, _pack


def _current_report_case(tmp_path, *, partial=False):
    store = StateStore(tmp_path)
    saved = _fixture(store, partial=partial)
    context, initial, _ = _pack(store, saved, allowance=1800)
    inclusion = bind_no_source_pack(store, context, initial, "current-peer-report")
    window = context.read_omitted_record(
        mission_id="m", actor_id="requester", task_id="requester-task",
        inclusion_ref=inclusion, pointer=saved["report_ref"], offset=0, limit=2000,
    )
    current = _save_read(
        store, context, saved, inclusion, saved["report_ref"], window,
        label="current-peer-report",
    )
    return store, context, saved, current


def _assemble(store, context, saved, current):
    return context.assemble(
        saved["call_ref"], saved["task_ref"],
        control_ref=ref(store.get("mission_task_control", "requester-control")),
        tool_turn_refs=[current], allowance=10000, estimate_tokens=len,
    )


def test_current_complete_peer_report_read_carries_question_and_review_owner(tmp_path):
    store, context, saved, current = _current_report_case(tmp_path)
    pack = _assemble(store, context, saved, current)
    index = json.loads(pack.messages[-1]["content"])
    disposition = next(item for item in index["request_dispositions"]
                       if item["ref"] == saved["request_ref"])
    assert disposition["answer_refs"] == [saved["answer_ref"]]
    assert disposition["question"] == "Please review this synthetic claim."
    assert disposition["owner"] == "you"
    assert disposition["action"] == "review_peer_answer"
    assert not index.get("peer_answer_previews")
    assert not index.get("peer_answer_review_cues")
    assert _peer_report_delivery(pack.messages[:-1], saved["report_ref"])[0] == (
        "complete_in_input"
    )
    assert any(message.get("role") == "tool"
               and json.loads(message["content"]).get("ref") == saved["report_ref"]
               for message in pack.messages[:-1] if message.get("role") == "tool")
    prior_call = store.get(**saved["call_ref"])
    next_call = store.transact("current-report-next-call", {}, lambda tx: ref(tx.put(
        "agent_call", "current-report-next-call", prior_call["body"],
    )), mission_id="m")
    next_pack = context.assemble(
        next_call, saved["task_ref"],
        control_ref=ref(store.get("mission_task_control", "requester-control")),
        tool_turn_refs=[current], allowance=10000, estimate_tokens=len,
    )
    bind_no_source_pack(store, context, next_pack, "current-peer-report-next")


def test_partial_answer_does_not_get_complete_report_question_projection(tmp_path):
    store, context, saved, current = _current_report_case(tmp_path, partial=True)
    pack = _assemble(store, context, saved, current)
    index = json.loads(pack.messages[-1]["content"])
    assert all("question" not in item and "action" not in item
               for item in index.get("request_dispositions", []))


def test_wrong_report_or_current_access_loss_cannot_claim_complete_read(
    tmp_path, monkeypatch,
):
    store, context, saved, current = _current_report_case(tmp_path)
    assert _assemble(store, context, saved, current)

    wrong = StateStore(tmp_path / "wrong-report")
    unrelated = _fixture(wrong)
    reader, initial, _ = _pack(wrong, unrelated, allowance=1800)
    inclusion = bind_no_source_pack(wrong, reader, initial, "wrong-peer-report")
    window = reader.read_omitted_record(
        mission_id="m", actor_id="requester", task_id="requester-task",
        inclusion_ref=inclusion, pointer=unrelated["report_ref"], offset=0, limit=2000,
    )
    forged = {**window, "ref": {"kind": "report", "id": "different-report", "version": 1}}
    assert _peer_report_delivery([{
        "role": "tool", "name": "read_context_record", "content": canonical(forged),
    }], unrelated["report_ref"])[0] == "unread_in_input"
    bad_response = _save_read(wrong, reader, unrelated, inclusion,
                              unrelated["report_ref"], forged, label="wrong-report")
    wrong_pack = _assemble(wrong, reader, unrelated, bad_response)
    # Exact-result replay replaces the malformed saved window with the
    # authorized report read before projecting the review question.
    assert any(message.get("role") == "tool"
               and json.loads(message["content"]).get("ref") == unrelated["report_ref"]
               for message in wrong_pack.messages[:-1] if message.get("role") == "tool")

    reopened = StateStore(tmp_path / "current-access")
    allowed = _fixture(reopened)
    reader, initial, _ = _pack(reopened, allowed, allowance=1800)
    inclusion = bind_no_source_pack(reopened, reader, initial, "denied-current-peer-report")
    window = reader.read_omitted_record(
        mission_id="m", actor_id="requester", task_id="requester-task",
        inclusion_ref=inclusion, pointer=allowed["report_ref"], offset=0, limit=2000,
    )
    response = _save_read(reopened, reader, allowed, inclusion, allowed["report_ref"],
                          window, label="denied-current-report")
    monkeypatch.setattr("ora.mission.context.report_text_access", lambda *_args: {
        "status": "data_unavailable_due_source_use", "source_dependency_refs": [],
        "unavailable_source_refs": [], "dependency_lineage_complete": False,
    })
    pack = _assemble(reopened, reader, allowed, response)
    index = json.loads(pack.messages[-1]["content"])
    assert all("question" not in item for item in index["request_dispositions"])
    assert "A bounded synthetic peer assessment." not in canonical(pack.messages)
