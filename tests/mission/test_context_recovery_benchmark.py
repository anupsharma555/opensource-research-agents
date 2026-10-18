"""Injected mission-loop evidence for the recovery evaluator, never model quality."""

from __future__ import annotations

import json
import time

import pytest

from ora.benchmarks.context_recovery import digest, evaluate_recovery, load_case
from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.evidence import read_source_window
from tests.mission.test_service import ROOT, answer, drain, service, tool


def _until_ready(host, run_id, count, requests):
    for _ in range(600):
        host._drive_run(host._run(run_id))
        control = host._control(run_id, "agent-synthesis")
        if len(requests) == count and control["body"]["phase"] == "ready_model":
            return control
        time.sleep(.005)
    raise AssertionError(f"Fixture did not reach accepted tool boundary {count}")


def recovery_run(tmp_path, monkeypatch, condition):
    case = load_case(ROOT)
    requests = []
    source = late_source = None
    early = late = None

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        requests.append(packet)
        step = len(requests)
        if step == 1:
            return tool("read_source_window", {
                "source_id": source["id"], "source_version": 1, "block_offset": 0,
                "char_offset": 0, "char_limit": 16000, "selection_mode": "replace",
            }, step)
        if step == 2:
            return tool("read_source_window", {
                "source_id": source["id"], "source_version": 1, "block_offset": 0,
                "char_offset": 0, "char_limit": len(case["early"]), "selection_mode": "replace",
            }, step)
        if step == 3:
            return tool("read_source_window", {
                "source_id": late_source["id"], "source_version": 1, "block_offset": 0,
                "char_offset": 0, "char_limit": len(case["late"]), "selection_mode": "append",
            }, step)
        assert step == 4
        text = "The six-month comparison does not establish durable benefit. "
        if condition != "revoked_source":
            text += ("The early result was restricted to higher baseline scores and six weeks; "
                     "the later null result has different samples and follow-up. "
                     f"[{early['id']}] ")
        else:
            text += "Earlier evidence is unavailable under current use permissions. "
        return answer(text + f"[{late['id']}]", step)

    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(
        request_id="recovery-" + condition, question=case["question"],
        evidence_text=case["early"] + case["pressure_unit"] * case["pressure_repetitions"],
        agent_ids=["agent-synthesis"],
    ))
    control = host._control(run.run_id, "agent-synthesis")
    source = control["body"]["source_refs"][0]
    initial = read_source_window(host.evidence, "fixture-early-window", source,
                                 block_offset=0, char_offset=0, char_limit=len(case["early"]))
    early = initial["passage_refs"][0]
    host._update_control(control, selected_passage_refs={source["id"]: [early]},
                         read_window_refs=[initial["window_ref"]])
    control = _until_ready(host, run.run_id, 1, requests)
    if condition == "compacted_recovery":
        host._update_control(control, input_budget={
            "observed_limit_tokens": 8000, "target_fraction": .70,
            "basis": "qualification_imposed_limit",
            "actual_provider_observation": False,
        })
    revoked = None
    if condition == "revoked_source":
        current = host.store.resolve("source_use.copy", f"{source['id']}:1")
        revoked = host.store.transact("fixture-revoke-early", {}, lambda tx: ref(tx.put(
            "source_use", current["id"], {
                **current["body"], "allowed_uses": [], "policy_status": "revoked",
            }, expected_version=current["version"],
        )), mission_id=control["body"]["mission_id"])
    # Restart at the accepted pressure-window boundary: no model response is
    # synthesized by restoration, and the next turn must recover by a real tool.
    host.close()
    host = service(tmp_path, monkeypatch, transport)
    control = _until_ready(host, run.run_id, 2, requests)
    saved_run = host._run(run.run_id)
    late_source = host._supplied_source({
        **saved_run, "id": run.run_id + "-late-fixture",
        "body": {**saved_run["body"], "profile": {
            **saved_run["body"]["profile"], "evidenceText": case["late"],
        }},
    })
    window = read_source_window(host.evidence, "fixture-late-window", late_source,
                                block_offset=0, char_offset=0, char_limit=len(case["late"]))
    late = window["passage_refs"][0]
    host._update_control(control, source_refs=[source, late_source],
                         selected_passage_refs={
                             **control["body"].get("selected_passage_refs", {}),
                             late_source["id"]: [late],
                         })
    drain(host, run.run_id, limit=30)
    assert len(requests) == 4
    assert host._run(run.run_id)["body"]["status"] == "completed"
    intents = host.store.records("model_request_intent")
    matched = [next(item for item in intents
                    if item["body"]["request_sha256"] == digest(request)) for request in requests]
    final = next(item for item in host.store.records("model_response_receipt")
                 if item["body"]["intent_ref"] == ref(matched[-1]))
    report = host.store.records("report")[0]
    selection = {
        "case_sha256": digest(case), "run_id": run.run_id, "condition": condition,
        "early_intent_ref": ref(matched[0]), "pressure_intent_ref": ref(matched[1]),
        "recovery_intent_ref": ref(matched[2]), "final_response_ref": ref(final),
        "report_ref": ref(report), "early_passage_ref": early, "late_passage_ref": late,
        "revoked_source_use_ref": revoked,
    }
    return host, selection, requests


@pytest.mark.parametrize("condition", ["unchanged_context", "compacted_recovery", "revoked_source"])
def test_real_mission_recovery_conditions_survive_restart(tmp_path, monkeypatch, condition):
    host, selection, requests = recovery_run(tmp_path, monkeypatch, condition)
    try:
        scored = evaluate_recovery(host.store, ROOT, selection)
        assert scored["mechanical_pass"], [key for key, value in scored["checks"].items() if not value]
        assert scored["semantic_review"] == "pending_or_not_passed"
        assert scored["qualified"] is False
        assert scored["execution_mode"] == "offline_test"
        assert scored["usage"]["actual_provider_usage"] is None
        assert len(scored["usage"]["injected_fixture_usage"]) == 4
        if condition == "compacted_recovery":
            intent = host.store.get(**selection["pressure_intent_ref"])
            budget = host.store.get("mission_call_budget", intent["body"]["call_ref"]["id"])
            assert budget["body"]["source_turn_mode"] == "source_free_recovery"
            assert budget["body"]["rate_admission_policy"] != "provider_feedback_probe"
        if condition == "revoked_source":
            pressure = host.store.get(**selection["pressure_intent_ref"])
            response = host.store.get(
                "model_response_receipt", f"model-response:{pressure['id']}",
            )
            result = host.store.get("context_tool_result", f"tool-result:{response['id']}")
            message = json.loads(host.store.read_blob(result["body"]["blob"]["sha256"]))
            assert json.loads(message["content"])["status"] == "rejected"
    finally:
        host.close()
