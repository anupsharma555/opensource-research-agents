"""A prior local token denial survives a later captured daily refusal."""

from __future__ import annotations

import json
import shutil
import sqlite3
import urllib.error
import urllib.request

import pytest

from ora.retrieval.agent_validation_budget import AgentBudget
from ora.state import StateStore
from tests.benchmarks.test_search_agent_runner import project, response, runner
from tests.mission.test_service import tool


def _two_phase_case(tmp_path, monkeypatch):
    project(tmp_path, monkeypatch)
    request = {
        "request_id": "synthetic-cap-then-daily-refusal",
        "question": "Summarize a synthetic observation and its uncertainty.",
        "scope": "Use only the supplied synthetic observation.",
        "evidence_text": "Synthetic observation: one site recorded 3 units.",
        "agent_ids": ["agent-synthesis"], "credential_profile": "primary",
        "allow_vision": False,
    }
    sent = []

    def injected_http(_handler, _class, outgoing, **_kwargs):
        sent.append(outgoing)
        if len(sent) == 1:
            payload = tool("discover_capabilities", {}, 1).payload
            payload["usage"] = {
                "prompt_tokens": 11_000, "completion_tokens": 1_000,
                "total_tokens": 12_000,
            }
            return response(outgoing, json.dumps(payload).encode())
        if len(sent) == 2:
            refusal = response(outgoing, json.dumps({"error": {
                "type": "tokens", "code": "rate_limit_exceeded",
                "message": "Synthetic tokens per day (TPD): Limit 200000, Used 199000, Requested 6000",
            }}).encode(), status=429)
            refusal.headers["retry-after"] = "1"
            refusal.headers["x-ratelimit-limit-tokens"] = "8000"
            return refusal
        pytest.fail("Daily refusal must stop before a second phase-two HTTP request")

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", injected_http)
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
    common = dict(
        execution_mode="offline_test", start_request=request, model_limit=None,
        retrieval_limit=0, deadline_seconds=30, accounting_only=False,
        until_complete=False,
    )
    first = tmp_path / ".local/search-validation/cap-then-daily-first"
    first_result = runner.run_case(tmp_path, first, token_limit=20_000, **common)
    assert first_result["outcome"] == "token_cap"
    assert len(sent) == 1
    first_store = StateStore(first / "state")
    assert sum(item["body"].get("status") == "blocked_before_external_effect"
               for item in first_store.records("model_dispatch_attempt")) == 1
    assert json.loads((first / "budget.json").read_text())["planned_tokens"] == 12_000

    original_open = AgentBudget.open

    def stop_after_captured_daily(self, *args, **kwargs):
        try:
            return original_open(self, *args, **kwargs)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                error = json.loads(exc.fp.getvalue())["error"]
                assert error["type"] == "tokens"
                assert error["code"] == "rate_limit_exceeded"
                assert "TPD" in error["message"]
                with self._mutex:
                    self.data["stop_reason"] = "observation_window_ended"
                    self._save()
            raise

    monkeypatch.setattr(AgentBudget, "open", stop_after_captured_daily)
    second = tmp_path / ".local/search-validation/cap-then-daily-second"
    second_result = runner.run_case(
        tmp_path, second, resume_case=first, token_limit=20_000,
        token_ceiling_increase=20_000, **common,
    )
    assert second_result["outcome"] == "observation_window_ended"
    assert second_result["dispatch_drain_status"] == "settled"
    assert len(sent) == 2
    budget = json.loads((second / "cumulative-budget.json").read_text())
    assert len(budget["model_attempts"]) == 2
    daily = budget["model_attempts"][-1]
    assert daily["status"] == 429 and daily["state"] == "rate_limited"
    assert daily.get("actual_tokens") is None
    assert daily["usage_status"] == "unreported_reservation_retained"
    assert budget["planned_tokens"] == 12_000 + daily["planned_tokens"]
    second_store = StateStore(second / "state")
    assert sum(item["body"].get("status") == "blocked_before_external_effect"
               for item in second_store.records("model_dispatch_attempt")) == 1
    return second, request


def _admit(tmp_path, case, request):
    return runner._resume_source(
        tmp_path, case, request, "offline_test", model_limit=None,
        token_limit=40_000, retrieval_limit=0, deadline_seconds=30,
        accounting_only=False, until_complete=False,
    )


def test_cap_then_daily_refusal_preserves_local_denial_and_unknown_reservation(
    tmp_path, monkeypatch,
):
    second, request = _two_phase_case(tmp_path, monkeypatch)
    before = runner._state_digest(second / "state")
    lineage = _admit(tmp_path, second, request)
    assert lineage["source_boundary"] == "clean_observation_stop"
    assert lineage["terminal_observation_429"] is True
    assert lineage["prior_cumulative"]["model_http_attempts"] == 2
    assert lineage["prior_cumulative"]["unknown_model_reservations"] > 0
    assert runner._state_digest(second / "state") == before


@pytest.mark.parametrize("tamper", ["missing_daily_receipt", "old_denial_in_new_phase"])
def test_cap_then_daily_refusal_rejects_broken_saved_lineage(
    tmp_path, monkeypatch, tamper,
):
    second, request = _two_phase_case(tmp_path, monkeypatch)
    bad = tmp_path / ".local/search-validation" / f"cap-then-daily-{tamper}"
    shutil.copytree(second, bad, symlinks=True)
    store = StateStore(bad / "state")
    if tamper == "missing_daily_receipt":
        receipt = next(item for item in store.records("model_response_receipt")
                       if item["body"].get("http_status") == 429)
        with sqlite3.connect(bad / "state/state.sqlite3") as db:
            db.execute(
                "DELETE FROM heads WHERE kind='model_response_receipt' AND id=?",
                (receipt["id"],),
            )
    else:
        marker = next(item for item in store.records("model_dispatch_attempt")
                      if item["body"].get("status") == "blocked_before_external_effect")
        work = next(item for item in store.records("executor_work")
                    if item["body"].get("superseded_intent_ref") == marker["body"]["intent_ref"])
        cap_id = work["id"].replace("executor-work:", "executor-cap:", 1)
        capacity = store.get("capacity_operation", cap_id)
        segments = json.loads((bad / "observation.json").read_text())["controller_segments"]
        attempts = [dict(item) for item in capacity["body"]["attempts"]]
        attempts[0]["at"] = segments[1]["started_at"] + 1

        def move_denial(tx):
            return tx.put("capacity_operation", cap_id, {
                **capacity["body"], "attempts": attempts,
            }, expected_version=capacity["version"])

        store.transact("move-old-denial-to-daily-phase", {}, move_denial)
    with pytest.raises(ValueError, match="exact active run"):
        _admit(tmp_path, bad, request)
