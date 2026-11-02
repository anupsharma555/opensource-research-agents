"""A clean finite observation stop resumes without replaying captured HTTP work."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from ora.mission import MissionService
from ora.state import StateStore
from tests.benchmarks.test_search_agent_runner import project, response, runner
from tests.mission.test_service import answer, tool


def _stopped_observation(tmp_path, monkeypatch, *, prior_calls: int = 1,
                         daily_429_on_last: bool = False,
                         uncertain_on_last: bool = False,
                         two_peers: bool = False
                         ) -> tuple[Path, dict, Path]:
    policy = project(tmp_path, monkeypatch)
    request = {
        "request_id": "synthetic-clean-observation-resume",
        "question": "Summarize the supplied synthetic note and its uncertainty.",
        "scope": "Use only the supplied synthetic note; no external retrieval.",
        "evidence_text": "Synthetic note: a measured change has uncertain attribution.",
        "agent_ids": (["agent-synthesis", "agent-critique"] if two_peers
                      else ["agent-synthesis"]),
        "credential_profile": "primary",
        "allow_vision": False,
    }
    sent = []

    def first_http(_handler, _class, outgoing, **_kwargs):
        sent.append(json.loads(outgoing.data))
        # Let the finite observation deadline close dispatch before the local
        # discovery result can schedule a second model request.
        time.sleep(.15)
        if uncertain_on_last and len(sent) == prior_calls:
            raise urllib.error.URLError(ConnectionError("synthetic closed socket"))
        if daily_429_on_last and len(sent) == prior_calls:
            rejected = response(outgoing, json.dumps({"error": {
                "type": "tokens", "code": "rate_limit_exceeded",
                "message": "Synthetic tokens per day (TPD): Limit 200,000, Used 199,800, Requested 7,000",
            }}).encode(), status=429)
            rejected.headers["retry-after"] = "120"
            return rejected
        return response(outgoing, json.dumps(tool(
            "discover_capabilities", {}, len(sent),
        ).payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", first_http)
    actual_time = time.time
    source = tmp_path / ".local/search-validation/observation-source"
    with monkeypatch.context() as clock:
        if uncertain_on_last:
            # Model the operator's closed observation before host repair runs.
            clock.setattr(MissionService, "_repair_unknown_model",
                          lambda *_args, **_kwargs: False)
        if prior_calls > 1:
            start_monotonic = time.monotonic()
            clock.setattr(time, "time", lambda: actual_time()
                          + 100 * (time.monotonic() - start_monotonic))
        clock.setattr(runner, "time", SimpleNamespace(
            time=lambda: actual_time() + (
                2000 if len(sent) >= prior_calls and not uncertain_on_last else 0),
            sleep=time.sleep,
        ))
        result = runner.run_case(
            tmp_path, source, execution_mode="offline_test",
            start_request=request, model_limit=None, token_limit=20_000,
            retrieval_limit=0, deadline_seconds=1800,
        )
    assert result["outcome"] == (
        "uncertain_transport" if uncertain_on_last else "observation_window_ended"
    ), result
    assert result["dispatch_drain_status"] == "settled"
    assert result["mission_status"] == "active"
    assert result["model_attempts"] == len(sent) == prior_calls
    assert result["evaluation_observation_saved"] is True
    assert result["private_profile_disabled"] is True
    assert json.loads((source / "budget.json").read_text())["model_attempts"][0][
        "status"] == 200
    return source, request, policy


def _resume_args(source: Path, request_file: Path, output: Path) -> list[str]:
    return [
        "--resume-case", str(source), "--start-request", str(request_file),
        "--output", str(output), "--no-model-call-cap", "--token-limit", "20000",
        "--retrieval-limit", "0", "--deadline-seconds", "1800",
    ]


def test_historical_blocked_estimate_must_match_capacity_and_prior_ceiling() -> None:
    intent_ref = {"kind": "model_request_intent", "id": "synthetic-intent", "version": 1}
    marker = {
        "request_sha256": "a" * 64, "intent_ref": intent_ref,
        "call_ref": {"kind": "agent_call", "id": "synthetic-call", "version": 1},
    }
    estimate = {"tokens": 6876, "basis": "synthetic-pinned-estimator",
                "asset_sha256": "b" * 64, "fallback_reason": None}
    intent = {"request_token_estimate": estimate}
    work = {"spec": {"proposal_ref": {"id": "synthetic-task"},
                     "output_reservation_tokens": 960}}
    capacity = {
        "kind": "model", "phase": "failed", "reservation": False,
        "diagnostic": "confirmed_pre_dispatch_no_effect", "external_active": False,
        "mission_id": "synthetic-mission", "run_id": "synthetic-run",
        "call_id": "synthetic-call", "task_id": "synthetic-task",
        "output_reservation_tokens": 960, "estimate_basis": estimate["basis"],
        "estimated_tokens": 7161,
        "request_token_estimate": {
            **estimate, "intent_ref": intent_ref, "request_sha256": "a" * 64,
        },
        "attempts": [{"at": 120, "status": "terminal_no_effect",
                      "usage_known": True, "output_usage_known": True,
                      "charged_tokens": 0, "charged_output_tokens": 0,
                      "reserved_output_tokens": 960, "reserved_tokens": 7161}],
    }
    segment = {"started_at": 100, "elapsed_seconds": 60,
               "controller_elapsed_seconds": 60}
    ledger = {"planned_tokens": 77_385, "limits": {"tokens": 84_000}}
    args = dict(marker=marker, intent=intent, work=work, capacity=capacity,
                prior_segment=segment, prior_ledger=ledger,
                run_id="synthetic-run", mission_id="synthetic-mission")
    assert runner._historical_blocked_capacity_ok(**args)

    changed_intent = deepcopy(intent)
    changed_intent["request_token_estimate"]["tokens"] = 1
    assert not runner._historical_blocked_capacity_ok(
        **{**args, "intent": changed_intent})
    changed_capacity = deepcopy(capacity)
    changed_capacity["request_token_estimate"]["tokens"] = 1
    assert not runner._historical_blocked_capacity_ok(
        **{**args, "capacity": changed_capacity})
    assert not runner._historical_blocked_capacity_ok(
        **{**args, "prior_ledger": {**ledger, "limits": {"tokens": 90_000}}})
    assert not runner._historical_blocked_capacity_ok(
        **{**args, "prior_segment": {"started_at": 200, "elapsed_seconds": 60}})


def test_clean_observation_resume_cli_preserves_prior_receipt_and_budget(
    tmp_path, monkeypatch, capsys,
) -> None:
    source, request, policy = _stopped_observation(tmp_path, monkeypatch)
    protected = {name: (source / name).read_bytes() for name in (
        "case.json", "budget.json", "observation.json", "source-manifest.json",
    )}
    old_store = StateStore(source / "state", read_only=True)
    old_receipt = old_store.records("model_response_receipt")[0]
    old_intent = old_store.records("model_request_intent")[0]
    old_request = old_store.read_blob(old_intent["body"]["blob"]["sha256"])
    old_response = old_store.read_blob(old_receipt["body"]["blob"]["sha256"])
    original_state_digest = runner._state_digest(source / "state")
    old_accounting = json.loads(protected["budget.json"])["planned_tokens"]
    original_policy = policy.read_bytes()
    request_file = tmp_path / "resume-request.json"
    request_file.write_text(json.dumps({"start_request": request}))
    output = tmp_path / ".local/search-validation/observation-resumed"
    flags = _resume_args(source, request_file, output)

    with monkeypatch.context() as preview:
        preview.setattr(runner, "ROOT", tmp_path)
        preview.setattr(runner, "run_case", lambda *_a, **_kw: pytest.fail(
            "Default CLI preview must not execute the case"))
        assert runner.main(flags) == 0
    assert json.loads(capsys.readouterr().out)["live"] is False
    assert not output.exists()

    resumed = []

    def final_http(_handler, _class, outgoing, **_kwargs):
        resumed.append(json.loads(outgoing.data))
        return response(outgoing, json.dumps(answer(
            "The synthetic note reports a change but cannot attribute its cause.", 2,
        ).payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", final_http)
    actual_time = time.time
    real_run_case = runner.run_case
    with monkeypatch.context() as continuation:
        continuation.setattr(time, "time", lambda: actual_time() + 120)
        continuation.setattr(runner, "ROOT", tmp_path)
        continuation.setattr(runner, "run_case", lambda *args, **kwargs: real_run_case(
            *args, execution_mode="offline_test", **kwargs,
        ))
        assert runner.main([*flags, "--live"]) == 0
    result = json.loads((output / "observation.json").read_text())
    assert result["outcome"] == "completed", result
    assert result["phase_model_attempts"] == len(resumed) == 1
    assert result["model_attempts"] == 2
    assert result["phase_retrieval_attempts"] == result["retrieval_attempts"] == 0
    assert result["phase_accounting"]["unknown_model_reservations"] == 0
    assert result["cumulative_accounting"]["unknown_model_reservations"] == 0
    assert result["cumulative_accounting"]["settled_or_reserved_tokens"] == (
        old_accounting + result["phase_accounting"]["settled_or_reserved_tokens"]
    )
    assert result["resume_lineage"]["next_phase_limits"]["tokens"] == 20_000 - old_accounting
    assert result["resume_lineage"]["source_boundary"] == "clean_observation_stop"
    assert result["resume_source_unchanged"] is True
    assert result["prior_evaluation_observation_preserved"] is True
    assert result["resume_integrity_complete"] is True
    assert (output / "prior-cumulative-budget.json").read_bytes() == protected["budget.json"]
    assert runner._state_digest(source / "state") == original_state_digest
    assert policy.read_bytes() == original_policy
    assert all((source / name).read_bytes() == contents for name, contents in protected.items())
    copied = StateStore(output / "state", read_only=True)
    assert copied.get("model_request_intent", old_intent["id"]) == old_intent
    assert copied.read_blob(old_intent["body"]["blob"]["sha256"]) == old_request
    assert copied.get("model_response_receipt", old_receipt["id"]) == old_receipt
    assert copied.read_blob(old_receipt["body"]["blob"]["sha256"]) == old_response
    assert len(copied.records("model_response_receipt")) == 2
    assert len(json.loads((output / "budget.json").read_text())["model_attempts"]) == 1


def test_ten_captured_successes_are_preserved_once_after_synthetic_resume(
    tmp_path, monkeypatch,
) -> None:
    source, request, _policy = _stopped_observation(
        tmp_path, monkeypatch, prior_calls=10,
    )
    prior = StateStore(source / "state", read_only=True)
    old_receipts = {row["id"]: row for row in prior.records("model_response_receipt")}
    assert len(old_receipts) == 10
    source_digest = runner._state_digest(source / "state")
    budget = json.loads((source / "budget.json").read_text())
    assert len(budget["model_attempts"]) == 10
    request_file = tmp_path / "ten-call-resume-request.json"
    request_file.write_text(json.dumps({"start_request": request}))
    output = tmp_path / ".local/search-validation/ten-call-resumed"
    seen = []

    def final_http(_handler, _class, outgoing, **_kwargs):
        seen.append(json.loads(outgoing.data))
        return response(outgoing, json.dumps(answer(
            "The synthetic observation has uncertain attribution.", 11,
        ).payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", final_http)
    actual_time = time.time
    offset = max(120, budget["model_attempts"][-1]["ended_at"] - actual_time() + 120)
    real_run_case = runner.run_case
    with monkeypatch.context() as continuation:
        continuation.setattr(time, "time", lambda: actual_time() + offset)
        continuation.setattr(runner, "ROOT", tmp_path)
        continuation.setattr(runner, "run_case", lambda *args, **kwargs: real_run_case(
            *args, execution_mode="offline_test", **kwargs,
        ))
        assert runner.main([*_resume_args(source, request_file, output), "--live"]) == 0
    observed = json.loads((output / "observation.json").read_text())
    assert observed["outcome"] == "completed"
    assert observed["phase_model_attempts"] == len(seen) == 1
    assert observed["model_attempts"] == 11
    assert observed["cumulative_accounting"]["model_http_attempts"] == 11
    assert observed["cumulative_accounting"]["unknown_model_reservations"] == 0
    assert observed["resume_integrity_complete"] is True
    assert runner._state_digest(source / "state") == source_digest
    copied = StateStore(output / "state", read_only=True)
    assert len(copied.records("model_response_receipt")) == 11
    assert all(copied.get("model_response_receipt", key) == value
               for key, value in old_receipts.items())


def test_finite_observation_with_daily_429_keeps_exact_wait_and_unknown_reservation(
    tmp_path, monkeypatch,
) -> None:
    source, request, policy = _stopped_observation(
        tmp_path, monkeypatch, prior_calls=2, daily_429_on_last=True,
    )
    before = {name: (source / name).read_bytes() for name in (
        "case.json", "budget.json", "observation.json", "source-manifest.json",
    )}
    source_digest = runner._state_digest(source / "state")
    budget = json.loads(before["budget.json"])
    assert [item["status"] for item in budget["model_attempts"]] == [200, 429]
    assert budget["model_attempts"][1]["usage_status"] == "unreported_reservation_retained"
    expected_unknown = budget["model_attempts"][1]["planned_tokens"]
    original_policy = policy.read_bytes()
    lineage = runner._resume_source(
        tmp_path, source, request, "offline_test", model_limit=None,
        token_limit=20_000, retrieval_limit=0, deadline_seconds=1800,
        accounting_only=False, until_complete=False,
    )
    assert lineage["terminal_observation_429"] is True
    assert lineage["prior_cumulative"]["unknown_model_reservations"] == expected_unknown
    assert lineage["next_phase_limits"]["tokens"] == 20_000 - budget["planned_tokens"]
    store = StateStore(source / "state", read_only=True)
    terminal = store.get(**lineage["last_response_ref"])
    control = next(row for row in store.records("mission_task_control")
                   if (row["body"].get("feedback") or {}).get(
                       "previous_response_ref") == lineage["last_response_ref"])
    retry_at = control["body"]["retry_at"]
    assert terminal["body"]["http_status"] == 429 and retry_at > time.time()
    assert control["body"]["phase"] in {"retry_at", "ready_model"}
    source_digest = runner._state_digest(source / "state")

    output = tmp_path / ".local/search-validation/observation-rate-wait-copy"
    attempted = []

    def deny_http(*_args, **_kwargs):
        attempted.append(1)
        raise AssertionError("No retry may dispatch before the saved wait")

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", deny_http)
    actual_time = time.time
    start_monotonic = time.monotonic()
    with monkeypatch.context() as quick_observation:
        quick_observation.setattr(runner, "time", SimpleNamespace(
            time=lambda: actual_time() + 20_000 * (
                time.monotonic() - start_monotonic),
            sleep=time.sleep,
        ))
        resumed = runner.run_case(
            tmp_path, output, execution_mode="offline_test", start_request=request,
            resume_case=source, model_limit=None, token_limit=20_000,
            retrieval_limit=0, deadline_seconds=1800,
        )
    assert not attempted
    assert resumed["outcome"] == "observation_window_ended"
    assert resumed["phase_model_attempts"] == 0
    assert resumed["cumulative_accounting"]["unknown_model_reservations"] == expected_unknown
    assert resumed["resume_source_unchanged"] is True
    assert resumed["resume_integrity_complete"] is True
    assert runner._state_digest(source / "state") == source_digest
    assert policy.read_bytes() == original_policy
    assert all((source / name).read_bytes() == value for name, value in before.items())


def test_closed_uncertain_model_is_superseded_without_replaying_or_releasing_reservation(
    tmp_path, monkeypatch,
) -> None:
    source, request, policy = _stopped_observation(
        tmp_path, monkeypatch, prior_calls=2, uncertain_on_last=True,
    )
    original_policy = policy.read_bytes()
    original_budget = json.loads((source / "budget.json").read_text())
    assert [item.get("status") for item in original_budget["model_attempts"]] == [200, None]
    unknown = original_budget["model_attempts"][-1]["planned_tokens"]
    assert original_budget["model_attempts"][-1]["state"] == "uncertain"
    lineage = runner._resume_source(
        tmp_path, source, request, "offline_test", model_limit=None,
        token_limit=20_000, retrieval_limit=0, deadline_seconds=1800,
        accounting_only=False, until_complete=False,
        token_ceiling_increase=20_000,
    )
    assert lineage["source_boundary"] == "uncertain_model_response"
    assert lineage["prior_cumulative"]["unknown_model_reservations"] == unknown
    old_store = StateStore(source / "state", read_only=True)
    old_dispatches = old_store.records("model_dispatch_attempt")
    old_responses = old_store.records("model_response_receipt")
    assert len(old_dispatches) == 2 and len(old_responses) == 1
    original_digest = runner._state_digest(source / "state")

    attempted = []

    def deny_http(_handler, _class, outgoing, **_kwargs):
        attempted.append(hashlib.sha256(outgoing.data).hexdigest())
        raise urllib.error.URLError(ConnectionError("synthetic socket denied"))

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", deny_http)
    actual_time = time.time
    output = tmp_path / ".local/search-validation/uncertain-model-copy"
    with monkeypatch.context() as quick_observation:
        quick_observation.setattr(runner, "time", SimpleNamespace(
            time=lambda: actual_time() + 2000, sleep=time.sleep,
        ))
        resumed = runner.run_case(
            tmp_path, output, execution_mode="offline_test", start_request=request,
            resume_case=source, model_limit=None, token_limit=20_000,
            retrieval_limit=0, deadline_seconds=1800,
            token_ceiling_increase=20_000,
        )
    assert len(attempted) == 1
    assert attempted[0] != original_budget["model_attempts"][-1]["request_sha256"]
    assert resumed["outcome"] == "uncertain_transport"
    assert resumed["phase_model_attempts"] == 1
    assert resumed["cumulative_accounting"]["unknown_model_reservations"] > unknown
    assert resumed["resume_source_unchanged"] is True
    assert resumed["resume_integrity_complete"] is True
    assert runner._state_digest(source / "state") == original_digest
    assert policy.read_bytes() == original_policy
    copied = StateStore(output / "state", read_only=True)
    assert len(copied.records("model_dispatch_attempt")) == len(old_dispatches) + 1
    assert len(copied.records("model_response_receipt")) == len(old_responses)
    for row in (*old_dispatches, *old_responses):
        assert copied.get(row["kind"], row["id"]) == row
    control = copied.get("mission_task_control", lineage["uncertain_control_id"])
    recovered = copied.records("model_response_lost")
    old_recovery = [row for row in recovered
                    if row["body"]["call_ref"] == lineage["uncertain_call_ref"]]
    assert len(old_recovery) == 1
    assert old_recovery[0]["body"]["usage"] == (
        "unknown_conservative_quota_charge_retained"
    )
    assert {item["id"] for item in control["body"]["model_recovery_refs"]} >= {
        old_recovery[0]["id"],
    }


def test_later_uncertain_phase_keeps_both_unknown_reservations_and_never_replays(
    tmp_path, monkeypatch,
) -> None:
    first, request, _policy = _stopped_observation(
        tmp_path, monkeypatch, prior_calls=2, uncertain_on_last=True,
    )
    first_budget = json.loads((first / "budget.json").read_text())
    first_unknown = first_budget["model_attempts"][-1]
    attempted = []

    def deny_http(_handler, _class, outgoing, **_kwargs):
        attempted.append(hashlib.sha256(outgoing.data).hexdigest())
        raise urllib.error.URLError(ConnectionError("synthetic socket denied"))

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", deny_http)
    second = tmp_path / ".local/search-validation/second-uncertain-phase"
    actual_time = time.time
    first_control = next(row for row in StateStore(first / "state", read_only=True).records(
        "mission_task_control") if row["body"].get("waiting_reason")
        == "pending_model has uncertain external effect")
    first_call_id = first_control["body"]["call_ref"]["id"]
    repair = MissionService._repair_unknown_model

    def repair_first_only(host, pending, work, capacity):
        if pending["body"].get("call_ref", {}).get("id") == first_call_id:
            return repair(host, pending, work, capacity)
        return False

    with monkeypatch.context() as quick_observation:
        quick_observation.setattr(MissionService, "_repair_unknown_model",
                                  repair_first_only)
        quick_observation.setattr(runner, "time", SimpleNamespace(
            time=lambda: actual_time() + 2000, sleep=time.sleep,
        ))
        result = runner.run_case(
            tmp_path, second, execution_mode="offline_test", start_request=request,
            resume_case=first, model_limit=None, token_limit=20_000,
            retrieval_limit=0, deadline_seconds=1800,
            token_ceiling_increase=20_000,
        )
    assert result["outcome"] == "uncertain_transport"
    second_budget = json.loads((second / "budget.json").read_text())
    second_unknown = second_budget["model_attempts"][-1]
    assert attempted == [second_unknown["request_sha256"]]
    assert second_unknown["request_sha256"] != first_unknown["request_sha256"]
    before = runner._state_digest(second / "state")
    lineage = runner._resume_source(
        tmp_path, second, request, "offline_test", model_limit=None,
        token_limit=40_000, retrieval_limit=0, deadline_seconds=1800,
        accounting_only=False, until_complete=False,
    )
    assert lineage["source_boundary"] == "uncertain_model_response"
    assert lineage["prior_cumulative"]["unknown_model_reservations"] == (
        first_unknown["planned_tokens"] + second_unknown["planned_tokens"]
    )
    assert lineage["last_response_request_sha256"] == second_unknown["request_sha256"]
    assert runner._state_digest(second / "state") == before


def test_later_already_recovered_uncertain_phase_keeps_ledger_and_dispatches_fresh(
    tmp_path, monkeypatch,
) -> None:
    first, request, _policy = _stopped_observation(
        tmp_path, monkeypatch, prior_calls=2, uncertain_on_last=True,
    )
    attempts = []

    def deny_http(_handler, _class, outgoing, **_kwargs):
        attempts.append(hashlib.sha256(outgoing.data).hexdigest())
        raise urllib.error.URLError(ConnectionError("synthetic socket denied"))

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", deny_http)
    second = tmp_path / ".local/search-validation/recovered-second-phase"
    actual_time = time.time
    with monkeypatch.context() as quick_observation:
        quick_observation.setattr(runner, "time", SimpleNamespace(
            time=lambda: actual_time() + 2000, sleep=time.sleep,
        ))
        second_result = runner.run_case(
            tmp_path, second, execution_mode="offline_test", start_request=request,
            resume_case=first, model_limit=None, token_limit=20_000,
            retrieval_limit=0, deadline_seconds=1800,
            token_ceiling_increase=20_000,
        )
    assert second_result["outcome"] == "uncertain_transport"
    # Construct the alternate saved boundary where the host has already
    # superseded the second unknown response before the next controller phase.
    host = MissionService(second / "runtime", second / "state",
                          execution_mode="offline_test", start_supervisor=False)
    pending = next((row for row in host.store.records("mission_task_control")
                    if row["body"].get("waiting_reason")
                    == "pending_model has uncertain external effect"), None)
    if pending is not None:
        work = host._work_for(pending)
        capacity = host.capacity.get(host.executor._capacity_id(
            host.executor._spec_from(work)))
        assert host._repair_unknown_model(pending, work, capacity)
    assert len(host.store.records("model_response_lost")) == 2
    host.close()
    second_budget = json.loads((second / "budget.json").read_text())
    old_shas = {json.loads((first / "budget.json").read_text())[
        "model_attempts"][-1]["request_sha256"],
        second_budget["model_attempts"][-1]["request_sha256"]}
    source_digest = runner._state_digest(second / "state")
    lineage = runner._resume_source(
        tmp_path, second, request, "offline_test", model_limit=None,
        token_limit=40_000, retrieval_limit=0, deadline_seconds=1800,
        accounting_only=False, until_complete=False,
    )
    assert lineage["source_boundary"] == "recovered_uncertain_model_response"
    assert runner._state_digest(second / "state") == source_digest
    third = tmp_path / ".local/search-validation/after-recovered-uncertainty"
    attempts.clear()
    with monkeypatch.context() as quick_observation:
        quick_observation.setattr(time, "time", lambda: actual_time() + 180)
        quick_observation.setattr(runner, "time", SimpleNamespace(
            time=lambda: actual_time() + 180, sleep=time.sleep,
        ))
        third_result = runner.run_case(
            tmp_path, third, execution_mode="offline_test", start_request=request,
            resume_case=second, model_limit=None, token_limit=40_000,
            retrieval_limit=0, deadline_seconds=1800,
            token_ceiling_increase=20_000,
        )
    assert attempts and all(sha not in old_shas for sha in attempts), {
        key: third_result.get(key) for key in (
            "outcome", "stop_reason", "phase_model_attempts", "mission_status",
            "resume_integrity_complete", "dispatch_drain_status",
        )
    }
    assert third_result["model_attempts"] == 4
    assert third_result["cumulative_accounting"]["unknown_model_reservations"] >= (
        lineage["prior_cumulative"]["unknown_model_reservations"]
    )
    assert third_result["resume_integrity_complete"] is True
    assert runner._state_digest(second / "state") == source_digest

    for tamper in ("earlier_recovery", "terminal_recovery", "cumulative_ledger"):
        bad = tmp_path / ".local/search-validation" / f"bad-{tamper}"
        shutil.copytree(second, bad)
        if tamper == "cumulative_ledger":
            path = bad / "cumulative-budget.json"
            budget = json.loads(path.read_text())
            budget["model_attempts"][0]["actual_tokens"] += 1
            path.write_text(json.dumps(budget))
        else:
            selected_sha = (next(iter(old_shas - {
                second_budget["model_attempts"][-1]["request_sha256"]
            })) if tamper == "earlier_recovery" else
                second_budget["model_attempts"][-1]["request_sha256"])
            with sqlite3.connect(bad / "state/state.sqlite3") as db:
                rows = db.execute(
                    "SELECT r.id,r.version,r.body FROM records r "
                    "JOIN heads h USING(kind,id,version) "
                    "WHERE r.kind='model_response_lost'"
                ).fetchall()
                row = next(row for row in rows
                           if json.loads(row[2]).get("request_sha256") == selected_sha)
                body = json.loads(row[2])
                body["usage"] = "unknown_charge_discarded"
                db.execute(
                    "UPDATE records SET body=? WHERE kind='model_response_lost' "
                    "AND id=? AND version=?",
                    (json.dumps(body), row[0], row[1]),
                )
        with pytest.raises(ValueError):
            runner._resume_source(
                tmp_path, bad, request, "offline_test", model_limit=None,
                token_limit=40_000, retrieval_limit=0, deadline_seconds=1800,
                accounting_only=False, until_complete=False,
            )


@pytest.mark.parametrize("tamper", [
    "missing_observed_response", "missing_uncertain_dispatch", "captured_uncertain_dispatch",
    "wrong_uncertain_intent", "missing_uncertain_capacity", "changed_unknown_reservation",
])
def test_uncertain_resume_rejects_missing_or_corrupt_dispatch_evidence(
    tmp_path, monkeypatch, tamper,
) -> None:
    source, request, _policy = _stopped_observation(
        tmp_path, monkeypatch, prior_calls=2, uncertain_on_last=True,
    )
    bad = tmp_path / ".local/search-validation" / f"bad-uncertain-{tamper}"
    shutil.copytree(source, bad)
    if tamper == "changed_unknown_reservation":
        path = bad / "budget.json"
        budget = json.loads(path.read_text())
        budget["model_attempts"][-1]["planned_tokens"] -= 1
        path.write_text(json.dumps(budget))
    else:
        db = sqlite3.connect(bad / "state/state.sqlite3")
        try:
            if tamper == "missing_observed_response":
                db.execute("DELETE FROM heads WHERE kind='model_response_receipt'")
            else:
                kind = ("model_dispatch_attempt" if "dispatch" in tamper else
                        "model_request_intent" if tamper == "wrong_uncertain_intent" else
                        "capacity_operation")
                row = db.execute(
                    "SELECT r.id,r.version,r.body FROM records r "
                    "JOIN heads h USING(kind,id,version) WHERE r.kind=? "
                    "ORDER BY r.created_at DESC LIMIT 1", (kind,),
                ).fetchone()
                assert row is not None
                record_id, version, raw = row
                if tamper in {"missing_uncertain_dispatch", "missing_uncertain_capacity"}:
                    db.execute("DELETE FROM heads WHERE kind=? AND id=?", (kind, record_id))
                else:
                    body = json.loads(raw)
                    if tamper == "captured_uncertain_dispatch":
                        body["status"] = "captured"
                    else:
                        body["request_sha256"] = "0" * 64
                    db.execute(
                        "UPDATE records SET body=? WHERE kind=? AND id=? AND version=?",
                        (json.dumps(body), kind, record_id, version),
                    )
            db.commit()
        finally:
            db.close()
    with pytest.raises(ValueError):
        runner._resume_source(
            tmp_path, bad, request, "offline_test", model_limit=None,
            token_limit=20_000, retrieval_limit=0, deadline_seconds=1800,
            accounting_only=False, until_complete=False,
        )

@pytest.mark.parametrize("tamper", [
    "missing_429_receipt", "corrupt_429_blob", "unreserved_429",
    "missing_retry", "changed_profile_model",
])
def test_finite_observation_429_rejects_broken_receipt_wait_or_binding(
    tmp_path, monkeypatch, tamper,
) -> None:
    source, request, _policy = _stopped_observation(
        tmp_path, monkeypatch, prior_calls=2, daily_429_on_last=True,
    )
    source_digest = runner._state_digest(source / "state")
    bad = tmp_path / ".local/search-validation" / f"bad-daily-429-{tamper}"
    shutil.copytree(source, bad)
    if tamper == "unreserved_429":
        path = bad / "budget.json"
        budget = json.loads(path.read_text())
        budget["model_attempts"][-1].pop("usage_status")
        path.write_text(json.dumps(budget))
    elif tamper == "corrupt_429_blob":
        db = sqlite3.connect(bad / "state/state.sqlite3")
        try:
            raw = db.execute(
                "SELECT body FROM records WHERE kind='model_response_receipt' "
                "AND json_extract(body,'$.http_status')=429 LIMIT 1"
            ).fetchone()[0]
        finally:
            db.close()
        pointer = json.loads(raw)["blob"]
        (bad / "state/objects" / pointer["sha256"]).write_bytes(b"{}")
    else:
        db = sqlite3.connect(bad / "state/state.sqlite3")
        try:
            if tamper == "missing_429_receipt":
                db.execute(
                    "DELETE FROM heads WHERE kind='model_response_receipt' "
                    "AND id IN (SELECT id FROM records WHERE kind='model_response_receipt' "
                    "AND json_extract(body,'$.http_status')=429)"
                )
            else:
                kind = ("mission_task_control" if tamper == "missing_retry"
                        else "mission_run")
                rows = db.execute(
                    "SELECT r.id,r.version,r.body FROM records r "
                    "JOIN heads h USING(kind,id,version) WHERE r.kind=?", (kind,),
                ).fetchall()
                for record_id, version, raw in rows:
                    body = json.loads(raw)
                    if tamper == "missing_retry":
                        if (body.get("feedback") or {}).get("status") != (
                            "provider_rate_limit_wait_completed"
                        ):
                            continue
                        body["retry_at"] = None
                    else:
                        body["profile"]["selectedModel"] = "synthetic-other-model"
                    db.execute(
                        "UPDATE records SET body=? WHERE kind=? AND id=? AND version=?",
                        (json.dumps(body), kind, record_id, version),
                    )
                    break
            db.commit()
        finally:
            db.close()
    with pytest.raises(ValueError):
        runner._resume_source(
            tmp_path, bad, request, "offline_test", model_limit=None,
            token_limit=20_000, retrieval_limit=0, deadline_seconds=1800,
            accounting_only=False, until_complete=False,
        )
    assert runner._state_digest(source / "state") == source_digest


@pytest.mark.parametrize("tamper", [
    "unsettled_drain", "uncertain_attempt", "missing_receipt",
    "unknown_receipt_status", "profile_model", "profile_reasoning",
    "profile_completion", "profile_quota_domain",
])
def test_observation_resume_rejects_unsettled_or_mismatched_boundary(
    tmp_path, monkeypatch, tamper,
) -> None:
    source, request, _policy = _stopped_observation(tmp_path, monkeypatch)
    bad = tmp_path / ".local/search-validation" / f"bad-{tamper}"
    shutil.copytree(source, bad)
    if tamper == "unsettled_drain":
        path = bad / "observation.json"
        data = json.loads(path.read_text())
        data["dispatch_drain_status"] = "timed_out"
        path.write_text(json.dumps(data))
    elif tamper == "uncertain_attempt":
        path = bad / "budget.json"
        data = json.loads(path.read_text())
        data["model_attempts"][-1]["state"] = "uncertain"
        path.write_text(json.dumps(data))
    elif tamper == "missing_receipt":
        db = sqlite3.connect(bad / "state/state.sqlite3")
        try:
            db.execute("DELETE FROM heads WHERE kind='model_response_receipt'")
            db.commit()
        finally:
            db.close()
    else:
        kind = ("model_response_receipt" if tamper == "unknown_receipt_status"
                else "mission_run")
        db = sqlite3.connect(bad / "state/state.sqlite3")
        try:
            record_id, version, raw = db.execute(
                "SELECT r.id,r.version,r.body FROM records r "
                "JOIN heads h USING(kind,id,version) WHERE r.kind=? LIMIT 1",
                (kind,),
            ).fetchone()
            body = json.loads(raw)
            if tamper == "unknown_receipt_status":
                body["status"] = "unsettled"
            elif tamper == "profile_model":
                body["profile"]["selectedModel"] = "synthetic-other-model"
            elif tamper == "profile_reasoning":
                body["profile"]["reasoningSettings"]["reasoning_effort"] = "high"
            elif tamper == "profile_completion":
                body["profile"]["modelSettings"]["max_completion_tokens"] = 4096
            else:
                body["profile"]["modelSettings"]["quota_domain"] = "groq-synthetic-other"
            db.execute(
                "UPDATE records SET body=? WHERE kind=? AND id=? AND version=?",
                (json.dumps(body), kind, record_id, version),
            )
            db.commit()
        finally:
            db.close()
    with pytest.raises(ValueError):
        runner._resume_source(
            tmp_path, bad, request, "offline_test", model_limit=None,
            token_limit=20_000, retrieval_limit=0, deadline_seconds=1800,
            accounting_only=False, until_complete=False,
        )
