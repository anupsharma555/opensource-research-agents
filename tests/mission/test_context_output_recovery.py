"""Selected-call context can page a visible cutoff without replaying private output."""

from __future__ import annotations

import json

import pytest

from ora.agents import CapabilityCatalog, ConfigurationRegistry
from ora.agents.capabilities import content_hash
from ora.coordination.board import ref
from ora.mission.context import ContextError, WorkingContext
from ora.models import GroqSelectedAdapter, ModelSession, SourceSelection
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_output_recovery import ROOT, VISIBLE, _saved_partial


def _next_call(store: StateStore, actor_id: str, source_ref: dict, response_ref: dict) -> tuple:
    receipt = store.get(**response_ref)
    prior_call = store.get(**receipt["body"]["call_ref"])
    registry = ConfigurationRegistry(store, ROOT, CapabilityCatalog.with_common())
    call = registry.start_call(
        "partial-next-call", call_id="call-next",
        configuration_id=prior_call["body"]["configuration_id"],
        mission_id="mission", task_id="task", attempt_id="attempt-next",
        loaded_skill_ids=[], exposed_tool_ids=[], require_common_tools=False,
    )

    def save(tx):
        run = tx.put("mission_run", "run", {
            "mission_id": "mission", "question": "Continue synthetic synthesis?",
            "scope": "Use the exact selected source", "source_refs": [source_ref],
        })
        task = tx.put("board_proposal", "task", {
            "mission_id": "mission", "status": "open",
            "objective": "Finish the supported contribution",
            "next_action": "Recover only permitted visible draft text",
        })
        control = tx.put("mission_task_control", "partial-control", {
            "mission_id": "mission", "run_id": run["id"],
            "actor_id": actor_id, "task_id": task["id"],
            "source_refs": [source_ref],
            "feedback": {"status": "partial", "previous_response_ref": response_ref},
        })
        return {"task": ref(task), "control": ref(control)}

    saved = store.transact("partial-current-task", {}, save, mission_id="mission")
    return ref(call), saved["task"], saved["control"]


def test_partial_visible_output_rebuilds_and_pages_from_actual_selected_call(tmp_path) -> None:
    visible = VISIBLE * 90
    store, response_ref, actor_id, source_ref, policy = _saved_partial(
        tmp_path, visible=visible
    )
    call_ref, task_ref, control_ref = _next_call(
        store, actor_id, source_ref, response_ref
    )
    context = WorkingContext(store)
    pack = context.assemble(
        call_ref, task_ref, control_ref=control_ref, source_refs=[source_ref],
    )
    view = json.loads(pack.messages[-1]["content"])["partial_visible_output"]
    assert view["status"] == "available"
    assert view["response_ref"] == response_ref
    assert view["text_preview"] == visible[:600]
    assert view["next_offset"] == 600
    assert view["next_request"]["offset"] == 600
    assert "PRIVATE_REASONING_SENTINEL" not in canonical(pack.messages)
    assert "cutoff-tool" not in canonical(pack.messages)
    tight = context.assemble(
        call_ref, task_ref, control_ref=control_ref, source_refs=[source_ref],
        allowance=len(canonical(pack.messages)) - 300, estimate_tokens=len,
    )
    tight_view = json.loads(tight.messages[-1]["content"])["partial_visible_output"]
    assert tight_view["status"] == "available"
    assert "text_preview" not in tight_view
    assert tight_view["preview_characters"] == 0
    assert tight_view["next_request"]["offset"] == 0

    prepared = ModelSession(store, ROOT, adapter=GroqSelectedAdapter(ROOT)).prepare(
        call_id=call_ref["id"], call_version=call_ref["version"], phase="final",
        messages=pack.messages,
        sources=(SourceSelection(source_ref, "permitted", True),),
    )
    inclusion = context.save_inclusion(
        "partial-current-inclusion", pack, prepared.intent_ref
    )
    reopened = WorkingContext(StateStore(tmp_path))
    rebuilt = reopened.assemble(
        call_ref, task_ref, control_ref=control_ref, source_refs=[source_ref],
    )
    assert json.loads(rebuilt.messages[-1]["content"])["partial_visible_output"] == view

    page_position = next(
        index for index, item in enumerate(pack.omitted)
        if item["type"] == "partial_visible_output"
    )
    page = reopened.read_omitted_page(
        mission_id="mission", actor_id=actor_id, task_id="task",
        inclusion_ref=inclusion, cursor=page_position, limit=1,
    )
    assert page["items"][0]["summary"]["status"] == "available"
    assert "text" not in page["items"][0]["summary"]
    assert visible[:100] not in canonical(page)
    chunks = []
    offset = 0
    while True:
        window = reopened.read_omitted_record(
            mission_id="mission", actor_id=actor_id, task_id="task",
            inclusion_ref=inclusion, pointer=response_ref, offset=offset, limit=50_000,
        )
        assert window["status"] == "available"
        assert window["effective_limit"] == 2000
        chunks.append(window["text"])
        if window["next_offset"] is None:
            break
        offset = window["next_offset"]
    assert "".join(chunks) == visible
    with pytest.raises(ContextError, match="another peer"):
        reopened.read_omitted_record(
            mission_id="mission", actor_id="foreign-actor", task_id="task",
            inclusion_ref=inclusion, pointer=response_ref, offset=0, limit=2000,
        )

    def revoke(tx):
        previous = tx.get("source_use", "policy-first")
        return ref(tx.put(
            "source_use", "policy-first",
            {**policy, "policy_status": "revoked", "allowed_uses": ["discovery"]},
            expected_version=previous["version"],
        ))

    store.transact("partial-current-revoke", {}, revoke, mission_id="mission")
    denied = reopened.assemble(
        call_ref, task_ref, control_ref=control_ref, source_refs=[source_ref],
    )
    assert json.loads(denied.messages[-1]["content"])["partial_visible_output"]["status"] == "reissue_required"
    assert visible[:600] not in canonical(denied.messages)
    denied_window = reopened.read_omitted_record(
        mission_id="mission", actor_id=actor_id, task_id="task",
        inclusion_ref=inclusion, pointer=response_ref, offset=0, limit=2000,
    )
    assert denied_window["status"] == "reissue_required"
    assert "text" not in denied_window


def test_partial_draft_is_diagnostic_when_current_call_excludes_prior_source(tmp_path) -> None:
    store, response_ref, actor_id, source_ref, _policy = _saved_partial(tmp_path)
    call_ref, task_ref, control_ref = _next_call(store, actor_id, source_ref, response_ref)
    prior_call = store.get(**call_ref)
    prior_config = store.get("agent_configuration", prior_call["body"]["configuration_id"], 1)

    def save_source_free(tx):
        config_body = {**prior_config["body"], "input_refs": []}
        config_body["configuration_sha256"] = content_hash({
            key: value for key, value in config_body.items() if key != "configuration_sha256"
        })
        config = tx.put("agent_configuration", "source-free-config", config_body)
        call = tx.put("agent_call", "source-free-call", {
            **prior_call["body"], "configuration_id": config["id"],
            "configuration_sha256": config_body["configuration_sha256"],
            "input_refs": [], "attempt_id": "source-free-attempt",
        })
        current = tx.get(**control_ref)
        control = tx.put("mission_task_control", current["id"], {
            **current["body"], "source_refs": [],
        }, expected_version=current["version"])
        return {"call": ref(call), "control": ref(control)}

    changed = store.transact("source-free-partial-call", {}, save_source_free,
                             mission_id="mission")
    pack = WorkingContext(store).assemble(
        changed["call"], task_ref, control_ref=changed["control"],
    )
    view = json.loads(pack.messages[-1]["content"])["partial_visible_output"]
    assert view["status"] == "reissue_required"
    assert view["reason"] == "source_context_changed"
    assert "text_preview" not in view
    assert all(item["type"] != "partial_visible_output" for item in pack.omitted)


def test_partial_without_visible_text_has_no_pageable_draft(tmp_path) -> None:
    store, response_ref, actor_id, source_ref, _policy = _saved_partial(
        tmp_path, visible=None
    )
    call_ref, task_ref, control_ref = _next_call(store, actor_id, source_ref, response_ref)
    pack = WorkingContext(store).assemble(
        call_ref, task_ref, control_ref=control_ref, source_refs=[source_ref],
    )
    view = json.loads(pack.messages[-1]["content"])["partial_visible_output"]
    assert view["status"] == "content_unavailable"
    assert "text_preview" not in view
    assert all(item["type"] != "partial_visible_output" for item in pack.omitted)
