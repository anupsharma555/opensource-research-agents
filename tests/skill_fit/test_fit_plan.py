"""Check the four planned skill prompts before spending on model calls.

The tests confirm that paired variants see matched synthetic sources, answer
references stay hidden, and the estimated cost is bounded. A fake network
response checks the one-call result path without contacting the provider.
"""

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
        assert request["max_completion_tokens"] == 8192
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
    packet = json.loads(request["messages"][1]["content"].split("Source packet (simulated data):\n", 1)[1])
    assert [source["source_id"] for source in packet["sources"]] == ["A1"]


def test_peer_revision_preserves_sources_and_selected_model() -> None:
    request = fit.request_for("X1")
    prior = {"schema": "ora.skill_fit.result.v1", "blind_id": "X1", "model_requested": fit.MODEL,
             "model_returned": fit.MODEL,
             "request_sha256": fit.digest(request), "finish_reason": "stop",
             "final_content": "Synthetic earlier answer",
             "final_content_sha256": fit.digest("Synthetic earlier answer")}
    revised = fit.revision_request("X1", prior, "Check the interpretation against the counts.")
    assert revised["messages"][:2] == request["messages"]
    assert [m["role"] for m in revised["messages"]] == ["system", "user", "assistant", "user"]
    assert revised["model"] == request["model"]
    assert revised["reasoning_effort"] == request["reasoning_effort"]
    assert revised["max_completion_tokens"] == request["max_completion_tokens"]
    assert revised["messages"][2]["content"] == prior["final_content"]
    for changed in ({"schema": "wrong"}, {"phase": "peer_revision"},
                    {"blind_id": "X2"}, {"model_requested": "different-model"},
                    {"model_returned": "different-model"},
                    {"request_sha256": "wrong"}, {"finish_reason": "length"},
                    {"final_content": ""}, {"final_content": "Edited earlier answer"}):
        with pytest.raises(ValueError):
            fit.revision_request("X1", {**prior, **changed}, "Review this case.")
    # Historical experiment receipts predate content hashes and remain identified
    # by the whole-parent hash; the result labels their content hash as not recorded.
    legacy = {k: v for k, v in prior.items() if k != "final_content_sha256"}
    assert fit.revision_request("X1", legacy, "Review this case.")["model"] == fit.MODEL


def test_raw_results_cannot_be_saved_in_source_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_key_read():
        raise AssertionError("Rejected output must not read credentials")

    monkeypatch.setattr(fit, "_key", forbidden_key_read)
    with pytest.raises(ValueError, match="below .local"):
        fit.run_one("X1", output_dir=ROOT / "experiments" / "skill_fit" / "results")


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
        captured.append((request.full_url, timeout, request.get_header("Authorization"),
                         request.get_header("User-agent")))
        return FakeResponse()

    monkeypatch.setattr(fit, "OUT", tmp_path)
    monkeypatch.setattr(fit, "_key", lambda: "synthetic-test-key")
    monkeypatch.setattr(fit.urllib.request, "urlopen", fake_urlopen)
    summary = fit.run_one("X1")
    assert summary["reasoning_returned"] is True
    assert "final_content" not in summary
    assert captured == [(fit.URL, 120, "Bearer synthetic-test-key",
                         "OpenSourceResearchAgents/0.1 skill-fit")]
    saved = (tmp_path / "X1.json").read_text()
    assert "Synthetic final answer" in saved
    assert "private reasoning must be discarded" not in saved
    assert "synthetic-test-key" not in saved
    assert json.loads(saved)["final_content_sha256"] == fit.digest("Synthetic final answer")
    with pytest.raises(RuntimeError, match="already been attempted"):
        fit.run_one("X1")
    assert len(captured) == 1
