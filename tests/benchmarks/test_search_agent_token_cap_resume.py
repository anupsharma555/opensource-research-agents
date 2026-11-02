"""A settled local token denial can resume only with an explicit finite grant."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ora.coordination.board import ref
from ora.models.token_estimate import estimate_request_tokens
from ora.observability.evaluation import advance_evaluation_observation
from ora.state import StateStore
from ora.state.store import canonical
from tests.benchmarks.test_search_agent_observation_resume import _stopped_observation
from tests.benchmarks.test_search_agent_runner import project, response, runner
from tests.mission.test_service import tool


def _token_stopped_case(tmp_path, monkeypatch) -> tuple[Path, dict]:
    source, request, _policy = _stopped_observation(tmp_path, monkeypatch)
    budget_path = source / "budget.json"
    budget = json.loads(budget_path.read_text())
    budget["stop_reason"] = "token_cap"
    budget_path.write_text(json.dumps(budget))
    observation_path = source / "observation.json"
    observation = json.loads(observation_path.read_text())
    observation.update(outcome="token_cap", stop_reason="token_cap")
    observation_path.write_text(json.dumps(observation))
    store = StateStore(source / "state")
    run_id = json.loads((source / "case.json").read_text())["run_id"]
    run = store.get("mission_run", run_id)
    prior = store.get("evaluation_observation", f"evaluation-observation:{run_id}")
    original_call = store.records("agent_call")[0]
    remaining = budget["limits"]["tokens"] - budget["planned_tokens"]
    input_tokens = remaining
    output_tokens = original_call["body"]["settings"]["max_completion_tokens"]
    encoded = canonical({
        "model": original_call["body"]["model_id"],
        "reasoning_effort": original_call["body"]["reasoning_settings"]["reasoning_effort"],
        "max_completion_tokens": output_tokens,
        "stream": False,
        "messages": [{"role": "developer", "content": "Synthetic denied request"}],
    }).encode()
    blob = store.put_blob(encoded)
    denied_sha = hashlib.sha256(encoded).hexdigest()

    def save(tx):
        call = tx.put("agent_call", "token-cap-denied-call", original_call["body"])
        call_ref = ref(call)
        intent = tx.put("model_request_intent", "token-cap-denied-intent", {
            "mission_id": run["body"]["mission_id"],
            "call_ref": call_ref, "credential_profile": "primary",
            "configuration_ref": {"kind": "agent_configuration",
                                  "id": call["body"]["configuration_id"], "version": 1},
            "configuration_sha256": call["body"]["configuration_sha256"],
            "request_sha256": denied_sha,
            "blob": blob,
            "request_token_estimate": {"tokens": input_tokens},
        })
        intent_ref = ref(intent)
        tx.put("model_dispatch_attempt", "token-cap-denied-dispatch", {
            "mission_id": run["body"]["mission_id"],
            "status": "blocked_before_external_effect",
            "request_sha256": denied_sha,
            "call_ref": call_ref, "intent_ref": intent_ref,
            "credential_profile": "primary", "receipt_ref": None,
        })
        tx.put("executor_work", "token-cap-denied-work", {
            "mission_id": run["body"]["mission_id"],
            "phase": "superseded", "status_basis": "local_model_admission_blocked",
            "superseded_intent_ref": intent_ref,
            "receipt_ref": None, "result_ref": None,
            "spec": {"kind": "model", "call_id": call["id"],
                     "actor_id": call["body"]["instance_id"],
                     "proposal_ref": {"kind": "board_proposal",
                                      "id": call["body"]["task_id"], "version": 1},
                     "output_reservation_tokens": output_tokens},
        })
        return {"blocked_request_sha256": denied_sha}

    store.transact("synthetic-token-cap-denial", {}, save)
    advance_evaluation_observation(
        store, run_id, ref(prior), budget, "token_cap", 0.0,
    )
    return source, request


def _resume(tmp_path, source, request, *, grant=5_000):
    return runner._resume_source(
        tmp_path, source, request, "offline_test", model_limit=None,
        token_limit=20_000, retrieval_limit=0, deadline_seconds=1_800,
        accounting_only=False, until_complete=False,
        token_ceiling_increase=grant,
    )


def test_settled_token_cap_resume_preserves_attempts_and_needs_grant(
    tmp_path, monkeypatch,
):
    source, request = _token_stopped_case(tmp_path, monkeypatch)
    before = runner._state_digest(source / "state")
    budget = json.loads((source / "budget.json").read_text())
    with pytest.raises(ValueError, match="explicit ceiling increase"):
        _resume(tmp_path, source, request, grant=None)
    lineage = _resume(tmp_path, source, request)
    assert lineage["source_boundary"] == "clean_token_cap_stop"
    assert lineage["source_outcome"] == "token_cap"
    assert lineage["next_phase_limits"] == {
        "model": None, "tokens": 25_000 - budget["planned_tokens"],
        "retrieval": 0,
    }
    assert lineage["prior_cumulative"]["model_http_attempts"] == 1
    assert lineage["prior_cumulative"]["unknown_model_reservations"] == 0
    assert runner._state_digest(source / "state") == before


@pytest.mark.parametrize("tamper", [
    "unsettled", "uncertain", "missing_denial", "small_candidate",
    "wrong_intent", "wrong_work", "missing_receipt", "bad_blob",
    "wrong_output_cap",
])
def test_token_cap_resume_rejects_inconsistent_local_denial(
    tmp_path, monkeypatch, tamper,
):
    source, request = _token_stopped_case(tmp_path, monkeypatch)
    bad = tmp_path / ".local/search-validation" / f"token-cap-{tamper}"
    shutil.copytree(source, bad)
    if tamper in {"unsettled", "uncertain"}:
        path = bad / ("observation.json" if tamper == "unsettled" else "budget.json")
        data = json.loads(path.read_text())
        if tamper == "unsettled":
            data["dispatch_drain_status"] = "timed_out"
        else:
            data["model_attempts"][-1]["state"] = "uncertain"
        path.write_text(json.dumps(data))
    else:
        store = StateStore(bad / "state")
        kind, ident = {
            "missing_denial": ("model_dispatch_attempt", "token-cap-denied-dispatch"),
            "small_candidate": ("model_request_intent", "token-cap-denied-intent"),
            "wrong_intent": ("model_request_intent", "token-cap-denied-intent"),
            "wrong_work": ("executor_work", "token-cap-denied-work"),
            "missing_receipt": ("model_response_receipt", None),
            "bad_blob": ("model_request_intent", "token-cap-denied-intent"),
            "wrong_output_cap": ("executor_work", "token-cap-denied-work"),
        }[tamper]
        saved = (store.get(kind, ident) if ident else
                 store.records("model_response_receipt")[0])
        if tamper == "bad_blob":
            pointer = saved["body"]["blob"]
            blob_path = bad / "state/objects" / pointer["sha256"]
            blob_path.write_bytes(b"X" * pointer["size"])
        else:
            def mutate(tx):
                body = dict(saved["body"])
                if tamper == "small_candidate":
                    body["request_token_estimate"] = {"tokens": 1}
                elif tamper == "wrong_intent":
                    body["request_sha256"] = "0" * 64
                elif tamper == "wrong_work":
                    body["status_basis"] = "unrelated"
                elif tamper == "wrong_output_cap":
                    body["spec"] = {**body["spec"], "output_reservation_tokens":
                                    body["spec"]["output_reservation_tokens"] - 1}
                elif tamper == "missing_receipt":
                    body["response_sha256"] = "0" * 64
                else:
                    body["status"] = "uncertain"
                tx.put(kind, saved["id"], body, expected_version=saved["version"])
                return {"tamper": tamper}
            store.transact(f"tamper-{tamper}", {}, mutate)
    with pytest.raises(ValueError):
        _resume(tmp_path, bad, request)


def test_later_token_cap_keeps_inherited_unknown_and_two_peer_denials(
    tmp_path, monkeypatch,
) -> None:
    source, request, _policy = _stopped_observation(
        tmp_path, monkeypatch, prior_calls=2, uncertain_on_last=True,
    )
    unknown = json.loads((source / "budget.json").read_text())["model_attempts"][-1][
        "planned_tokens"
    ]

    phase_http_calls = []

    def one_synthetic_http(_handler, _class, outgoing, **_kwargs):
        phase_http_calls.append(1)
        if len(phase_http_calls) != 1:
            pytest.fail("The later candidate must be blocked before HTTP")
        payload = tool("discover_capabilities", {}, 3).payload
        payload["usage"] = {"prompt_tokens": 14_000,
                            "completion_tokens": 500, "total_tokens": 14_500}
        return response(outgoing, json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", one_synthetic_http)
    phase2 = tmp_path / ".local/search-validation/synthetic-two-peer-token-cap"
    from ora.retrieval.agent_validation_budget import AgentBudget

    state_init = StateStore.__init__
    budget_init = AgentBudget.__init__
    def aligned_store(self, *args, **kwargs):
        kwargs.setdefault("clock", lambda: runner.time.time())
        return state_init(self, *args, **kwargs)
    def aligned_budget(self, *args, **kwargs):
        kwargs.setdefault("clock", lambda: runner.time.time())
        return budget_init(self, *args, **kwargs)
    with monkeypatch.context() as clock:
        clock.setattr(StateStore, "__init__", aligned_store)
        clock.setattr(AgentBudget, "__init__", aligned_budget)
        result = runner.run_case(
            tmp_path, phase2, execution_mode="offline_test", start_request=request,
            resume_case=source, model_limit=None, token_limit=20_000,
            retrieval_limit=0, deadline_seconds=1_800, token_ceiling_increase=15_000,
        )
    assert result["outcome"] == "token_cap"
    assert len(phase_http_calls) == 1
    store = StateStore(phase2 / "state")
    run_id = json.loads((phase2 / "case.json").read_text())["run_id"]
    run = store.get("mission_run", run_id)
    control = store.records("mission_task_control")[0]
    marker = next(row for row in store.records("model_dispatch_attempt")
                  if row["body"].get("status") == "blocked_before_external_effect")
    intent = store.get(**marker["body"]["intent_ref"])
    call = store.get(**marker["body"]["call_ref"])
    work = next(row for row in store.records("executor_work")
                if row["body"].get("superseded_intent_ref") == ref(intent))
    capacity = store.get("capacity_operation", work["id"].replace(
        "executor-work:", "executor-cap:", 1))
    instance = store.get("agent_instance", call["body"]["instance_id"], 1)
    proposal = store.get("board_proposal", call["body"]["task_id"], 1)
    configuration = store.get("agent_configuration", call["body"]["configuration_id"], 1)
    packet = json.loads(store.read_blob(intent["body"]["blob"]["sha256"]))
    packet["messages"].append({"role": "user", "content": "Synthetic second peer work."})
    encoded = canonical(packet).encode()
    blob = store.put_blob(encoded)
    peer_sha = hashlib.sha256(encoded).hexdigest()
    peer_input = estimate_request_tokens(encoded).tokens
    actor_id = "synthetic-peer-actor"
    task_id = "synthetic-peer-task"
    call_id = "synthetic-peer-call"
    configuration_id = "synthetic-peer-configuration"
    configuration_ref = {"kind": "agent_configuration",
                         "id": configuration_id, "version": 1}
    intent_id = f"model-intent:{call_id}:1"
    intent_ref = {"kind": "model_request_intent", "id": intent_id, "version": 1}
    call_ref = {"kind": "agent_call", "id": call_id, "version": 1}

    def add_peer(tx):
        run_body = dict(run["body"])
        run_body["agent_ids"] = ["agent-synthesis", "agent-critique"]
        run_body["profile"] = {**run_body["profile"],
                               "agentIds": ["agent-synthesis", "agent-critique"]}
        tx.put("mission_run", run_id, run_body, expected_version=run["version"])
        tx.put("agent_instance", actor_id, {
            **instance["body"], "display_name": "Synthetic second peer",
            "role_id": "independent_methods_reviewer_critic",
        })
        tx.put("board_proposal", task_id, {
            **proposal["body"],
            "actor_ref": {"kind": "agent_instance", "id": actor_id, "version": 1},
        })
        peer_config = {**configuration["body"], "instance_id": actor_id}
        peer_config["configuration_sha256"] = hashlib.sha256(canonical({
            key: value for key, value in peer_config.items()
            if key != "configuration_sha256"
        }).encode()).hexdigest()
        tx.put("agent_configuration", configuration_id, peer_config)
        call_body = {**call["body"], "call_id": call_id,
                     "instance_id": actor_id, "task_id": task_id,
                     "configuration_id": configuration_id,
                     "configuration_sha256": peer_config["configuration_sha256"],
                     "attempt_id": "synthetic-peer-attempt"}
        tx.put("agent_call", call_id, call_body)
        intent_body = {**intent["body"], "attempt_id": "synthetic-peer-attempt",
                       "call_ref": call_ref, "instance_id": actor_id,
                       "configuration_ref": configuration_ref,
                       "configuration_sha256": peer_config["configuration_sha256"],
                       "task_id": task_id, "request_sha256": peer_sha, "blob": blob,
                       "request_token_estimate": {
                           **intent["body"]["request_token_estimate"],
                           "tokens": peer_input,
                       }}
        tx.put("model_request_intent", intent_id, intent_body)
        marker_body = {**marker["body"], "request_sha256": peer_sha,
                       "call_ref": call_ref, "intent_ref": intent_ref}
        tx.put("model_dispatch_attempt", f"model-dispatch:{intent_id}", marker_body)
        work_body = {**work["body"], "superseded_intent_ref": intent_ref,
                     "spec": {**work["body"]["spec"], "actor_id": actor_id,
                              "lane": actor_id,
                              "configuration_ref": configuration_ref,
                              "call_id": call_id,
                              "attempt_id": "synthetic-peer-attempt",
                              "work_id": "synthetic-peer-work",
                              "proposal_ref": {"kind": "board_proposal",
                                               "id": task_id, "version": 1}}}
        tx.put("executor_work", "executor-work:synthetic-peer", work_body)
        tx.put("capacity_operation", "executor-cap:synthetic-peer", {
            **capacity["body"], "call_id": call_id, "task_id": task_id,
            "lane": actor_id,
            "attempt_id": "synthetic-peer-attempt",
            "request_token_estimate": {
                **capacity["body"]["request_token_estimate"],
                "intent_ref": intent_ref, "request_sha256": peer_sha,
                "tokens": peer_input,
            },
        })
        control_body = {**control["body"], "actor_id": actor_id,
                        "alias": "agent-critique", "task_id": task_id,
                        "call_ref": call_ref, "phase": "ready_model", "work_id": None,
                        "proposal_ref": {"kind": "board_proposal",
                                         "id": task_id, "version": 1},
                        "origin_proposal_ref": {"kind": "board_proposal",
                                                "id": task_id, "version": 1}}
        tx.put("mission_task_control", "synthetic-peer-control", control_body)
        return {"second_peer_sha256": peer_sha}

    original_clock = store.clock
    store.clock = lambda: capacity["body"]["attempts"][0]["at"]
    try:
        store.transact("add-synthetic-blocked-peer", {}, add_peer)
    finally:
        store.clock = original_clock
    request = {**request, "agent_ids": ["agent-synthesis", "agent-critique"]}
    source_digest = runner._state_digest(phase2 / "state")

    def resume(case):
        return runner._resume_source(
            tmp_path, case, request, "offline_test", model_limit=None,
            token_limit=35_000, retrieval_limit=0, deadline_seconds=1_800,
            accounting_only=False, until_complete=False,
            token_ceiling_increase=5_000,
        )

    lineage = resume(phase2)
    assert lineage["source_boundary"] == "clean_token_cap_stop"
    assert lineage["prior_cumulative"]["unknown_model_reservations"] == unknown
    assert lineage["prior_cumulative"]["model_http_attempts"] == 3
    assert runner._state_digest(phase2 / "state") == source_digest

    bad_marker = tmp_path / ".local/search-validation/synthetic-missing-peer-marker"
    shutil.copytree(phase2, bad_marker, symlinks=True)
    broken = StateStore(bad_marker / "state")
    saved = broken.get("model_dispatch_attempt", f"model-dispatch:{intent_id}")
    def break_marker(tx):
        tx.put("model_dispatch_attempt", saved["id"],
               {**saved["body"], "status": "captured"},
               expected_version=saved["version"])
        return {"tamper": "captured_without_response"}
    broken.transact("tamper-synthetic-peer-marker", {}, break_marker)
    with pytest.raises(ValueError):
        resume(bad_marker)

    bad_reservation = tmp_path / ".local/search-validation/synthetic-lost-reservation"
    shutil.copytree(phase2, bad_reservation, symlinks=True)
    ledger_path = bad_reservation / "cumulative-budget.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["model_attempts"][-1]["planned_tokens"] -= 1
    ledger["planned_tokens"] -= 1
    ledger_path.write_text(json.dumps(ledger))
    with pytest.raises(ValueError):
        resume(bad_reservation)


def test_two_serial_clean_token_caps_bind_each_denial_to_its_own_phase(
    tmp_path, monkeypatch,
) -> None:
    """A later grant must retain both finite local denials without inventing HTTP."""
    project(tmp_path, monkeypatch)
    from ora.retrieval.agent_validation_budget import AgentBudget

    state_init = StateStore.__init__
    budget_init = AgentBudget.__init__
    def aligned_store(self, *args, **kwargs):
        kwargs.setdefault("clock", lambda: runner.time.time())
        return state_init(self, *args, **kwargs)
    def aligned_budget(self, *args, **kwargs):
        kwargs.setdefault("clock", lambda: runner.time.time())
        return budget_init(self, *args, **kwargs)
    monkeypatch.setattr(StateStore, "__init__", aligned_store)
    monkeypatch.setattr(AgentBudget, "__init__", aligned_budget)
    request = {
        "request_id": "synthetic-serial-clean-token-cap",
        "question": "Interpret the supplied synthetic observation and state its limit.",
        "scope": "Use only the supplied synthetic observation.",
        "evidence_text": "Synthetic observation: one site read 3 units.",
        "agent_ids": ["agent-synthesis"],
        "credential_profile": "primary", "allow_vision": False,
    }
    calls = []

    def injected_http(_handler, _class, outgoing, **_kwargs):
        calls.append(outgoing)
        if len(calls) > 3:
            pytest.fail("Each finite phase should stop locally before another HTTP")
        payload = tool("discover_capabilities", {}, len(calls)).payload
        prompt = (11_000 if len(calls) == 1 else
                  14_000 if len(calls) == 2 else 25_000)
        payload["usage"] = {"prompt_tokens": prompt,
                            "completion_tokens": 1_000,
                            "total_tokens": prompt + 1_000}
        return response(outgoing, json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", injected_http)
    first = tmp_path / ".local/search-validation/serial-clean-cap-1"
    common = dict(execution_mode="offline_test", start_request=request,
                  model_limit=None, retrieval_limit=0, deadline_seconds=30,
                  accounting_only=False, until_complete=False)
    first_result = runner.run_case(
        tmp_path, first, token_limit=20_000, **common,
    )
    assert first_result["outcome"] == "token_cap"
    assert len(calls) == 1

    second = tmp_path / ".local/search-validation/serial-clean-cap-2"
    second_result = runner.run_case(
        tmp_path, second, resume_case=first, token_limit=20_000,
        token_ceiling_increase=20_000, **common,
    )
    assert second_result["outcome"] == "token_cap"
    assert len(calls) == 2
    saved = json.loads((second / "observation.json").read_text())
    assert len(saved["controller_segments"]) == 2
    cumulative = json.loads((second / "cumulative-budget.json").read_text())
    assert cumulative["limits"]["tokens"] == 40_000
    assert cumulative["planned_tokens"] == 27_000
    store = StateStore(second / "state")
    blocked = [item for item in store.records("model_dispatch_attempt")
               if item["body"].get("status") == "blocked_before_external_effect"]
    assert len(blocked) == 2
    def denied_records(marker):
        intent = store.get(**marker["body"]["intent_ref"])
        work = next(item for item in store.records("executor_work")
                    if item["body"].get("superseded_intent_ref") == ref(intent))
        capacity = store.get("capacity_operation", work["id"].replace(
            "executor-work:", "executor-cap:", 1))
        return intent, work, capacity
    blocked.sort(key=lambda item: denied_records(item)[2]["body"]["attempts"][0]["at"])
    first_at = denied_records(blocked[0])[2]["body"]["attempts"][0]["at"]
    second_at = denied_records(blocked[1])[2]["body"]["attempts"][0]["at"]
    segments = saved["controller_segments"]
    assert segments[0]["started_at"] <= first_at <= (
        segments[0]["started_at"] + segments[0]["elapsed_seconds"])
    assert segments[1]["started_at"] <= second_at <= (
        segments[1]["started_at"] + segments[1]["elapsed_seconds"])
    source_digest = runner._state_digest(second / "state")

    def resume(case, *, original_limit=40_000):
        return runner._resume_source(
            tmp_path, case, request, "offline_test", model_limit=None,
            token_limit=original_limit, retrieval_limit=0, deadline_seconds=30,
            accounting_only=False, until_complete=False,
            token_ceiling_increase=10_000,
        )

    lineage = resume(second)
    assert lineage["source_boundary"] == "clean_token_cap_stop"
    assert lineage["prior_cumulative"]["model_http_attempts"] == 2
    assert lineage["prior_cumulative"]["unknown_model_reservations"] == 0
    assert lineage["next_phase_limits"] == {
        "model": None, "tokens": 50_000 - cumulative["planned_tokens"],
        "retrieval": 0,
    }
    assert runner._state_digest(second / "state") == source_digest

    third = tmp_path / ".local/search-validation/serial-clean-cap-3"
    third_result = runner.run_case(
        tmp_path, third, resume_case=second, token_limit=40_000,
        token_ceiling_increase=20_000, **common,
    )
    assert third_result["outcome"] == "token_cap"
    assert len(calls) == 3
    third_cumulative = json.loads((third / "cumulative-budget.json").read_text())
    assert third_cumulative["limits"]["tokens"] == 60_000
    assert third_cumulative["planned_tokens"] == 53_000
    third_observation = json.loads((third / "observation.json").read_text())
    assert len(third_observation["controller_segments"]) == 3
    third_store = StateStore(third / "state")
    assert sum(item["body"].get("status") == "blocked_before_external_effect"
               for item in third_store.records("model_dispatch_attempt")) == 3
    third_digest = runner._state_digest(third / "state")
    fourth_lineage = runner._resume_source(
        tmp_path, third, request, "offline_test", model_limit=None,
        token_limit=60_000, retrieval_limit=0, deadline_seconds=30,
        accounting_only=False, until_complete=False,
        token_ceiling_increase=10_000,
    )
    assert fourth_lineage["source_boundary"] == "clean_token_cap_stop"
    assert fourth_lineage["prior_cumulative"]["model_http_attempts"] == 3
    assert fourth_lineage["prior_cumulative"]["unknown_model_reservations"] == 0
    assert fourth_lineage["next_phase_limits"] == {
        "model": None, "tokens": 70_000 - third_cumulative["planned_tokens"],
        "retrieval": 0,
    }
    assert runner._state_digest(third / "state") == third_digest

    bad_history = tmp_path / ".local/search-validation/serial-clean-bad-history"
    shutil.copytree(third, bad_history, symlinks=True)
    with sqlite3.connect(bad_history / "state/state.sqlite3") as db:
        row = db.execute(
            "SELECT body FROM records WHERE kind='evaluation_observation' "
            "AND version=1"
        ).fetchone()
        assert row is not None
        body = json.loads(row[0])
        body["ledger_sha256"] = "0" * 64
        db.execute(
            "UPDATE records SET body=? WHERE kind='evaluation_observation' "
            "AND version=1", (json.dumps(body),),
        )
    with pytest.raises(ValueError, match="exact active run"):
        runner._resume_source(
            tmp_path, bad_history, request, "offline_test", model_limit=None,
            token_limit=60_000, retrieval_limit=0, deadline_seconds=30,
            accounting_only=False, until_complete=False,
            token_ceiling_increase=10_000,
        )

    with pytest.raises(ValueError, match="Resume request"):
        resume(second, original_limit=20_000)

    bad_time = tmp_path / ".local/search-validation/serial-clean-bad-time"
    shutil.copytree(second, bad_time, symlinks=True)
    changed = StateStore(bad_time / "state")
    old_capacity = changed.get("capacity_operation", denied_records(blocked[0])[2]["id"])
    attempts = [dict(item) for item in old_capacity["body"]["attempts"]]
    attempts[0]["at"] = segments[1]["started_at"] + 1
    changed.transact("move-old-denial-into-new-phase", {}, lambda tx: tx.put(
        "capacity_operation", old_capacity["id"],
        {**old_capacity["body"], "attempts": attempts},
        expected_version=old_capacity["version"],
    ))
    with pytest.raises(ValueError, match="exact active run"):
        resume(bad_time)

    bad_finalization = tmp_path / ".local/search-validation/serial-clean-bad-finalization"
    shutil.copytree(second, bad_finalization, symlinks=True)
    changed = StateStore(bad_finalization / "state")
    old_capacity = changed.get("capacity_operation", denied_records(blocked[0])[2]["id"])
    attempts = [dict(item) for item in old_capacity["body"]["attempts"]]
    assert segments[0]["elapsed_seconds"] > segments[0]["controller_elapsed_seconds"]
    attempts[0]["at"] = segments[0]["started_at"] + (
        segments[0]["controller_elapsed_seconds"] +
        segments[0]["elapsed_seconds"]
    ) / 2
    changed.transact("move-old-denial-after-controller-stop", {}, lambda tx: tx.put(
        "capacity_operation", old_capacity["id"],
        {**old_capacity["body"], "attempts": attempts},
        expected_version=old_capacity["version"],
    ))
    with pytest.raises(ValueError, match="exact active run"):
        resume(bad_finalization)

    bad_marker_time = tmp_path / ".local/search-validation/serial-clean-bad-marker-time"
    shutil.copytree(second, bad_marker_time, symlinks=True)
    marker_at = segments[0]["started_at"] + (
        segments[0]["controller_elapsed_seconds"] +
        segments[0]["elapsed_seconds"]
    ) / 2
    with sqlite3.connect(bad_marker_time / "state/state.sqlite3") as db:
        db.execute(
            "UPDATE records SET created_at=? WHERE kind='model_dispatch_attempt' "
            "AND id=? AND version=1",
            (datetime.fromtimestamp(marker_at, timezone.utc).isoformat(),
             blocked[0]["id"]),
        )
    with pytest.raises(ValueError, match="exact active run"):
        resume(bad_marker_time)

    bad_actor = tmp_path / ".local/search-validation/serial-clean-bad-actor"
    shutil.copytree(second, bad_actor, symlinks=True)
    changed = StateStore(bad_actor / "state")
    old_intent, old_work, _ = denied_records(blocked[0])
    call = changed.get(**blocked[0]["body"]["call_ref"])
    work = changed.get("executor_work", old_work["id"])
    def drift_actor(tx):
        tx.put("agent_call", call["id"],
               {**call["body"], "instance_id": "agent-other-actor"},
               expected_version=call["version"])
        return tx.put("executor_work", work["id"],
                      {**work["body"], "spec": {
                          **work["body"]["spec"], "actor_id": "agent-other-actor",
                      }}, expected_version=work["version"])
    changed.transact("drift-old-denial-actor", {}, drift_actor)
    with pytest.raises(ValueError, match="exact active run"):
        resume(bad_actor)
