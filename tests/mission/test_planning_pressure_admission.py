"""Keep an explicitly selected plan usable after a substantial model completion.

Use the normal model/plan boundaries under an observed token-rate limit. The
fixture proves safe request packing and exact revision mechanics, not model
choice quality, and performs no provider calls.
"""
from __future__ import annotations

import copy
import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.tool_catalog import DEFERABLE_CORE_TOOLS
from ora.models.token_estimate import estimate_request_tokens
from tests.mission.test_capability_catalog import _index
from tests.mission.test_planning_small_output_admission import _drive_until
from tests.mission.test_service import answer, drain, service, tool

QUESTION = (
    "Plan two three-week synthetic survey cohorts targeting 30 completions within "
    "48 staff hours and $200. Email historically yields 18 completions, 14 hours, "
    "$60; onsite cards yield 14 completions, 20 hours, $100. These small convenience "
    "samples do not guarantee future yields. Save a working plan and wait for "
    "new evidence before finalizing. Use only these facts."
)
UPDATE = (
    "New synthetic memo: email is unavailable in weeks 1–3 and available in weeks "
    "4–6. Cards now cost $110 per cohort. Revise the saved plan and recommendation."
)
PLAN = {
    "goal": "Two survey cohorts reaching 30 completions within 48 staff hours and $200",
    "completion_criteria": ["Compare feasible schedules and revise from the new memo"],
    "uncertainties": ["Small nonrandomized synthetic cohorts do not guarantee future yields"],
    "status": "active",
    "steps": [
        {"id": "constraints", "description": "Retain the six-week timeline, minimum completion target, staff-hour ceiling and total budget.", "status": "done"},
        {"id": "compare", "description": "Compare email/email (36 completions, 28 hours, $120), mixed (32, 34, $160), and cards/cards (28, 40, $200).", "status": "done"},
        {"id": "first", "description": "Provisionally use email in weeks 1–3: 18 projected completions, 14 staff hours and $60.", "status": "todo", "depends_on": ["compare"]},
        {"id": "second", "description": "Provisionally use email in weeks 4–6 with a distinct participant pool: 18 completions, 14 hours and $60.", "status": "todo", "depends_on": ["compare"]},
        {"id": "revise", "description": "Read the pending operations memo and revise affected schedule and cost assumptions while retaining the original checkpoint.", "status": "todo"},
        {"id": "report", "description": "Recommend a feasible schedule with arithmetic, remaining resource margins and explicit uncertainty from the small samples.", "status": "todo", "depends_on": ["revise"]},
    ],
    "revision_reason": "Initial synthetic checkpoint, pending operational evidence",
}


@pytest.mark.parametrize("long_read_completions", [False, True])
def test_selected_planning_survives_high_completion_and_preserves_deferred_tools(
    tmp_path, monkeypatch, long_read_completions,
):
    packets, failures = [], []

    def transport(request, *, timeout):
        try:
            packet = json.loads(request.data)
            packets.append(packet)
            n = len(packets)
            attached = {t["function"]["name"] for t in packet["tools"]}
            assert DEFERABLE_CORE_TOOLS <= _index(packet["messages"][0]["content"])["tools"].keys()
            if n == 1:
                response = tool("activate_skill", {"skill_id": "task-planning"}, n)
            elif n == 2:
                assert "# Task planning" in packet["messages"][0]["content"]
                response = tool("save_plan", {"expected_version": 0, "plan": PLAN}, n)
            elif n == 3:
                assert "read_plan" in attached and "modify_plan" in attached
                assert not (attached & DEFERABLE_CORE_TOOLS)
                assert any(UPDATE in (m.get("content") or "") for m in packet["messages"])
                response = tool("read_plan", {}, n)
            elif n == 4:
                result = json.loads(next(m["content"] for m in reversed(packet["messages"])
                                         if m["role"] == "tool"))
                next_page = result["next_request"]
                assert next_page["tool"] == "read_tool_result"
                response = tool("read_tool_result", {
                    **{k: v for k, v in next_page.items() if k != "tool"}, "limit": 1800,
                }, n)
            elif n == 5:
                assert any(m.get("name") == "read_tool_result" for m in packet["messages"])
                assert "first" in json.dumps(packet) and "second" in json.dumps(packet)
                assert any(UPDATE in (m.get("content") or "") for m in packet["messages"])
                response = tool("modify_plan", {
                    "expected_version": 1, "revision_reason": "New availability and cost memo",
                    "changes": {"update_steps": [{"id": "first", "description": "Use cards in weeks 1–3: 14 projected completions, 20 hours, $110."}]},
                }, n)
            else:
                assert n == 6
                response = answer("Cards then email project 32 completions, 34 hours and $170. Future yield remains uncertain.", n)
            completion = 2061 if n == 2 else 200
            if long_read_completions:
                completion = {2: 2061, 3: 666, 4: 1256, 5: 1973}.get(n, 200)
            response.payload["usage"] = {"prompt_tokens": 100, "completion_tokens": completion,
                                         "total_tokens": 100 + completion}
            return response
        except Exception as exc:
            failures.append(repr(exc))
            raise

    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(request_id="planning-rate-pressure", question=QUESTION,
                                      agent_ids=["agent-synthesis"]))
    control = host._control(run.run_id, "agent-synthesis")
    host._update_control(control, input_budget={"observed_limit_tokens": 8000, "target_fraction": .70})
    try:
        _drive_until(host, run.run_id, lambda: (
            len(packets) == 2 and bool(host.store.records("working_note"))
            and host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
        ), failures)
        original = host.store.records("working_note")[0]
        control = host._control(run.run_id, "agent-synthesis")
        response_ref = control["body"]["tool_turn_refs"][-1]
        assert host._completed_plan_has_checkpoint(response_ref, control)
        foreign = copy.deepcopy(control)
        foreign["body"]["actor_id"] = "different-peer"
        assert not host._completed_plan_has_checkpoint(response_ref, foreign)
        assert not host._completed_plan_has_checkpoint(control["body"]["tool_turn_refs"][0], control)
        # Deferred defaults remain explicitly selectable without losing skill tools.
        config = host._configuration(control)
        for name in DEFERABLE_CORE_TOOLS:
            selected = copy.deepcopy(control)
            selected["body"]["requested_capabilities"] = [name]
            exposed, unavailable = host._exposed_tools(selected, config, defer_core_tools=True)
            assert name in exposed and name not in unavailable
            assert {"modify_plan", "read_plan"} <= set(exposed)
        host._update_control(control, feedback={"user_request": UPDATE})
        # Preserve the same event on later turns, as the user-update controller does.
        schedule = host._schedule_model

        def with_update(current, **kwargs):
            if current["body"].get("feedback") != {"user_request": UPDATE}:
                host._update_control(current, feedback={"user_request": UPDATE})
                current = host._control(run.run_id, "agent-synthesis")
            return schedule(current, **kwargs)

        host._schedule_model = with_update
        drain(host, run.run_id)
        assert not failures
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert len(packets) == 6
        current = host.store.get("working_note", original["id"])
        assert current["version"] == 2 and host.store.get(**ref(original)) == original
        assert not host._completed_plan_has_checkpoint(response_ref, host._control(run.run_id, "agent-synthesis"))
        revised = json.loads(current["body"]["text"])
        assert revised["steps"][3] == json.loads(original["body"]["text"])["steps"][3]
        assert "$110" in revised["steps"][2]["description"]
        admitted = []
        for intent in host.store.records("model_request_intent"):
            b = intent["body"]
            budget = host.store.get("mission_call_budget", b["call_ref"]["id"])["body"]
            estimate = estimate_request_tokens(host.store.read_blob(b["blob"]["sha256"]))
            if budget["rate_admission_policy"] == "provider_feedback_probe":
                assert estimate.tokens + 8192 <= budget["model_context_limit"]
                assert budget["estimated_tokens_with_completion_reservation"] == (
                    estimate.tokens + budget["prior_completion_reservation"]
                )
            else:
                assert estimate.tokens + budget["prior_completion_reservation"] <= 8000
            if budget["prior_completion_reservation"] == 2061:
                admitted.append(budget)
        assert len(admitted) == 1 and admitted[0]["deferred_core_tool_ids"]
        assert admitted[0]["current_tool_turn_ref"] == response_ref
        assert response_ref not in admitted[0]["compacted_tool_turn_refs"]
        assert any(
            message.get("name") == "save_plan"
            and json.loads(message["content"]).get("status") == "saved"
            for message in packets[2]["messages"]
        )
    finally:
        host.close()
