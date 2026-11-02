"""Page one saved capability result without changing its scope or purpose text."""

from __future__ import annotations

import json

import pytest

from ora.mission.context import ContextError, WorkingContext
from ora.mission.tool_results import MAX_RESULT_PAGE_CHARS, ResultProjectionChanged
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import fixture
from tests.mission.test_tool_result_paging import save_result


def _catalog() -> dict:
    granted = {
        f"synthetic_tool_{index:02d}": (
            f"Exact saved purpose {index:02d}: read a bounded synthetic item; "
            "the original host wording and punctuation must survive paging."
        )
        for index in range(72)
    }
    return {
        "granted": granted,
        "exposed": ["synthetic_tool_00"],
        "not_handled_here": ["synthetic_tool_71"],
        "requestable": sorted(set(granted) - {
            "synthetic_tool_00", "synthetic_tool_70", "synthetic_tool_71",
        }),
        "not_ready": {"synthetic_tool_70": "Prerequisite synthetic record is missing."},
        "requestable_native_tools": [],
        "next_action": "Request an eligible saved tool by its exact ID on a later turn.",
    }


def _page(context: WorkingContext, result: dict, *, offset: int = 0,
          projection_sha256: str = "", mission_id: str = "m",
          actor_id: str = "peer", task_id: str = "task") -> dict:
    return context.read_tool_result(
        mission_id=mission_id, actor_id=actor_id, task_id=task_id,
        result_id=result["id"], offset=offset, limit=900,
        projection_sha256=projection_sha256,
    )


def test_saved_capability_purposes_roundtrip_across_exact_pages_and_restart(tmp_path):
    store = StateStore(tmp_path)
    call, _task = fixture(store)
    expected = _catalog()
    response, result = save_result(store, call, "discover_capabilities", expected)
    original_blob = store.read_blob(store.get(**result)["body"]["blob"]["sha256"])
    original_records = len(store.records("context_tool_result"))

    exchange, receipt = WorkingContext(store)._tool_exchange(response)
    first = json.loads(exchange[1]["content"])
    assert first["schema"] == "ora.saved_tool_result_window.v1"
    assert first["saved_tool_result_ref"] == result
    assert receipt["saved_tool_result_ref"] == result

    reopened = WorkingContext(StateStore(tmp_path))
    pages = []
    current = first
    while True:
        assert len(canonical(current)) <= MAX_RESULT_PAGE_CHARS
        assert current["end_offset"] > current["offset"]
        pages.append(current)
        next_request = current["next_request"]
        if next_request is None:
            break
        assert next_request["tool"] == "read_tool_result"
        current = _page(
            reopened, result, offset=next_request["offset"],
            projection_sha256=next_request["projection_sha256"],
        )

    reconstructed = json.loads("".join(page["text"] for page in pages))
    assert len(pages) > 1
    assert reconstructed == expected
    assert reconstructed["granted"]["synthetic_tool_37"] == expected["granted"][
        "synthetic_tool_37"
    ]
    assert store.read_blob(store.get(**result)["body"]["blob"]["sha256"]) == original_blob
    assert len(store.records("context_tool_result")) == original_records == 1
    assert len(store.records("model_response_receipt")) == 1
    assert len(store.records("agent_call")) == 1
    with pytest.raises(ResultProjectionChanged):
        _page(reopened, result, offset=pages[0]["end_offset"],
              projection_sha256="0" * 64)


@pytest.mark.parametrize("scope", [
    {"mission_id": "other"}, {"actor_id": "other"}, {"task_id": "other"},
])
def test_saved_capability_pages_reject_foreign_scope(tmp_path, scope):
    store = StateStore(tmp_path)
    call, _task = fixture(store)
    _response, result = save_result(store, call, "discover_capabilities", _catalog())
    with pytest.raises(ContextError):
        _page(WorkingContext(store), result, **scope)


@pytest.mark.parametrize("change", [
    {"granted": {"synthetic_tool_00": 7}},
    {"requestable": ["outside_grant"]},
    {"next_action": ""},
    {"unrecognized_private_payload": {"text": "must not page"}},
])
def test_saved_capability_reader_rejects_malformed_result(tmp_path, change):
    store = StateStore(tmp_path)
    call, _task = fixture(store)
    _response, result = save_result(store, call, "discover_capabilities", {
        **_catalog(), **change,
    })
    with pytest.raises(ContextError, match="capability catalog result is malformed"):
        _page(WorkingContext(store), result)
