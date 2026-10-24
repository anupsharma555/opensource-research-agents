"""Prepared peer turns name registered targets without attaching every tool."""

from __future__ import annotations

import copy
import importlib
import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.mission.context import ContextError, WorkingContext
from ora.models.token_estimate import estimate_request_tokens
from ora.state.store import canonical
from tests.mission.test_service import service
from tests.mission.test_tool_result_paging import save_result


def _prepared(host, run_id: str, alias: str) -> tuple[dict, dict, dict]:
    control = host._control(run_id, alias)
    assert host._schedule_model(control)
    control = host._control(run_id, alias)
    call = host.store.get(**control["body"]["call_ref"])
    intent = host.store.get(
        "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
    )
    packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
    availability = next(
        json.loads(message["content"])
        for message in packet["messages"]
        if message.get("role") == "user"
        and "trusted_host_attached_tool_availability" in message.get("content", "")
    )
    return packet, availability, call


def _orientation_tokens(packet: dict) -> int:
    without = copy.deepcopy(packet)
    for message in without["messages"]:
        if message.get("role") != "user":
            continue
        try:
            content = json.loads(message["content"])
        except (TypeError, ValueError):
            continue
        if isinstance(content, dict) and "peer_routes" in content:
            content.pop("peer_routes")
            message["content"] = canonical(content)
            break
    return (
        estimate_request_tokens(canonical(packet).encode()).tokens
        - estimate_request_tokens(canonical(without).encode()).tokens
    )


def test_actual_peer_request_names_registered_alias_and_deferred_invitation(
    tmp_path, monkeypatch,
) -> None:
    def no_transport(*_args, **_kwargs):
        raise AssertionError("Preparation must not dispatch a provider request")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="peer-tool-orientation", question="Compare a synthetic claim",
            evidence_text="A short synthetic source for both peers.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        foreign = host.start(LiveStartRequest(
            request_id="foreign-peer-tool-orientation", question="Separate run",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        packet, availability, call = _prepared(host, run.run_id, "agent-critique")
        sage_packet, _sage_availability, _sage_call = _prepared(
            host, run.run_id, "agent-synthesis",
        )
        for prepared in (packet, sage_packet):
            instructions = prepared["messages"][0]["content"]
            assert "Build a provisional answer from permitted evidence when feasible" in instructions
            assert "Ask focused questions early for genuine gaps or later validation" in instructions
            assert "Address existing incoming requests before asking for another independent check" in instructions
            assert "answer when able, even while your own check is pending" in instructions
            assert "read the saved question if only refs appear" in instructions.casefold()
            assert "For replies to your requests, if report text is absent" in instructions
            assert "use the supplied read_request with read_context_record directly" in instructions
            assert "request_capability for it only when absent" in instructions
            assert "use review_peer_answer to record whether it answers your question" in instructions
            assert "publication is not a review" in instructions
            assert "Check pending asks/answers before duplicating" in instructions
            functions = {item["function"]["name"]: item["function"]
                         for item in prepared["tools"]}
            assert {"peer_request", "publish_result"} <= functions.keys()
            assert "answer_request_id" in functions["publish_result"]["parameters"]["properties"]
        attached = {item["function"]["name"] for item in packet["tools"]}
        assert attached == set(call["body"]["exposed_tools"])
        assert {"peer_request", "read_source_window", "search_saved_passages"} <= attached
        assert "read_context_record" in attached
        assert "read_context_record" in availability["call_now_local_functions"]
        assert "read_context_record" not in availability["request_first_local_functions"]
        assert "invite_peer" not in attached
        assert "invite_peer" in availability["request_first_local_functions"]
        assert availability["peer_routes"] == {
            "registered": ["agent-synthesis"],
            "ask_registered": "peer_request",
            "invite_absent": "request_capability(tool_id=invite_peer)",
        }
        assert host._control(foreign.run_id, "agent-synthesis")["body"][
            "actor_id"
        ] not in canonical(availability["peer_routes"])
        assert 0 < _orientation_tokens(packet) <= 101
        deferred_call = {**call, "body": {**call["body"], "exposed_tools": [
            name for name in call["body"]["exposed_tools"] if name != "peer_request"
        ]}}
        assert host._tool_availability(
            host._control(run.run_id, "agent-critique"), deferred_call, [],
        )["peer_routes"]["ask_registered"] == "request_capability(tool_id=peer_request)"
        deferred_reader = {**call, "body": {**call["body"], "exposed_tools": [
            name for name in call["body"]["exposed_tools"]
            if name != "read_context_record"
        ]}}
        deferred_availability = host._tool_availability(
            host._control(run.run_id, "agent-critique"), deferred_reader, [],
        )
        assert "read_context_record" not in deferred_availability["call_now_local_functions"]
        assert "read_context_record" in deferred_availability["request_first_local_functions"]
    finally:
        host.close()


def test_saved_old_configuration_and_request_survive_new_instruction_revision(
    tmp_path, monkeypatch,
) -> None:
    def no_transport(*_args, **_kwargs):
        raise AssertionError("Preparation must not dispatch")

    mission_service = importlib.import_module("ora.mission.service")
    current_guidance = mission_service.CALLABILITY_GUIDANCE
    monkeypatch.setattr(mission_service, "CALLABILITY_GUIDANCE", "Historical callability guidance.")
    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="pinned-peer-orientation", question="Assess a synthetic claim",
            evidence_text="A synthetic source for a scoped test.",
            agent_ids=["agent-synthesis"],
        ))
        packet, _availability, call = _prepared(host, run.run_id, "agent-synthesis")
        old = host.store.get("agent_configuration", call["body"]["configuration_id"], 1)
        old_instructions = old["body"]["instructions"]
        old_intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
        )
        old_request_sha = old_intent["body"]["request_sha256"]
        assert packet["messages"][0]["content"] == old_instructions
        assert "Historical callability guidance." in old_instructions

        monkeypatch.setattr(mission_service, "CALLABILITY_GUIDANCE", current_guidance)
        refreshed = host._configuration(host._control(run.run_id, "agent-synthesis"))
        assert refreshed["id"] != old["id"]
        assert refreshed["body"]["previous_configuration_id"] == old["id"]
        assert refreshed["body"]["settings"] == old["body"]["settings"]
        assert refreshed["body"]["reasoning_settings"] == old["body"]["reasoning_settings"]
        assert "use the supplied read_request with read_context_record directly" in (
            refreshed["body"]["instructions"]
        )
        assert host.store.get("agent_configuration", old["id"], 1)["body"][
            "instructions"
        ] == old_instructions
        assert host.store.get("model_request_intent", old_intent["id"], 1)["body"][
            "request_sha256"
        ] == old_request_sha
    finally:
        host.close()


def test_single_peer_and_older_control_keep_invitation_request_first(
    tmp_path, monkeypatch,
) -> None:
    def no_transport(*_args, **_kwargs):
        raise AssertionError("Preparation must not dispatch")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="single-peer-orientation", question="Review a synthetic item",
            agent_ids=["agent-synthesis"],
        ))
        packet, availability, call = _prepared(host, run.run_id, "agent-synthesis")
        assert availability["peer_routes"] == {
            "registered": [],
            "invite_absent": "request_capability(tool_id=invite_peer)",
        }
        assert "invite_peer" not in {
            item["function"]["name"] for item in packet["tools"]
        }
        control = host._control(run.run_id, "agent-synthesis")
        older = {**control, "body": {
            key: value for key, value in control["body"].items()
            if key not in {"control_type", "requested_capabilities"}
        }}
        assert host._tool_availability(older, call, [])["peer_routes"] == availability[
            "peer_routes"
        ]
        legacy = host.start(LiveStartRequest(
            request_id="legacy-peer-orientation", question="Older saved policy",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        old_run = host._run(legacy.run_id)
        host._save("mission_run", old_run["id"], {
            **old_run["body"], "participation_policy_version": 1,
        }, mission_id=old_run["body"]["mission_id"])
        _old_packet, old_notice, _old_call = _prepared(
            host, legacy.run_id, "agent-synthesis",
        )
        assert "peer_routes" not in old_notice
        native_run = host.start(LiveStartRequest(
            request_id="native-peer-orientation", question="Native turn",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        native_control = host._control(native_run.run_id, "agent-synthesis")
        host._update_control(native_control, requested_capabilities=["browser_search"])
        native_packet, native_notice, _native_call = _prepared(
            host, native_run.run_id, "agent-synthesis",
        )
        assert [item["type"] for item in native_packet["tools"]] == ["browser_search"]
        assert "peer_routes" not in native_notice
    finally:
        host.close()


def test_roster_deduplicates_tasks_and_uses_ids_only_for_ambiguous_aliases(
    tmp_path, monkeypatch,
) -> None:
    def no_transport(*_args, **_kwargs):
        raise AssertionError("Preparation must not dispatch")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="duplicate-peer-orientation", question="Review an item",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        _packet, _availability, call = _prepared(host, run.run_id, "agent-critique")
        critic = host._control(run.run_id, "agent-critique")
        sage = host._control(run.run_id, "agent-synthesis")
        mission_id = critic["body"]["mission_id"]
        host._save("mission_task_control", "second-task-same-actor", {
            **sage["body"], "task_id": "second-task-same-actor",
        }, mission_id=mission_id)
        assert host._tool_availability(critic, call, [])["peer_routes"]["registered"] == [
            "agent-synthesis"
        ]
        other = host.registry.register(
            "another-synthesis-instance", mission_id=mission_id, run_id=run.run_id,
            role_id=host.role_choices["agent-synthesis"][1], display_name="Another Sage",
        )
        host._save("mission_task_control", "same-alias-different-actor", {
            **sage["body"], "actor_id": other["id"],
            "task_id": "same-alias-different-actor",
        }, mission_id=mission_id)
        routes = host._tool_availability(critic, call, [])["peer_routes"]
        assert routes["registered"] == []
        assert routes["registered_by_id"] == [{
            "alias": "agent-synthesis", "target_instance_id": min(
                sage["body"]["actor_id"], other["id"],
            ),
        }]
        assert routes["additional_registered"] == 1
        assert routes["read_more"] == "board_read"
    finally:
        host.close()


def test_larger_registered_roster_stays_within_small_orientation_budget(
    tmp_path, monkeypatch,
) -> None:
    def no_transport(*_args, **_kwargs):
        raise AssertionError("Preparation must not dispatch")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        aliases = list(host.role_choices)[:9]
        assert len(aliases) == 9
        run = host.start(LiveStartRequest(
            request_id="larger-peer-orientation", question="Compare synthetic methods",
            agent_ids=aliases,
        ))
        packet, availability, _call = _prepared(host, run.run_id, aliases[0])
        assert len(availability["peer_routes"]["registered"]) < 8
        assert availability["peer_routes"]["additional_registered"] > 0
        assert availability["peer_routes"]["read_more"] == "board_read"
        assert 0 < _orientation_tokens(packet) <= 101
    finally:
        host.close()


def test_alias_colliding_with_another_peer_display_name_uses_exact_id(
    tmp_path, monkeypatch,
) -> None:
    def no_transport(*_args, **_kwargs):
        raise AssertionError("Preparation must not dispatch")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="peer-display-collision", question="Review a claim",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        critic = host._control(run.run_id, "agent-critique")
        sage = host._control(run.run_id, "agent-synthesis")
        mission_id = critic["body"]["mission_id"]
        other = host.registry.register(
            "display-name-collision", mission_id=mission_id, run_id=run.run_id,
            role_id=host.role_choices["agent-critique"][1],
            display_name="agent-synthesis",
        )
        host._save("mission_task_control", "display-name-collision-control", {
            **critic["body"], "actor_id": other["id"],
            "task_id": "display-name-collision-task",
        }, mission_id=mission_id)
        packet, availability, _call = _prepared(host, run.run_id, "agent-critique")
        routes = availability["peer_routes"]
        assert routes["registered"] == ["agent-critique"]
        assert routes["registered_by_id"] == [{
            "alias": "agent-synthesis",
            "target_instance_id": sage["body"]["actor_id"],
        }]
        assert "invite_peer" not in {item["function"]["name"] for item in packet["tools"]}
    finally:
        host.close()


def test_omitted_registered_peer_is_recovered_from_paged_board_result(
    tmp_path, monkeypatch,
) -> None:
    def no_transport(*_args, **_kwargs):
        raise AssertionError("Preparation must not dispatch")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        aliases = list(host.role_choices)[:9]
        run = host.start(LiveStartRequest(
            request_id="paged-peer-roster", question="Compare synthetic methods",
            agent_ids=aliases,
        ))
        owner = host._control(run.run_id, aliases[0])
        mission_id = owner["body"]["mission_id"]
        for index in range(10):
            actor = host.registry.register(
                f"extra-peer-{index}", mission_id=mission_id, run_id=run.run_id,
                role_id=host.role_choices[aliases[1]][1],
                display_name=f"Additional Reviewer {index}",
            )
            template = host._control(run.run_id, aliases[1])
            host._save("mission_task_control", f"extra-peer-control-{index}", {
                **template["body"], "actor_id": actor["id"],
                "task_id": f"extra-peer-task-{index}",
            }, mission_id=mission_id)
        packet, availability, call = _prepared(host, run.run_id, aliases[0])
        routes = availability["peer_routes"]
        assert routes["additional_registered"] > 0
        assert routes["read_more"] == "board_read"
        assert 0 < _orientation_tokens(packet) <= 101
        unavailable_board = {**call, "body": {**call["body"],
            "exposed_tools": [name for name in call["body"]["exposed_tools"]
                              if name != "board_read"],
            "permitted_tools": {name: descriptor
                                for name, descriptor in call["body"]["permitted_tools"].items()
                                if name != "board_read"},
        }}
        assert host._tool_availability(owner, unavailable_board, [])["peer_routes"][
            "read_more"
        ] == "unavailable"
        peers = host._registered_peer_targets(owner)
        omitted_id = peers[-1]["target_instance_id"]
        assert omitted_id not in canonical(routes)
        _response, result = save_result(host.store, {
            "kind": "agent_call", "id": call["id"], "version": call["version"],
        }, "board_read", {
            "open_work": [], "optional_work": [],
            "registered_peers": [
                {key: item[key] for key in ("alias", "target_instance_id")}
                for item in peers
            ],
        }, suffix="peer-roster")
        context = WorkingContext(host.store)
        request = {
            "mission_id": mission_id, "actor_id": owner["body"]["actor_id"],
            "task_id": owner["body"]["task_id"], "result_id": result["id"],
            "offset": 0, "limit": 200, "projection_sha256": "",
        }
        pages = []
        while True:
            page = context.read_tool_result(**request)
            pages.append(page["text"])
            if page["next_request"] is None:
                break
            request = {**request, **{
                key: value for key, value in page["next_request"].items()
                if key != "tool"
            }}
        assert len(pages) > 1
        recovered = json.loads("".join(pages))
        assert omitted_id in {
            item["target_instance_id"] for item in recovered["registered_peers"]
        }
        foreign = host.start(LiveStartRequest(
            request_id="foreign-paged-roster", question="Unrelated work",
            agent_ids=["agent-synthesis"],
        ))
        foreign_id = host._control(foreign.run_id, "agent-synthesis")["body"]["actor_id"]
        _response, unsafe = save_result(host.store, {
            "kind": "agent_call", "id": call["id"], "version": call["version"],
        }, "board_read", {"open_work": [], "optional_work": [], "registered_peers": [
            {"alias": "agent-synthesis", "target_instance_id": foreign_id},
        ]}, suffix="foreign-peer-roster")
        with pytest.raises(ContextError, match="outside this run"):
            context.read_tool_result(
                mission_id=mission_id, actor_id=owner["body"]["actor_id"],
                task_id=owner["body"]["task_id"], result_id=unsafe["id"],
                offset=0, limit=200, projection_sha256="",
            )
    finally:
        host.close()
