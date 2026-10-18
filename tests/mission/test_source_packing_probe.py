"""Unselected schemas yield to checked source text before a rate probe."""

import json

from ora.coordination.board import ref
from ora.mission.source_context import plan_source_turn
from ora.mission.tool_catalog import DEFERABLE_CORE_TOOLS
from ora.models.token_estimate import estimate_request_tokens
from tests.mission.test_capability_catalog import _index
from tests.mission.test_selected_window_admission import _ready_selected_window


def test_selected_source_packs_before_provider_feedback_probe(tmp_path, monkeypatch):
    host, run, actor, copy_ref, window, _response, _result_ref = _ready_selected_window(
        tmp_path, monkeypatch, window_chars=2000, crowded=True,
    )
    try:
        control = host._control(run.run_id, actor)
        proposal = host.store.get("board_proposal", control["body"]["task_id"])
        boundary = None
        for target in range(5500, 8001, 50):
            plan = plan_source_turn(host.store, ref(control),
                                    source_input_allowance_tokens=target)
            if plan["mode"] == "source_free_recovery":
                continue
            normal = host._preview_soft_source_plan(
                control, proposal, plan, total_target=target,
                sequence=control["body"]["sequence"] + 1,
            )
            compact = host._preview_soft_source_plan(
                control, proposal, plan, total_target=target,
                sequence=control["body"]["sequence"] + 1,
                defer_core_tools=True,
            )
            if normal is None and compact is not None:
                boundary = target
                break
        assert boundary is not None, "Fixture must expose a schema-only fit gap"
        host._update_control(control, input_budget={
            "observed_limit_tokens": boundary + 6, "target_fraction": 0.70,
        })
        assert host._schedule_model(host._control(run.run_id, actor))
        prepared = host._control(run.run_id, actor)
        call = host.store.get(**prepared["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
        )
        raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
        packet = json.loads(raw)
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        assert budget["source_turn_mode"] == "all_allowed_sources_fit"
        assert budget["rate_admission_policy"] == "estimate_pacing"
        assert set(budget["deferred_core_tool_ids"]) <= DEFERABLE_CORE_TOOLS
        assert budget["deferred_core_tool_ids"]
        assert intent["body"]["source_decisions"][0]["passage_refs"] == (
            window["passage_refs"]
        )
        passage_text = host.store.get(**window["passage_refs"][0])["body"]["text"]
        source_packet = next(
            json.loads(message["content"])["source_packets"][0]
            for message in packet["messages"]
            if message["role"] == "user" and "source_packets" in message["content"]
        )
        assert source_packet["text"].count(passage_text) == 1
        assert not any(passage_text in (message.get("content") or "")
                       for message in packet["messages"] if message["role"] == "tool")
        assert copy_ref in intent["body"]["source_refs"]
        assert estimate_request_tokens(raw).tokens + 6 <= boundary + 6
        assert packet["max_completion_tokens"] == 8192
        available = _index(packet["messages"][0]["content"])
        assert set(DEFERABLE_CORE_TOOLS) <= set(available["tools"])
        config = host._configuration(prepared)
        requested = {**prepared, "body": {
            **prepared["body"], "requested_capabilities": ["search_scholarly"],
        }}
        exposed, _ = host._exposed_tools(requested, config, defer_core_tools=True)
        assert "search_scholarly" in exposed
    finally:
        host.close()


def test_forecast_only_overflow_probes_selected_source_after_deferral(
    tmp_path, monkeypatch,
):
    host, run, actor, copy_ref, window, _response, _result_ref = _ready_selected_window(
        tmp_path, monkeypatch, window_chars=2000, crowded=True,
    )
    try:
        control = host._control(run.run_id, actor)
        host._update_control(control, input_budget={
            "observed_limit_tokens": 4500, "target_fraction": 0.70,
        })
        assert host._schedule_model(host._control(run.run_id, actor))
        prepared = host._control(run.run_id, actor)
        call = host.store.get(**prepared["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
        )
        raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        assert budget["source_turn_mode"] == "all_allowed_sources_fit"
        assert budget["rate_admission_policy"] == "provider_feedback_probe"
        assert budget["deferred_core_tool_ids"]
        assert copy_ref in intent["body"]["source_refs"]
        assert intent["body"]["source_decisions"][0]["passage_refs"] == (
            window["passage_refs"]
        )
        assert estimate_request_tokens(raw).tokens + budget["prior_completion_reservation"] > 4500
        assert json.loads(raw)["max_completion_tokens"] == 8192
    finally:
        host.close()
