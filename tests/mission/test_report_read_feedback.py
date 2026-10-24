"""Report-reader feedback describes only an exact repeat in the current input."""

import hashlib
import json
from dataclasses import replace

from ora.coordination.board import ref
from ora.mission.context import WorkingContext
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack
from tests.mission.test_duplicate_report_read_compaction import (
    _assemble,
    _case,
    _receipt,
    _save_read,
    _serialized_tokens,
    _tool_ids,
)
from tests.mission.test_report_window_source_fit import _report_turn


def _inclusion_id(store, response_ref):
    response = store.get(**response_ref)
    private = json.loads(store.read_blob(response["body"]["blob"]["sha256"]))
    return json.loads(private["tool_calls"][0]["function"]["arguments"])[
        "inclusion_id"
    ]


def test_exact_duplicate_cue_is_small_persistent_and_offset_specific(tmp_path):
    store, context, saved, direct, _answer, older, _ = _case(tmp_path)
    inclusion = {"id": _inclusion_id(store, older)}
    original = store.get("context_tool_result", f"tool-result:{older['id']}")
    original_bytes = store.read_blob(original["body"]["blob"]["sha256"])
    repeated = _save_read(
        store, context, saved, inclusion, saved["report_ref"], direct,
        label="current-exact-report",
    )
    same = _assemble(store, context, saved, older, repeated)
    feedback = json.loads(same.messages[-1]["content"])["reader_feedback"]
    assert feedback == {
        "status": "same_exact_report_page_returned_by_earlier_saved_call",
        "previous_response_ref": older, "offset": direct["offset"],
        "current_report_text_in_input": True,
    }
    assert _tool_ids(same) == ["current-exact-report-call"]
    assert _receipt(same)["status"] == "same_exact_window_already_in_input"
    current_text = next(item["content"] for item in same.messages
                        if item.get("tool_call_id") == "current-exact-report-call")
    assert json.loads(current_text)["text"] == direct["text"]
    without_cue_index = json.loads(same.messages[-1]["content"])
    without_cue_index.pop("reader_feedback")
    without_cue = replace(same, messages=[
        *same.messages[:-1], {**same.messages[-1], "content": canonical(without_cue_index)},
    ])
    extra_tokens = (
        _serialized_tokens(tmp_path, same.messages)
        - _serialized_tokens(tmp_path, without_cue.messages)
    )
    assert 0 < extra_tokens < 100
    print(f"feedback_estimated_tokens={extra_tokens}")

    scope = {
        "mission_id": "m", "actor_id": "requester",
        "task_id": saved["task_ref"]["id"],
        "inclusion_ref": {"kind": "context_inclusion", **inclusion, "version": 1},
    }
    next_page = context.read_omitted_record(
        **scope, pointer=saved["report_ref"], offset=1,
        limit=direct["requested_limit"],
    )
    assert next_page["offset"] == 1 and next_page["text"] != direct["text"]
    changed = _save_read(
        store, context, saved, inclusion, saved["report_ref"], next_page,
        label="current-next-report",
    )
    offset_pack = _assemble(store, context, saved, older, changed)
    assert "reader_feedback" not in json.loads(offset_pack.messages[-1]["content"])
    assert _tool_ids(offset_pack) == ["older-report-call", "current-next-report-call"]
    assert _receipt(offset_pack)["status"] == "included_exact_window"

    reopened = StateStore(store.root)
    persisted = _assemble(reopened, WorkingContext(reopened), saved, older, repeated)
    assert persisted.messages == same.messages
    assert json.loads(persisted.messages[-1]["content"])["reader_feedback"] == feedback
    assert reopened.read_blob(original["body"]["blob"]["sha256"]) == original_bytes
    assert hashlib.sha256(original_bytes).hexdigest() == original["body"]["blob"]["sha256"]
    assert reopened.get("context_tool_result", original["id"])["body"] == original["body"]


def test_new_report_version_keeps_both_exact_reads_without_repeat_cue(tmp_path):
    store, context, saved, direct, _answer, older, _ = _case(tmp_path)

    def revise(tx):
        report = tx.get(**saved["report_ref"])
        current_report = tx.put("report", report["id"], {
            **report["body"], "body": "Revised synthetic peer report, version two.",
        }, expected_version=report["version"])
        answer = tx.get(**saved["answer_ref"])
        current_answer = tx.put("mail_answer", answer["id"], {
            **answer["body"], "artifact_ref": ref(current_report),
        }, expected_version=answer["version"])
        request = tx.get("mail_request", "peer-preview-request")
        tx.put("mail_request", request["id"], {
            **request["body"], "answer_refs": [ref(current_answer)],
        }, expected_version=request["version"])
        old_call = tx.get(**saved["call_ref"])
        new_call = tx.put("agent_call", "revised-report-reader-call", old_call["body"])
        return {"report_ref": ref(current_report), "call_ref": ref(new_call)}

    revised = store.transact("revised-report-reader", {}, revise, mission_id="m")
    new_pack = context.assemble(
        revised["call_ref"], saved["task_ref"],
        control_ref=ref(store.get("mission_task_control", "requester-control")),
        allowance=1800, estimate_tokens=len,
    )
    inclusion = bind_no_source_pack(store, context, new_pack, "revised-report-read")
    current_window = context.read_omitted_record(
        mission_id="m", actor_id="requester", task_id=saved["task_ref"]["id"],
        inclusion_ref=inclusion, pointer=revised["report_ref"], offset=0,
        limit=direct["requested_limit"],
    )
    assert current_window["ref"] == revised["report_ref"]
    assert current_window["text"] != direct["text"]
    current = _save_read(
        store, context, saved, inclusion, revised["report_ref"], current_window,
        label="current-revised-report",
    )
    packed = _assemble(store, context, saved, older, current)
    assert "reader_feedback" not in json.loads(packed.messages[-1]["content"])
    assert _tool_ids(packed) == ["older-report-call", "current-revised-report-call"]
    assert _receipt(packed)["status"] == "included_exact_window"
    assert store.get(**saved["report_ref"])["body"]["body"] in canonical(packed.messages)
    current_content = next(item["content"] for item in packed.messages
                           if item.get("tool_call_id") == "current-revised-report-call")
    assert json.loads(current_content)["text"] == current_window["text"]


def test_permission_loss_removes_repeat_cue_without_changing_saved_read(tmp_path):
    store, context, task, copy_ref, refs, _parent, _text, older, window, result_ref = (
        _report_turn(tmp_path, source_linked=True)
    )
    original_result = store.get(**result_ref)
    original_bytes = store.read_blob(original_result["body"]["blob"]["sha256"])
    old_response = store.get(**older)
    private = json.loads(store.read_blob(old_response["body"]["blob"]["sha256"]))
    private["tool_calls"][0]["id"] = "current-source-linked-report"
    blob = store.put_blob(canonical(private).encode())
    current = store.transact("current-source-linked-report", {}, lambda tx: ref(tx.put(
        "model_response_receipt", "current-source-linked-report", {
            **old_response["body"], "blob": blob,
        },
    )), mission_id="m")
    context.save_tool_result(
        "current-source-linked-result", response_ref=current,
        tool_call_id="current-source-linked-report", name="read_context_record",
        content=canonical(window),
    )

    def pack(active_store):
        return WorkingContext(active_store).assemble(
            refs["source_free_call"], task, control_ref=refs["control"],
            archived_tool_turn_refs=[older], tool_turn_refs=[current],
            allowance=20000, estimate_tokens=len,
        )

    before = pack(store)
    assert json.loads(before.messages[-1]["content"])["reader_feedback"]["previous_response_ref"] == older
    assert _tool_ids(before) == ["current-source-linked-report"]
    assert _receipt(before)["status"] == "same_exact_window_already_in_input"
    prior_use = store.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")

    def revoke(tx):
        return tx.put("source_use", prior_use["id"], {
            **prior_use["body"], "allowed_uses": [], "policy_status": "restricted",
        }, expected_version=prior_use["version"])

    store.transact("revoke-reader-feedback-source", {}, revoke, mission_id="m")
    after = pack(store)
    assert "reader_feedback" not in json.loads(after.messages[-1]["content"])
    assert window["text"] not in canonical(after.messages)
    assert _receipt(after)["status"] == "omitted_due_source_use"
    assert store.read_blob(original_result["body"]["blob"]["sha256"]) == original_bytes
    reopened = StateStore(store.root)
    assert pack(reopened).messages == after.messages
    assert reopened.get("source_use", prior_use["id"])["body"]["policy_status"] == "restricted"
