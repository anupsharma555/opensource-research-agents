"""A finite campaign admission stop keeps exact prior model work resumable."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from ora.observability.evaluation import (
    advance_evaluation_observation,
    read_evaluation_observation,
)
from ora.state import StateStore
from tests.benchmarks.test_search_agent_observation_resume import _stopped_observation
from tests.benchmarks.test_search_agent_runner import runner


def _cap_stopped_case(tmp_path, monkeypatch) -> tuple[Path, dict, Path]:
    source, request, policy = _stopped_observation(tmp_path, monkeypatch)
    continuation = tmp_path / ".local/search-validation/campaign-cap-source"
    shutil.copytree(source, continuation)
    case_path = continuation / "case.json"
    case = json.loads(case_path.read_text())
    case["phase"] = 2
    case_path.write_text(json.dumps(case))
    budget_path = continuation / "budget.json"
    prior_budget = json.loads(budget_path.read_text())
    prior_copy = continuation / "prior-cumulative-budget.json"
    prior_copy.write_text(json.dumps(prior_budget))
    case["resume_lineage"] = {
        "prior_cumulative_budget_sha256": hashlib.sha256(prior_copy.read_bytes()).hexdigest(),
    }
    case_path.write_text(json.dumps(case))
    budget = {**prior_budget, "limits": {
        "model": None, "tokens": 20_000 - prior_budget["planned_tokens"],
        "retrieval": 0,
    }, "model_attempts": [], "retrieval_attempts": [], "planned_tokens": 0,
        "peak_planned_tokens": 0, "stop_reason": "campaign_phase_cap"}
    budget_path.write_text(json.dumps(budget))
    cumulative_path = continuation / "cumulative-budget.json"
    cumulative = {**prior_budget, "stop_reason": "campaign_phase_cap"}
    assert len(cumulative["model_attempts"]) == 1
    cumulative_path.write_text(json.dumps(cumulative))
    blocked_sha = hashlib.sha256(b"synthetic denied model packet").hexdigest()
    guard_path = continuation / "campaign-guard.json"
    guard_path.write_text(json.dumps({
        "schema": "private_campaign_phase_guard.v1",
        "canonical_case_cumulative_limit": 20_000,
        "canonical_new_phase_limit": budget["limits"]["tokens"],
        "effective_new_phase_limit": 9_000,
        "campaign_prior_charge": 11_000,
        "campaign_total_limit": 20_000,
        "blocked_candidate": {
            "request_sha256": blocked_sha, "prior_phase_charge": 0,
            "estimated_input_tokens": 2_000, "max_completion_tokens": 8_192,
            "candidate_reservation": 10_192,
            "external_action": "blocked_before_http",
        },
    }))
    observation_path = continuation / "observation.json"
    observation = json.loads(observation_path.read_text())
    prior_accounting = runner._prior_accounting(prior_budget)
    observation.update(
        phase=2, outcome="campaign_phase_cap", stop_reason="campaign_phase_cap",
        phase_model_attempts=0, phase_retrieval_attempts=0,
        phase_accounting=runner._prior_accounting(budget),
        cumulative_accounting=prior_accounting,
        controller_segments=[runner._controller_segment(
            prior_budget, observation["controller_elapsed_seconds"],
            observation["elapsed_seconds"],
        ), runner._controller_segment(budget, 0, 0)],
    )
    observation_path.write_text(json.dumps(observation))
    store = StateStore(continuation / "state")
    evaluation_id = f"evaluation-observation:{case['run_id']}"
    prior = store.get("evaluation_observation", evaluation_id)
    run = store.get("mission_run", case["run_id"])
    mission_id = run["body"]["mission_id"]
    call_ref = {"kind": "agent_call", "id": "synthetic-blocked-call", "version": 1}
    intent_ref = {"kind": "model_request_intent", "id": "synthetic-blocked-intent",
                  "version": 1}

    def update(tx):
        tx.put("model_request_intent", intent_ref["id"], {
            "mission_id": mission_id, "request_sha256": blocked_sha,
            "call_ref": call_ref, "credential_profile": "primary",
            "request_token_estimate": {"tokens": 2_000},
        })
        tx.put("model_dispatch_attempt", "synthetic-blocked-dispatch", {
            "mission_id": mission_id, "status": "blocked_before_external_effect",
            "request_sha256": blocked_sha, "call_ref": call_ref,
            "intent_ref": intent_ref, "credential_profile": "primary",
        })
        tx.put("executor_work", "synthetic-blocked-work", {
            "mission_id": mission_id, "phase": "superseded",
            "status_basis": "local_model_admission_blocked",
            "superseded_intent_ref": intent_ref,
            "spec": {"kind": "model", "call_id": call_ref["id"]},
            "receipt_ref": None, "result_ref": None,
        })
        return {"blocked_request_sha256": blocked_sha}

    store.transact("synthetic-campaign-cap-stop", {}, update)
    saved = advance_evaluation_observation(
        store, case["run_id"],
        {key: prior[key] for key in ("kind", "id", "version")},
        cumulative, "campaign_phase_cap", 0.0,
    )
    assert saved["body"]["outcome"] == "campaign_phase_cap"
    assert read_evaluation_observation(store, case["run_id"])["outcome"] == (
        "campaign_phase_cap"
    )
    return continuation, request, policy


def test_clean_zero_http_campaign_cap_is_resumable_without_double_count(
    tmp_path, monkeypatch, capsys,
) -> None:
    source, request, policy = _cap_stopped_case(tmp_path, monkeypatch)
    protected = {name: (source / name).read_bytes() for name in (
        "case.json", "budget.json", "observation.json", "source-manifest.json",
        "cumulative-budget.json", "campaign-guard.json",
    )}
    old_receipts = StateStore(source / "state", read_only=True).records(
        "model_response_receipt",
    )
    original_policy = policy.read_bytes()
    lineage = runner._resume_source(
        tmp_path, source, request, "offline_test", model_limit=None,
        token_limit=20_000, retrieval_limit=0, deadline_seconds=1_800,
        accounting_only=False, until_complete=False,
    )
    assert lineage["source_boundary"] == "clean_campaign_cap_stop"
    assert lineage["prior_cumulative"]["model_http_attempts"] == 1
    assert lineage["prior_cumulative"]["unknown_model_reservations"] == 0
    assert lineage["next_phase_limits"]["tokens"] == (
        20_000 - lineage["prior_cumulative"]["known_model_tokens"]
    )
    request_path = tmp_path / "continuation-request.json"
    request_path.write_text(json.dumps({"start_request": request}))
    output = tmp_path / ".local/search-validation/campaign-cap-future"
    with monkeypatch.context() as preview:
        preview.setattr(runner, "ROOT", tmp_path)
        preview.setattr(runner, "run_case", lambda *_args, **_kwargs: pytest.fail(
            "Default CLI preview must not resume or dispatch"))
        assert runner.main([
            "--resume-case", str(source), "--start-request", str(request_path),
            "--output", str(output), "--no-model-call-cap", "--token-limit", "20000",
            "--retrieval-limit", "0", "--deadline-seconds", "1800",
        ]) == 0
    assert json.loads(capsys.readouterr().out)["live"] is False
    assert not output.exists()
    assert StateStore(source / "state", read_only=True).records(
        "model_response_receipt",
    ) == old_receipts
    assert policy.read_bytes() == original_policy
    assert all((source / name).read_bytes() == content for name, content in protected.items())


@pytest.mark.parametrize("tamper", [
    "unsettled", "uncertain", "wrong_profile", "phase_one",
    "guard_mismatch", "missing_blocked_marker",
])
def test_campaign_cap_rejects_bad_boundary(tmp_path, monkeypatch, tamper) -> None:
    source, request, _policy = _cap_stopped_case(tmp_path, monkeypatch)
    bad = tmp_path / ".local/search-validation" / f"campaign-cap-{tamper}"
    shutil.copytree(source, bad)
    if tamper == "unsettled":
        path = bad / "observation.json"
        data = json.loads(path.read_text())
        data["dispatch_drain_status"] = "timed_out"
        path.write_text(json.dumps(data))
    elif tamper == "uncertain":
        path = bad / "budget.json"
        data = json.loads(path.read_text())
        data["model_attempts"] = [{"state": "uncertain"}]
        path.write_text(json.dumps(data))
    elif tamper == "wrong_profile":
        policy_path = bad / "runtime" / runner.PROFILE
        policy = json.loads(policy_path.read_text())
        policy["profiles"]["primary"]["enabled"] = True
        policy_path.write_text(json.dumps(policy))
    elif tamper == "guard_mismatch":
        path = bad / "campaign-guard.json"
        data = json.loads(path.read_text())
        data["blocked_candidate"]["request_sha256"] = "0" * 64
        path.write_text(json.dumps(data))
    elif tamper == "missing_blocked_marker":
        db = sqlite3.connect(bad / "state/state.sqlite3")
        try:
            db.execute("DELETE FROM heads WHERE kind='model_dispatch_attempt' "
                       "AND id='synthetic-blocked-dispatch'")
            db.commit()
        finally:
            db.close()
    else:
        path = bad / "case.json"
        data = json.loads(path.read_text())
        data["phase"] = 1
        path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        runner._resume_source(
            tmp_path, bad, request, "offline_test", model_limit=None,
            token_limit=20_000, retrieval_limit=0, deadline_seconds=1_800,
            accounting_only=False, until_complete=False,
        )
