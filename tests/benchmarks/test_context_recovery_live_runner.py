"""No-call plan and injected transport checks for the isolated live driver."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import urllib.request

import pytest

from ora.credentials import GroqCredentialProfiles
from ora.models.groq import MODEL, URL
from tests.mission.test_service import ROOT, answer, tool

SPEC = importlib.util.spec_from_file_location("recovery_live", ROOT / "scripts/qualify_context_recovery_live.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _transport(request, *, timeout):
    packet = json.loads(request.data)
    def feedback(value):
        if isinstance(value, dict):
            if isinstance(value.get("host_feedback"), dict):
                return value["host_feedback"]
            for item in value.values():
                if found := feedback(item):
                    return found
        elif isinstance(value, list):
            for item in value:
                if found := feedback(item):
                    return found
        return None
    event = None
    for message in reversed(packet["messages"]):
        try:
            event = feedback(json.loads(message.get("content") or "null"))
        except (ValueError, TypeError):
            continue
        if event:
            break
    assert event, packet["messages"]
    if action := event.get("next_requested_action"):
        return tool(action["name"], action["arguments"], hashlib.sha256(request.data).hexdigest()[:12])
    passages = [pointer for message in packet["messages"]
                if message.get("role") == "user" and message.get("content", "").startswith("{")
                for source in json.loads(message["content"]).get("source_packets", [])
                for pointer in source.get("passage_refs", [])]
    return answer("Synthetic bounded comparison. " + " ".join(f"[{p['id']}]" for p in passages), 2)


def _enabled(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-offline-key")
    monkeypatch.setattr(GroqCredentialProfiles, "is_enabled", lambda *_: True)
    monkeypatch.setattr(GroqCredentialProfiles, "is_run_enabled", lambda *_: True)


def test_guided_runner_exercises_real_host_with_injected_transport(tmp_path, monkeypatch):
    _enabled(monkeypatch)
    selected = runner.plan("synthetic-guided-smoke", "compacted_recovery", "primary", 5)
    with runner.BatchBudget(tmp_path / "ledger.json", 5, "primary", transport=_transport) as budget:
        score = runner.run_case(tmp_path / "case", selected, budget, transport_mode="offline_test")
        assert budget.calls == 4
        assert score["mechanical_pass"], score["checks"]
        assert score["execution_mode"] == "offline_test"
        assert not score["qualified"]
        assert score["semantic_review"] == "pending_or_not_passed"
    assert (tmp_path / "case/selection.json").is_file()
    assert (tmp_path / "case/state/state.sqlite3").is_file()


def test_mismatch_stops_before_unapproved_tool_or_second_model_call(tmp_path, monkeypatch):
    _enabled(monkeypatch)
    def divergent(request, *, timeout):
        return tool("search_scholarly", {"route": "europe_pmc.search", "query": "synthetic", "limit": 1}, 1)
    ledger = tmp_path / "ledger.json"
    with runner.BatchBudget(ledger, 5, "primary", transport=divergent) as budget:
        with pytest.raises(runner.QualificationStop, match="tool choice"):
            runner.run_case(tmp_path / "case", runner.plan(
                "synthetic-divergent-smoke", "compacted_recovery", "primary", 5,
            ), budget, transport_mode="offline_test")
        assert budget.calls == 1
        assert budget.data["stop_reason"]
    with pytest.raises(runner.QualificationStop, match="already stopped"):
        with runner.BatchBudget(ledger, 5, "primary", transport=divergent):
            pass
    assert (tmp_path / "case/stopped.json").is_file()


def test_budget_counts_failed_transport_and_rejects_retry(tmp_path):
    attempts = []
    def failed(request, *, timeout):
        attempts.append(request)
        raise OSError("synthetic failure")
    request = urllib.request.Request(URL, data=json.dumps({
        "model": MODEL, "reasoning_effort": "high", "max_completion_tokens": 8192,
    }).encode())
    with runner.BatchBudget(tmp_path / "ledger.json", 1, "primary", transport=failed) as budget:
        with pytest.raises(OSError):
            budget.transport(request, timeout=1)
        with pytest.raises(runner.QualificationStop):
            budget.transport(request, timeout=1)
        assert len(attempts) == budget.calls == 1


def test_plan_is_read_only_and_run_holds_are_preserved(tmp_path, monkeypatch):
    result = subprocess.run([
        str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/qualify_context_recovery_live.py"),
        "--condition", "compacted_recovery", "--request-id", "synthetic-plan",
        "--credential-profile", "primary", "--max-calls", "5",
        "--batch-ledger", str(tmp_path / "ledger.json"), "--output-dir", str(tmp_path / "case"),
    ], cwd=ROOT, capture_output=True, text=True, check=True)
    planned = json.loads(result.stdout)
    assert planned["run_id"] == runner.plan("synthetic-plan", "compacted_recovery", "primary", 5)["run_id"]
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(GroqCredentialProfiles, "is_run_enabled", lambda *_: False)
    with runner.BatchBudget(tmp_path / "ledger.json", 5, "primary") as budget:
        with pytest.raises(runner.QualificationStop, match="held"):
            runner.run_case(tmp_path / "case", planned, budget)
        assert budget.calls == 0
    assert not (tmp_path / "case").exists()


def test_local_reader_rejection_stops_before_next_provider_call(tmp_path, monkeypatch):
    import ora.mission.service as mission_module
    from ora.evidence import EvidenceError

    _enabled(monkeypatch)
    def rejected(*args, **kwargs):
        raise EvidenceError("Synthetic local reader rejection")
    monkeypatch.setattr(mission_module, "read_source_window", rejected)
    with runner.BatchBudget(tmp_path / "ledger.json", 5, "primary", transport=_transport) as budget:
        with pytest.raises(runner.QualificationStop, match="Local reader rejected"):
            runner.run_case(tmp_path / "case", runner.plan(
                "synthetic-local-rejection", "compacted_recovery", "primary", 5,
            ), budget, transport_mode="offline_test")
        assert budget.calls == 1
        assert budget.data["stop_reason"]
