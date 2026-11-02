"""Offline runner checks for explicit per-agent model choices and replay drift."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import urllib.request
from types import SimpleNamespace

import pytest

from ora.state import StateStore
from tests.benchmarks.test_search_agent_runner import project, response, runner
from tests.mission.test_service import answer, tool

QWEN = "qwen/qwen3.8-27b"
GPT_OSS = "openai/gpt-oss-120b"


def _request():
    return {
        "request_id": "synthetic-runner-per-agent-models",
        "question": "Summarize the supplied synthetic note.",
        "evidence_text": "Synthetic note: one bounded observation was recorded.",
        "agent_ids": ["agent-critique"], "credential_profile": "primary",
        "agent_models": {"agent-critique": {
            "model": QWEN, "reasoning_effort": "medium",
            "max_completion_tokens": 512,
        }},
    }


def test_explicit_agent_models_preview_is_read_only(tmp_path, monkeypatch, capsys):
    request = _request()
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"start_request": request}))
    output = tmp_path / "no-call"
    monkeypatch.setattr(runner, "run_case", lambda *_a, **_kw: pytest.fail("live invoked"))
    assert runner.main(["--start-request", str(path), "--output", str(output)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["live"] is False and preview["agent_models"] == request["agent_models"]
    assert not output.exists()


def test_followup_budget_opt_in_comes_from_saved_parent_catalog(tmp_path):
    store = StateStore(tmp_path)
    for run_id, model in (("run-qwen-parent", QWEN), ("run-legacy-parent", GPT_OSS)):
        store.transact(f"save-{run_id}", {}, lambda tx, name=run_id, selected=model:
                       tx.put("mission_run", name, {"profile": {"agentModelBindings": {
                           "agent-critique": {"model": selected},
                       }}}))
    assert runner._parent_catalog_uses_qwen(tmp_path, "run-qwen-parent") is True
    assert runner._parent_catalog_uses_qwen(tmp_path, "run-legacy-parent") is False


def test_runner_pins_mixed_catalog_and_rejects_changed_resume_map(
    tmp_path, monkeypatch,
):
    project(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    sent = []

    def http(_handler, _class, outgoing, **_kwargs):
        assert outgoing.full_url == "https://api.groq.com/openai/v1/chat/completions"
        packet = json.loads(outgoing.data)
        sent.append(packet)
        payload = answer("The synthetic note reports one observation.", len(sent)).payload
        payload["model"] = packet["model"]
        return response(outgoing, json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", http)
    request = _request()
    folder = tmp_path / ".local/search-validation/mixed-models"
    result = runner.run_case(
        tmp_path, folder, execution_mode="offline_test", start_request=request,
        model_limit=1, token_limit=100_000, retrieval_limit=0, deadline_seconds=8,
    )
    assert result["model_attempts"] == len(sent) == 1
    assert result["retrieval_attempts"] == 0
    assert sent[0]["model"] == QWEN
    assert [item["role"] for item in sent[0]["messages"][:2]] == ["system", "user"]
    case = json.loads((folder / "case.json").read_text())
    assert case["agent_models"] == request["agent_models"]
    assert case["model_selection"] == "per_agent"
    assert case["agent_model_bindings"]["agent-critique"] == {
        "model": QWEN, "reasoningEffort": "medium", "maxCompletionTokens": 512,
    }
    assert case["agent_model_bindings"]["agent-synthesis"]["model"] == GPT_OSS
    assert case["qwen_estimate_policy"] == "native_when_supported"
    budget = json.loads((folder / "budget.json").read_text())
    assert budget["limits"]["allow_qwen_text"] is True
    assert budget["limits"]["qwen_estimate_policy"] == "native_when_supported"
    assert budget["model_attempts"][0]["model"] == QWEN
    changed = json.loads(json.dumps(request))
    changed["agent_models"]["agent-critique"]["model"] = GPT_OSS
    with pytest.raises(ValueError, match="Resume request, model or accounting mode differs"):
        runner._resume_source(
            tmp_path, folder, changed, "offline_test", model_limit=1,
            token_limit=100_000, retrieval_limit=0, deadline_seconds=8,
            accounting_only=False, until_complete=False,
        )


def test_qwen_native_policy_survives_clean_resume_without_repricing(
    tmp_path, monkeypatch,
):
    project(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    request = _request()
    source = tmp_path / ".local/search-validation/qwen-native-source"
    first_sent = []

    def first_http(_handler, _class, outgoing, **_kwargs):
        first_sent.append(json.loads(outgoing.data))
        time.sleep(.15)
        payload = tool("discover_capabilities", {}, len(first_sent)).payload
        payload["model"] = QWEN
        return response(outgoing, json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", first_http)
    actual_time = time.time
    with monkeypatch.context() as clock:
        clock.setattr(runner, "time", SimpleNamespace(
            time=lambda: actual_time() + (2000 if first_sent else 0),
            sleep=time.sleep,
        ))
        stopped = runner.run_case(
            tmp_path, source, execution_mode="offline_test", start_request=request,
            model_limit=None, token_limit=30_000, retrieval_limit=0,
            deadline_seconds=1800,
        )
    assert stopped["outcome"] == "observation_window_ended"
    assert stopped["dispatch_drain_status"] == "settled"
    assert len(first_sent) == 1
    case = json.loads((source / "case.json").read_text())
    budget = json.loads((source / "budget.json").read_text())
    assert case["qwen_estimate_policy"] == "native_when_supported"
    assert budget["limits"]["qwen_estimate_policy"] == "native_when_supported"
    assert budget["model_attempts"][0]["token_estimate_basis"].startswith("ora.qwen38_27b_official")
    original = {name: (source / name).read_bytes() for name in (
        "case.json", "budget.json", "observation.json", "source-manifest.json",
    )}
    digest = runner._state_digest(source / "state")
    state_files = {str(path.relative_to(source / "state")): hashlib.sha256(
        path.read_bytes()).hexdigest() for path in (source / "state").rglob("*") if path.is_file()}
    options = {"model_limit": None, "token_limit": 30_000,
               "retrieval_limit": 0, "deadline_seconds": 1800,
               "accounting_only": False, "until_complete": False}
    resumed_sent = []

    def second_http(_handler, _class, outgoing, **_kwargs):
        packet = json.loads(outgoing.data)
        resumed_sent.append(packet)
        payload = answer("The synthetic note reports one observation.", 2).payload
        payload["model"] = packet["model"]
        return response(outgoing, json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", second_http)
    target = tmp_path / ".local/search-validation/qwen-native-resumed"
    continued = runner.run_case(
        tmp_path, target, execution_mode="offline_test", start_request=request,
        resume_case=source, **options,
    )
    assert continued["resume_integrity_complete"] is True
    assert continued["resume_lineage"]["qwen_estimate_policy"] == "native_when_supported"
    assert continued["resume_lineage"]["allow_qwen_text"] is True
    assert len(resumed_sent) == 1
    phase = json.loads((target / "budget.json").read_text())
    cumulative = json.loads((target / "cumulative-budget.json").read_text())
    assert phase["limits"]["qwen_estimate_policy"] == "native_when_supported"
    assert cumulative["limits"]["qwen_estimate_policy"] == "native_when_supported"
    assert cumulative["model_attempts"][0] == budget["model_attempts"][0]
    assert cumulative["planned_tokens"] == budget["planned_tokens"] + phase["planned_tokens"]
    changed_files = {str(path.relative_to(source / "state")): hashlib.sha256(
        path.read_bytes()).hexdigest() for path in (source / "state").rglob("*")
        if path.is_file() and state_files.get(str(path.relative_to(source / "state")))
        != hashlib.sha256(path.read_bytes()).hexdigest()}
    assert runner._state_digest(source / "state") == digest, changed_files
    assert all((source / name).read_bytes() == value for name, value in original.items())

    for label, change in (
        ("missing-case-policy", "case"), ("missing-budget-policy", "budget"),
    ):
        tampered = tmp_path / f".local/search-validation/qwen-{label}"
        shutil.copytree(source, tampered)
        path = tampered / ("case.json" if change == "case" else "budget.json")
        saved = json.loads(path.read_text())
        if change == "case":
            saved.pop("qwen_estimate_policy")
        else:
            saved["limits"].pop("qwen_estimate_policy")
        path.write_text(json.dumps(saved))
        with pytest.raises(ValueError, match="limits disagree"):
            runner._resume_source(tmp_path, tampered, request, "offline_test", **options)
