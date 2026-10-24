"""Synthetic local reasoning artifacts retain exact bytes and honest availability."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ora.models.reasoning_artifacts import (
    ReasoningArtifactError,
    export_receipt_reasoning,
    export_run_reasoning,
    record_export_failure,
)
from ora.state import StateStore


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _fixture(store: StateStore, *, number: int = 1,
             raw: str | None = "  α🙂\nnext  \n",
             redacted: str | None = None, status: str = "returned",
             raw_record: bool = True, run_id: str = "run/fixture") -> dict:
    """Build an exact model receipt without any provider dispatch."""
    raw_blob = store.put_blob(raw.encode("utf-8")) if raw is not None else None
    legacy_text = redacted if redacted is not None else raw
    legacy_blob = store.put_blob(legacy_text.encode("utf-8")) if legacy_text is not None else None

    def save(tx):
        run = tx.get("mission_run", run_id, 1)
        if run is None:
            tx.put("mission_run", run_id, {"mission_id": "mission", "status": "running"})
        actor = tx.get("agent_instance", f"actor-{number}", 1)
        if actor is None:
            actor = tx.put("agent_instance", f"actor-{number}", {
                "mission_id": "mission", "run_id": run_id, "display_name": "Synthetic peer",
            })
        config = tx.put("agent_configuration", f"config-{number}", {
            "mission_id": "mission", "provider": "groq", "model_id": "synthetic-model",
        })
        call_ref = {"kind": "agent_call", "id": f"call-{number}", "version": 1}
        intent = tx.put("model_request_intent", f"intent-{number}", {
            "mission_id": "mission", "instance_id": actor["id"],
            "call_ref": call_ref, "configuration_ref": _ref(config),
            "task_id": "task", "attempt_id": f"attempt-{number}",
            "request_sha256": f"request-{number}",
        })
        raw_ref = None
        if raw_record and raw_blob is not None:
            raw_saved = tx.put("restricted_reasoning_raw", f"raw-{number}", {
                "mission_id": "mission", "run_id": run_id, "blob": raw_blob,
                "intent_ref": _ref(intent), "request_sha256": f"request-{number}",
                "origin": "provider_returned", "access": "restricted_diagnostics",
            })
            raw_ref = _ref(raw_saved)
        reason = tx.put("restricted_reasoning", f"reason-{number}", {
            "mission_id": "mission", "run_id": run_id, "status": status,
            "blob": legacy_blob, "raw_ref": raw_ref,
            "redacted": redacted is not None, "origin": "provider_returned",
            "intent_ref": _ref(intent), "request_sha256": f"request-{number}",
            "access": "restricted_diagnostics",
            "instance_id": actor["id"],
            "task_id": "task", "attempt_id": f"attempt-{number}",
            "operation_id": f"model-call:call-{number}:1",
            "call_id": f"call-{number}", "call_version": 1,
            "configuration_ref": _ref(config), "parent_operation_id": None,
        })
        if raw_ref is not None:
            tx.link(reason, raw_saved, "has_exact_provider_text")
        receipt = tx.put("model_response_receipt", f"receipt-{number}", {
            "mission_id": "mission", "task_id": "task", "attempt_id": f"attempt-{number}",
            "call_ref": call_ref, "configuration_ref": _ref(config),
            "intent_ref": _ref(intent), "request_sha256": f"request-{number}",
            "reasoning_ref": _ref(reason), "reasoning_status": status,
            "returned_model": "synthetic-model",
        })
        tx.link(receipt, reason, "has_restricted_reasoning")
        return {"receipt_ref": _ref(receipt)}

    return store.transact(f"fixture-{number}", {"number": number}, save,
                          mission_id="mission")["receipt_ref"]


def test_exact_unicode_whitespace_private_files_and_combined_report(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    text = "  α🙂\nnext  \n![remote](https://example.invalid/image) <img src=x>\n"
    first = _fixture(store, raw=text, redacted="[redacted]", status="truncated")
    second = _fixture(store, number=2, raw="\nsecond\t\n")
    result = export_run_reasoning(store, "run/fixture")
    assert result["receipt_count"] == 2
    assert result["status"] == "partial" and result["truncated_count"] == 1
    assert text not in json.dumps(result)
    steps = result["steps"]
    assert [item["receipt_ref"] for item in steps] == [first, second]
    assert [item["capture_fidelity"] for item in steps] == ["provider_exact", "provider_exact"]
    assert Path(steps[0]["text_path"]).read_bytes() == text.encode("utf-8")
    private = json.loads(Path(steps[0]["json_path"]).read_text(encoding="utf-8"))
    assert private["content"] == text and private["status"] == "truncated"
    assert private["provider"] == "groq" and private["model"] == "synthetic-model"
    assert private["agent"] == "Synthetic peer"
    assert stat.S_IMODE(os.stat(steps[0]["text_path"]).st_mode) == 0o600
    step_markdown_path = Path(steps[0]["markdown_path"])
    step_markdown = step_markdown_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(step_markdown_path.stat().st_mode) == 0o600
    assert "- Run: run/fixture" in step_markdown
    assert "- Agent: Synthetic peer" in step_markdown
    assert "- Model: synthetic\\-model" in step_markdown
    assert "- Provider: groq" in step_markdown
    assert "- Status: truncated" in step_markdown
    assert "- Capture fidelity: provider\\_exact" in step_markdown
    assert "[reasoning_trace.txt](reasoning_trace.txt)" in step_markdown
    assert "[reasoning_trace.json](reasoning_trace.json)" in step_markdown
    assert "&lt;img" in step_markdown
    assert "![remote](https://example.invalid/image)" not in step_markdown
    assert "<img" not in step_markdown
    assert "&gt;" in step_markdown
    assert stat.S_IMODE(os.stat(Path(steps[0]["text_path"]).parent).st_mode) == 0o700
    markdown = Path(result["combined_markdown_path"]).read_text(encoding="utf-8")
    assert "Step 1" in markdown and "Step 2" in markdown
    assert "Step Markdown: [reasoning_trace.md]" in markdown
    assert "![remote](https://example.invalid/image)" not in markdown
    assert "<img" not in markdown
    assert "\nsecond\t\n" in Path(result["combined_text_path"]).read_text(encoding="utf-8")
    assert len(json.loads(Path(result["combined_json_path"]).read_text())["steps"]) == 2
    assert export_run_reasoning(store, "run/fixture") == result  # replay is idempotent


@pytest.mark.parametrize(("redacted", "expected"), [
    (None, "saved_unredacted"), ("[redacted]", "saved_redacted"),
])
def test_legacy_saved_reasoning_fidelity(tmp_path: Path, redacted: str | None,
                                          expected: str) -> None:
    store = StateStore(tmp_path / "state")
    receipt = _fixture(store, raw="private original", redacted=redacted,
                       raw_record=False)
    result = export_receipt_reasoning(store, receipt)
    assert result["capture_fidelity"] == expected
    content = json.loads(Path(result["json_path"]).read_text())["content"]
    assert content == (redacted if redacted is not None else "private original")
    assert Path(result["text_path"]).read_text() == content


@pytest.mark.parametrize("status", ["absent", "unknown", "unsupported", "not_requested"])
def test_missing_reasoning_has_json_without_text(tmp_path: Path, status: str) -> None:
    store = StateStore(tmp_path / "state")
    receipt = _fixture(store, raw=None, status=status)
    result = export_receipt_reasoning(store, receipt)
    assert result["text_path"] is None
    assert result["capture_fidelity"] == "unavailable"
    assert json.loads(Path(result["json_path"]).read_text())["content"] is None
    step_markdown = Path(result["markdown_path"]).read_text(encoding="utf-8")
    escaped_status = status.replace("_", "\\_")
    assert f"- Status: {escaped_status}" in step_markdown
    assert "No saved provider reasoning text is available" in step_markdown
    assert "[reasoning_trace.json](reasoning_trace.json)" in step_markdown
    assert "reasoning_trace.txt" not in step_markdown


def test_unsafe_symlink_failure_is_repairable_without_recapture(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    receipt = _fixture(store)
    destination = tmp_path / "private"
    destination.symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ReasoningArtifactError, match="symbolic link") as failure:
        export_receipt_reasoning(store, receipt, output_dir=destination)
    record_export_failure(store, receipt, failure.value)
    assert store.records("reasoning_export_failure")[0]["body"]["error_class"] == "ReasoningArtifactError"
    destination.unlink()
    repaired = export_run_reasoning(store, "run/fixture", output_dir=destination)
    assert repaired["status"] == "complete"
    assert Path(repaired["steps"][0]["text_path"]).read_text() == "  α🙂\nnext  \n"
    assert len(store.records("model_response_receipt")) == 1


def test_backup_restore_retains_separate_raw_record(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    receipt = _fixture(store, raw="PRIVATE full", redacted="[redacted]")
    manifest = store.backup(tmp_path / "backup")
    assert len(manifest["objects"]) == 2
    restored = StateStore.restore(tmp_path / "backup", tmp_path / "restored")
    result = export_receipt_reasoning(restored, receipt)
    assert result["capture_fidelity"] == "provider_exact"
    assert Path(result["text_path"]).read_text() == "PRIVATE full"


def test_foreign_run_or_forged_reasoning_lineage_is_rejected(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    receipt = _fixture(store)
    with pytest.raises(ReasoningArtifactError, match="Selected run"):
        export_run_reasoning(store, "other-run")
    wrong = store.transact("wrong-reason", {}, lambda tx: _ref(tx.put(
        "restricted_reasoning", "foreign-reason", {
            "mission_id": "foreign", "status": "returned", "blob": None,
        })), mission_id="mission")
    forged = store.transact("forged-receipt", {}, lambda tx: _ref(tx.put(
        "model_response_receipt", "forged", {
            **store.get(**receipt)["body"], "reasoning_ref": wrong,
        })), mission_id="mission")
    with pytest.raises(ReasoningArtifactError, match="exact restricted reasoning link"):
        export_receipt_reasoning(store, forged)


def test_same_run_raw_text_cannot_be_substituted_between_calls(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    first = _fixture(store, raw="first")
    second = _fixture(store, number=2, raw="second")
    first_body = store.get(**first)["body"]
    first_reason = store.get(**first_body["reasoning_ref"])
    second_reason = store.get(**store.get(**second)["body"]["reasoning_ref"])

    def forge(tx):
        raw = tx.get(**second_reason["body"]["raw_ref"])
        reason = tx.put("restricted_reasoning", "substituted-reason", {
            **first_reason["body"], "raw_ref": second_reason["body"]["raw_ref"],
        })
        tx.link(reason, raw, "has_exact_provider_text")
        receipt = tx.put("model_response_receipt", "substituted-receipt", {
            **first_body, "reasoning_ref": _ref(reason),
        })
        tx.link(receipt, reason, "has_restricted_reasoning")
        return {"receipt_ref": _ref(receipt)}

    forged = store.transact("substitute", {}, forge, mission_id="mission")["receipt_ref"]
    with pytest.raises(ReasoningArtifactError, match="Raw reasoning differs"):
        export_receipt_reasoning(store, forged)


@pytest.mark.parametrize("locally_blocked", [False, True])
def test_dispatch_without_receipt_is_explicit_unknown_step(tmp_path: Path, locally_blocked) -> None:
    store = StateStore(tmp_path / "state")
    receipt = _fixture(store)
    base = store.get(**receipt)["body"]
    intent = store.get(**base["intent_ref"])
    marker = store.transact("dispatch-without-receipt", {}, lambda tx: _ref(tx.put(
        "model_dispatch_attempt", "model-dispatch:intent-2", {
            "mission_id": "mission", "intent_ref": {
                "kind": "model_request_intent", "id": "intent-2", "version": 1,
            }, "request_sha256": "request-2",
            "status": ("blocked_before_external_effect" if locally_blocked
                       else "attempted_external_effect"),
        })), mission_id="mission")
    # A second request reached its dispatch marker but has no provider response.
    store.transact("second-request", {}, lambda tx: _ref(tx.put(
        "model_request_intent", "intent-2", {
            **intent["body"], "request_sha256": "request-2",
        })), mission_id="mission")
    result = export_run_reasoning(store, "run/fixture")
    assert result["receipt_count"] == 1
    assert result["dispatch_only_count"] == 1
    assert result["status"] == "partial"
    pending = next(item for item in result["steps"] if item["status"] == "not_captured")
    assert pending["receipt_ref"] == marker
    assert pending["text_path"] is None
    payload = json.loads(Path(pending["json_path"]).read_text())
    assert payload["content"] is None and payload["receipt_ref"] is None
    assert payload["dispatch_ref"] == marker
    assert payload["unavailable_reason"] == (
        "blocked_before_http" if locally_blocked else "response_not_captured")
    if locally_blocked:
        assert "no inference occurred" in payload["capture_note"]
    assert "- Status: not\\_captured" in Path(pending["markdown_path"]).read_text()


def test_legacy_figure_receipt_without_reasoning_is_metadata_only(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")

    def save(tx):
        tx.put("mission_run", "legacy-run", {"mission_id": "mission"})
        intent = tx.put("figure_request_intent", "figure-intent", {
            "mission_id": "mission", "run_id": "legacy-run",
            "actor_id": "peer", "task_id": "task", "operation_id": "figure-op",
            "request_sha256": "hash",
        })
        receipt = tx.put("figure_response_receipt", "figure-response:figure-intent", {
            **intent["body"], "intent_ref": _ref(intent),
            "model": "synthetic-vision", "status": "generated_unverified",
        })
        return {"receipt_ref": _ref(receipt)}

    store.transact("legacy-figure", {}, save, mission_id="mission")
    result = export_run_reasoning(store, "legacy-run")
    assert result["status"] == "partial"
    assert result["receipt_count"] == 1 and result["unavailable_count"] == 1
    step = result["steps"][0]
    assert step["status"] == "unknown" and step["text_path"] is None
    assert step["unavailable_reason"] == "legacy_not_captured"
    assert json.loads(Path(step["json_path"]).read_text())["content"] is None
    assert "No saved provider reasoning text is available" in Path(step["markdown_path"]).read_text()
