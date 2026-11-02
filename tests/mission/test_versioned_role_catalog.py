"""New-run role catalog IDs preserve grants, attached schemas and old pins."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from ora.agents.capabilities import content_hash
from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.mission.service import CALLABILITY_GUIDANCE, UNHANDLED_COMMON_TOOLS
from ora.mission.skill_activation import catalog_prompt
from ora.mission.tool_attachment_policy import compact_model_tool_availability
from ora.mission.tool_catalog import PARALLEL_SEARCH_PURPOSE, TOOL_PURPOSES
from ora.models.profiles import QWEN_MODEL
from ora.state import StateError
from ora.state.store import canonical
from tests.mission.availability_assertions import request_first_names, saved_discovery_result
from tests.mission.test_capability_catalog import _index
from tests.mission.test_service import answer, drain, service, tool


def _prepared(host, run_id: str, alias: str) -> tuple[dict, dict, dict, dict]:
    control = host._control(run_id, alias)
    assert host._schedule_model(control)
    prepared = host._control(run_id, alias)
    call = host.store.get(**prepared["body"]["call_ref"])
    intent = host.store.get(
        "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
    )
    packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
    config = host.store.get("agent_configuration", call["body"]["configuration_id"], 1)
    return packet, control, call, config


def _notice(packet: dict) -> dict:
    return next(json.loads(message["content"]) for message in packet["messages"]
                if message.get("role") == "user"
                and '"authority":"trusted_host_attached_tool_availability"'
                in message.get("content", ""))


def _pin_older_projection(host, run_id: str, version: int | None) -> None:
    saved = host._run(run_id)

    def update(tx):
        current = tx.get(**ref(saved))
        body = {**current["body"]}
        if version is None:
            body.pop("tool_catalog_projection_version", None)
        else:
            body["tool_catalog_projection_version"] = version
        return ref(tx.put("mission_run", current["id"], body,
                          expected_version=current["version"]))

    host.store.transact(f"catalog-projection-{run_id}-{version}", {}, update,
                        mission_id=saved["body"]["mission_id"])


def test_new_run_catalog_purposes_match_exact_grants_and_attached_schemas(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Offline preparation cannot dispatch")))
    try:
        run = host.start(LiveStartRequest(
            request_id="catalog-purposes-current", question="Review a synthetic source.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        assert host._run(run.run_id)["body"]["tool_catalog_projection_version"] == 2
        packet, control, call, config = _prepared(host, run.run_id, "agent-synthesis")
        catalog = _index(packet["messages"][0]["content"])
        assert set(catalog) == {"schema", "skills", "tools"}
        assert catalog["schema"] == "ora.role_capability_catalog.purposes.v2"
        assert list(catalog["tools"]) == sorted(catalog["tools"])
        assert set(catalog["tools"]) == (
            set(call["body"]["permitted_tools"]) - UNHANDLED_COMMON_TOOLS
        )
        assert all(isinstance(purpose, str) and purpose.strip()
                   for purpose in catalog["tools"].values())
        assert catalog["tools"] == {
            name: (PARALLEL_SEARCH_PURPOSE if name == "search_web"
                   else TOOL_PURPOSES[name])
            for name in sorted(catalog["tools"])
        }
        assert {name: catalog["tools"][name] for name in (
            "peer_request", "peer_turn", "publish_result", "read_source_window",
            "request_capability",
        )} == {name: TOOL_PURPOSES[name] for name in (
            "peer_request", "peer_turn", "publish_result", "read_source_window",
            "request_capability",
        )}
        assert catalog["skills"]["evidence-linking"]["state"] == "active"
        assert catalog["skills"]["task-planning"]["state"] == "available_to_activate"
        attached = {item["function"]["name"]: item["function"]
                    for item in packet["tools"] if item.get("type") == "function"}
        assert set(attached) == set(call["body"]["exposed_tools"])
        assert all(item["description"].strip() for item in attached.values())
        assert all(config["body"]["permitted_tools"][name] == descriptor
                   for name, descriptor in call["body"]["permitted_tools"].items())
        notice = _notice(packet)
        assert request_first_names(packet, notice) == set(host._tool_availability(
            control, call, [],
        )["request_first_local_functions"])
        assert "discover_capabilities is optional" in packet["messages"][0]["content"]
        later = host._control(run.run_id, "agent-synthesis")
        host._update_control(later, requested_capabilities=["peer_request"])
        selected = host._control(run.run_id, "agent-synthesis")
        selected_config = host._configuration(selected)
        selected_catalog = _index(selected_config["body"]["instructions"])
        assert selected_catalog == catalog
        assert "peer_request" in selected_catalog["tools"]
    finally:
        host.close()


def test_invalid_id_catalog_falls_back_to_explicit_deferred_notice(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Offline preparation cannot dispatch")))
    try:
        run = host.start(LiveStartRequest(
            request_id="catalog-ids-invalid", question="Check synthetic capabilities.",
            agent_ids=["agent-synthesis"],
        ))
        _pin_older_projection(host, run.run_id, 1)
        packet, control, call, config = _prepared(host, run.run_id, "agent-synthesis")
        raw = host._tool_availability(control, call, [])
        marker = "Role capability catalog: "
        instructions = config["body"]["instructions"]
        start = instructions.index(marker) + len(marker)
        original, length = json.JSONDecoder().raw_decode(instructions[start:])
        for altered in (
            {**original, "tool_ids": original["tool_ids"][:-1]},
            {**original, "tool_ids": [*original["tool_ids"], original["tool_ids"][-1]]},
            {**original, "tool_ids": sorted([*original["tool_ids"], "unknown_tool"])},
            {**original, "extra": "unvalidated"},
            {**original, "schema": "ora.role_capability_catalog.ids.v2"},
        ):
            changed = deepcopy(config)
            changed_text = (instructions[:start] + json.dumps(altered, separators=(",", ":"))
                            + instructions[start + length:])
            changed["body"]["instructions"] = changed_text
            changed["body"]["instructions_sha256"] = hashlib.sha256(
                changed_text.encode()
            ).hexdigest()
            changed["body"]["configuration_sha256"] = content_hash({
                key: value for key, value in changed["body"].items()
                if key != "configuration_sha256"
            })
            changed_call = deepcopy(call)
            changed_call["body"]["configuration_sha256"] = changed["body"][
                "configuration_sha256"
            ]
            changed_call["body"]["instructions_sha256"] = changed["body"][
                "instructions_sha256"
            ]
            assert compact_model_tool_availability(
                raw, call=changed_call, configuration=changed,
                unhandled_tools=UNHANDLED_COMMON_TOOLS,
                current_guidance=CALLABILITY_GUIDANCE,
            ) == raw
        assert _index(packet["messages"][0]["content"]) == original
    finally:
        host.close()


def test_saved_v1_catalog_and_request_replay_unchanged(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Offline replay cannot dispatch")))
    try:
        run = host.start(LiveStartRequest(
            request_id="catalog-saved-v1", question="Review a synthetic source.",
            agent_ids=["agent-synthesis"],
        ))
        _pin_older_projection(host, run.run_id, 1)
        packet, _control, call, config = _prepared(host, run.run_id, "agent-synthesis")
        catalog = _index(packet["messages"][0]["content"])
        assert catalog["schema"] == "ora.role_capability_catalog.ids.v1"
        assert set(catalog) == {"schema", "skills", "tool_ids"}
        assert "tools" not in catalog
        assert "discover_capabilities supplies deferred tool purposes" in config["body"][
            "instructions"
        ]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
        )
        exact = host.store.read_blob(intent["body"]["blob"]["sha256"])
        assert hashlib.sha256(exact).hexdigest() == intent["body"]["request_sha256"]
        assert host.session.load(ref(intent), for_dispatch=True).request.request_sha256 == (
            intent["body"]["request_sha256"]
        )
    finally:
        host.close()


def test_unknown_catalog_projection_and_empty_v2_purpose_fail_closed():
    with pytest.raises(StateError, match="Unsupported role capability catalog projection"):
        catalog_prompt({}, [], {"peer_request": "Ask a registered peer"},
                       lifecycle=False, projection_version=3)
    with pytest.raises(StateError, match="Purpose catalog needs"):
        catalog_prompt({}, [], {"peer_request": " "},
                       lifecycle=False, projection_version=2)


def test_unknown_saved_catalog_projection_does_not_prepare(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Unknown catalog cannot dispatch")))
    try:
        run = host.start(LiveStartRequest(
            request_id="catalog-unknown-version", question="Review a synthetic source.",
            agent_ids=["agent-synthesis"],
        ))
        _pin_older_projection(host, run.run_id, 99)
        with pytest.raises(StateError, match="Unsupported role capability catalog projection"):
            host._configuration(host._control(run.run_id, "agent-synthesis"))
        assert not host.store.records("model_request_intent")
    finally:
        host.close()


def test_missing_flag_keeps_full_legacy_catalog_and_saved_request(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Offline preparation cannot dispatch")))
    try:
        run = host.start(LiveStartRequest(
            request_id="catalog-ids-legacy", question="Review a synthetic source.",
            agent_ids=["agent-synthesis"],
        ))
        _pin_older_projection(host, run.run_id, None)
        packet, _control, call, _config = _prepared(host, run.run_id, "agent-synthesis")
        catalog = _index(packet["messages"][0]["content"])
        assert "schema" not in catalog and set(catalog) == {"skills", "tools"}
        assert set(catalog["tools"]) == (
            set(call["body"]["permitted_tools"]) - UNHANDLED_COMMON_TOOLS
        )
        assert all(value.strip() for value in catalog["tools"].values())
        assert "Applicable numerical tools and their purposes are listed" in packet[
            "messages"
        ][0]["content"]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
        )
        exact = host.store.read_blob(intent["body"]["blob"]["sha256"])
        assert hashlib.sha256(exact).hexdigest() == intent["body"]["request_sha256"]
        assert host.session.load(ref(intent), for_dispatch=True).request.request_sha256 == (
            intent["body"]["request_sha256"]
        )
        assert canonical(json.loads(exact)) == exact.decode()
    finally:
        host.close()


def test_deferred_purposes_remain_available_on_demand(tmp_path, monkeypatch):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        assert _index(packet["messages"][0]["content"])["schema"] == (
            "ora.role_capability_catalog.ids.v1"
        )
        if len(packets) == 1:
            return tool("discover_capabilities", {}, 1)
        first = next(json.loads(message["content"]) for message in packet["messages"]
                     if message.get("role") == "tool"
                     and message.get("name") == "discover_capabilities")
        assert first["next_request"] is not None
        result = saved_discovery_result(
            host, host._control(run.run_id, "agent-synthesis"), first,
        )
        assert {"peer_turn", "peer_request", "calculate", "read_source_window"} <= set(
            result["granted"]
        )
        assert all(result["granted"][name].strip() for name in (
            "peer_turn", "peer_request", "calculate", "read_source_window"
        ))
        assert "request_capability" in result["exposed"]
        return answer("The saved task needs source evidence.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="catalog-deferred-discovery", question="Check a synthetic comparison.",
            agent_ids=["agent-synthesis"],
        ))
        _pin_older_projection(host, run.run_id, 1)
        drain(host, run.run_id)
        assert len(packets) == 2
    finally:
        host.close()


def test_followup_pins_new_catalog_profile(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: answer(
        "Synthetic source-free conclusion.", 1))
    try:
        parent = host.start(LiveStartRequest(
            request_id="catalog-followup-parent", question="Synthetic parent question",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, parent.run_id)
        snapshot = host.state(parent.run_id).snapshot
        target = snapshot.artifacts[0]
        child = host.followup(LiveFollowupRequest(
            request_id="catalog-followup-child", parent_run_id=parent.run_id,
            parent_snapshot_id=snapshot.snapshot_id, target_id=target.id,
            target_version=target.version, question="Synthetic follow-up question",
            agent_ids=["agent-synthesis"],
        ))
        assert host._run(child.run_id)["body"]["tool_catalog_projection_version"] == 2
        packet, _control, _call, _config = _prepared(host, child.run_id,
                                                     "agent-synthesis")
        assert _index(packet["messages"][0]["content"])["schema"] == (
            "ora.role_capability_catalog.purposes.v2"
        )
    finally:
        host.close()


def test_qwen_small_source_packet_keeps_v2_purposes_and_exact_text(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Offline preparation cannot dispatch")))
    try:
        source_text = "Synthetic trial: 40 adults; one outcome at week eight. " * 8
        run = host.start(LiveStartRequest(
            request_id="catalog-qwen-small-source",
            question="Compare the synthetic trial with a peer's methods assessment.",
            evidence_text=source_text, agent_ids=["agent-synthesis", "agent-critique"],
            agent_models={"agent-synthesis": {
                "model": QWEN_MODEL, "reasoning_effort": "low",
                "max_completion_tokens": 960,
            }},
        ))
        packet, _control, call, config = _prepared(host, run.run_id, "agent-synthesis")
        assert packet["model"] == QWEN_MODEL
        assert packet["reasoning_effort"] == "low"
        catalog = _index(packet["messages"][0]["content"])
        assert catalog["schema"] == "ora.role_capability_catalog.purposes.v2"
        assert set(catalog["tools"]) == (
            set(call["body"]["permitted_tools"]) - UNHANDLED_COMMON_TOOLS
        )
        assert catalog["tools"]["peer_request"] == TOOL_PURPOSES["peer_request"]
        assert packet["messages"][0]["content"] == config["body"]["instructions"]
        packets = [source for message in packet["messages"] if message["role"] == "user"
                   and message["content"].startswith("{")
                   for source in json.loads(message["content"]).get("source_packets", [])]
        assert len(packets) == 1
        assert source_text in packets[0]["text"]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
        )
        assert intent["body"]["request_token_estimate"]["basis"] == (
            "ora.qwen38_27b_official_chat_template_low_tokenizers.v1"
        )
        assert host.session.load(ref(intent), for_dispatch=True).request.request_sha256 == (
            intent["body"]["request_sha256"]
        )
        assert not host.store.records("model_dispatch_attempt")
    finally:
        host.close()
