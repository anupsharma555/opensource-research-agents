"""Real session capture writes exact private artifacts without another inference."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ora.coordination.traces import TraceJournal
from ora.models.reasoning_artifacts import export_run_reasoning
from tests.models.test_session import response, select, setup


@pytest.mark.parametrize("reasoning", [
    "  Synthetic α reasoning.\nAPI_KEY=fixture_secret_123456789\t\n",
    None,
])
def test_session_automatically_saves_each_provider_reasoning_artifact(
    tmp_path, monkeypatch, reasoning,
):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-key")
    store, _registry, bridge, call, _config, source, captured = setup(
        tmp_path, raw=response(reasoning=reasoning),
    )
    prepared = bridge.prepare(
        call_id=call["id"], call_version=call["version"], phase="collect",
        messages=[{"role": "user", "content": "Review the synthetic source."}],
        sources=select(source),
    )
    receipt_ref = bridge.invoke_capture(prepared.intent_ref)
    artifacts = list((tmp_path / "reasoning").glob("**/reasoning_trace.json"))
    assert len(artifacts) == 1  # Written by capture, without explicit export.
    saved = json.loads(artifacts[0].read_text())
    assert saved["receipt_ref"] == receipt_ref
    assert saved["content"] == reasoning
    text = artifacts[0].with_suffix(".txt")
    if reasoning is None:
        assert saved["status"] == "absent" and not text.exists()
        assert saved["unavailable_reason"]
    else:
        assert saved["capture_fidelity"] == "provider_exact"
        assert text.read_bytes() == reasoning.encode("utf-8")
        receipt = store.get(**receipt_ref)
        diagnostic = TraceJournal(store).read_reasoning(
            receipt["body"]["reasoning_ref"], mission_id="mission",
            diagnostic_granted=True,
        )
        assert "fixture_secret_123456789" not in diagnostic["text"]
    assert not store.records("reasoning_export_failure")
    assert bridge.invoke_capture(prepared.intent_ref) == receipt_ref
    assert len(captured) == 1


def test_export_error_keeps_captured_response_and_repair_needs_no_inference(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-key")
    text = "Synthetic returned text.\n"
    store, _registry, bridge, call, _config, source, captured = setup(
        tmp_path, raw=response(reasoning=text),
    )
    store.transact("seed-run", {}, lambda tx: tx.put(
        "mission_run", "run", {"mission_id": "mission"},
    ), mission_id="mission")
    prepared = bridge.prepare(
        call_id=call["id"], call_version=call["version"], phase="collect",
        messages=[{"role": "user", "content": "Review the synthetic source."}],
        sources=select(source),
    )

    def unavailable_export(*_args, **_kwargs):
        raise OSError("Synthetic artifact storage failure")

    with monkeypatch.context() as failed:
        failed.setattr("ora.models.session.export_receipt_reasoning", unavailable_export)
        receipt_ref = bridge.invoke_capture(prepared.intent_ref)
    assert store.get(**receipt_ref)["body"]["status"] == "tool_requested"
    assert store.records("model_dispatch_attempt")[0]["body"]["status"] == "captured"
    assert store.records("reasoning_export_failure")[0]["body"]["error_class"] == "OSError"
    repaired = export_run_reasoning(store, "run")
    assert repaired["status"] == "complete"
    assert Path(repaired["steps"][0]["text_path"]).read_bytes() == text.encode()
    assert len(captured) == 1
