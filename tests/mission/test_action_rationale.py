"""Ordinary action explanations accompany a tool turn without extra model calls.

These injected responses prove capture and execution boundaries, not that a live
model will consistently choose to explain its actions.
"""

from __future__ import annotations

import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.mission.call_inspection import inspect_calls
from tests.mission.test_service import answer, drain, service, tool


@pytest.mark.parametrize("alias,action,arguments,explanation", [
    ("agent-synthesis", "discover_capabilities", {},
     "I will check available capabilities to choose a supported next action."),
    ("agent-critique", "discover_capabilities", {},
     "I will check available capabilities to choose a supported next action."),
    ("agent-critique", "activate_skill", {"skill_id": "study-appraisal"},
     "I will load study appraisal guidance to structure the methods review."),
])
def test_ordinary_action_explanation_is_saved_with_one_tool_execution(
    tmp_path, monkeypatch, alias, action, arguments, explanation,
) -> None:
    private_marker = "Synthetic private reasoning must not enter the report projection."
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        instructions = packet["messages"][0]["content"]
        assert "ordinary assistant content" in instructions
        assert "Use the same response" in instructions
        assert "consequential skill choices and actions" in instructions
        assert "skill activation/release" in instructions
        assert "Skip repetitive explanations" in instructions
        communication_guidance = "Answer unanswered incoming requests when able"
        assert communication_guidance in instructions
        assert all(communication_guidance not in json.dumps(message)
                   for message in packet["messages"][1:])
        assert "exact unresolved peer/proposal dependency" in instructions
        assert "Wait only for pending retrieval" not in instructions
        if len(packets) == 1:
            response = tool(action, arguments, 1)
            message = response.payload["choices"][0]["message"]
            message["content"] = explanation
            message["reasoning"] = private_marker
            return response
        assert len(packets) == 2
        assert any(message.get("role") == "tool" for message in packet["messages"])
        if action == "activate_skill":
            assert "# Study appraisal" in instructions
        return answer("No source was supplied, so the synthetic claim remains unchecked.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="ordinary-action-explanation", question="Assess a synthetic claim.",
            agent_ids=[alias],
        ))
        drain(host, run.run_id)
        calls = inspect_calls(host.store, run.run_id)
        assert len(packets) == len(calls) == 2
        assert calls[0]["response"]["content"] == explanation
        assert calls[0]["response"]["tool_calls"][0]["function"]["name"] == action
        assert len(calls[0]["toolResults"]) == 1
        assert calls[1]["response"]["content"].startswith("No source was supplied")
        assert private_marker not in json.dumps(calls)
        assert len(host.store.records("context_tool_result")) == 1
    finally:
        host.close()
