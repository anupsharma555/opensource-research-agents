"""Runtime staged counts telescope to the exact saved provider request."""

from __future__ import annotations

import hashlib

from ora.contracts.models import LiveStartRequest
from ora.mission.service import _staged_request_estimate
from ora.models.token_estimate import LEGACY_BASIS, estimate_request_tokens
from ora.state.store import canonical
from tests.mission.test_service import answer, drain, service


def ordinary_request(*, source_text: str = "Synthetic checked passage.",
                     extra_tool: bool = False, extra_outcome: bool = False):
    tools = [{"type": "function", "function": {
        "name": "calculate", "description": "Synthetic local arithmetic",
        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}},
    }}]
    if extra_tool:
        tools.append({"type": "function", "function": {
            "name": "read_skill_resource", "description": "Synthetic optional skill reader",
            "parameters": {"type": "object", "properties": {"offset": {"type": "integer"}}},
        }})
    context = [
        {"role": "user", "content": canonical({"authority": "host", "skill_hint": "Optional skill"})},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "synthetic-call", "type": "function",
            "function": {"name": "calculate", "arguments": '{"expression":"1+1"}'},
        }]},
        {"role": "tool", "name": "calculate", "tool_call_id": "synthetic-call",
         "content": canonical({"result": 2, "status": "checked_local_result"})},
    ]
    if extra_outcome:
        context.extend([
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "skill-call", "type": "function",
                "function": {"name": "read_skill_resource", "arguments": '{"offset":0}'},
            }]},
            {"role": "tool", "name": "read_skill_resource", "tool_call_id": "skill-call",
             "content": canonical({"status": "loaded", "text": "Synthetic skill guidance " * 30})},
        ])
    context.append({"role": "user", "content": canonical({
        "schema": "ora.working_context.v4", "task": "Synthetic research task",
    })})
    request = {
        "model": "openai/gpt-oss-120b", "reasoning_effort": "high",
        "include_reasoning": True, "max_completion_tokens": 8192,
        "stream": False, "tool_choice": "auto", "parallel_tool_calls": False,
        "tools": tools,
        "messages": [
            {"role": "developer", "content": "Synthetic pinned instructions"},
            {"role": "user", "content": canonical({"source_packets": [{
                "id": "synthetic-copy", "version": 1, "text": source_text,
                "sha256": hashlib.sha256(source_text.encode()).hexdigest(),
            }]})},
            *context,
        ],
    }
    return request, context


def test_stages_use_one_basis_and_exact_marginal_final_count():
    request, context = ordinary_request()
    final, staged = _staged_request_estimate(request, context)
    encoded = canonical(request).encode()
    assert final.tokens == estimate_request_tokens(encoded).tokens
    assert staged["request_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert staged["request_bytes"] == len(encoded)
    assert staged["basis"] == final.basis
    assert staged["asset_sha256"] == final.asset_sha256
    assert sum(item["marginal_tokens"] for item in staged["stages"]) == final.tokens
    assert staged["stages"][-1]["cumulative_tokens"] == final.tokens
    assert [item["kind"] for item in staged["stages"]] == [
        "request_envelope", "pinned_instructions", "attached_tool_schemas",
        "source_packet", "context_or_skill_message", "assistant_tool_call",
        "tool_outcome", "working_context",
    ]


def test_source_tool_and_skill_additions_change_their_stages_and_final_request():
    base, context = ordinary_request()
    baseline, stages = _staged_request_estimate(base, context)
    expanded, expanded_context = ordinary_request(
        source_text="Synthetic checked passage. " * 50,
        extra_tool=True, extra_outcome=True,
    )
    larger, changed = _staged_request_estimate(expanded, expanded_context)
    assert larger.tokens > baseline.tokens
    assert changed["tool_schema_count"] == stages["tool_schema_count"] + 1
    assert changed["source_packet_count"] == stages["source_packet_count"] == 1
    def marginal(rows, kind):
        return next(item["marginal_tokens"] for item in rows["stages"]
                    if item["kind"] == kind)
    assert marginal(changed, "attached_tool_schemas") > marginal(stages, "attached_tool_schemas")
    assert marginal(changed, "source_packet") > marginal(stages, "source_packet")
    assert any(item.get("name") == "read_skill_resource" and item["kind"] == "tool_outcome"
               for item in changed["stages"])
    assert sum(item["marginal_tokens"] for item in changed["stages"]) == larger.tokens


def test_native_tool_fallback_keeps_one_legacy_basis_for_all_stages():
    request, context = ordinary_request()
    request["tools"].append({"type": "browser_search"})
    final, staged = _staged_request_estimate(request, context)
    assert final.basis == staged["basis"] == LEGACY_BASIS
    assert staged["fallback_reason"] == "native_or_unfamiliar_tool"
    assert sum(item["marginal_tokens"] for item in staged["stages"]) == final.tokens


def test_final_runtime_budget_stages_match_saved_intent_and_reserved_demand(tmp_path, monkeypatch):
    packets = []
    in_flight = []

    def model(request, *, timeout):
        packets.append(request.data)
        staged_budgets = [
            record for record in host.store.records("mission_call_budget")
            if record["body"].get("request_stage_token_estimate")
        ]
        in_flight.append(bool(staged_budgets))
        return answer("Synthetic checked answer.", len(packets))

    host = service(tmp_path, monkeypatch, model)
    try:
        run = host.start(LiveStartRequest(
            request_id="synthetic-staged-count", question="Answer a synthetic question.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        assert packets
        assert all(in_flight)
        budget = next(record for record in host.store.records("mission_call_budget")
                      if record["body"].get("request_stage_token_estimate"))
        stage = budget["body"]["request_stage_token_estimate"]
        intent = host.store.get(**stage["intent_ref"])
        saved = host.store.read_blob(intent["body"]["blob"]["sha256"])
        assert packets[0] == saved
        assert stage["request_sha256"] == intent["body"]["request_sha256"]
        assert stage["request_sha256"] == hashlib.sha256(saved).hexdigest()
        assert stage["final_input_tokens"] == estimate_request_tokens(saved).tokens
        assert stage["final_input_tokens"] == intent["body"]["request_token_estimate"]["tokens"]
        assert stage["admission_target_tokens"] >= stage["final_input_tokens"]
        assert stage["estimated_demand_tokens"] == (
            stage["final_input_tokens"] + stage["prior_completion_reservation"]
        )
        assert budget["body"]["estimated_tokens_with_completion_reservation"] == (
            stage["estimated_demand_tokens"]
        )
        assert sum(item["marginal_tokens"] for item in stage["stages"]) == (
            stage["final_input_tokens"]
        )
    finally:
        host.close()
