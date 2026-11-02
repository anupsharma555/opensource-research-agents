"""Compact model tool status preserves exact requestability and peer work."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from ora.agents.capabilities import content_hash
from ora.contracts.models import LiveStartRequest
from ora.mission.service import CALLABILITY_GUIDANCE, UNHANDLED_COMMON_TOOLS
from ora.mission.tool_attachment_policy import compact_model_tool_availability
from tests.mission.test_capability_catalog import _index
from tests.mission.test_service import service


def _prepared(host, run_id: str, alias: str) -> tuple[dict, dict, dict, dict]:
    control = host._control(run_id, alias)
    assert host._schedule_model(control)
    prepared = host._control(run_id, alias)
    call = host.store.get(**prepared["body"]["call_ref"])
    intent = host.store.get(
        "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
    )
    packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
    notice = next(
        json.loads(message["content"])
        for message in packet["messages"]
        if message.get("role") == "user"
        and "trusted_host_attached_tool_availability" in message.get("content", "")
    )
    return packet, notice, prepared, call


def _reconstructed(notice: dict, catalog: dict) -> set[str]:
    return (
        set(catalog.get("tool_ids", catalog.get("tools", {})))
        - set(notice["call_now_local_functions"])
        - set(notice["not_ready_local_functions"])
        - set(notice["request_first_native_tools"])
        - set(notice.get("unavailable_now", []))
        - {"browser_search"}
    )


def test_compact_notice_keeps_catalog_schemas_and_exact_deferred_membership(
    tmp_path, monkeypatch,
) -> None:
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Preparation must not call a provider")))
    try:
        run = host.start(LiveStartRequest(
            request_id="compact-availability-two-peers",
            question="Compare these synthetic sources and ask a peer one useful question.",
            evidence_text="DOI: 10.1234/synthetic. Synthetic checked passage.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        packet, notice, control, call = _prepared(host, run.run_id, "agent-synthesis")
        raw = host._tool_availability(control, call, [])
        catalog = _index(packet["messages"][0]["content"])
        attached = {item["function"]["name"] for item in packet["tools"]
                    if item.get("type") == "function"}
        assert catalog["schema"] == "ora.role_capability_catalog.purposes.v2"
        assert list(catalog["tools"]) == sorted(catalog["tools"])
        assert all(isinstance(purpose, str) and purpose.strip()
                   for purpose in catalog["tools"].values())
        assert set(catalog["tools"]) == (
            set(call["body"]["permitted_tools"]) - UNHANDLED_COMMON_TOOLS
        )
        assert "tool_ids" not in catalog
        assert attached == set(call["body"]["exposed_tools"])
        assert attached == set(notice["call_now_local_functions"])
        assert "request_first_local_functions" not in notice
        assert _reconstructed(notice, catalog) == set(raw["request_first_local_functions"])
        assert notice["unavailable_now"] == ["release_capability"]
        for field in ("not_ready_local_functions", "request_first_native_tools",
                      "native_browser_now", "peer_routes", "requested_but_not_attached"):
            assert notice[field] == raw[field]
        assert "discover_capabilities is optional" in packet["messages"][0]["content"]
        assert "discover_capabilities is optional" in notice["next_action"]
        assert "request_first_native_tools use a native provider turn" in notice[
            "request_first_rule"
        ]

        config = host.store.get("agent_configuration", call["body"]["configuration_id"], 1)
        inputs = {
            "call": call, "configuration": config,
            "unhandled_tools": UNHANDLED_COMMON_TOOLS,
            "current_guidance": CALLABILITY_GUIDANCE,
        }
        assert compact_model_tool_availability(raw, **inputs) == notice
        unexpected_gap = deepcopy(raw)
        unexpected_gap["request_first_local_functions"].remove("invite_peer")
        assert compact_model_tool_availability(unexpected_gap, **inputs) == unexpected_gap
        for marker in ("Historical callability guidance.", "Broken capability catalog: "):
            old = deepcopy(config)
            old["body"]["instructions"] = old["body"]["instructions"].replace(
                CALLABILITY_GUIDANCE if marker.startswith("Historical")
                else "Role capability catalog: ", marker,
            )
            old["body"]["instructions_sha256"] = hashlib.sha256(
                old["body"]["instructions"].encode()
            ).hexdigest()
            old["body"]["configuration_sha256"] = content_hash({
                key: value for key, value in old["body"].items()
                if key != "configuration_sha256"
            })
            old_call = deepcopy(call)
            old_call["body"]["configuration_sha256"] = old["body"]["configuration_sha256"]
            old_call["body"]["instructions_sha256"] = old["body"]["instructions_sha256"]
            assert compact_model_tool_availability(raw, **{
                **inputs, "call": old_call, "configuration": old,
            }) == raw
    finally:
        host.close()


def test_selected_capability_and_fresh_incoming_question_keep_current_state(
    tmp_path, monkeypatch,
) -> None:
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Preparation must not call a provider")))
    try:
        run = host.start(LiveStartRequest(
            request_id="compact-availability-peer-question",
            question="Review a synthetic comparison with another peer.",
            evidence_text="DOI: 10.1234/peer. Synthetic checked passage.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        sender = host._control(run.run_id, "agent-synthesis")
        recipient = host._control(run.run_id, "agent-critique")
        question = "Which exact synthetic passage supports the comparison?"
        sent = host.mailbox.send_request(
            "compact-peer-send", mission_id=sender["body"]["mission_id"],
            request_id="compact-peer-question", message_id="compact-peer-message",
            requester_id=sender["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"], question=question,
            expected_artifact="Source-backed assessment",
            cause_ref=sender["body"]["proposal_ref"],
            task_ref=sender["body"]["proposal_ref"],
            payload={"question": question},
        )
        assert sent["status"] == "accepted"
        assert host.mailbox.mark_delivered(
            "compact-peer-deliver", mission_id=sender["body"]["mission_id"],
            message_id="compact-peer-message",
        )["status"] == "accepted"
        host._update_control(recipient, requested_capabilities=["write_note"])
        packet, notice, control, call = _prepared(host, run.run_id, "agent-critique")
        catalog = _index(packet["messages"][0]["content"])
        raw = host._tool_availability(control, call, [])
        assert _reconstructed(notice, catalog) == set(raw["request_first_local_functions"])
        assert "write_note" in notice["call_now_local_functions"]
        assert "release_capability" in notice["call_now_local_functions"]
        assert "release_capability" not in notice.get("unavailable_now", [])
        current = next(json.loads(message["content"])
                       for message in packet["messages"] if message.get("role") == "user"
                       and '"incoming_request"' in message.get("content", ""))
        assert current["incoming_request"]["question"] == question
        assert current["incoming_request"]["owner"] == "peer"
        assert current["incoming_request"]["recipient_id"] == recipient["body"]["actor_id"]
    finally:
        host.close()


def test_native_and_legacy_or_invalid_catalog_keep_explicit_notice(
    tmp_path, monkeypatch,
) -> None:
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Preparation must not call a provider")))
    try:
        run = host.start(LiveStartRequest(
            request_id="compact-availability-fallback",
            question="Check synthetic capabilities.",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        host._update_control(control, requested_capabilities=["browser_search"])
        _packet, native, control, call = _prepared(host, run.run_id, "agent-synthesis")
        assert native["native_browser_now"] is True
        assert native["request_first_local_functions"] == []
        assert "request_first_rule" not in native

        raw = host._tool_availability(control, call, [])
        config = host.store.get("agent_configuration", call["body"]["configuration_id"], 1)
        inputs = {
            "call": call, "configuration": config,
            "unhandled_tools": UNHANDLED_COMMON_TOOLS,
            "current_guidance": CALLABILITY_GUIDANCE,
        }
        assert compact_model_tool_availability(raw, **inputs) == raw
    finally:
        host.close()
