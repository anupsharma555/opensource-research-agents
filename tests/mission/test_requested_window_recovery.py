"""An exact requested read can survive a crowded two-copy continuation."""

from __future__ import annotations

import json
import math

import pytest

from ora.coordination.board import ref
from ora.mission.source_context import plan_source_turn, save_working_sources
from tests.mission.test_selected_window_admission import (
    _ready_selected_window,
    _saved_tool_request,
)
from tests.mission.test_service import MODEL


def _two_copy_read(tmp_path, monkeypatch, *, window_chars=2000,
                   completion_tokens=6, crowded=True, late_text=None):
    host, run, actor, early, window, response, _ = _ready_selected_window(
        tmp_path, monkeypatch, window_chars=window_chars,
        completion_tokens=completion_tokens, crowded=crowded,
    )
    run_record = host._run(run.run_id)
    late = host._supplied_source({
        **run_record,
        "id": run.run_id + "-second-synthetic-source",
        "body": {
            **run_record["body"],
            "profile": {
                **run_record["body"]["profile"],
                "evidenceText": late_text or "SECOND_COPY_MARKER " + "B" * 1805,
            },
        },
    })
    control = host._control(run.run_id, actor)
    host._update_control(
        control, source_refs=[early, late], response_ref=response,
    )
    return host, run, actor, early, late, window, response


def test_latest_requested_window_is_in_next_request_with_other_copy_recoverable(
    tmp_path, monkeypatch
):
    host, run, actor, early, late, window, response = _two_copy_read(
        tmp_path, monkeypatch
    )
    try:
        control = host._control(run.run_id, actor)
        proposal = host.store.get("board_proposal", control["body"]["task_id"])
        target = None
        observed_limit = None
        # The full planning/search catalog adds fixed overhead; allow room around the
        # one-copy boundary for generated record IDs and timestamps to vary.
        # Preview the same hard input bound the scheduler will apply after its
        # completion reservation and observed-limit headroom.
        for allowance in range(5300, 9001, 100):
            candidate_limit = math.floor((allowance + 6) / .90)
            candidate_target = candidate_limit - math.ceil(candidate_limit * .10) - 6
            all_sources = plan_source_turn(
                host.store, ref(control), source_input_allowance_tokens=candidate_target
            )
            all_fit = (
                host._preview_soft_source_plan(
                    control, proposal, all_sources, total_target=candidate_target,
                    sequence=control["body"]["sequence"] + 1,
                )
                if all_sources["mode"] != "source_free_recovery" else None
            )
            source_free = plan_source_turn(
                host.store, ref(control), source_input_allowance_tokens=1
            )
            one = host._requested_window_source_plan(control, source_free)
            one_fit = (
                host._preview_soft_source_plan(
                    control, proposal, one, total_target=candidate_target,
                    sequence=control["body"]["sequence"] + 1,
                ) if one else None
            )
            if (not all_fit and one_fit and (
                one_fit["compact_selected_window"] or one_fit["archive_selected_window"]
            )):
                target = candidate_target
                observed_limit = candidate_limit
                break
        assert target is not None, "Synthetic input must separate one-copy and two-copy fit"
        host._update_control(control, input_budget={
            "observed_limit_tokens": observed_limit, "target_fraction": 1.0,
        })
        assert host._schedule_model(host._control(run.run_id, actor))
        prepared = host._control(run.run_id, actor)
        assert prepared["body"]["source_refs"] == [early, late]
        call = host.store.get(**prepared["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        )
        request = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        text = json.dumps(request["messages"])
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        assert request["model"] == MODEL
        assert request["reasoning_effort"] == "medium"
        expected_tools = {
            "activate_skill", "board_read", "discover_capabilities", "peer_request",
            "publish_result", "read_context_page", "read_context_record",
            "read_source_window", "search_saved_passages", "release_skill", "request_capability",
            "review_peer_answer", "search_scholarly",
        }
        assert len(request["tools"]) == len(expected_tools)
        assert {item["function"]["name"] for item in request["tools"]} == expected_tools
        assert {"read_source_window", "discover_capabilities", "request_capability"} <= set(
            call["body"]["exposed_tools"]
        )
        configuration = host.store.get(**intent["body"]["configuration_ref"])
        assert {"read_source_inventory", "select_working_sources"} <= set(
            configuration["body"]["permitted_tools"]
        )
        catalog_text = request["messages"][0]["content"].split(
            "Role capability catalog: ", 1
        )[1]
        visible_catalog, _ = json.JSONDecoder().raw_decode(catalog_text)
        assert {"read_source_inventory", "select_working_sources"} <= set(
            visible_catalog["tools"]
        )
        assert intent["body"]["source_refs"] == [early]
        assert budget["source_turn_mode"] == "requested_window_recovery"
        assert budget["retained_source_refs"] == [early, late]
        assert budget["prior_completion_reservation"] == 6
        assert budget["exact_complete_request_estimate_tokens"] + 6 <= observed_limit
        assert "SELECTED_MARKER" in text
        assert "SECOND_COPY_MARKER" not in text
        assert late in [
            item for message in request["messages"] if message["role"] == "user"
            for item in json.loads(message["content"]).get("omitted_source_refs", [])
        ]
        assert response in (
            budget["compacted_tool_turn_refs"]
            + ([budget["current_tool_turn_ref"]] if budget["current_tool_turn_ref"] else [])
        )
        later_plan = plan_source_turn(
            host.store, ref(prepared), source_input_allowance_tokens=20000
        )
        assert [item["copy_ref"] for item in later_plan["source_inputs"]] == [early, late]
    finally:
        host.close()


def test_realistic_completion_reservation_delivers_requested_window(
    tmp_path, monkeypatch,
):
    late_text = "SECOND_COPY_MARKER " + " ".join(
        f"Participant {number:04d} had endpoint {number * 7919 % 100000:05d};"
        for number in range(200)
    )
    host, run, actor, early, late, window, response = _two_copy_read(
        tmp_path, monkeypatch, window_chars=1900, completion_tokens=628, crowded=False,
        late_text=late_text,
    )
    try:
        control = host._control(run.run_id, actor)
        host._update_control(control, input_budget={
            "observed_limit_tokens": 8000, "target_fraction": .70,
        })
        assert host._schedule_model(host._control(run.run_id, actor))
        prepared = host._control(run.run_id, actor)
        call = host.store.get(**prepared["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        )
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        request = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        packets = [packet for message in request["messages"] if message["role"] == "user"
                   for packet in json.loads(message["content"]).get("source_packets", [])]
        omitted = [pointer for message in request["messages"] if message["role"] == "user"
                   for pointer in json.loads(message["content"]).get("omitted_source_refs", [])]
        selected_text = host.store.get(**window["passage_refs"][0])["body"]["text"]
        index = json.loads(request["messages"][-1]["content"])
        configuration = host.store.get(**intent["body"]["configuration_ref"])
        assert budget["source_turn_mode"] == "requested_window_recovery"
        assert budget["prior_completion_reservation"] == 628
        assert budget["observed_account_limit"] == 8000
        soft_target = int(budget["observed_account_limit"] * .70)
        hard_target = (
            budget["observed_account_limit"]
            - budget["prior_completion_reservation"]
            - budget["rate_input_headroom_tokens"]
        )
        assert budget["preview_admission_target_tokens"] in {soft_target, hard_target}
        assert budget["exact_complete_request_estimate_tokens"] <= budget["preview_admission_target_tokens"]
        assert budget["exact_complete_request_estimate_tokens"] + 628 <= 8000
        assert intent["body"]["source_refs"] == [early]
        assert budget["retained_source_refs"] == prepared["body"]["source_refs"] == [early, late]
        assert len(packets) == 1 and selected_text in packets[0]["text"]
        assert packets[0]["passage_refs"] == window["passage_refs"]
        assert late in omitted
        selected_scope = next(item["selected_input_scope"] for item in index["source_retrieval"]
                              if item["ref"] == early)
        assert selected_scope["first_omitted_cursor"] == window["next_cursor"]
        assert index["followup_target"]["target_ref"] == host._run(run.run_id)["body"]["target_ref"]
        assert response in (
            budget["compacted_tool_turn_refs"]
            + ([budget["current_tool_turn_ref"]] if budget["current_tool_turn_ref"] else [])
        )
        assert request["reasoning_effort"] == "medium"
        assert request["max_completion_tokens"] == 8192
        assert {"read_source_window", "read_context_record", "request_capability"} <= {
            item["function"]["name"] for item in request["tools"]
        }
        assert {"read_source_inventory", "select_working_sources"} <= set(
            configuration["body"]["permitted_tools"]
        )
        assert "request_capability" in call["body"]["exposed_tools"]
    finally:
        host.close()


@pytest.mark.parametrize("completion_tokens", [1210, 4500])
def test_larger_completion_retains_selected_read_for_provider_feedback(
    tmp_path, monkeypatch, completion_tokens,
):
    host, run, actor, early, late, window, _response = _two_copy_read(
        tmp_path, monkeypatch, window_chars=1000,
        completion_tokens=completion_tokens, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        host._update_control(control, input_budget={
            "observed_limit_tokens": 8000, "target_fraction": .70,
        })
        dispatches_before = len(host.store.records("model_dispatch_attempt"))
        assert host._schedule_model(host._control(run.run_id, actor))
        prepared = host._control(run.run_id, actor)
        call = host.store.get(**prepared["body"]["call_ref"])
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        )
        request = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        assert budget["source_turn_mode"] in {
            "all_allowed_sources_fit", "requested_window_recovery",
        }
        assert budget["prior_completion_reservation"] == completion_tokens
        assert budget["observed_account_limit"] == 8000
        demand = budget["exact_complete_request_estimate_tokens"] + completion_tokens
        if completion_tokens == 4500:
            assert demand > 8000
            assert budget["rate_admission_policy"] == "provider_feedback_probe"
            assert request["max_completion_tokens"] == 8192
            assert budget["exact_complete_request_estimate_tokens"] + 8192 <= budget["model_context_limit"]
        else:
            assert demand <= 8000
        # Descriptions change fixed overhead, not the reading contract:
        # deliver the selected window and keep the other copy recoverable.
        packets = [packet for message in request["messages"] if message["role"] == "user"
                   for packet in json.loads(message["content"]).get("source_packets", [])]
        selected_text = host.store.get(**window["passage_refs"][0])["body"]["text"]
        assert any(selected_text in packet["text"] for packet in packets)
        assert early in intent["body"]["source_refs"]
        assert budget["retained_source_refs"] == prepared["body"]["source_refs"] == [early, late]
        if late not in intent["body"]["source_refs"]:
            omitted = [pointer for message in request["messages"] if message["role"] == "user"
                       for pointer in json.loads(message["content"]).get("omitted_source_refs", [])]
            assert late in omitted
            assert "read_source_window" in call["body"]["exposed_tools"]
            assert host._source_policy_decisions([late]) == ([late], [])
        assert host.store.get(**window["window_ref"])["body"]["copy_ref"] == early
        assert "SELECTED_MARKER" in json.dumps(request)
        assert len(host.store.records("model_dispatch_attempt")) == dispatches_before
    finally:
        host.close()


def test_rate_forecast_probe_preserves_explicit_source_free_recovery(tmp_path, monkeypatch):
    host, run, actor, early, late, _window, _response = _two_copy_read(
        tmp_path, monkeypatch, completion_tokens=8000, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        host._update_control(control, input_budget={
            "observed_limit_tokens": 8000, "target_fraction": .70,
        })
        assert host._schedule_model(host._control(run.run_id, actor), force_soft_recovery=True)
        prepared = host._control(run.run_id, actor)
        call = host.store.get(**prepared["body"]["call_ref"])
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        intent = host.store.get("model_request_intent", f"model-intent:{call['id']}:1")
        request = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        assert budget["rate_admission_policy"] == "provider_feedback_probe"
        assert budget["source_turn_mode"] == "source_free_recovery"
        assert intent["body"]["source_refs"] == []
        assert prepared["body"]["source_refs"] == [early, late]
        assert "SELECTED_MARKER" not in json.dumps(request)
        assert "SECOND_COPY_MARKER" not in json.dumps(request)
        assert host.store.records("model_dispatch_attempt") == []
    finally:
        host.close()


def test_roomier_limit_keeps_ordinary_all_source_turn(tmp_path, monkeypatch):
    host, run, actor, early, late, _window, _response = _two_copy_read(
        tmp_path, monkeypatch, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        host._update_control(control, input_budget={
            "observed_limit_tokens": 10000, "target_fraction": .70,
        })
        assert host._schedule_model(host._control(run.run_id, actor))
        prepared = host._control(run.run_id, actor)
        call = host.store.get(**prepared["body"]["call_ref"])
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        )
        assert budget["source_turn_mode"] == "all_allowed_sources_fit"
        assert intent["body"]["source_refs"] == [early, late]
    finally:
        host.close()


def test_requested_window_candidate_respects_exact_response_and_working_choice(
    tmp_path, monkeypatch
):
    host, run, actor, early, late, _window, response = _two_copy_read(
        tmp_path, monkeypatch
    )
    try:
        control = host._control(run.run_id, actor)
        source_free = plan_source_turn(
            host.store, ref(control), source_input_allowance_tokens=1
        )
        assert host._requested_window_source_plan(control, source_free)
        host._update_control(control, response_ref=None)
        stale = host._control(run.run_id, actor)
        assert host._requested_window_source_plan(stale, source_free) is None
        host._update_control(stale, response_ref=response)
        control = host._control(run.run_id, actor)
        save_working_sources(
            host.store, "synthetic-explicit-late-only", control_ref=ref(control),
            response_ref=response, selected_source_refs=[late],
        )
        selected = plan_source_turn(
            host.store, ref(control), source_input_allowance_tokens=1
        )
        assert selected["working_sources_ref"] is not None
        assert host._requested_window_source_plan(control, selected) is None
        assert early in control["body"]["source_refs"]
    finally:
        host.close()


@pytest.mark.parametrize("boundary", ["failed", "cross_scope", "revoked", "unselected"])
def test_requested_window_candidate_rejects_invalid_latest_read(
    tmp_path, monkeypatch, boundary
):
    host, run, actor, early, _late, _window, response = _two_copy_read(
        tmp_path, monkeypatch
    )
    try:
        control = host._control(run.run_id, actor)
        if boundary == "failed":
            failed, _ = _saved_tool_request(
                host, control, response_id="synthetic-failed-window-response",
                tool_id="synthetic-failed-window-tool", name="read_source_window",
                arguments={"source_id": early["id"], "source_version": early["version"]},
                result={"status": "rejected", "reason": "synthetic failure"},
            )
            host._update_control(
                control, response_ref=failed,
                tool_turn_refs=[*control["body"]["tool_turn_refs"], failed],
            )
        elif boundary == "cross_scope":
            original = host.store.get(**response)

            def save_foreign(tx):
                return ref(tx.put("model_response_receipt", "synthetic-foreign-window", {
                    **original["body"], "mission_id": "unrelated-mission",
                }))

            foreign = host.store.transact(
                "synthetic-foreign-window", {}, save_foreign,
                mission_id=control["body"]["mission_id"],
            )
            host._update_control(
                control, response_ref=foreign,
                tool_turn_refs=[*control["body"]["tool_turn_refs"], foreign],
            )
        elif boundary == "revoked":
            use_ref = host.context._source_status(host.store.get(**early))[
                "current_source_use_ref"
            ]

            def revoke(tx):
                previous = tx.get(**use_ref)
                return ref(tx.put(previous["kind"], previous["id"], {
                    **previous["body"], "allowed_uses": [],
                    "policy_status": "requires_source_policy_resolution",
                }, expected_version=previous["version"]))

            host.store.transact(
                "synthetic-requested-window-revoke", {}, revoke,
                mission_id=control["body"]["mission_id"],
            )
        else:
            host._update_control(
                control, selected_passage_refs={early["id"]: []},
            )
        control = host._control(run.run_id, actor)
        source_free = plan_source_turn(
            host.store, ref(control), source_input_allowance_tokens=1
        )
        assert host._requested_window_source_plan(control, source_free) is None
    finally:
        host.close()
