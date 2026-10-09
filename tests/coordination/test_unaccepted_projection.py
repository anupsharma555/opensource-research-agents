"""Keep captured-but-unaccepted outcomes distinct without exporting result bytes."""

import json

import pytest

from ora.state.view_policy import project_record


@pytest.mark.parametrize("effect,wording", [
    ("known", "An execution outcome was recorded"),
    ("known_rejection", "known rejection, not an uncertain completed call"),
    ("no_request", "No provider request was sent"),
    ("unverified", "external outcome is not verified"),
])
def test_unaccepted_result_projection_preserves_evidence_and_hides_payload(effect, wording):
    receipt = {"kind": "model_response_receipt", "id": "observed-response", "version": 1}
    record = {"kind": "executor_unaccepted_result", "id": "unaccepted", "version": 1,
              "body": {"mission_id": "mission", "work_id": "work",
                       "external_effect": effect, "observed_receipt_ref": receipt,
                       "reason_code": "acceptance_conflict", "capacity_reconciled": effect == "known",
                       "result_sha256": "a" * 64, "used_tokens": 12,
                       "result": {"payload": "PRIVATE_RESULT_MUST_NOT_ENTER_OVERVIEW",
                                  "reasoning": "PRIVATE_REASONING_MUST_NOT_ENTER_OVERVIEW"}}}
    projected = project_record(record)
    assert projected["external_effect"] == effect
    assert projected["observed_receipt_ref"] == receipt
    assert projected["capacity_reconciled"] is (effect == "known")
    assert wording in projected["explanation"]
    assert "PRIVATE_" not in json.dumps(projected)
    assert "result" not in projected
