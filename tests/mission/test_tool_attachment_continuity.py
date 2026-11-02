"""Offline normal-host continuation keeps selected review without tool-menu growth."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import ora
from ora.contracts.models import LiveStartRequest
from ora.mission.tool_catalog import DEFERABLE_CORE_TOOLS
from ora.models.token_estimate import encode_context_token_ids, estimate_request_tokens
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.availability_assertions import request_first_names
from tests.mission.test_service import ROOT, answer, service, tool


def _tool_names(packet: dict) -> set[str]:
    return {entry["function"]["name"] for entry in packet["tools"]
            if entry.get("type") == "function"}


def _source() -> str:
    return ("SYNTHETIC_SOURCE_START "
            + " ".join(f"observation-{n:04d}={n * 337 % 991}" for n in range(390))
            + " SYNTHETIC_SOURCE_END")


def _assert_snapshot_runtime() -> None:
    assert Path(ora.__file__).resolve().is_relative_to(ROOT / "src")


def test_selected_review_remains_attached_after_pressure_deferral(tmp_path, monkeypatch):
    _assert_snapshot_runtime()
    def no_socket(*_args, **_kwargs):
        raise AssertionError("Offline attachment test attempted a socket")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(socket.socket, "connect_ex", no_socket)
    source = _source()
    packets: list[dict] = []
    raw_requests: list[bytes] = []

    def transport(request, *, timeout):
        raw_requests.append(request.data)
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            return tool("request_capability", {
                "tool_id": "review_peer_answer",
                "purpose": "Review a pending synthetic peer answer.",
            }, 1)
        return answer("Synthetic source review remains bounded.", len(packets))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="attachment-continuity-synthetic",
            question="Assess the synthetic observation and its limits.",
            scope="Use only the supplied synthetic source. Disclose uncertainty.",
            evidence_text=source,
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        # Keep the full synthetic source in this pressure-deferral fixture;
        # the current normal catalog moved its exact fit boundary above 8K.
        host._update_control(control, input_budget={
            "observed_limit_tokens": 8500, "target_fraction": 0.90,
        })
        for _ in range(300):
            host._drive_run(host._run(run.run_id))
            if len(packets) >= 2:
                break
        assert len(packets) >= 2
        first, second = packets[:2]
        first_names, second_names = _tool_names(first), _tool_names(second)
        assert "request_capability" in first_names
        assert "review_peer_answer" not in first_names
        assert "review_peer_answer" in second_names
        assert source in json.dumps(first["messages"])
        assert source in json.dumps(second["messages"])
        assert all(estimate_request_tokens(raw).tokens > 0 for raw in raw_requests[:2])
    finally:
        host.close()


def test_same_checkpoint_measures_carried_deferral_vs_full_menu(tmp_path, monkeypatch):
    """A real capability result feeds two identical-state preparation branches."""
    _assert_snapshot_runtime()
    def no_socket(*_args, **_kwargs):
        raise AssertionError("Offline attachment comparison attempted a socket")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(socket.socket, "connect_ex", no_socket)
    source = _source()
    packets = []
    checkpoint = tmp_path / "checkpoint-backup"

    def transport(request, *, timeout):
        packets.append(json.loads(request.data))
        assert len(packets) == 1, "Preparation checkpoint must precede next dispatch"
        return tool("request_capability", {
            "tool_id": "review_peer_answer",
            "purpose": "Review the pending synthetic peer answer.",
        }, 1)

    original = service(tmp_path / "original", monkeypatch, transport)
    try:
        run = original.start(LiveStartRequest(
            request_id="attachment-comparison-synthetic",
            question="Assess the synthetic observation and its limits.",
            scope="Use only the supplied synthetic source. Disclose uncertainty.",
            evidence_text=source,
            agent_ids=["agent-synthesis"],
        ))
        control = original._control(run.run_id, "agent-synthesis")
        # Preserve full source input before comparing the same configuration
        # across two pressure-deferral branches.
        original._update_control(control, input_budget={
            "observed_limit_tokens": 8500, "target_fraction": 0.90,
        })
        ordinary_schedule = original._schedule_model
        checkpointed = []

        def hold_selected_review(control, **kwargs):
            if (packets and not checkpointed
                    and "review_peer_answer" in control["body"].get(
                        "requested_capabilities", [])):
                # The first pressured request has been settled by the normal
                # executor. Raise only the next admission target equally in
                # both copies, so a full menu fits and isolates carry-forward.
                original._update_control(control, input_budget={
                    "observed_limit_tokens": 10000, "target_fraction": 0.90,
                })
                original.store.backup(checkpoint)
                checkpointed.append(True)
                return False
            if checkpointed:
                return False
            return ordinary_schedule(control, **kwargs)

        original._schedule_model = hold_selected_review
        for _ in range(600):
            original._drive_run(original._run(run.run_id))
            if checkpointed:
                break
            time.sleep(.005)
        assert checkpointed
        assert "review_peer_answer" not in _tool_names(packets[0])
    finally:
        original.close()

    prepared = []
    for label, carry in (("carried", True), ("baseline", False)):
        target = tmp_path / label
        StateStore.restore(checkpoint, target)
        host = service(target, monkeypatch, lambda *_args, **_kwargs: (
            no_socket()
        ))
        try:
            if not carry:
                monkeypatch.setattr(host, "_continued_core_deferral",
                                    lambda *_args: False)
            control = host._control(run.run_id, "agent-synthesis")
            assert "review_peer_answer" in control["body"]["requested_capabilities"]
            assert host._schedule_model(control)
            control = host._control(run.run_id, "agent-synthesis")
            call_ref = control["body"]["call_ref"]
            intent = host.store.get(
                "model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}",
            )
            raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
            packet = json.loads(raw)
            budget = host.store.get("mission_call_budget", call_ref["id"])["body"]
            config = host.store.get(**intent["body"]["configuration_ref"])
            requestable = host._requestable_tools(control, config["body"])
            assert {"board_read", "peer_request", "review_peer_answer",
                    "search_scholarly"} <= requestable
            assert source in json.dumps(packet["messages"])
            prepared.append({
                "label": label, "packet": packet, "raw": raw,
                "tokens": estimate_request_tokens(raw).tokens,
                "message_tokens": len(encode_context_token_ids(canonical(packet["messages"]))),
                "tool_tokens": len(encode_context_token_ids(canonical(packet["tools"]))),
                "tools": _tool_names(packet), "budget": budget,
                "source_refs": intent["body"]["source_refs"],
                "config_ref": intent["body"]["configuration_ref"],
            })
        finally:
            host.close()
    carried, baseline = prepared
    assert carried["config_ref"] == baseline["config_ref"]
    assert carried["source_refs"] == baseline["source_refs"]
    assert [message["role"] for message in carried["packet"]["messages"]] == [
        message["role"] for message in baseline["packet"]["messages"]
    ]
    for index in (0, 1, 3, 4):
        assert carried["packet"]["messages"][index] == baseline["packet"]["messages"][index]
    assert carried["packet"]["model"] == baseline["packet"]["model"]
    assert carried["packet"]["max_completion_tokens"] == baseline["packet"]["max_completion_tokens"]
    assert carried["packet"]["reasoning_effort"] == baseline["packet"]["reasoning_effort"]
    assert "review_peer_answer" in carried["tools"] & baseline["tools"]
    assert {"board_read", "peer_request", "search_scholarly"}.isdisjoint(carried["tools"])
    assert {"board_read", "peer_request", "search_scholarly"} <= baseline["tools"]
    assert {"board_read", "peer_request", "search_scholarly"} <= set(
        carried["budget"]["deferred_core_tool_ids"]
    )
    assert set(carried["budget"]["exposed_tool_ids"]) == carried["tools"]
    assert set(carried["budget"]["deferred_core_tool_ids"]) == (
        DEFERABLE_CORE_TOOLS - carried["tools"]
    )
    assert set(baseline["budget"]["exposed_tool_ids"]) == baseline["tools"]
    assert baseline["budget"]["deferred_core_tool_ids"] == []
    for branch in (carried, baseline):
        availability = json.loads(branch["packet"]["messages"][2]["content"])
        assert availability["authority"] == "trusted_host_attached_tool_availability"
        assert set(availability["call_now_local_functions"]) == branch["tools"]
        assert request_first_names(branch["packet"], availability).isdisjoint(branch["tools"])
    # Attached-tool notices and the packed context index can change with the
    # menu. Compare the complete requests and schema cost, not message equality.
    assert carried["tool_tokens"] < baseline["tool_tokens"]
    assert carried["tokens"] < baseline["tokens"]
    (tmp_path / "attachment-proof.json").write_text(json.dumps({
        "schema": "ora.private_attachment_continuity.v1",
        "provider_socket_calls": 0,
        "same_checkpoint_and_source": True,
        "selected_review_attached_both": True,
        "unrelated_core_deferred_only_in_carried": True,
        "carried_tool_count": len(carried["tools"]),
        "baseline_tool_count": len(baseline["tools"]),
        "carried_full_request_tokens": carried["tokens"],
        "baseline_full_request_tokens": baseline["tokens"],
        "carried_message_tokens": carried["message_tokens"],
        "baseline_message_tokens": baseline["message_tokens"],
        "carried_whole_tool_schema_tokens": carried["tool_tokens"],
        "baseline_whole_tool_schema_tokens": baseline["tool_tokens"],
    }, indent=2) + "\n")


def test_other_explicit_selection_and_release_keep_catalog_open(tmp_path, monkeypatch):
    _assert_snapshot_runtime()
    def no_socket(*_args, **_kwargs):
        raise AssertionError("Offline selection/release sequence attempted a socket")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(socket.socket, "connect_ex", no_socket)
    packets = []
    source = _source()

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        number = len(packets)
        names = _tool_names(packet)
        assert source in json.dumps(packet["messages"])
        if number == 1:
            assert "review_peer_answer" not in names
            return tool("request_capability", {"tool_id": "review_peer_answer"}, number)
        if number == 2:
            assert "review_peer_answer" in names and "board_read" not in names
            return tool("request_capability", {"tool_id": "board_read"}, number)
        if number == 3:
            assert {"review_peer_answer", "board_read", "release_capability"} <= names
            assert {"peer_request", "search_scholarly"}.isdisjoint(names)
            return tool("release_capability", {"tool_id": "review_peer_answer"}, number)
        assert number == 4
        assert "board_read" in names and "review_peer_answer" not in names
        assert {"peer_request", "search_scholarly"}.isdisjoint(names)
        return answer("Synthetic source remains the only evidence.", number)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="attachment-selection-release-synthetic",
            question="Assess the synthetic observation and its limits.",
            scope="Use only the supplied synthetic source. Disclose uncertainty.",
            evidence_text=source,
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        # Keep full source and the first pressure-deferral choice available
        # throughout the selected-tool release sequence.
        host._update_control(control, input_budget={
            "observed_limit_tokens": 8500, "target_fraction": 0.90,
        })
        original_schedule = host._schedule_model
        raised_target = []

        def ordinary_with_equal_later_target(control, **kwargs):
            if (packets and not raised_target
                    and "review_peer_answer" in control["body"].get(
                        "requested_capabilities", [])):
                control = host._update_control(control, input_budget={
                    "observed_limit_tokens": 10000, "target_fraction": 0.90,
                })
                raised_target.append(True)
            return original_schedule(control, **kwargs)

        host._schedule_model = ordinary_with_equal_later_target
        for _ in range(900):
            host._drive_run(host._run(run.run_id))
            if len(packets) >= 4:
                break
            time.sleep(.005)
        assert len(packets) == 4 and raised_target
        latest = host._control(run.run_id, "agent-synthesis")
        assert latest["body"]["requested_capabilities"] == ["board_read"]
        call = host.store.get(**latest["body"]["call_ref"])
        config = host.store.get("agent_configuration", call["body"]["configuration_id"], 1)
        requestable = host._requestable_tools(latest, config["body"])
        assert {"review_peer_answer", "board_read", "peer_request",
                "search_scholarly"} <= requestable
        budgets = sorted(host.store.records("mission_call_budget"),
                         key=lambda row: row["created_at"])
        assert len(budgets) == 4
        for packet, budget in zip(packets, budgets):
            exposed = set(budget["body"]["exposed_tool_ids"])
            assert exposed == _tool_names(packet)
            assert set(budget["body"]["deferred_core_tool_ids"]) == (
                DEFERABLE_CORE_TOOLS - exposed
            )
    finally:
        host.close()


def test_changed_final_source_configuration_does_not_inherit_provisional_deferral(
    tmp_path, monkeypatch,
):
    """Narrow host diagnostic: inherited carry is rechecked at final config."""
    _assert_snapshot_runtime()

    def no_socket(*_args, **_kwargs):
        raise AssertionError("Configuration diagnostic attempted a socket")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(socket.socket, "connect_ex", no_socket)
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: no_socket())
    try:
        run = host.start(LiveStartRequest(
            request_id="changed-final-config-synthetic",
            question="Assess the synthetic source.",
            scope="Use only the supplied synthetic source.",
            evidence_text="Synthetic observation with a bounded interpretation.",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        # Establish a genuine saved call/budget with explicit pressure
        # deferral, without dispatching it. This is a fixture boundary, not
        # a simulated provider response.
        assert host._schedule_model(control, force_soft_recovery=False)
        first = host._control(run.run_id, "agent-synthesis")
        prior_call = host.store.get(**first["body"]["call_ref"])
        prior_budget = host.store.get("mission_call_budget", prior_call["id"])
        assert prior_budget is not None
        # The ordinary first request might fit all tools. Reprepare this
        # fixture once with an explicit pressure flag in a separate call.
        if not prior_budget["body"].get("deferred_core_tool_ids"):
            host._update_control(first, phase="ready_model", work_id=None)
            assert host._prepare_model(host._control(run.run_id, "agent-synthesis"),
                                       defer_core_tools=True)
            first = host._control(run.run_id, "agent-synthesis")
            prior_call = host.store.get(**first["body"]["call_ref"])
            prior_budget = host.store.get("mission_call_budget", prior_call["id"])
        assert prior_budget["body"]["deferred_core_tool_ids"]
        host._update_control(first, phase="ready_model", work_id=None)
        control = host._control(run.run_id, "agent-synthesis")
        provisional = host._configuration(control, control["body"]["source_refs"])
        final = host._configuration(control, [])
        assert provisional["body"]["configuration_sha256"] != final["body"][
            "configuration_sha256"
        ]
        assert host._continued_core_deferral(control, provisional)
        assert not host._continued_core_deferral(control, final)

        import ora.mission.service as mission_service

        monkeypatch.setattr(mission_service, "plan_source_turn", lambda *_args, **_kwargs: {
            "mode": "source_free_recovery", "source_inputs": [],
            "retained_source_refs": [], "working_sources_ref": None,
            "reason": "fixture_alternate_final_source_configuration",
        })
        monkeypatch.setattr(host, "_requested_window_source_plan",
                            lambda *_args, **_kwargs: None)
        ordinary_exposed = host._exposed_tools

        class ReachedFinalConfiguration(Exception):
            pass

        def observe(explicit: bool) -> list[tuple[str, bool]]:
            seen = []

            def capture_exposed(control, config, **kwargs):
                seen.append((config["body"]["configuration_sha256"],
                             kwargs["defer_core_tools"]))
                if len(seen) == 2:
                    raise ReachedFinalConfiguration
                return ordinary_exposed(control, config, **kwargs)

            monkeypatch.setattr(host, "_exposed_tools", capture_exposed)
            try:
                host._prepare_model(control, defer_core_tools=explicit)
            except ReachedFinalConfiguration:
                pass
            else:
                raise AssertionError("Final configuration was not exercised")
            return seen

        inherited = observe(False)
        explicit = observe(True)
        assert inherited == [
            (provisional["body"]["configuration_sha256"], True),
            (final["body"]["configuration_sha256"], False),
        ]
        assert explicit == [
            (provisional["body"]["configuration_sha256"], True),
            (final["body"]["configuration_sha256"], True),
        ]
    finally:
        host.close()
