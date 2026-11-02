"""Archived HTML reads keep safe next locations in the next model input."""

from __future__ import annotations

import json

import pytest

from ora.coordination.board import ref
from ora.evidence import read_source_window
from ora.mission.context import WorkingContext
from ora.state.store import canonical
from tests.evidence.test_coverage import checked_copy
from tests.mission.test_context import fixture

HTML = (
    b"<html><h1>Abstract</h1><p>DOI: 10.1234/coverage.</p>"
    b"<h2>Results</h2><p>A synthetic outcome was reported.</p>"
    b"<h2>Discussion</h2><p>Interpretation was limited.</p></html>"
)


def _archived_html_read(tmp_path, *, legacy=False, altered_section=None):
    library, saved = checked_copy(
        tmp_path, HTML, mime="text/html", surface="full_text", truncated=False,
    )
    store = library.store
    prior_call, task = fixture(store)
    copy_ref = saved["copy_ref"]
    window = read_source_window(
        library, "first-html-window", copy_ref,
        block_offset=0, block_limit=1, char_offset=0, char_limit=2000,
    )
    assert window["passages"][0]["text_preview"] == "Abstract"
    assert window["next_cursor"] == {"block_offset": 1, "char_offset": 0}
    sections = window["navigation"]["sections"]
    assert [(item["label"], item["block_offset"]) for item in sections] == [
        ("Abstract", 0), ("Results", 2), ("Discussion", 4),
    ]
    result = {
        **window,
        "passages": [
            {**item, "span_id": store.get(**item["passage_ref"])["body"]["span_id"]}
            for item in window["passages"]
        ],
        "model_selection": {
            "mode": "replace", "passage_refs": window["passage_refs"],
            "prior_passage_refs": [],
        },
        "next_request": {
            "source_id": copy_ref["id"], "source_version": copy_ref["version"],
            **window["next_cursor"], "char_limit": 2000,
            "selection_mode": "replace",
        },
    }
    if not legacy:
        # This is the normal MissionService navigation projection from the
        # retained outline. It points to a location; it does not read that text.
        result["navigation"]["next_section_request"] = {
            "source_id": copy_ref["id"], "source_version": copy_ref["version"],
            "block_offset": 3 if altered_section == "unlisted_offset" else sections[1]["block_offset"],
            "char_offset": 0, "char_limit": 2000, "selection_mode": "replace",
        }
        if altered_section == "foreign_copy":
            result["navigation"]["next_section_request"]["source_id"] = "another-copy"
    else:
        result.pop("navigation")
    private = store.put_blob(canonical({
        "content": None,
        "tool_calls": [{
            "id": "html-window-tool", "type": "function", "function": {
                "name": "read_source_window", "arguments": canonical({
                    "source_id": copy_ref["id"], "source_version": copy_ref["version"],
                    "block_offset": 0, "char_offset": 0, "char_limit": 2000,
                    "selection_mode": "replace",
                }),
            },
        }],
    }).encode())

    def save(tx):
        run = tx.put("mission_run", "html-run", {
            "mission_id": "m", "question": "Read the synthetic HTML source",
            "scope": "Use the retained HTML", "source_refs": [copy_ref],
        })
        control = tx.put("mission_task_control", "html-control", {
            "mission_id": "m", "run_id": run["id"], "actor_id": "peer",
            "task_id": task["id"], "source_refs": [copy_ref],
            "selected_passage_refs": {copy_ref["id"]: window["passage_refs"]},
            "read_window_refs": [window["window_ref"]],
        })
        earlier = tx.get(**prior_call)
        call = tx.put("agent_call", "next-html-call", {
            **earlier["body"], "input_refs": [copy_ref],
        })
        response = tx.put("model_response_receipt", "html-read-response", {
            "mission_id": "m", "task_id": task["id"],
            "call_ref": prior_call, "status": "tool_requested", "blob": private,
        })
        return {"control": ref(control), "call": ref(call), "response": ref(response)}

    refs = store.transact("html-read-setup", {}, save, mission_id="m")
    control, call, response = refs["control"], refs["call"], refs["response"]
    context = WorkingContext(store)
    context.save_tool_result(
        "html-read-result", response_ref=response, tool_call_id="html-window-tool",
        name="read_source_window", content=canonical(result),
    )
    return store, context, task, copy_ref, window, control, call, response


def _next_input(context, task, copy_ref, control, call, response, *, allowance=8000):
    pack = context.assemble(
        call, task, source_refs=[copy_ref], control_ref=control,
        archived_tool_turn_refs=[response], allowance=allowance,
        estimate_tokens=len,
    )
    assert pack.estimated_tokens <= allowance
    serialized = canonical({"messages": pack.messages})
    return pack, json.loads(json.loads(serialized)["messages"][-1]["content"])


@pytest.mark.parametrize("legacy,altered_section", [
    (False, None), (True, None),
    (False, "unlisted_offset"), (False, "foreign_copy"),
])
@pytest.mark.parametrize("allowance", [8000, 3200, 2600])
def test_archived_html_navigation_in_serialized_next_input(
    tmp_path, legacy, altered_section, allowance,
):
    _, context, task, copy_ref, window, control, call, response = (
        _archived_html_read(tmp_path, legacy=legacy, altered_section=altered_section)
    )
    pack, index = _next_input(
        context, task, copy_ref, control, call, response, allowance=allowance,
    )
    latest = index["last_checked_window"]
    if allowance == 8000:
        assert latest["copy_ref"] == copy_ref
    else:
        assert "copy_ref" not in latest
        assert latest["source_text_status"] == "checked_window_passages_in_selected_source_packet"
    assert latest["window_ref"] == window["window_ref"]
    assert latest["next_request"] == {
        "source_id": copy_ref["id"], "source_version": copy_ref["version"],
        "block_offset": 1, "char_offset": 0, "char_limit": 2000,
        "selection_mode": "replace",
    }
    if legacy or altered_section:
        assert "next_section_request" not in latest
    else:
        assert latest["next_section_request"] == {
            "source_id": copy_ref["id"], "source_version": copy_ref["version"],
            "block_offset": 2, "char_offset": 0, "char_limit": 2000,
            "selection_mode": "replace",
        }
    serialized = canonical({"messages": pack.messages})
    assert "A synthetic outcome was reported" not in serialized
    assert "Interpretation was limited" not in serialized
    assert '"label":"Results"' not in serialized


@pytest.mark.parametrize("restriction", ["unselected", "revoked"])
def test_archived_html_navigation_cannot_bypass_current_source_use(tmp_path, restriction):
    store, context, task, copy_ref, _, control, call, response = (
        _archived_html_read(tmp_path)
    )
    if restriction == "unselected":
        def unselect(tx):
            prior = tx.get(**control)
            return ref(tx.put(prior["kind"], prior["id"], {
                **prior["body"], "selected_passage_refs": {copy_ref["id"]: []},
            }, expected_version=prior["version"]))

        control = store.transact("unselect-html-window", {}, unselect, mission_id="m")
    else:
        use_ref = context._source_status(store.get(**copy_ref))["current_source_use_ref"]

        def revoke(tx):
            prior = tx.get(**use_ref)
            changed = tx.put(prior["kind"], prior["id"], {
                **prior["body"], "allowed_uses": [], "policy_status": "revoked",
            }, expected_version=prior["version"])
            return ref(changed)

        store.transact("revoke-html-use", {}, revoke, mission_id="m")
    pack, index = _next_input(context, task, copy_ref, control, call, response)
    assert "last_checked_window" not in index
    assert "next_section_request" not in canonical(pack.messages)
