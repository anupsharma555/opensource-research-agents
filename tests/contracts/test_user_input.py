"""Validate the additive command that targets one saved research peer."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ora.contracts.models import LiveUserInputReceipt, LiveUserInputRequest

ROOT = Path(__file__).resolve().parents[2]


def _request():
    return {
        "requestId": "synthetic-input-1", "runId": "run-1", "taskId": "task-1",
        "targetAgentId": "agent-instance-1", "expectedTaskVersion": 2,
        "kind": "unverified_pasted_evidence", "text": "Synthetic pasted observation.",
    }


def test_user_input_request_keeps_exact_target_and_rejects_spoofed_authority():
    body = _request()
    parsed = LiveUserInputRequest.model_validate(body)
    assert parsed.model_dump(by_alias=True) == body
    for change in (
        {"runId": ""}, {"taskId": ""}, {"targetAgentId": ""},
        {"expectedTaskVersion": 0}, {"kind": "verified_source"},
        {"text": "   "}, {"origin": "research_peer"},
        {"senderId": "agent-instance-2"}, {"evidenceRefs": []},
        {"sourceUse": "granted"}, {"authority": "system"},
    ):
        with pytest.raises(ValidationError):
            LiveUserInputRequest.model_validate({**body, **change})


def test_user_input_receipt_pins_saved_input_without_claiming_delivery():
    receipt = LiveUserInputReceipt.model_validate({
        "requestId": "synthetic-input-1", "runId": "run-1", "taskId": "task-1",
        "targetAgentId": "agent-instance-1", "status": "queued_for_next_boundary",
        "reused": False,
        "inputRef": {"kind": "mission_user_input", "id": "input-1", "version": 1},
        "taskRef": {"kind": "board_proposal", "id": "task-1", "version": 2},
    })
    wire = receipt.model_dump(by_alias=True, mode="json")
    assert wire["status"] == "queued_for_next_boundary"
    assert wire["inputRef"] == {"kind": "mission_user_input", "id": "input-1", "version": 1}
    with pytest.raises(ValidationError):
        LiveUserInputReceipt.model_validate({**wire, "status": "delivered"})
    with pytest.raises(ValidationError):
        LiveUserInputReceipt.model_validate({
            **wire, "taskRef": {"kind": "board_proposal", "id": "other-task", "version": 2},
        })


def test_generated_user_input_schema_has_exact_wire_fields():
    schema = json.loads((ROOT / "contracts/generated/live_user_input.schema.json").read_text())
    assert set(_request()) <= set(schema["properties"])
    assert "origin" not in schema["properties"]
    assert schema["additionalProperties"] is False
