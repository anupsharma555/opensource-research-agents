"""Inline public tool-turn prose keeps every producing source through dispatch."""

from __future__ import annotations

import json

import pytest

from ora.coordination.board import ref
from ora.mission.tool_catalog import TOOL_PURPOSES, UNHANDLED_COMMON_TOOLS
from ora.models import SessionError
from ora.state.store import canonical
from tests.mission.test_archived_public_tool_text import _saved_archived_turn
from tests.mission.test_note_source_use import revoke
from tests.mission.test_source_rich_preparation import _prepared, _three_source_host


@pytest.mark.parametrize("next_limit,compacted", [(7600, True), (8000, False)])
def test_inline_public_assessment_extra_source_revocation_blocks_dispatch(
    tmp_path, monkeypatch, next_limit, compacted,
):
    host, run, sources, _texts = _three_source_host(
        tmp_path, monkeypatch, items=8, limit=8000,
    )
    try:
        control, call, intent, packet, _budget = _prepared(
            host, run.run_id, "agent-critique",
        )
        assert intent["body"]["source_refs"] == sources
        catalog, _ = json.JSONDecoder().raw_decode(packet["messages"][0][
            "content"].split("Role capability catalog: ", 1)[1])
        assert catalog["schema"] == "ora.role_capability_catalog.purposes.v2"
        availability = host._tool_availability(control, call, [])
        discovered = {
            "granted": {name: catalog["tools"].get(
                name, TOOL_PURPOSES.get(name, "Saved capability"))
                        for name in sorted(call["body"]["permitted_tools"])},
            "exposed": sorted(call["body"]["exposed_tools"]),
            "not_handled_here": sorted(
                UNHANDLED_COMMON_TOOLS & call["body"]["permitted_tools"].keys()),
            "requestable": availability["request_first_local_functions"],
            "not_ready": availability["not_ready_local_functions"],
            "requestable_native_tools": availability["request_first_native_tools"],
            "next_action": "Use the current exact catalog and callability.",
        }
        public = "Synthetic public methods observation from three source copies."
        raw_response = host.store.put_blob(canonical({
            "content": public,
            "tool_calls": [{"id": "public-discovery-call", "type": "function",
                            "function": {"name": "discover_capabilities",
                                         "arguments": "{}"}}],
        }).encode())

        def save_response(tx):
            return ref(tx.put("model_response_receipt", "public-discovery-response", {
                "mission_id": control["body"]["mission_id"],
                "task_id": control["body"]["task_id"],
                "call_ref": ref(call), "intent_ref": ref(intent),
                "request_sha256": intent["body"]["request_sha256"],
                "status": "tool_requested", "http_status": 200,
                "error_kind": None, "blob": raw_response,
                "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                          "total_tokens": 120},
            }))

        response = host.store.transact(
            "public-discovery-response", {}, save_response,
            mission_id=control["body"]["mission_id"],
        )
        host.context.save_tool_result(
            "public-discovery-result", response_ref=response,
            tool_call_id="public-discovery-call", name="discover_capabilities",
            content=canonical(discovered),
        )
        # Only the first copy remains a current model source. The public
        # observation still depends on all three copies from its prior input.
        host._update_control(
            control, phase="ready_model", response_ref=response,
            tool_turn_refs=[response], source_refs=[sources[0]],
            input_budget={"basis": "qualification_imposed_limit",
                          "observed_limit_tokens": next_limit, "target_fraction": 1.0},
        )
        assert host._schedule_model(host._control(run.run_id, "agent-critique"))
        prepared = host._control(run.run_id, "agent-critique")
        next_call = host.store.get(**prepared["body"]["call_ref"])
        next_intent = host.store.get(
            "model_request_intent", f"model-intent:{next_call['id']}:{next_call['version']}",
        )
        next_packet = json.loads(host.store.read_blob(next_intent["body"]["blob"]["sha256"]))
        assert next_intent["body"]["source_refs"] == [sources[0]]
        notices = [json.loads(message["content"])
                   for message in next_packet["messages"]
                   if message["role"] == "user" and
                   '"authority":"saved_host_capability_result_not_source_instruction"'
                   in message["content"]]
        assert bool(notices) is compacted
        if compacted:
            assert notices[0]["public_assistant_texts"] == [{
                "response_ref": response,
                "origin": "prior_model_public_assistant_content",
                "status": "inline", "text": public,
            }]
            assert not any(message["role"] == "assistant"
                           and message.get("content") == public
                           for message in next_packet["messages"])
        else:
            assert any(message["role"] == "assistant"
                       and message.get("content") == public
                       for message in next_packet["messages"])
        assert {canonical(item) for item in next_intent["body"][
            "context_source_dependency_refs"
        ]} >= {canonical(item) for item in sources[1:]}
        assert public in canonical(next_packet)
        assert host.session.load(ref(next_intent), for_dispatch=True)

        use_ref = host.context._source_status(host.store.get(**sources[1]))[
            "current_source_use_ref"
        ]

        def revoke(tx):
            use = tx.get(**use_ref)
            return ref(tx.put("source_use", use["id"], {
                **use["body"], "allowed_uses": [], "policy_status": "revoked",
            }, expected_version=use["version"]))

        host.store.transact("revoke-extra-public-dependency", {}, revoke,
                            mission_id=control["body"]["mission_id"])
        with pytest.raises(SessionError):
            host.session.invoke_capture(ref(next_intent))
        assert not host.store.records("model_dispatch_attempt")
    finally:
        host.close()


def test_revoked_public_prose_is_withheld_without_breaking_tool_exchange(tmp_path):
    store, context, source, response, _inclusion = _saved_archived_turn(
        tmp_path, public="Short synthetic source-dependent assessment.",
    )
    revoke(store, source["use"])
    exchange, _compaction = context._tool_exchange(
        response, scope={"mission_id": "m", "actor_id": "peer", "task_id": "task"},
    )
    assert exchange[0]["role"] == "assistant"
    assert exchange[0]["content"] is None
    assert exchange[0]["tool_calls"][0]["function"]["name"] == "discover_capabilities"
    assert exchange[1]["role"] == "tool"
    assert exchange[1]["name"] == "discover_capabilities"
    assert "source-dependent assessment" not in canonical(exchange)
