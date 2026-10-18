"""Admit ordinary planning turns with small saved output caps and exact token accounting.

Synthetic responses exercise the unchanged mission catalog, context preparation,
dispatch and restart paths. The sizing receipt describes offline admission only;
it does not qualify a model's ability to choose or revise a useful plan.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import asdict

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission import service as service_module
from ora.models import ModelSession
from ora.models.token_estimate import LEGACY_BASIS, TOKENIZER_BASIS, estimate_request_tokens
from tests.mission.test_capability_catalog import _index
from tests.mission.test_service import ROOT, service, tool

PROMPTS = (
    "Please make and save a short two-step plan for a small volunteer reading group "
    "to compare two meeting formats. Include a clear completion condition. Use only "
    "this prompt; do not research or carry out the activities yet.",
    "Change of scope: the group now has only 30 minutes and no shared readings. "
    "Revise the saved plan for those constraints, keeping it to two short steps. "
    "Do not research or carry out the activities.",
)
OBSERVED_LIMIT = 8000
PRIOR_COMPLETION = 771


def _plan():
    return {
        "goal": "Compare two reading-group meeting formats",
        "completion_criteria": ["Choose one format using participation and usefulness criteria"],
        "uncertainties": ["The group has not tried either format"],
        "status": "active",
        "steps": [
            {"id": "compare", "description": "Define the two formats and compare their trade-offs",
             "status": "todo", "depends_on": [], "linked_refs": []},
            {"id": "choose", "description": "Choose a format using participation and usefulness",
             "status": "todo", "depends_on": ["compare"], "linked_refs": []},
        ],
        "linked_refs": [],
        "revision_reason": "Initial short plan requested by the user",
    }


def _drive_until(host, run_id, predicate, failures):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        assert not failures
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Small-output planning continuation did not reach its durable boundary")


@pytest.mark.parametrize("output_cap,observed_limit", [(1536, 8000), (2048, 8000), (2048, 10000)])
def test_small_output_plan_requests_keep_full_catalog_and_exact_admission_across_restart(
    tmp_path, monkeypatch, output_cap, observed_limit,
):
    # Select a real configuration setting; never override the admission outcome.
    monkeypatch.setattr(service_module, "DEFAULT_MODEL_SETTINGS", {
        **service_module.DEFAULT_MODEL_SETTINGS, "max_completion_tokens": output_cap,
    })
    dispatch_bytes = []
    failures = []
    initial = _plan()
    revised = copy.deepcopy(initial)
    revised["goal"] = "Compare two meeting formats in 30 minutes without shared readings"
    revised["completion_criteria"] = ["Choose a discussion-only format within 30 minutes"]
    revised["steps"][0]["description"] = "Spend 15 minutes comparing two discussion-only formats"
    revised["steps"][1]["description"] = "Spend 15 minutes choosing a format and recording why"
    revised["revision_reason"] = "The user limited time to 30 minutes and removed shared readings"

    def transport(request, *, timeout):
        try:
            raw = request.data
            packet = json.loads(raw)
            dispatch_bytes.append(raw)
            step = len(dispatch_bytes)
            assert packet["max_completion_tokens"] == output_cap
            assert packet["reasoning_effort"] == "high"
            catalog = _index(packet["messages"][0]["content"])
            # Full current catalog includes web navigation and both document readers.
            assert len(catalog["tools"]) == 54
            assert {"save_plan", "read_plan", "browser_search", "search_scholarly"} <= set(catalog["tools"])
            assert catalog["skills"]["task-planning"]["state"] == "available_to_activate"
            attached = {item["function"]["name"] for item in packet["tools"]}
            if step == 1:
                assert {"save_plan", "read_plan"}.isdisjoint(attached)
                assert PROMPTS[0] in raw.decode()
                response = tool("request_capability", {"tool_id": "save_plan"}, step)
            elif step == 2:
                assert "save_plan" in attached and "read_plan" not in attached
                response = tool("save_plan", {"plan": initial, "expected_version": 0}, step)
            elif step == 3:
                assert PROMPTS[1] in raw.decode()
                assert "save_plan" not in attached
                response = tool("request_capability", {"tool_id": "modify_plan"}, step)
            elif step == 4:
                assert "modify_plan" in attached and "save_plan" not in attached
                response = tool("modify_plan", {"expected_version": 1,
                    "revision_reason": revised["revision_reason"], "changes": {
                        "goal": revised["goal"], "completion_criteria": revised["completion_criteria"],
                        "update_steps": [{"id": item["id"], "description": item["description"]}
                                         for item in revised["steps"]]}}, step)
            elif step == 5:
                last_tool = next(item for item in reversed(packet["messages"]) if item["role"] == "tool")
                result = json.loads(last_tool["content"])
                assert last_tool["name"] == "modify_plan" and result["plan_ref"]["version"] == 2
                response = tool("request_capability", {"tool_id": "read_plan"}, step)
            else:
                assert step == 6
                assert {"modify_plan", "read_plan"} <= attached
                response = tool("read_plan", {}, step)
            response.payload["usage"] = {
                "prompt_tokens": 100, "completion_tokens": PRIOR_COMPLETION,
                "total_tokens": 100 + PRIOR_COMPLETION,
            }
            return response
        except Exception as exc:
            failures.append(repr(exc))
            raise

    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(
        request_id=f"small-output-planning-{output_cap}-{observed_limit}", question=PROMPTS[0],
        agent_ids=["agent-synthesis"],
    ))
    control = host._control(run.run_id, "agent-synthesis")
    task_id, actor_id = control["body"]["task_id"], control["body"]["actor_id"]
    host._update_control(control, input_budget={
        "observed_limit_tokens": observed_limit, "target_fraction": .70,
    })
    try:
        _drive_until(host, run.run_id, lambda: (
            len(dispatch_bytes) == 2 and bool(host.store.records("working_note"))
            and host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
        ), failures)
        original = host.store.records("working_note")[0]
        assert original["version"] == 1
        assert json.loads(original["body"]["text"]) == initial
        assert host._control(run.run_id, "agent-synthesis")["body"]["source_refs"] == []
    finally:
        host.close()

    resumed = service(tmp_path, monkeypatch, transport)
    try:
        control = resumed._control(run.run_id, "agent-synthesis")
        assert control["body"]["task_id"] == task_id and control["body"]["actor_id"] == actor_id
        assert control["body"]["requested_capabilities"] == ["save_plan"]
        resumed._update_control(control, feedback={"user_request": PROMPTS[1]})
        _drive_until(resumed, run.run_id, lambda: (
            len(dispatch_bytes) == 6
            and resumed.store.get("working_note", original["id"])["version"] == 2
            and resumed._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
        ), failures)
        current = resumed.store.get("working_note", original["id"])
        assert json.loads(current["body"]["text"]) == revised
        assert resumed.store.get(**ref(original)) == original
        assert len(resumed._controls(run.run_id)) == 1
        assert not resumed.store.records("source_copy")
        assert not resumed._control(run.run_id, "agent-synthesis")["body"]["active_skill_ids"]

        intents = resumed.store.records("model_request_intent")
        assert len(intents) == len(dispatch_bytes) == 6
        by_hash = {hashlib.sha256(raw).hexdigest(): raw for raw in dispatch_bytes}
        sizing = []
        reservations = []
        for intent in intents:
            body = intent["body"]
            raw = resumed.store.read_blob(body["blob"]["sha256"])
            assert by_hash[body["request_sha256"]] == raw
            assert hashlib.sha256(raw).hexdigest() == body["request_sha256"]
            estimate = estimate_request_tokens(raw)
            assert estimate.basis == TOKENIZER_BASIS and estimate.basis.endswith(".v3")
            assert estimate.fallback_reason is None
            assert body["request_token_estimate"] == asdict(estimate)
            assert body["source_refs"] == body["context_source_dependency_refs"] == []
            budget = resumed.store.get("mission_call_budget", body["call_ref"]["id"])["body"]
            reserve = budget["prior_completion_reservation"]
            reservations.append(reserve)
            assert budget["observed_account_limit"] == observed_limit
            assert budget["request_token_estimate"] == asdict(estimate)
            assert estimate.tokens <= budget["preview_admission_target_tokens"]
            assert budget["estimated_tokens_with_completion_reservation"] == estimate.tokens + reserve
            assert estimate.tokens + reserve <= observed_limit
            call = resumed.store.get(**body["call_ref"])
            config = resumed.store.get("agent_configuration", call["body"]["configuration_id"], 1)
            assert config["body"]["settings"]["max_completion_tokens"] == output_cap
            capacity = next(item["body"] for item in resumed.store.records("capacity_operation")
                            if item["body"].get("call_id") == body["call_ref"]["id"]
                            and item["body"]["kind"] == "model")
            assert capacity["estimated_tokens"] == estimate.tokens + reserve
            assert capacity["estimate_basis"] == TOKENIZER_BASIS
            assert capacity["request_token_estimate"]["request_sha256"] == body["request_sha256"]
            assert ModelSession(resumed.store, ROOT).load(ref(intent), for_dispatch=True).intent_ref == ref(intent)
            sizing.append({
                "dispatch_index": dispatch_bytes.index(raw) + 1,
                "request_sha256": body["request_sha256"], "request_bytes": len(raw),
                "attached_tools": list(call["body"]["exposed_tools"]),
                "request_tokens": estimate.tokens, "estimate_basis": estimate.basis,
                "prior_completion_reservation": reserve,
                "capacity_reserved_tokens": capacity["estimated_tokens"],
                "configured_output_cap": output_cap,
                "request_plus_configured_output_cap": estimate.tokens + output_cap,
                "configured_output_cap_fits_observed_limit": estimate.tokens + output_cap <= observed_limit,
                "legacy_byte_estimate": estimate_request_tokens(raw, basis=LEGACY_BASIS).tokens,
            })
        assert sorted(reservations) == [0, *([PRIOR_COMPLETION] * 5)]
        assert any(row["legacy_byte_estimate"] > OBSERVED_LIMIT for row in sizing)
        (tmp_path / "admission-sizing.json").write_text(json.dumps({
            "proof_kind": "offline_scripted_admission", "output_cap": output_cap,
            "observed_limit_tokens": observed_limit, "catalog_count": 54,
            "prompts": list(PROMPTS), "requests": sorted(sizing, key=lambda row: row["dispatch_index"]),
        }, indent=2) + "\n")
    finally:
        resumed.close()


def test_v13_waiting_preparation_recovers_at_same_packer_with_small_output_cap(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(service_module, "DEFAULT_MODEL_SETTINGS", {
        **service_module.DEFAULT_MODEL_SETTINGS, "max_completion_tokens": 1536,
    })

    def no_dispatch(_request, *, timeout):
        pytest.fail("Historical preparation recovery must not dispatch a provider request")

    host = service(tmp_path, monkeypatch, no_dispatch)
    run = host.start(LiveStartRequest(
        request_id="small-output-v13-preparation", question=PROMPTS[0],
        agent_ids=["agent-synthesis"],
    ))
    control = host._control(run.run_id, "agent-synthesis")
    host._update_control(control, requested_capabilities=["save_plan"], input_budget={
        "observed_limit_tokens": OBSERVED_LIMIT, "target_fraction": .70,
    })
    control = host._control(run.run_id, "agent-synthesis")
    call_id, _attempt_id = host._model_ids(control, control["body"]["sequence"] + 1)
    # A synthetic saved pre-intent failure, not an override of admission logic.
    prior_budget = host._save("mission_call_budget", call_id, {
        "mission_id": control["body"]["mission_id"], "run_id": run.run_id,
        "actor_id": control["body"]["actor_id"], "task_id": control["body"]["task_id"],
        "estimator_version": "source_working_set_v13",
        "packer_version": service_module.PACKER_VERSION,
        "estimate_basis": LEGACY_BASIS, "observed_account_limit": OBSERVED_LIMIT,
    }, mission_id=control["body"]["mission_id"], operation_id="synthetic-v13-overflow-budget")
    prior_budget = host.store.get(**prior_budget)
    host._update_control(
        control, phase="waiting", work_id=None,
        waiting_reason="ContextOverflow: Required source inventory and task context exceed the observed input target",
    )
    host.close()

    resumed = service(tmp_path, monkeypatch, no_dispatch)
    try:
        waiting = resumed._control(run.run_id, "agent-synthesis")
        assert resumed._recover_preparation(waiting)
        recovered = resumed._control(run.run_id, "agent-synthesis")
        assert recovered["body"]["phase"] == "ready_model"
        assert resumed._schedule_model(recovered)
        pending = resumed._control(run.run_id, "agent-synthesis")
        assert pending["body"]["phase"] == "pending_model"
        assert pending["body"]["call_ref"]["id"] == call_id
        current_budget = resumed.store.get("mission_call_budget", call_id)
        assert current_budget["body"]["estimator_version"] == service_module.ESTIMATOR_VERSION
        assert current_budget["body"]["estimator_version"] != "source_working_set_v13"
        assert current_budget["body"]["packer_version"] == prior_budget["body"]["packer_version"]
        assert resumed.store.get(**ref(prior_budget)) == prior_budget
        intent = resumed.store.get("model_request_intent", f"model-intent:{call_id}:1")
        raw = resumed.store.read_blob(intent["body"]["blob"]["sha256"])
        estimate = estimate_request_tokens(raw)
        assert estimate.basis == TOKENIZER_BASIS and estimate.fallback_reason is None
        packet = json.loads(raw)
        assert packet["max_completion_tokens"] == 1536
        assert len(_index(packet["messages"][0]["content"])["tools"]) == 54
        names = {item["function"]["name"] for item in packet["tools"]}
        assert "save_plan" in names and "read_plan" not in names
        assert current_budget["body"]["estimated_tokens_with_completion_reservation"] <= OBSERVED_LIMIT
        assert ModelSession(resumed.store, ROOT).load(ref(intent), for_dispatch=True).intent_ref == ref(intent)
        assert not resumed.store.records("model_dispatch_attempt")
    finally:
        resumed.close()
