"""Omitted checked reads distinguish prior requests from next navigation."""

from __future__ import annotations

import json
import math

import pytest

from ora.coordination.board import ref
from ora.evidence import EvidenceLibrary, read_source_window
from ora.evidence.coverage import extracted_text_coverage
from ora.state.store import canonical
from tests.mission.test_context_window_compaction import _window_turn
from tests.mission.test_requested_window_recovery import _two_copy_read
from tests.mission.test_selected_window_admission import _saved_tool_request


def _delivery(pack):
    index = json.loads(pack.messages[-1]["content"])
    feedback = index["source_read_delivery"]
    if feedback.get("details_in_current_tool_result"):
        return next(
            item for message in pack.messages if message["role"] == "tool"
            if (item := json.loads(message["content"])).get("schema")
            == "ora.source_window_delivery.v1"
        )
    return feedback


@pytest.mark.parametrize("allowance,with_inventory", [(5000, False), (1800, True)])
def test_partial_read_cue_preserves_cursor_and_distinguishes_source_inventory(
    tmp_path, allowance, with_inventory,
):
    store, context, task, copy_ref, window, refs, result_ref = _window_turn(tmp_path)
    call_ref = refs["source_free_call"]
    if with_inventory:
        call = store.get(**call_ref)
        call_ref = store.transact(
            "attach-reader-for-cue", {},
            lambda tx: ref(tx.put("agent_call", call["id"], {
                **call["body"], "exposed_tools": ["read_source_inventory", "read_source_window"],
            }, expected_version=call["version"])), mission_id="m",
        )
    original_result = store.read_blob(store.get(**result_ref)["body"]["blob"]["sha256"])
    pack = context.assemble(
        call_ref, task, control_ref=refs["control"],
        history=[{"role": "user", "content": canonical({
            "source_context_mode": "source_free_recovery",
            "reason": "exact_source_text_exceeds_allowance",
        })}],
        tool_turn_refs=[refs["response"]], allowance=allowance,
        estimate_tokens=lambda value: math.ceil(len(value) / 4 * 1.25),
    )
    feedback = _delivery(pack)
    assert feedback["copy_ref"] == copy_ref
    assert feedback["window_ref"] == window["window_ref"]
    assert feedback["result_ref"] == result_ref
    assert feedback["requested_window"] == {
        "block_offset": 0, "char_offset": 1600, "char_limit": 800,
    }
    assert feedback["first_uncovered_cursor"] == {
        "block_offset": 0, "char_offset": 2400,
    }
    assert feedback["source_text_in_this_call"] is False
    assert feedback["status"] == "saved_text_omitted_for_input_fit"
    action = feedback["next_action"]
    assert "requested_window is prior" in action
    assert "repeat adds no coverage" in action
    assert "may fit (unverified)" in action
    assert "read_source_inventory if attached" in action
    assert "read_context_page is archive-only" in action
    assert store.read_blob(store.get(**result_ref)["body"]["blob"]["sha256"]) == original_result


def test_full_saved_window_does_not_suggest_repeating_it(tmp_path):
    store, context, task, copy_ref, first, refs, _result_ref = _window_turn(tmp_path)
    tail = read_source_window(
        EvidenceLibrary(store), "complete-synthetic-window", copy_ref,
        block_offset=0, char_offset=2400, block_limit=1, char_limit=500,
    )
    raw = store.put_blob(canonical({"content": None, "tool_calls": [{
        "id": "full-window-tool", "type": "function", "function": {
            "name": "read_source_window", "arguments": canonical({
                "source_id": copy_ref["id"], "source_version": copy_ref["version"],
                "block_offset": 0, "char_offset": 2400, "char_limit": 500,
            }),
        },
    }]}).encode())

    def save(tx):
        control = tx.get(**refs["control"])
        response = tx.put("model_response_receipt", "complete-synthetic-response", {
            "mission_id": "m", "task_id": task["id"],
            "call_ref": refs["source_free_call"], "status": "tool_requested",
            "blob": raw,
        })
        changed = tx.put("mission_task_control", control["id"], {
            **control["body"],
            "read_window_refs": [*control["body"]["read_window_refs"], tail["window_ref"]],
            "selected_passage_refs": {
                copy_ref["id"]: [
                    *control["body"]["selected_passage_refs"][copy_ref["id"]],
                    *tail["passage_refs"],
                ],
            },
        }, expected_version=control["version"])
        return {"response": ref(response), "control": ref(changed)}

    saved = store.transact("complete-synthetic-turn", {}, save, mission_id="m")
    response, control_ref = saved["response"], saved["control"]
    context.save_tool_result(
        "complete-synthetic-result", response_ref=response,
        tool_call_id="full-window-tool", name="read_source_window",
        content=canonical(tail),
    )
    feedback = context._source_fit_feedback(
        store.get(**control_ref), (refs["response"], response),
        "exact_source_text_exceeds_allowance",
    )
    assert feedback is not None
    assert feedback["copy_ref"] == copy_ref
    assert feedback["window_ref"] == tail["window_ref"]
    assert feedback["latest_window_next_cursor"] is None
    assert feedback["requested_window"] == {
        "block_offset": 0, "char_offset": 2400, "char_limit": 500,
    }
    complete = store.get(**control_ref)["body"]
    assert extracted_text_coverage(
        store, copy_ref, complete["selected_passage_refs"][copy_ref["id"]],
        complete["read_window_refs"],
    )["status"] == "complete_extracted_text"
    assert "requested_window is prior" in feedback["next_action"]
    assert "repeat adds no coverage" in feedback["next_action"]
    assert "smaller char_limit" in feedback["next_action"]
    assert first["window_ref"] != tail["window_ref"]


def test_one_character_and_repeated_window_have_no_smaller_read_claim(
    tmp_path, monkeypatch,
):
    host, run, actor, early, _late, window, response = _two_copy_read(
        tmp_path, monkeypatch, window_chars=1, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        feedback = host.context._source_fit_feedback(
            control, (response,), "exact_source_text_exceeds_allowance",
        )
        assert feedback is not None
        assert feedback["copy_ref"] == early
        assert feedback["window_ref"] == window["window_ref"]
        assert feedback["requested_window"]["char_limit"] == 1
        assert "requested_window is prior" in feedback["next_action"]
        assert "repeat adds no coverage" in feedback["next_action"]
        assert "smaller char_limit" not in feedback["next_action"]

        repeated, _ = _saved_tool_request(
            host, control, response_id="synthetic-repeat-one-char",
            tool_id="synthetic-repeat-tool-one-char", name="read_source_window",
            arguments={
                "source_id": early["id"], "source_version": early["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 1,
            }, result=window,
        )
        host._update_control(
            control, response_ref=repeated,
            tool_turn_refs=[*control["body"]["tool_turn_refs"], repeated],
        )
        repeated_feedback = host.context._source_fit_feedback(
            host._control(run.run_id, actor), (response, repeated),
            "exact_source_text_exceeds_allowance",
        )
        assert repeated_feedback is not None
        assert repeated_feedback["window_ref"] == window["window_ref"]
        assert "repeat adds no coverage" in repeated_feedback["next_action"]
    finally:
        host.close()


def test_unselected_and_revoked_windows_do_not_claim_size_recovery(tmp_path):
    store, context, task, copy_ref, _window, refs, _result_ref = _window_turn(tmp_path)
    control = store.get(**refs["control"])
    unselected = context._source_fit_feedback(control, (refs["response"],), None)
    assert unselected is not None
    assert unselected["status"] == "saved_text_not_selected_for_current_input"
    assert "smaller char_limit" not in unselected["next_action"]

    use_ref = context._source_status(store.get(**copy_ref))["current_source_use_ref"]

    def revoke(tx):
        prior = tx.get(**use_ref)
        return ref(tx.put(prior["kind"], prior["id"], {
            **prior["body"], "allowed_uses": [],
            "policy_status": "requires_source_policy_resolution",
        }, expected_version=prior["version"]))

    store.transact("revoke-cue-source", {}, revoke, mission_id="m")
    revoked = context._source_fit_feedback(control, (refs["response"],),
                                           "exact_source_text_exceeds_allowance")
    assert revoked is not None
    assert revoked["status"] == "saved_text_unavailable_under_current_source_policy"
    assert "Current source policy prevents delivery" in revoked["next_action"]
    assert "smaller char_limit" not in revoked["next_action"]
    assert revoked["copy_ref"] == copy_ref
    assert revoked["source_text_in_this_call"] is False
