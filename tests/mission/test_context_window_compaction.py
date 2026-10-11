"""A long checked window remains available without duplicating its text in context."""

from __future__ import annotations

import hashlib
import json
import math

import pytest

from ora.coordination.board import ref
from ora.evidence import read_source_window
from ora.evidence.coverage import extracted_text_coverage
from ora.mission.context import ContextOverflow, WorkingContext
from ora.state.store import canonical
from tests.evidence.test_coverage import checked_copy
from tests.mission.test_context import fixture


def _window_turn(tmp_path, *, omit_legacy_optional_fields=False):
    text = "DOI: 10.1234/coverage. " + "A" * 2638
    library, saved = checked_copy(tmp_path, text.encode(), selected=[text[:1600]])
    store = library.store
    prior_call, task = fixture(store)
    copy_ref = saved["copy_ref"]
    window = read_source_window(
        library, "second-synthetic-window", copy_ref,
        block_offset=0, char_offset=1600, block_limit=1, char_limit=800,
    )
    selected = [*saved["passage_refs"], *window["passage_refs"]]
    result = {
        **window,
        "passages": [
            {**item, "span_id": store.get(**item["passage_ref"])["body"]["span_id"]}
            for item in window["passages"]
        ],
        "model_selection": {
            "mode": "append", "passage_refs": selected,
            "prior_passage_refs": saved["passage_refs"],
        },
        "next_request": {
            "source_id": copy_ref["id"], "source_version": copy_ref["version"],
            **window["next_cursor"], "char_limit": 800,
            "selection_mode": "append",
        },
    }
    if omit_legacy_optional_fields:
        result.pop("omissions")
        result.pop("source_selection_omissions")
    private = store.put_blob(canonical({
        "content": None,
        "tool_calls": [{
            "id": "window-tool", "type": "function",
            "function": {"name": "read_source_window", "arguments": canonical({
                "source_id": copy_ref["id"], "source_version": copy_ref["version"],
                "block_offset": 0, "char_offset": 1600, "char_limit": 800,
            })},
        }],
    }).encode())

    def save(tx):
        run = tx.put("mission_run", "window-run", {
            "mission_id": "m", "question": "Check the retained abstract",
            "scope": "Abstract only", "source_refs": [copy_ref],
        })
        control = tx.put("mission_task_control", "window-control", {
            "mission_id": "m", "run_id": run["id"], "actor_id": "peer",
            "task_id": task["id"], "source_refs": [copy_ref],
            "selected_passage_refs": {copy_ref["id"]: selected},
            "read_window_refs": [window["window_ref"]],
        })
        original = tx.get(**prior_call)
        next_call = tx.put("agent_call", "window-next-call", {
            **original["body"], "input_refs": [copy_ref],
        })
        source_free_call = tx.put("agent_call", "window-source-free-call", {
            **original["body"], "input_refs": [],
        })
        response = tx.put("model_response_receipt", "window-response", {
            "mission_id": "m", "task_id": task["id"],
            "call_ref": prior_call, "status": "tool_requested", "blob": private,
        })
        return {
            "control": ref(control), "next_call": ref(next_call),
            "source_free_call": ref(source_free_call), "response": ref(response),
        }

    refs = store.transact("window-continuation", {}, save, mission_id="m")
    context = WorkingContext(store)
    tool_result_ref = context.save_tool_result(
        "window-result", response_ref=refs["response"],
        tool_call_id="window-tool", name="read_source_window",
        content=canonical(result),
    )
    return store, context, task, copy_ref, window, refs, tool_result_ref


def test_duplicate_checked_window_preview_compacts_only_after_exact_selection(tmp_path):
    store, context, task, copy_ref, window, refs, tool_result_ref = _window_turn(tmp_path)
    original = json.loads(store.read_blob(store.get(**tool_result_ref)["body"]["blob"]["sha256"]))
    original_content_hash = hashlib.sha256(original["content"].encode()).hexdigest()
    pack = context.assemble(
        refs["next_call"], task, source_refs=[copy_ref],
        control_ref=refs["control"], tool_turn_refs=[refs["response"]],
        allowance=1500, estimate_tokens=lambda value: math.ceil(len(value) / 4 * 1.25),
    )
    presented = json.loads(pack.messages[-2]["content"])
    assert pack.estimated_tokens <= 1500
    assert presented["copy_ref"] == copy_ref
    assert presented["window_ref"] == window["window_ref"]
    assert presented["next_request"]["char_offset"] == 2400
    assert presented["status"] == "saved_window_text_in_selected_source_packet"
    assert presented["source_text_status"] == "exact_checked_passages_in_this_model_input"
    assert presented["original_source_partial"] is False
    assert presented["next_cursor_scope"] == "retained_extracted_text_only"
    assert presented["source_id"] == copy_ref["id"]
    assert presented["source_version"] == copy_ref["version"]
    assert "passage_refs" not in presented
    assert presented["passages"][0]["span_id"] == store.get(**window["passage_refs"][0])[
        "body"
    ]["span_id"]
    assert "text_preview" not in presented["passages"][0]
    assert presented["model_selection_mode"] == "append"
    assert presented["selected_passage_count"] == len(store.get(**refs["control"])[
        "body"
    ]["selected_passage_refs"][copy_ref["id"]])
    assert pack.tool_result_compactions[0]["original_content_sha256"] == original_content_hash
    assert pack.tool_result_compactions[0]["saved_tool_result_ref"] == tool_result_ref
    assert "text_preview" in json.loads(original["content"])["passages"][0]
    coverage = extracted_text_coverage(
        store, copy_ref, [*store.get(**refs["control"])["body"]["selected_passage_refs"][copy_ref["id"]]],
        [window["window_ref"]],
    )
    assert coverage["status"] == "incomplete_extracted_text"
    assert coverage["next_unread_cursor"] == {"block_offset": 0, "char_offset": 2400}
    assert coverage["uncovered_chars"] == 261


def test_ordinary_fit_compacts_exact_selected_window_without_changing_saved_result(tmp_path):
    store, context, task, copy_ref, window, refs, result_ref = _window_turn(tmp_path)
    original = store.read_blob(store.get(**result_ref)["body"]["blob"]["sha256"])
    original_content = json.loads(original)["content"]
    pack = context.assemble(
        refs["next_call"], task, source_refs=[copy_ref],
        control_ref=refs["control"], tool_turn_refs=[refs["response"]],
        allowance=10000, estimate_tokens=lambda value: math.ceil(len(value) / 4 * 1.25),
    )
    presented = json.loads(pack.messages[-2]["content"])
    assert presented["status"] == "saved_window_text_in_selected_source_packet"
    assert presented["window_ref"] == window["window_ref"]
    assert presented["passages"][0]["passage_ref"] == window["passage_refs"][0]
    assert presented["next_request"]["char_offset"] == 2400
    assert presented["next_cursor_scope"] == "retained_extracted_text_only"
    assert presented["saved_omissions_count"] == len(json.loads(original_content)["omissions"])
    assert presented["source_text_status"] == "exact_checked_passages_in_this_model_input"
    assert presented["source_id"] == copy_ref["id"]
    assert presented["source_version"] == copy_ref["version"]
    assert presented["passages"][0]["span_id"] == store.get(**window["passage_refs"][0])[
        "body"
    ]["span_id"]
    assert "text_preview" not in presented["passages"][0]
    assert presented["saved_omissions_count"] == len(json.loads(original_content)["omissions"])
    assert "A" * 100 not in pack.messages[-2]["content"]
    assert len(pack.tool_result_compactions) == 1
    assert pack.tool_result_compactions[0]["omitted_preview_characters"] == 800
    assert pack.tool_result_compactions[0]["saved_tool_result_ref"] == result_ref
    assert store.read_blob(store.get(**result_ref)["body"]["blob"]["sha256"]) == original


def test_ordinary_fit_keeps_window_text_when_its_passage_is_not_selected(tmp_path):
    store, context, task, copy_ref, _window, refs, _result_ref = _window_turn(tmp_path)

    def remove_window(tx):
        prior = tx.get(**refs["control"])
        selected = prior["body"]["selected_passage_refs"][copy_ref["id"]]
        changed = tx.put("mission_task_control", prior["id"], {
            **prior["body"],
            "selected_passage_refs": {copy_ref["id"]: selected[:1]},
        }, expected_version=prior["version"])
        return ref(changed)

    changed_control = store.transact("remove-window-ample", {}, remove_window, mission_id="m")
    pack = context.assemble(
        refs["next_call"], task, source_refs=[copy_ref],
        control_ref=changed_control, tool_turn_refs=[refs["response"]],
        allowance=10000, estimate_tokens=lambda value: math.ceil(len(value) / 4 * 1.25),
    )
    assert pack.tool_result_compactions == ()
    assert "text_preview" in pack.messages[-2]["content"]


def test_source_free_recovery_archives_window_as_exact_text_free_record(tmp_path):
    store, context, task, _copy_ref, _window, refs, _tool_result_ref = _window_turn(tmp_path)
    pack = context.assemble(
        refs["source_free_call"], task,
        archived_tool_turn_refs=[refs["response"]],
        allowance=1700, estimate_tokens=lambda value: math.ceil(len(value) / 4 * 1.25),
    )
    assert pack.tool_result_compactions == ()
    assert all(message["role"] != "tool" for message in pack.messages)
    assert refs["response"] in [item["ref"] for item in pack.omitted]
    assert "A" * 100 not in canonical(pack.messages)


def test_legacy_window_without_optional_limits_keeps_quality_backed_partial_status(tmp_path):
    _store, context, task, copy_ref, window, refs, _result_ref = _window_turn(
        tmp_path, omit_legacy_optional_fields=True
    )
    pack = context.assemble(
        refs["next_call"], task, source_refs=[copy_ref],
        control_ref=refs["control"], tool_turn_refs=[refs["response"]],
        allowance=1500, estimate_tokens=lambda value: math.ceil(len(value) / 4 * 1.25),
        compact_selected_window=True,
    )
    presented = json.loads(pack.messages[-2]["content"])
    assert presented["status"] == "saved_window_text_in_selected_source_packet"
    assert presented["window_ref"] == window["window_ref"]
    assert presented["returned_chars"] == 800
    assert presented["original_source_partial"] is False
    assert presented["saved_omissions_count"] is None
    assert presented["remaining_in_block_chars"] is None
    assert presented["next_cursor_scope"] == "retained_extracted_text_only"


def test_window_preview_is_not_suppressed_without_that_exact_selected_passage(tmp_path):
    store, context, task, copy_ref, _window, refs, _tool_result_ref = _window_turn(tmp_path)

    def remove_window(tx):
        prior = tx.get(**refs["control"])
        selected = prior["body"]["selected_passage_refs"][copy_ref["id"]]
        changed = tx.put("mission_task_control", prior["id"], {
            **prior["body"],
            "selected_passage_refs": {copy_ref["id"]: selected[:1]},
        }, expected_version=prior["version"])
        return ref(changed)

    changed_control = store.transact(
        "remove-selected-window", {}, remove_window, mission_id="m"
    )
    with pytest.raises(ContextOverflow, match="Required task"):
        context.assemble(
            refs["next_call"], task, source_refs=[copy_ref],
            control_ref=changed_control, tool_turn_refs=[refs["response"]],
            allowance=1500, estimate_tokens=lambda value: math.ceil(len(value) / 4 * 1.25),
        )
