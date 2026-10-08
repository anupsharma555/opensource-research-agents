"""Offline checks before the bounded selected-model calls."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("skill_fit_run", ROOT / "experiments/skill_fit/run.py")
assert SPEC and SPEC.loader
fit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fit)


def test_four_fixed_selected_model_requests() -> None:
    assert len(fit.CASES) == 4
    for case_id in fit.CASES:
        request = fit.request_for(case_id)
        assert request["model"] == "openai/gpt-oss-120b"
        assert request["reasoning_effort"] == "high"
        assert request["include_reasoning"] is False
        assert request["max_completion_tokens"] == 4096
        assert "tools" not in request
        assert "response_format" not in request


def test_matched_inputs_and_withheld_reference() -> None:
    for a, b in (("X1", "X2"), ("X3", "X4")):
        left = fit.request_for(a)
        right = fit.request_for(b)
        assert left["messages"][1] == right["messages"][1]
        assert left["messages"][0] != right["messages"][0]
    for case_id in fit.CASES:
        serialized = json.dumps(fit.request_for(case_id))
        assert "withheld_marker" not in serialized
        assert "reference_judgment" not in serialized
        assert "GROQ_API_KEY" not in serialized


def test_plan_cost_and_hashes() -> None:
    plan = fit.plan()
    assert plan["initial_call_count"] == 4
    assert 0 < plan["worst_case_usd"] < 1
    assert len({case["request_sha256"] for case in plan["cases"]}) == 4
    assert all(len(case["prompt_sha256"]) == 64 for case in plan["cases"])


def test_appraisal_input_is_only_a1() -> None:
    request = fit.request_for("X3")
    packet = json.loads(request["messages"][1]["content"].split("Synthetic source packet:\n", 1)[1])
    assert [source["source_id"] for source in packet["sources"]] == ["A1"]


def test_one_attempt_discards_reasoning_and_refuses_repeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return json.dumps({
                "model": fit.MODEL,
                "choices": [{"finish_reason": "stop", "message": {
                    "content": "Synthetic final answer", "reasoning": "private reasoning must be discarded"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
            }).encode()

    captured = []

    def fake_urlopen(request, timeout):
        captured.append((request.full_url, timeout, request.get_header("Authorization")))
        return FakeResponse()

    monkeypatch.setattr(fit, "OUT", tmp_path)
    monkeypatch.setattr(fit, "_key", lambda: "synthetic-test-key")
    monkeypatch.setattr(fit.urllib.request, "urlopen", fake_urlopen)
    summary = fit.run_one("X1")
    assert summary["reasoning_returned"] is True
    assert "final_content" not in summary
    assert captured == [(fit.URL, 120, "Bearer synthetic-test-key")]
    saved = (tmp_path / "X1.json").read_text()
    assert "Synthetic final answer" in saved
    assert "private reasoning must be discarded" not in saved
    assert "synthetic-test-key" not in saved
    with pytest.raises(RuntimeError, match="already been attempted"):
        fit.run_one("X1")
    assert len(captured) == 1
