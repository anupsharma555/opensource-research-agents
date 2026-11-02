"""New research results and their model-supplied arguments keep source lineage."""

import json

import pytest

from ora.coordination.board import ref
from ora.mission.analysis_extended import EXTENDED_ANALYSIS_TOOL_SCHEMAS
from ora.mission.context import ContextError, WorkingContext
from ora.state import StateStore
from tests.mission.test_context import fixture
from tests.mission.test_note_source_use import revoke, source_note
from tests.mission.test_tool_result_paging import save_result


@pytest.mark.parametrize("name", [
    "calculate", "analyze_values", "convert_units", "search_saved_passages",
    *EXTENDED_ANALYSIS_TOOL_SCHEMAS,
])
@pytest.mark.parametrize("rejected", [False, True])
def test_result_and_argument_revocation_before_and_after_preparation(tmp_path, name, rejected):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    saved = source_note(store, task)
    secret = ("73829.25" if name in {"analyze_values", "convert_units"}
              else "SYNTHETIC_SOURCE_DERIVED_VALUE")
    result = {"status": "rejected"} if rejected else {
        **({"group_a": {"mean": secret}} if name == "analyze_values" else
           {"value": secret} if name == "convert_units" else {"text": secret}),
        "source_dependency_refs": [saved["copy"]],
    }
    arguments = (
        {"operation": "describe", "group_a": [73829.25, 73829.25]}
        if name == "analyze_values" else
        {"value": 73829.25, "from_unit": "gram", "to_unit": "milligram"}
        if name == "convert_units" else
        {"value": secret}
    )
    response, _ = save_result(store, call, name, result, arguments=arguments)
    store.transact("research-intent", {}, lambda tx: ref(tx.put(
        "model_request_intent", f"model-intent:{call['id']}:1", {
            "mission_id": "m", "call_ref": call, "source_refs": [],
            "context_source_dependency_refs": [saved["copy"]],
        },
    )), mission_id="m")
    context = WorkingContext(store)
    prepared = context.assemble(call, task, tool_turn_refs=[response])
    assert secret in json.dumps(prepared.messages)
    assert saved["copy"] in context.context_source_dependency_refs(prepared)
    revoke(store, saved["use"])
    with pytest.raises(ContextError):
        context.context_source_dependency_refs(prepared)
    replayed_context = WorkingContext(StateStore(tmp_path))
    replayed = replayed_context.assemble(call, task, tool_turn_refs=[response])
    assert secret not in json.dumps(replayed.messages)
    assert "data_unavailable_due_source_use" in json.dumps(replayed.messages)
    assert replayed_context.context_source_dependency_refs(replayed) == ()


def test_saved_search_snippet_binds_source_even_without_source_in_producing_input(tmp_path):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    saved = source_note(store, task)
    response, _ = save_result(store, call, "search_saved_passages", {
        "results": [{"snippet": "SYNTHETIC_NEW_SEARCH_TEXT"}],
        "source_dependency_refs": [saved["copy"]],
    })
    store.transact("source-free-search-intent", {}, lambda tx: ref(tx.put(
        "model_request_intent", f"model-intent:{call['id']}:1", {
            "mission_id": "m", "call_ref": call, "source_refs": [],
            "context_source_dependency_refs": [],
        },
    )), mission_id="m")
    context = WorkingContext(store)
    pack = context.assemble(call, task, tool_turn_refs=[response])
    assert saved["copy"] in context.context_source_dependency_refs(pack)
    revoke(store, saved["use"])
    pack = context.assemble(call, task, tool_turn_refs=[response])
    assert "SYNTHETIC_NEW_SEARCH_TEXT" not in json.dumps(pack.messages)
