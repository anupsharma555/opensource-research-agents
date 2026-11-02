"""An exact append read can retain adjacent checked text from the same copy."""

from __future__ import annotations

import json
import math

import pytest

from ora.coordination.board import ref
from ora.evidence import read_source_window
from ora.mission.source_context import plan_source_turn
from tests.mission.test_multi_source_window_recovery import _read_second
from tests.mission.test_requested_window_recovery import _two_copy_read
from tests.mission.test_selected_window_admission import _saved_tool_request


def _adjacent_read(host, control, copy_ref, first, *, selection_mode=None):
    offset = first["returned_chars"]
    window = read_source_window(
        host.evidence, "synthetic-adjacent-append-read", copy_ref,
        block_offset=0, block_limit=1, char_offset=offset, char_limit=700,
    )
    selected = (window["passage_refs"] if selection_mode == "replace"
                else [*first["passage_refs"], *window["passage_refs"]])
    window.update(
        requested_char_limit=700, effective_char_limit=700,
        returned_chars=sum(len(item["text_preview"]) for item in window["passages"]),
        remaining_in_block_chars=next(
            (item["characters"] for item in window["omissions"]
             if item["reason"] == "remaining_characters_in_block"), 0,
        ),
        model_selection={
            "mode": selection_mode or "append", "passage_refs": selected,
            "prior_passage_refs": first["passage_refs"],
        },
    )
    window["passages"] = [
        {**item, "span_id": host.store.get(**item["passage_ref"])["body"]["span_id"]}
        for item in window["passages"]
    ]
    arguments = {
        "source_id": copy_ref["id"], "source_version": copy_ref["version"],
        "block_offset": 0, "char_offset": offset, "char_limit": 700,
    }
    if selection_mode is not None:
        arguments["selection_mode"] = selection_mode
    response, _ = _saved_tool_request(
        host, control, response_id="synthetic-adjacent-response",
        tool_id="synthetic-adjacent-tool", name="read_source_window",
        arguments=arguments, result=window,
    )
    host._update_control(
        control, response_ref=response,
        tool_turn_refs=[*control["body"]["tool_turn_refs"], response],
        read_window_refs=[*control["body"]["read_window_refs"], window["window_ref"]],
        selected_passage_refs={**control["body"]["selected_passage_refs"],
                               copy_ref["id"]: selected},
    )
    return window


def _plans(host, control):
    recovery = plan_source_turn(host.store, ref(control), source_input_allowance_tokens=1)
    latest = host._requested_window_source_plan(control, recovery)
    assert latest is not None
    return latest, host._current_copy_append_source_plan(control, latest)


def test_default_append_keeps_both_exact_adjacent_passages(tmp_path, monkeypatch):
    host, run, actor, early, _late, first, _ = _two_copy_read(
        tmp_path, monkeypatch, window_chars=1000, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        second = _adjacent_read(host, control, early, first)
        control = host._control(run.run_id, actor)
        latest, combined = _plans(host, control)
        assert latest["source_inputs"] == [
            {"copy_ref": early, "passage_refs": second["passage_refs"]}
        ]
        assert combined is not None
        assert combined["source_inputs"] == [{
            "copy_ref": early,
            "passage_refs": [*first["passage_refs"], *second["passage_refs"]],
        }]
        assert combined["mode"] == "requested_window_recovery"
        proposal = host.store.get("board_proposal", control["body"]["task_id"])
        captured = []
        preview = host.session.preview_request

        def capture(**kwargs):
            prepared = preview(**kwargs)
            captured.append(prepared)
            return prepared

        host.session.preview_request = capture
        fit = host._preview_soft_source_plan(
            control, proposal, combined, total_target=12_000,
            sequence=control["body"]["sequence"] + 1, defer_core_tools=True,
        )
        assert fit is not None
        packet = next(item.body for item in captured
                      if item.request_sha256 == fit["request_sha256"])
        source_packets = [source for message in packet["messages"]
                          if message["role"] == "user" and message["content"].startswith("{")
                          for source in json.loads(message["content"]).get("source_packets", [])]
        assert len(source_packets) == 1
        assert source_packets[0]["passage_refs"] == combined["source_inputs"][0]["passage_refs"]
        for passage_ref in source_packets[0]["passage_refs"]:
            text = host.store.get(**passage_ref)["body"]["text"]
            assert source_packets[0]["text"].count(passage_ref["id"]) == 1
            assert text in source_packets[0]["text"]
        assert control["body"]["selected_passage_refs"][early["id"]] == (
            combined["source_inputs"][0]["passage_refs"]
        )
    finally:
        host.close()


def test_explicit_replace_does_not_revive_prior_passage(tmp_path, monkeypatch):
    host, run, actor, early, _late, first, _ = _two_copy_read(
        tmp_path, monkeypatch, window_chars=1000, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        second = _adjacent_read(host, control, early, first, selection_mode="replace")
        control = host._control(run.run_id, actor)
        latest, combined = _plans(host, control)
        assert latest["source_inputs"][0]["passage_refs"] == second["passage_refs"]
        assert combined is None
        assert control["body"]["selected_passage_refs"][early["id"]] == second[
            "passage_refs"
        ]
    finally:
        host.close()


@pytest.mark.parametrize("input_cap, fraction, expected_selection", [
    (6400, .9, "both"), (6000, 1.0, "bounded"),
])
def test_normal_preparation_admits_merged_or_bounded_recovery(
    tmp_path, monkeypatch, input_cap, fraction, expected_selection,
):
    host, run, actor, early, late, first, _ = _two_copy_read(
        tmp_path, monkeypatch, window_chars=1000, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        second = _adjacent_read(host, control, early, first)
        control = host._control(run.run_id, actor)
        selected = control["body"]["selected_passage_refs"][early["id"]]
        host._update_control(control, input_budget={
            "observed_limit_tokens": input_cap, "target_fraction": fraction,
            "basis": "qualification_imposed_limit",
        })
        assert host._schedule_model(host._control(run.run_id, actor))
        prepared = host._control(run.run_id, actor)
        call = host.store.get(**prepared["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        )
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        source_packets = [source for message in packet["messages"]
                          if message["role"] == "user" and message["content"].startswith("{")
                          for source in json.loads(message["content"]).get("source_packets", [])]
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        assert budget["exact_complete_request_estimate_tokens"] <= input_cap
        if expected_selection == "both":
            assert budget["source_turn_mode"] == "requested_window_recovery"
            assert intent["body"]["source_refs"] == [early]
            assert len(source_packets) == 1
            assert source_packets[0]["passage_refs"] == selected
        else:
            # At this genuinely tight bound, local packing may admit the
            # latest window or an honest source-free inventory, depending on
            # exact synthetic reference lengths. It must not imply that both
            # checked windows were delivered.
            assert budget["source_turn_mode"] in {
                "requested_window_recovery", "source_free_recovery",
            }
            assert len(source_packets) <= 1
            if source_packets:
                assert intent["body"]["source_refs"] == [early]
                assert source_packets[0]["passage_refs"] == second["passage_refs"]
            else:
                assert intent["body"]["source_refs"] == []
        assert prepared["body"]["source_refs"] == [early, late]
        assert prepared["body"]["selected_passage_refs"][early["id"]] == selected
        working = json.loads(packet["messages"][-1]["content"])
        assert working["task"]["objective"] == "Review the supplied abstract"
        assert "read_source_window" in {item["function"]["name"] for item in packet["tools"]}
    finally:
        host.close()


def test_observed_rate_window_keeps_adjacent_checked_selection_above_soft_target(
    tmp_path, monkeypatch,
):
    # The second copy prevents the ordinary all-source packet from masking the
    # same-copy recovery path. It remains available through the source inventory.
    host, run, actor, early, late, first, _ = _two_copy_read(
        tmp_path, monkeypatch, window_chars=1500, crowded=False,
        late_text="SECOND_COPY_MARKER " + "B" * 15_000,
    )
    try:
        control = host._control(run.run_id, actor)
        second = _adjacent_read(host, control, early, first)
        control = host._control(run.run_id, actor)
        selected = [*first["passage_refs"], *second["passage_refs"]]
        assert control["body"]["selected_passage_refs"][early["id"]] == selected
        latest, combined = _plans(host, control)
        assert combined is not None
        proposal = host.store.get("board_proposal", control["body"]["task_id"])
        sequence = control["body"]["sequence"] + 1

        observed_limit = 9430
        soft_target = math.floor(observed_limit * .70)
        prior_completion = 6
        headroom = math.ceil(observed_limit * .10)
        safe_hard_target = observed_limit - prior_completion - headroom
        def preview(plan, target):
            return host._preview_soft_source_plan(
                control, proposal, plan, total_target=target,
                sequence=sequence, defer_core_tools=True,
            )
        latest_soft = preview(latest, soft_target)
        assert latest_soft is not None
        assert preview(combined, soft_target) is None
        combined_hard = preview(combined, safe_hard_target)
        assert combined_hard is not None
        assert combined_hard["estimated_request_tokens"] <= safe_hard_target

        host._update_control(control, input_budget={
            "observed_limit_tokens": observed_limit, "target_fraction": .70,
            "basis": "same_local_store_selected_model_observed_provider_token_limit",
        })
        dispatches_before = len(host.store.records("model_dispatch_attempt"))
        assert host._schedule_model(host._control(run.run_id, actor))
        prepared = host._control(run.run_id, actor)
        call = host.store.get(**prepared["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        )
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        source_packets = [source for message in packet["messages"]
                          if message["role"] == "user" and message["content"].startswith("{")
                          for source in json.loads(message["content"]).get("source_packets", [])]
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        assert budget["source_turn_mode"] == "requested_window_recovery"
        assert budget["preview_admission_target_tokens"] == safe_hard_target
        assert budget["prior_completion_reservation"] == prior_completion
        assert budget["rate_input_headroom_tokens"] == headroom
        assert budget["exact_complete_request_estimate_tokens"] <= safe_hard_target
        assert (budget["exact_complete_request_estimate_tokens"]
                + prior_completion + headroom <= observed_limit)
        assert intent["body"]["source_refs"] == [early]
        assert len(source_packets) == 1
        assert source_packets[0]["passage_refs"] == selected
        for passage_ref in selected:
            assert host.store.get(**passage_ref)["body"]["text"] in source_packets[0]["text"]
        assert prepared["body"]["source_refs"] == [early, late]
        assert prepared["body"]["selected_passage_refs"][early["id"]] == selected
        assert len(host.store.records("model_dispatch_attempt")) == dispatches_before
    finally:
        host.close()


def test_prior_older_copy_enrichment_keeps_existing_priority(tmp_path, monkeypatch):
    host, run, actor, early, late, first, _ = _two_copy_read(
        tmp_path, monkeypatch, window_chars=1000, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        older, _ = _read_second(host, control, late)
        control = host._control(run.run_id, actor)
        current = _adjacent_read(host, control, early, first)
        control = host._control(run.run_id, actor)
        latest, same = _plans(host, control)
        recovery = plan_source_turn(host.store, ref(control), source_input_allowance_tokens=1)
        older_plan = host._older_selected_window_source_plan(control, recovery, latest)
        assert same is not None and older_plan is not None
        host._update_control(control, input_budget={
            "observed_limit_tokens": 6700, "target_fraction": 1.0,
            "basis": "qualification_imposed_limit",
        })
        assert host._schedule_model(host._control(run.run_id, actor))
        prepared = host._control(run.run_id, actor)
        call = host.store.get(**prepared["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        )
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        source_packets = [source for message in packet["messages"]
                          if message["role"] == "user" and message["content"].startswith("{")
                          for source in json.loads(message["content"]).get("source_packets", [])]
        assert [item["id"] for item in source_packets] == [early["id"], late["id"]]
        assert source_packets[0]["passage_refs"] == current["passage_refs"]
        assert source_packets[1]["passage_refs"] == older["passage_refs"]
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        assert budget["source_turn_mode"] == "requested_window_recovery"
        assert budget["exact_complete_request_estimate_tokens"] <= 6700
    finally:
        host.close()


@pytest.mark.parametrize("boundary", ["revoked", "wrong_version"])
def test_append_candidate_requires_current_permitted_copy(tmp_path, monkeypatch, boundary):
    host, run, actor, early, late, first, _ = _two_copy_read(
        tmp_path, monkeypatch, window_chars=1000, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        _adjacent_read(host, control, early, first)
        control = host._control(run.run_id, actor)
        latest, combined = _plans(host, control)
        assert combined is not None
        if boundary == "revoked":
            use_ref = host.context._source_status(host.store.get(**early))[
                "current_source_use_ref"
            ]

            def revoke(tx):
                prior = tx.get(**use_ref)
                return ref(tx.put(prior["kind"], prior["id"], {
                    **prior["body"], "allowed_uses": [],
                    "policy_status": "requires_source_policy_resolution",
                }, expected_version=prior["version"]))

            host.store.transact("synthetic-append-revoke", {}, revoke,
                                mission_id=control["body"]["mission_id"])
        else:
            control = {**control, "body": {**control["body"], "source_refs": [
                {**early, "version": early["version"] + 1}, late,
            ]}}
        assert host._current_copy_append_source_plan(control, latest) is None
    finally:
        host.close()
