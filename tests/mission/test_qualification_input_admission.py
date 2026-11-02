"""A local input qualification cap does not consume prior completion tokens."""

from __future__ import annotations

import json
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.models.token_estimate import estimate_request_tokens
from tests.mission.test_service import service, tool


def test_qualification_input_cap_counts_only_prepared_input(tmp_path, monkeypatch):
    sent = []

    def transport(request, *, timeout):
        sent.append(json.loads(request.data))
        if len(sent) != 1:
            pytest.fail("Preparation must not dispatch the next model call")
        response = tool("discover_capabilities", {}, 1)
        response.payload["usage"] = {
            "prompt_tokens": 100, "completion_tokens": 5_000,
            "total_tokens": 5_100,
        }
        return response

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="qualification-input-only",
            question="Inspect the synthetic available capabilities",
            agent_ids=["agent-synthesis"],
        ))
        for _ in range(200):
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            if control["body"]["phase"] == "ready_model" and control["body"]["tool_turn_refs"]:
                break
            time.sleep(.005)
        else:
            pytest.fail("The synthetic first tool turn was not saved")
        host._update_control(control, input_budget={
            "observed_limit_tokens": 8_000, "target_fraction": .70,
            "basis": "qualification_imposed_limit",
            "actual_provider_observation": False,
        })
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        prepared = host._control(run.run_id, "agent-synthesis")
        call = host.store.get(**prepared["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
        )
        raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
        estimate = estimate_request_tokens(raw).tokens
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        stage = budget["request_stage_token_estimate"]
        assert estimate <= 8_000
        assert estimate + 5_000 > 8_000
        assert stage["admission_target_tokens"] <= 8_000
        assert stage["prior_completion_reservation"] == 5_000
        assert stage["estimated_demand_tokens"] == estimate + 5_000
        assert stage["output_cap_tokens"] == 8_192
        assert len(sent) == 1
    finally:
        host.close()
