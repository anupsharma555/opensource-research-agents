"""A finite resume grant changes only a new derived cumulative ceiling."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import urllib.request
from types import SimpleNamespace

import pytest

from ora.observability.evaluation import combine_validation_ledgers
from ora.state import StateError
from ora.state.store import canonical
from tests.benchmarks.test_search_agent_observation_resume import _stopped_observation
from tests.benchmarks.test_search_agent_runner import response, runner
from tests.mission.test_service import answer, tool


def _revision(prior: dict, increase: int) -> dict:
    old = prior["limits"]["tokens"]
    return {
        "schema": "ora.validation_token_ceiling_revision.v1",
        "prior_limit_tokens": old,
        "additional_tokens": increase,
        "new_limit_tokens": old + increase,
        "source_ledger_sha256": hashlib.sha256(canonical(prior).encode()).hexdigest(),
    }


def test_cli_preview_names_the_explicit_new_ceiling_without_running(
    tmp_path, capsys,
):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"start_request": {
        "request_id": "synthetic-grant-preview", "question": "Synthetic question",
        "credential_profile": "primary", "agent_ids": ["agent-synthesis"],
    }}))
    output = tmp_path / "unused-output"
    assert runner.main([
        "--resume-case", str(tmp_path / "source"), "--start-request", str(request),
        "--output", str(output), "--no-model-call-cap", "--token-limit", "20000",
        "--resume-token-ceiling-increase", "5000", "--retrieval-limit", "0",
        "--deadline-seconds", "1800",
    ]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["planned_tokens"] == 20_000
    assert preview["effective_cumulative_token_ceiling"] == 25_000
    assert not output.exists()


def test_derived_ledger_requires_exact_grant_and_preserves_unknown_charge():
    prior = {
        "schema": "ora.agent_validation_budget.v1",
        "limits": {"model": None, "tokens": 100, "retrieval": 0},
        "model_attempts": [{"number": 1, "planned_tokens": 50, "actual_tokens": 30},
                           {"number": 2, "planned_tokens": 20, "actual_tokens": None}],
        "retrieval_attempts": [], "planned_tokens": 50,
        "peak_planned_tokens": 70, "stop_reason": "observation_window_ended",
    }
    phase = {
        "schema": "ora.agent_validation_budget.v1",
        "limits": {"model": None, "tokens": 70, "retrieval": 0},
        "model_attempts": [{"number": 1, "planned_tokens": 15, "actual_tokens": 10}],
        "retrieval_attempts": [], "planned_tokens": 10,
        "peak_planned_tokens": 15, "stop_reason": "observation_window_ended",
    }
    grant = _revision(prior, 20)
    with pytest.raises(StateError, match="finite cumulative limits"):
        combine_validation_ledgers(prior, phase)
    merged = combine_validation_ledgers(prior, phase, token_ceiling_revision=grant)
    assert merged["limits"]["tokens"] == 120
    assert merged["token_ceiling_revisions"] == [grant]
    assert merged["planned_tokens"] == 60
    assert merged["model_attempts"][1]["actual_tokens"] is None
    assert prior["limits"]["tokens"] == 100 and "token_ceiling_revisions" not in prior
    later = {**phase, "limits": {**phase["limits"], "tokens": 60}}
    chained = combine_validation_ledgers(merged, later)
    assert chained["limits"]["tokens"] == 120
    assert chained["token_ceiling_revisions"] == [grant]
    second_grant = _revision(merged, 10)
    extended = combine_validation_ledgers(
        merged, {**later, "limits": {**later["limits"], "tokens": 70}},
        token_ceiling_revision=second_grant,
    )
    assert extended["limits"]["tokens"] == 130
    assert extended["token_ceiling_revisions"] == [grant, second_grant]
    for bad in (
        {**grant, "additional_tokens": 0},
        {**grant, "prior_limit_tokens": 101},
        {**grant, "new_limit_tokens": 121},
        {**grant, "source_ledger_sha256": "0" * 64},
    ):
        with pytest.raises(StateError, match="exact grant"):
            combine_validation_ledgers(prior, phase, token_ceiling_revision=bad)
    with pytest.raises(StateError, match="finite cumulative limits"):
        combine_validation_ledgers(prior, {**phase, "limits": {**phase["limits"],
                                                                "retrieval": 1}},
                                   token_ceiling_revision=grant)


def test_runner_grant_copies_exact_prior_case_and_saves_new_ceiling(tmp_path, monkeypatch):
    source, request, policy = _stopped_observation(tmp_path, monkeypatch)
    old = {name: (source / name).read_bytes() for name in (
        "case.json", "budget.json", "observation.json", "source-manifest.json",
    )}
    old_state_digest = runner._state_digest(source / "state")
    old_policy = policy.read_bytes()
    old_budget = json.loads(old["budget.json"])
    no_grant = runner._resume_source(
        tmp_path, source, request, "offline_test", model_limit=None,
        token_limit=20_000, retrieval_limit=0, deadline_seconds=1800,
        accounting_only=False, until_complete=False,
    )
    assert no_grant["next_phase_limits"]["tokens"] == 20_000 - old_budget["planned_tokens"]
    grant_preview = runner._resume_source(
        tmp_path, source, request, "offline_test", model_limit=None,
        token_limit=20_000, retrieval_limit=0, deadline_seconds=1800,
        accounting_only=False, until_complete=False, token_ceiling_increase=5_000,
    )
    assert grant_preview["next_phase_limits"]["tokens"] == 25_000 - old_budget["planned_tokens"]
    assert grant_preview["token_ceiling_revision"]["source_ledger_sha256"] == (
        hashlib.sha256(canonical(old_budget).encode()).hexdigest()
    )
    for bad in (0, -1, True):
        with pytest.raises(ValueError, match="positive finite grant|positive grant"):
            runner._resume_source(
                tmp_path, source, request, "offline_test", model_limit=None,
                token_limit=20_000, retrieval_limit=0, deadline_seconds=1800,
                accounting_only=False, until_complete=False,
                token_ceiling_increase=bad,
            )
    with pytest.raises(ValueError, match="differs from stopped case"):
        runner._resume_source(
            tmp_path, source, request, "offline_test", model_limit=None,
            token_limit=25_000, retrieval_limit=0, deadline_seconds=1800,
            accounting_only=False, until_complete=False,
        )

    sent = []

    def final_http(_handler, _class, outgoing, **_kwargs):
        sent.append(json.loads(outgoing.data))
        return response(outgoing, json.dumps(answer(
            "The synthetic note reports a change with uncertain attribution.", 2,
        ).payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", final_http)
    original_time = time.time
    monkeypatch.setattr(time, "time", lambda: original_time() + 120)
    output = tmp_path / ".local/search-validation/granted-resume"
    result = runner.run_case(
        tmp_path, output, execution_mode="offline_test", start_request=request,
        resume_case=source, accounting_only=False, until_complete=False,
        model_limit=None, token_limit=20_000, retrieval_limit=0,
        deadline_seconds=1800, token_ceiling_increase=5_000,
    )
    assert result["outcome"] == "completed" and result["resume_integrity_complete"]
    assert len(sent) == 1
    case = json.loads((output / "case.json").read_text())
    phase = json.loads((output / "budget.json").read_text())
    cumulative = json.loads((output / "cumulative-budget.json").read_text())
    grant = grant_preview["token_ceiling_revision"]
    assert case["max_planned_tokens"] == cumulative["limits"]["tokens"] == 25_000
    assert case["token_ceiling_revision"] == cumulative["token_ceiling_revisions"][-1] == grant
    assert phase["limits"]["tokens"] == 25_000 - old_budget["planned_tokens"]
    assert cumulative["planned_tokens"] == old_budget["planned_tokens"] + phase["planned_tokens"]
    assert cumulative["model_attempts"][0] == old_budget["model_attempts"][0]
    assert (output / "prior-cumulative-budget.json").read_bytes() == old["budget.json"]
    assert runner._state_digest(source / "state") == old_state_digest
    assert policy.read_bytes() == old_policy
    assert all((source / name).read_bytes() == content for name, content in old.items())


def test_granted_observation_stop_resumes_at_new_ceiling_without_second_grant(
    tmp_path, monkeypatch,
):
    source, request, _ = _stopped_observation(tmp_path, monkeypatch)
    sent = []

    def tool_http(_handler, _class, outgoing, **_kwargs):
        sent.append(json.loads(outgoing.data))
        return response(outgoing, json.dumps(tool(
            "discover_capabilities", {}, 2,
        ).payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", tool_http)
    real_time = time.time
    with monkeypatch.context() as clock:
        clock.setattr(runner, "time", SimpleNamespace(
            time=lambda: real_time() + (2_000 if sent else 0), sleep=time.sleep,
        ))
        output = tmp_path / ".local/search-validation/granted-stopped"
        result = runner.run_case(
            tmp_path, output, execution_mode="offline_test", start_request=request,
            resume_case=source, accounting_only=False, until_complete=False,
            model_limit=None, token_limit=20_000, retrieval_limit=0,
            deadline_seconds=1800, token_ceiling_increase=5_000,
        )
    assert result["outcome"] == "observation_window_ended"
    assert result["resume_integrity_complete"] and len(sent) == 1
    cumulative = json.loads((output / "cumulative-budget.json").read_text())
    next_lineage = runner._resume_source(
        tmp_path, output, request, "offline_test", model_limit=None,
        token_limit=25_000, retrieval_limit=0, deadline_seconds=1800,
        accounting_only=False, until_complete=False,
    )
    assert next_lineage["token_ceiling_revision"] is None
    assert next_lineage["last_token_ceiling_revision"] == cumulative[
        "token_ceiling_revisions"][-1]
    assert next_lineage["next_phase_limits"]["tokens"] == (
        25_000 - cumulative["planned_tokens"])
    with pytest.raises(ValueError, match="differs from stopped case"):
        runner._resume_source(
            tmp_path, output, request, "offline_test", model_limit=None,
            token_limit=20_000, retrieval_limit=0, deadline_seconds=1800,
            accounting_only=False, until_complete=False,
        )
    tampered = tmp_path / ".local/search-validation/granted-tampered"
    shutil.copytree(output, tampered)
    prior_path = tampered / "prior-cumulative-budget.json"
    changed_prior = json.loads(prior_path.read_text())
    changed_prior["stop_reason"] = "tampered_local_copy"
    changed_bytes = json.dumps(changed_prior, indent=2).encode()
    prior_path.write_bytes(changed_bytes)
    changed_case_path = tampered / "case.json"
    changed_case = json.loads(changed_case_path.read_text())
    changed_case["resume_lineage"]["prior_cumulative_budget_sha256"] = (
        hashlib.sha256(changed_bytes).hexdigest())
    changed_case_path.write_text(json.dumps(changed_case, indent=2))
    with pytest.raises(ValueError, match="grant differs from its prior ledger"):
        runner._resume_source(
            tmp_path, tampered, request, "offline_test", model_limit=None,
            token_limit=25_000, retrieval_limit=0, deadline_seconds=1800,
            accounting_only=False, until_complete=False,
        )

    later_sent = []

    def later_tool_http(_handler, _class, outgoing, **_kwargs):
        later_sent.append(json.loads(outgoing.data))
        return response(outgoing, json.dumps(tool(
            "discover_capabilities", {}, 3,
        ).payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", later_tool_http)
    with monkeypatch.context() as clock:
        clock.setattr(runner, "time", SimpleNamespace(
            time=lambda: real_time() + (2_000 if later_sent else 0), sleep=time.sleep,
        ))
        third = tmp_path / ".local/search-validation/granted-no-new-grant"
        third_result = runner.run_case(
            tmp_path, third, execution_mode="offline_test", start_request=request,
            resume_case=output, accounting_only=False, until_complete=False,
            model_limit=None, token_limit=25_000, retrieval_limit=0,
            deadline_seconds=1800,
        )
    assert third_result["outcome"] == "observation_window_ended"
    assert third_result["resume_integrity_complete"] and len(later_sent) == 1
    third_lineage = runner._resume_source(
        tmp_path, third, request, "offline_test", model_limit=None,
        token_limit=25_000, retrieval_limit=0, deadline_seconds=1800,
        accounting_only=False, until_complete=False,
    )
    assert third_lineage["last_token_ceiling_revision"] == cumulative[
        "token_ceiling_revisions"][-1]
    assert third_lineage["token_ceiling_revision"] is None
