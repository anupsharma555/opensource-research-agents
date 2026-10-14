"""Reject unsupported recovery claims even when final prose looks plausible."""

from __future__ import annotations

import copy
import json
import subprocess

import pytest

from ora.benchmarks.context_recovery import (
    RecoveryEvaluationError,
    digest,
    evaluate_recovery,
    load_case,
    make_worker_packet,
)
from tests.mission.test_context_recovery_benchmark import recovery_run
from tests.mission.test_service import ROOT


def test_saved_evidence_rejects_prepared_only_detached_report_and_false_conditions(tmp_path, monkeypatch):
    host, selected, requests = recovery_run(tmp_path, monkeypatch, "compacted_recovery")
    try:
        baseline = evaluate_recovery(host.store, ROOT, selected)
        assert baseline["mechanical_pass"]
        original_records, original_get = host.store.records, host.store.get
        pressure = selected["pressure_intent_ref"]

        def without_capture(kind, **kwargs):
            return [row for row in original_records(kind, **kwargs)
                    if kind != "model_response_receipt" or row["body"]["intent_ref"] != pressure]

        with monkeypatch.context() as patch:
            patch.setattr(host.store, "records", without_capture)
            scored = evaluate_recovery(host.store, ROOT, selected)
            assert not scored["checks"]["captured_checkpoint_responses"]
            assert not scored["checks"]["no_uncertain_dispatch_gaps"]
            assert scored["uncertain_dispatch_count"] == 1
            assert not scored["mechanical_pass"]

        def detached_report(kind, id, version=None):
            row = original_get(kind, id, version)
            if row and kind == "report":
                row = copy.deepcopy(row)
                row["body"]["producer_attempt_ref"]["id"] = "other-attempt"
            return row

        with monkeypatch.context() as patch:
            patch.setattr(host.store, "get", detached_report)
            scored = evaluate_recovery(host.store, ROOT, selected)
            assert not scored["checks"]["report_producer_lineage"]
            assert not scored["mechanical_pass"]

        def citation_free(kind, id, version=None):
            row = original_get(kind, id, version)
            if row and kind == "report":
                row = copy.deepcopy(row)
                row["body"]["citation_input_provenance"] = []
            return row

        with monkeypatch.context() as patch:
            patch.setattr(host.store, "get", citation_free)
            scored = evaluate_recovery(host.store, ROOT, selected)
            assert scored["checks"]["report_matches_captured_answer"]
            assert not scored["checks"]["early_citation_lineage"]
            assert not scored["mechanical_pass"]

        control = evaluate_recovery(host.store, ROOT, {**selected, "condition": "unchanged_context"})
        assert not control["checks"]["early_retained_at_pressure_checkpoint"]
        wrong_order = evaluate_recovery(host.store, ROOT, {
            **selected, "pressure_intent_ref": selected["early_intent_ref"],
        })
        assert not wrong_order["checks"]["ordered_task_lineage"]
        with pytest.raises(RecoveryEvaluationError, match="checksum"):
            evaluate_recovery(host.store, ROOT, {**selected, "case_sha256": "0" * 64})
        assert all("review_required" not in json.dumps(request) for request in requests)

        reference = json.loads((ROOT / "benchmarks/context_recovery/withheld/reference.json").read_text())
        review = {
            "report_sha256": baseline["report_sha256"], "reference_sha256": digest(reference),
            "condition": selected["condition"], "reviewer_id": "synthetic-reviewer",
            "status": "reviewed", "rationale": "Injected reviewer fixture, not quality evidence.",
            "judgments": dict.fromkeys(reference["review_required"], True),
        }
        scored = evaluate_recovery(host.store, ROOT, selected, review=review)
        assert scored["semantic_review"] == "reviewed_pass"
        assert not scored["qualified"]  # No amount of injected review makes this live proof.
        bad_review = evaluate_recovery(host.store, ROOT, selected, review={
            **review, "report_sha256": "0" * 64,
        })
        assert bad_review["semantic_review"] == "pending_or_not_passed"
    finally:
        host.close()


def test_cli_scores_existing_state_read_only_and_does_not_overwrite(tmp_path, monkeypatch):
    state = tmp_path / "state"
    host, selected, _ = recovery_run(state, monkeypatch, "compacted_recovery")
    host.close()
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps(selected))
    output = tmp_path / "receipt.json"
    command = [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/qualify_context_recovery.py"),
               "--state-dir", str(state), "--selection", str(selection), "--output", str(output)]
    before = (state / "state.sqlite3").read_bytes()
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["mechanical_pass"]
    assert (state / "state.sqlite3").read_bytes() == before
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())["qualified"] is False
    repeated = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert repeated.returncode != 0
    assert "FileExistsError" in repeated.stderr


def test_case_export_contains_no_withheld_judgments_or_state_creation(tmp_path):
    case = load_case(ROOT)
    result = subprocess.run([
        str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/qualify_context_recovery.py"),
    ], cwd=tmp_path, capture_output=True, text=True, check=True)
    packet = json.loads(result.stdout)
    assert packet == {"worker_packet": make_worker_packet(case), "case_sha256": digest(case)}
    assert case["late"] not in result.stdout
    assert case["pressure_unit"] not in result.stdout
    assert make_worker_packet(case, "late")["evidence_text"] == case["late"]
    assert case["late"] not in json.dumps(make_worker_packet(case, "pressure"))
    assert "review_required" not in result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("receipt_status", ["complete", "partial", "error"])
def test_unselected_intermediate_captured_requests_cannot_hide_leakage(
    tmp_path, monkeypatch, receipt_status,
):
    from datetime import datetime, timedelta

    from ora.coordination.board import ref
    from ora.state.store import canonical

    host, selected, _ = recovery_run(tmp_path, monkeypatch, "revoked_source")
    try:
        assert evaluate_recovery(host.store, ROOT, selected)["mechanical_pass"]
        original_get, original_records = host.store.get, host.store.records
        first = original_get(**selected["early_intent_ref"])
        pressure = original_get(**selected["pressure_intent_ref"])
        final_response = original_get(**selected["final_response_ref"])
        final = original_get(**final_response["body"]["intent_ref"])
        case = load_case(ROOT)

        before_first = {**first, "created_at": (
            datetime.fromisoformat(first["created_at"]) - timedelta(seconds=2)
        ).isoformat()}
        for text, left, right, failed_check in [
            (case["late"], before_first, first, "late_evidence_withheld_until_after_pressure"),
            (case["late"], first, pressure, "late_evidence_withheld_until_after_pressure"),
            (case["early"], pressure, final, "revoked_passage_absent_after_pressure"),
        ]:
            request = json.loads(host.store.read_blob(first["body"]["blob"]["sha256"]))
            request["messages"].append({"role": "user", "content": text})
            blob = host.store.put_blob(canonical(request).encode())
            left_time = datetime.fromisoformat(left["created_at"])
            right_time = datetime.fromisoformat(right["created_at"])
            injected_intent = copy.deepcopy(first)
            injected_intent.update(id="intermediate-leak", created_at=(
                left_time + (right_time - left_time) / 2
            ).isoformat())
            injected_intent["body"].update(
                blob=blob, request_sha256=digest(request),
                call_ref={"kind": "agent_call", "id": "intermediate-call", "version": 1},
            )
            injected_response = copy.deepcopy(final_response)
            injected_response.update(id="intermediate-response")
            injected_response["body"].update(
                status=receipt_status, intent_ref=ref(injected_intent), request_sha256=digest(request),
                call_ref=injected_intent["body"]["call_ref"],
            )
            injected = [injected_intent, injected_response]

            def get(kind, id, version=None):
                return next((row for row in injected if row["kind"] == kind and row["id"] == id),
                            original_get(kind, id, version))

            def records(kind, *, limit=1000, after_id=""):
                saved = original_records(kind, limit=limit, after_id=after_id)
                extras = [row for row in injected if row["kind"] == kind and row["id"] > after_id]
                return sorted([*saved, *extras], key=lambda row: row["id"])[:limit]

            with monkeypatch.context() as patch:
                patch.setattr(host.store, "get", get)
                patch.setattr(host.store, "records", records)
                scored = evaluate_recovery(host.store, ROOT, selected)
                assert not scored["checks"][failed_check]
                assert not scored["mechanical_pass"]
                assert scored["audited_captured_task_requests"] == 5
                assert receipt_status in scored["captured_task_statuses"]
    finally:
        host.close()
