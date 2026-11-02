"""An exact requested read can survive a crowded two-copy continuation."""

from __future__ import annotations

import json
import math
import socket

import pytest

from ora.coordination.board import ref
from ora.evidence import read_source_window
from ora.mission.source_context import plan_source_turn, save_working_sources
from tests.mission.availability_assertions import catalog_tool_names, request_first_names
from tests.mission.test_selected_window_admission import (
    _ready_selected_window,
    _saved_tool_request,
)
from tests.mission.test_service import MODEL, service


def _deny_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Offline source-flow test attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


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


def _saved_search_after_read(host, control, *, suffix="first"):
    response, _ = _saved_tool_request(
        host, control, response_id=f"synthetic-saved-search-{suffix}",
        tool_id=f"synthetic-saved-search-tool-{suffix}",
        name="search_saved_passages", arguments={"query": "synthetic endpoint"},
        result={"result_count": 0, "results": []},
    )
    host._update_control(
        control, response_ref=response,
        tool_turn_refs=[*control["body"]["tool_turn_refs"], response],
    )
    return response


def _saved_capability_after_read(host, control, *, status="selected_for_next_turn",
                                 selected=True):
    response, _ = _saved_tool_request(
        host, control, response_id="synthetic-capability-after-read",
        tool_id="synthetic-capability-tool", name="request_capability",
        arguments={"tool_id": "peer_request"},
        result={"status": status, "tool_id": "peer_request"},
    )
    host._update_control(
        control, response_ref=response,
        tool_turn_refs=[*control["body"]["tool_turn_refs"], response],
        requested_capabilities=["peer_request"] if selected else [],
    )
    return response


def _saved_read_only_after_read(host, control, *, name="discover_capabilities",
                                proposed_refs=None, result=None):
    if name == "discover_capabilities":
        arguments = {}
        if result is None:
            result = {"granted": {}, "exposed": [], "requestable": []}
    else:
        arguments = {
            "expected_version": 1,
            "source_refs": [
                {"source_id": item["id"], "source_version": item["version"]}
                for item in (proposed_refs or control["body"]["source_refs"])
            ],
        }
        if result is None:
            result = {
                "status": "rejected", "field": "expected_version",
                "condition": "working-source selection changed",
                "external_action": "none", "working_sources_expected_version": 0,
                "working_sources_ref": None, "selected_source_refs": [],
            }
    response, _ = _saved_tool_request(
        host, control, response_id=f"synthetic-read-only-{name}-{control['version']}",
        tool_id=f"synthetic-read-only-tool-{control['version']}",
        name=name, arguments=arguments, result=result,
    )
    host._update_control(
        control, response_ref=response,
        tool_turn_refs=[*control["body"]["tool_turn_refs"], response],
    )
    return response


@pytest.mark.parametrize("bridge", [
    "saved_search", "selected_capability", "discover_capabilities",
    "rejected_selection",
])
def test_saved_search_keeps_latest_checked_window_for_next_packet(
    tmp_path, monkeypatch, bridge,
):
    host, run, actor, early, late, window, read_response = _two_copy_read(
        tmp_path, monkeypatch, window_chars=1800, crowded=False,
        late_text="SECOND_COPY_MARKER " + " ".join(
            f"Participant {number:04d} had endpoint {number * 7919 % 100000:05d};"
            for number in range(200)
        ),
    )
    try:
        control = host._control(run.run_id, actor)
        bridge_response = (
            _saved_search_after_read(host, control)
            if bridge == "saved_search" else
            _saved_capability_after_read(host, control)
            if bridge == "selected_capability" else
            _saved_read_only_after_read(
                host, control,
                name=("discover_capabilities" if bridge == "discover_capabilities"
                      else "select_working_sources"),
            )
        )
        control = host._control(run.run_id, actor)
        proposal = host.store.get("board_proposal", control["body"]["task_id"])
        source_free = plan_source_turn(
            host.store, ref(control), source_input_allowance_tokens=1,
        )
        candidate = host._requested_window_source_plan(control, source_free)
        assert candidate is not None
        assert candidate["source_inputs"] == [
            {"copy_ref": early, "passage_refs": window["passage_refs"]}
        ]
        assert control["body"]["source_refs"] == [early, late]
        assert control["body"]["selected_passage_refs"][early["id"]] == window["passage_refs"]

        selected_limit = None
        for observed_limit in range(7000, 12001, 500):
            target = observed_limit - math.ceil(observed_limit * .10) - 6
            all_sources = plan_source_turn(
                host.store, ref(control), source_input_allowance_tokens=target,
            )
            all_fit = (
                host._preview_soft_source_plan(
                    control, proposal, all_sources, total_target=target,
                    sequence=control["body"]["sequence"] + 1, defer_core_tools=True,
                ) if all_sources["mode"] != "source_free_recovery" else None
            )
            latest_fit = host._preview_soft_source_plan(
                control, proposal, candidate, total_target=target,
                sequence=control["body"]["sequence"] + 1, defer_core_tools=True,
            )
            if latest_fit and not all_fit:
                selected_limit = observed_limit
                break
        assert selected_limit is not None
        host._update_control(control, input_budget={
            "observed_limit_tokens": selected_limit, "target_fraction": 1.0,
        })
        assert host._schedule_model(host._control(run.run_id, actor))
        prepared = host._control(run.run_id, actor)
        call = host.store.get(**prepared["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        )
        request = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        packets = [packet for message in request["messages"] if message["role"] == "user"
                   for packet in json.loads(message["content"]).get("source_packets", [])]
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        assert budget["source_turn_mode"] == "requested_window_recovery"
        assert intent["body"]["source_refs"] == [early]
        assert prepared["body"]["source_refs"] == [early, late]
        assert len(packets) == 1 and packets[0]["passage_refs"] == window["passage_refs"]
        checked_text = host.store.get(**window["passage_refs"][0])["body"]["text"]
        assert packets[0]["text"].count(checked_text) == 1
        assert "SELECTED_MARKER" in packets[0]["text"]
        assert "SECOND_COPY_MARKER" not in json.dumps(request["messages"])
        assert budget["exact_complete_request_estimate_tokens"] + 6 <= selected_limit
        assert bridge_response in (
            budget["compacted_tool_turn_refs"]
            + ([budget["current_tool_turn_ref"]] if budget["current_tool_turn_ref"] else [])
        )
        assert read_response in prepared["body"]["tool_turn_refs"]
        if bridge == "selected_capability":
            assert "peer_request" in {item["function"]["name"] for item in request["tools"]}
    finally:
        host.close()


@pytest.mark.parametrize("boundary", [
    "two_searches", "inventory", "selection", "inspection", "newer_bad_read",
    "foreign_search", "stale_response", "unselected", "version_changed", "revoked",
    "rejected_capability", "unselected_capability", "working_choice",
    "fetch_source", "browser_search", "unknown_tool",
])
def test_saved_search_continuity_respects_source_boundaries(
    tmp_path, monkeypatch, boundary,
):
    host, run, actor, early, late, window, _ = _two_copy_read(
        tmp_path, monkeypatch, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        search_response = _saved_search_after_read(host, control)
        control = host._control(run.run_id, actor)
        if boundary == "two_searches":
            _saved_search_after_read(host, control, suffix="second")
        elif boundary in {"rejected_capability", "unselected_capability"}:
            _saved_capability_after_read(
                host, control,
                status=("rejected" if boundary == "rejected_capability"
                        else "selected_for_next_turn"),
                selected=False,
            )
        elif boundary in {
            "inventory", "selection", "inspection", "newer_bad_read",
            "fetch_source", "browser_search", "unknown_tool",
        }:
            name = {
                "inventory": "read_source_inventory",
                "selection": "select_working_sources",
                "inspection": "inspect_hit",
                "newer_bad_read": "read_source_window",
                "fetch_source": "fetch_source",
                "browser_search": "browser_search",
                "unknown_tool": "synthetic_unknown_tool",
            }[boundary]
            response, _ = _saved_tool_request(
                host, control, response_id=f"synthetic-{boundary}-after-search",
                tool_id=f"synthetic-{boundary}-tool", name=name, arguments={},
                result={"status": "rejected"},
            )
            host._update_control(
                control, response_ref=response,
                tool_turn_refs=[*control["body"]["tool_turn_refs"], response],
            )
        elif boundary == "foreign_search":
            original = host.store.get(**search_response)

            def save_foreign(tx):
                return ref(tx.put("model_response_receipt", "synthetic-foreign-search", {
                    **original["body"], "task_id": "unrelated-task",
                }))

            foreign = host.store.transact(
                "synthetic-foreign-search", {}, save_foreign,
                mission_id=control["body"]["mission_id"],
            )
            host._update_control(
                control, response_ref=foreign,
                tool_turn_refs=[*control["body"]["tool_turn_refs"], foreign],
            )
        elif boundary == "stale_response":
            host._update_control(control, response_ref=None)
        elif boundary == "working_choice":
            save_working_sources(
                host.store, "synthetic-search-working-choice",
                control_ref=ref(control), response_ref=search_response,
                selected_source_refs=[late],
            )
        elif boundary == "unselected":
            host._update_control(control, selected_passage_refs={early["id"]: []})
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
                "synthetic-search-continuity-revoke", {}, revoke,
                mission_id=control["body"]["mission_id"],
            )
        control = host._control(run.run_id, actor)
        source_free = plan_source_turn(
            host.store, ref(control), source_input_allowance_tokens=1,
        )
        if boundary == "version_changed":
            control = {**control, "body": {**control["body"], "source_refs": [
                {**early, "version": early["version"] + 1}, late,
            ]}}
        candidate = host._requested_window_source_plan(control, source_free)
        if boundary == "two_searches":
            assert candidate is not None
            assert candidate["source_inputs"] == [
                {"copy_ref": early, "passage_refs": window["passage_refs"]}
            ]
        else:
            assert candidate is None
    finally:
        host.close()


@pytest.mark.parametrize("boundary", [
    "different_choice", "accepted_choice", "stale_feedback",
    "malformed_discovery", "revoked_source", "unselected_passage",
])
def test_read_only_bridge_keeps_only_current_checked_source(
    tmp_path, monkeypatch, boundary,
):
    host, run, actor, early, late, _window, _ = _two_copy_read(
        tmp_path, monkeypatch, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        if boundary == "malformed_discovery":
            _saved_read_only_after_read(
                host, control, result={"status": "unknown"},
            )
        else:
            result = None
            if boundary == "accepted_choice":
                result = {"status": "working_sources_selected",
                          "working_sources_ref": {"kind": "mission_working_sources",
                                                  "id": "synthetic", "version": 1}}
            elif boundary == "stale_feedback":
                result = {"status": "rejected", "field": "expected_version",
                          "condition": "working-source selection changed",
                          "external_action": "none",
                          "working_sources_expected_version": 1,
                          "working_sources_ref": None, "selected_source_refs": []}
            _saved_read_only_after_read(
                host, control, name="select_working_sources",
                proposed_refs=[late] if boundary == "different_choice" else None,
                result=result,
            )
        control = host._control(run.run_id, actor)
        if boundary == "revoked_source":
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
                "synthetic-read-only-bridge-revoke", {}, revoke,
                mission_id=control["body"]["mission_id"],
            )
        elif boundary == "unselected_passage":
            host._update_control(control, selected_passage_refs={early["id"]: []})
            control = host._control(run.run_id, actor)
        source_free = plan_source_turn(
            host.store, ref(control), source_input_allowance_tokens=1,
        )
        assert host._requested_window_source_plan(control, source_free) is None
    finally:
        host.close()


def test_checked_window_and_peer_route_survive_read_only_turns_and_reopen(
    tmp_path, monkeypatch,
):
    _deny_network(monkeypatch)

    def no_provider(_request, *, timeout):
        raise AssertionError("Preparation must not call a provider")

    host, run, actor, early, late, window, _ = _two_copy_read(
        tmp_path, monkeypatch, window_chars=1800, crowded=True,
        late_text="SECOND_COPY_MARKER " + " ".join(
            f"Participant {number:04d} had endpoint {number * 7919 % 100000:05d};"
            for number in range(200)
        ),
    )
    reopened = None
    try:
        control = host._control(run.run_id, actor)
        _saved_read_only_after_read(host, control)
        control = host._control(run.run_id, actor)
        rejected = _saved_read_only_after_read(
            host, control, name="select_working_sources",
        )
        control = host._control(run.run_id, actor)
        proposal = host.store.get("board_proposal", control["body"]["task_id"])
        source_free = plan_source_turn(
            host.store, ref(control), source_input_allowance_tokens=1,
        )
        candidate = host._requested_window_source_plan(control, source_free)
        assert candidate is not None
        selected_limit = None
        for observed_limit in range(7000, 14001, 500):
            target = observed_limit - math.ceil(observed_limit * .10) - 6
            all_sources = plan_source_turn(
                host.store, ref(control), source_input_allowance_tokens=target,
            )
            all_fit = (
                host._preview_soft_source_plan(
                    control, proposal, all_sources, total_target=target,
                    sequence=control["body"]["sequence"] + 1,
                    defer_core_tools=True,
                ) if all_sources["mode"] != "source_free_recovery" else None
            )
            one_fit = host._preview_soft_source_plan(
                control, proposal, candidate, total_target=target,
                sequence=control["body"]["sequence"] + 1,
                defer_core_tools=True,
            )
            if one_fit and not all_fit:
                selected_limit = observed_limit
                break
        assert selected_limit is not None
        host._update_control(control, input_budget={
            "observed_limit_tokens": selected_limit, "target_fraction": 1.0,
        })
        host.close()
        reopened = service(tmp_path, monkeypatch, no_provider)
        control = reopened._control(run.run_id, actor)
        assert rejected in control["body"]["tool_turn_refs"]
        assert reopened._schedule_model(control)
        prepared = reopened._control(run.run_id, actor)
        call = reopened.store.get(**prepared["body"]["call_ref"])
        intent = reopened.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        )
        request = json.loads(reopened.store.read_blob(intent["body"]["blob"]["sha256"]))
        packets = [packet for message in request["messages"] if message["role"] == "user"
                   for packet in json.loads(message["content"]).get("source_packets", [])]
        assert len(packets) == 1
        assert intent["body"]["source_refs"] == [early]
        assert packets[0]["passage_refs"] == window["passage_refs"]
        checked = reopened.store.get(**window["passage_refs"][0])["body"]["text"]
        assert packets[0]["text"].count(checked) == 1
        assert "SECOND_COPY_MARKER" not in json.dumps(request["messages"])
        attached = {item["function"]["name"] for item in request["tools"]}
        assert {"request_capability", "read_source_window"} <= attached
        availability = next(
            json.loads(message["content"]) for message in request["messages"]
            if message["role"] == "user"
            and json.loads(message["content"]).get("authority")
            == "trusted_host_attached_tool_availability"
        )
        assert availability["peer_routes"]["registered"] == ["agent-critique"]
        assert ("peer_request" in attached
                or availability["peer_routes"]["ask_registered"]
                == "request_capability(tool_id=peer_request)")
        budget = reopened.store.get("mission_call_budget", call["id"])["body"]
        assert budget["source_turn_mode"] == "requested_window_recovery"
        assert budget["exact_complete_request_estimate_tokens"] + 6 <= selected_limit
    finally:
        if reopened is not None:
            reopened.close()
        else:
            host.close()


@pytest.mark.parametrize("boundary", ["source_set_changed", "source_use_revoked"])
def test_reopen_does_not_carry_a_removed_or_denied_checked_window(
    tmp_path, monkeypatch, boundary,
):
    _deny_network(monkeypatch)

    def no_provider(_request, *, timeout):
        raise AssertionError("Preparation must not call a provider")

    host, run, actor, early, late, window, _ = _two_copy_read(
        tmp_path, monkeypatch, crowded=False,
    )
    reopened = None
    try:
        control = host._control(run.run_id, actor)
        _saved_read_only_after_read(host, control)
        control = host._control(run.run_id, actor)
        _saved_read_only_after_read(host, control, name="select_working_sources")
        host.close()
        reopened = service(tmp_path, monkeypatch, no_provider)
        control = reopened._control(run.run_id, actor)
        if boundary == "source_set_changed":
            reopened._update_control(control, source_refs=[late])
            control = reopened._control(run.run_id, actor)
        else:
            use_ref = reopened.context._source_status(reopened.store.get(**early))[
                "current_source_use_ref"
            ]

            def revoke(tx):
                previous = tx.get(**use_ref)
                return ref(tx.put(previous["kind"], previous["id"], {
                    **previous["body"], "allowed_uses": [],
                    "policy_status": "requires_source_policy_resolution",
                }, expected_version=previous["version"]))

            reopened.store.transact(
                "synthetic-reopened-source-revoke", {}, revoke,
                mission_id=control["body"]["mission_id"],
            )
        source_free = plan_source_turn(
            reopened.store, ref(control), source_input_allowance_tokens=1,
        )
        assert reopened._requested_window_source_plan(control, source_free) is None
        assert reopened._schedule_model(control)
        prepared = reopened._control(run.run_id, actor)
        call = reopened.store.get(**prepared["body"]["call_ref"])
        intent = reopened.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        )
        request = json.loads(reopened.store.read_blob(intent["body"]["blob"]["sha256"]))
        packets = [packet for message in request["messages"] if message["role"] == "user"
                   for packet in json.loads(message["content"]).get("source_packets", [])]
        messages = json.dumps(request["messages"])
        checked = reopened.store.get(**window["passage_refs"][0])["body"]["text"]
        assert all(packet["id"] != early["id"] for packet in packets)
        assert early not in intent["body"]["source_refs"]
        assert window["passage_refs"][0] not in [
            passage for packet in packets for passage in packet["passage_refs"]
        ]
        assert checked not in messages
        assert "SELECTED_MARKER" not in messages
    finally:
        if reopened is not None:
            reopened.close()
        else:
            host.close()


def test_newer_checked_read_takes_priority_over_pre_search_window(tmp_path, monkeypatch):
    host, run, actor, early, late, old_window, _ = _two_copy_read(
        tmp_path, monkeypatch, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        _saved_search_after_read(host, control)
        control = host._control(run.run_id, actor)
        new_window = read_source_window(
            host.evidence, "synthetic-newer-requested-read", late,
            block_offset=0, block_limit=1, char_offset=0, char_limit=900,
        )
        new_window.update(
            requested_char_limit=900, effective_char_limit=900,
            returned_chars=sum(len(item["text_preview"]) for item in new_window["passages"]),
            remaining_in_block_chars=next(
                (item["characters"] for item in new_window["omissions"]
                 if item["reason"] == "remaining_characters_in_block"), 0,
            ),
            next_request={"source_id": late["id"], "source_version": late["version"],
                          **new_window["next_cursor"], "char_limit": 900,
                          "selection_mode": "replace"},
            model_selection={
                "mode": "replace", "passage_refs": new_window["passage_refs"],
                "prior_passage_refs": [],
            },
        )
        new_window["passages"] = [
            {**item, "span_id": host.store.get(**item["passage_ref"])["body"]["span_id"]}
            for item in new_window["passages"]
        ]
        response, _ = _saved_tool_request(
            host, control, response_id="synthetic-newer-window-response",
            tool_id="synthetic-newer-window-tool", name="read_source_window",
            arguments={"source_id": late["id"], "source_version": late["version"],
                       "char_offset": 0, "char_limit": 900, "selection_mode": "replace"},
            result=new_window,
        )
        host._update_control(
            control, response_ref=response,
            tool_turn_refs=[*control["body"]["tool_turn_refs"], response],
            read_window_refs=[*control["body"]["read_window_refs"], new_window["window_ref"]],
            selected_passage_refs={**control["body"]["selected_passage_refs"],
                                   late["id"]: new_window["passage_refs"]},
        )
        control = host._control(run.run_id, actor)
        source_free = plan_source_turn(
            host.store, ref(control), source_input_allowance_tokens=1,
        )
        candidate = host._requested_window_source_plan(control, source_free)
        assert candidate is not None
        assert candidate["source_inputs"] == [
            {"copy_ref": late, "passage_refs": new_window["passage_refs"]}
        ]
        assert candidate["source_inputs"][0]["passage_refs"] != old_window["passage_refs"]
        assert early in control["body"]["source_refs"]
    finally:
        host.close()


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
            catalog_tool_names(visible_catalog)
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
        attached = {
            item["function"]["name"] for item in request["tools"]
        }
        assert {"read_source_window", "request_capability"} <= attached
        availability = json.loads(request["messages"][2]["content"])
        assert availability["authority"] == "trusted_host_attached_tool_availability"
        if "read_context_record" in attached:
            assert "read_context_record" in availability["call_now_local_functions"]
        else:
            assert "read_context_record" in request_first_names(request, availability)
            assert "read_context_record" in budget["deferred_core_tool_ids"]
        assert {"read_source_inventory", "select_working_sources", "read_context_record"} <= set(
            configuration["body"]["permitted_tools"]
        )
        assert "request_capability" in call["body"]["exposed_tools"]
    finally:
        host.close()


def test_append_overlap_delivers_only_latest_checked_window_under_tight_limit(
    tmp_path, monkeypatch,
):
    late_text = "SECOND_COPY_MARKER " + " ".join(
        f"Participant {number:04d} had endpoint {number * 7919 % 100000:05d};"
        for number in range(200)
    )
    host, run, actor, early, late, _early_window, _response = _two_copy_read(
        tmp_path, monkeypatch, crowded=True, completion_tokens=254,
        late_text=late_text,
    )
    try:
        control = host._control(run.run_id, actor)
        copy = host.store.get(**late)
        prior = [ref(host.store.get("source_passage", passage_id))
                 for passage_id in copy["body"]["passage_ids"]]
        assert len(prior) == 1
        window = read_source_window(
            host.evidence, "synthetic-overlapping-appended-read", late,
            block_offset=0, block_limit=1, char_offset=0, char_limit=1900,
        )
        window.update(
            requested_char_limit=1900, effective_char_limit=1900,
            returned_chars=sum(len(item["text_preview"]) for item in window["passages"]),
            remaining_in_block_chars=next(
                (item["characters"] for item in window["omissions"]
                 if item["reason"] == "remaining_characters_in_block"), 0,
            ),
            next_request={"source_id": late["id"], "source_version": late["version"],
                          **window["next_cursor"], "char_limit": 1900},
            model_selection={
                "mode": "append", "prior_passage_refs": prior,
                "passage_refs": [*prior, *window["passage_refs"]],
            },
        )
        window["passages"] = [
            {**item, "span_id": host.store.get(**item["passage_ref"])["body"]["span_id"]}
            for item in window["passages"]
        ]
        response, _ = _saved_tool_request(
            host, control, response_id="synthetic-overlapping-window-response",
            tool_id="synthetic-overlapping-window-tool", name="read_source_window",
            arguments={"source_id": late["id"], "source_version": late["version"],
                       "block_offset": 0, "char_offset": 0, "char_limit": 1900},
            result=window, completion_tokens=254,
        )
        host._update_control(
            control, response_ref=response,
            tool_turn_refs=[*control["body"]["tool_turn_refs"], response],
            read_window_refs=[*control["body"]["read_window_refs"], window["window_ref"]],
            selected_passage_refs={**control["body"]["selected_passage_refs"],
                                   late["id"]: [*prior, *window["passage_refs"]]},
        )
        control = host._control(run.run_id, actor)
        proposal = host.store.get("board_proposal", control["body"]["task_id"])
        source_free = plan_source_turn(
            host.store, ref(control), source_input_allowance_tokens=1,
        )
        requested = host._requested_window_source_plan(control, source_free)
        assert requested is not None
        assert requested["source_inputs"] == [
            {"copy_ref": late, "passage_refs": window["passage_refs"]}
        ]
        assert control["body"]["selected_passage_refs"][late["id"]] == [
            *prior, *window["passage_refs"]
        ]
        old_candidate = {**requested, "source_inputs": [
            {"copy_ref": late, "passage_refs": [*prior, *window["passage_refs"]]}
        ]}
        selected_limit = None
        for observed_limit in range(7000, 12001, 500):
            target = observed_limit - math.ceil(observed_limit * .10) - 254
            latest_fit = host._preview_soft_source_plan(
                control, proposal, requested, total_target=target,
                sequence=control["body"]["sequence"] + 1, defer_core_tools=True,
            )
            old_fit = host._preview_soft_source_plan(
                control, proposal, old_candidate, total_target=target,
                sequence=control["body"]["sequence"] + 1, defer_core_tools=True,
            )
            if latest_fit and not old_fit and (
                latest_fit["compact_selected_window"] or latest_fit["archive_selected_window"]
            ):
                selected_limit = observed_limit
                break
        assert selected_limit is not None, "Overlap must separate exact-window from appended-text fit"
        host._update_control(control, input_budget={
            "observed_limit_tokens": selected_limit, "target_fraction": 1.0,
        })
        assert host._schedule_model(host._control(run.run_id, actor))
        prepared = host._control(run.run_id, actor)
        call = host.store.get(**prepared["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        )
        request = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        packets = [packet for message in request["messages"] if message["role"] == "user"
                   for packet in json.loads(message["content"]).get("source_packets", [])]
        checked_text = host.store.get(**window["passage_refs"][0])["body"]["text"]
        # The host may also retain the separately checked early copy when its
        # deduplicated packet fits. The overlapping late submission itself
        # must still be represented only by the latest checked window.
        assert intent["body"]["source_refs"] in ([late], [late, early])
        assert prepared["body"]["source_refs"] == [early, late]
        assert [packet["id"] for packet in packets] == [
            pointer["id"] for pointer in intent["body"]["source_refs"]
        ]
        late_packet = packets[0]
        assert late_packet["passage_refs"] == window["passage_refs"]
        assert late_packet["text"].count(checked_text) == 1
        assert late_packet["text"].endswith(checked_text)
        if len(packets) == 2:
            assert packets[1]["passage_refs"] == control["body"]["selected_passage_refs"][early["id"]]
            assert checked_text not in packets[1]["text"]
        index = json.loads(request["messages"][-1]["content"])
        scope = next(item["selected_input_scope"] for item in index["source_retrieval"]
                     if item["ref"] == late)
        assert scope["status"] == "incomplete_extracted_text"
        assert scope["covered_chars"] == window["returned_chars"] == 1900
        assert scope["first_omitted_cursor"] == window["next_cursor"]
        assert scope["saved_read_status"] == "complete_extracted_text"
        assert intent["body"]["source_decisions"][0]["passage_refs"] == window["passage_refs"]
        assert host.store.get("mission_call_budget", call["id"])["body"][
            "source_turn_mode"] == "requested_window_recovery"
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
