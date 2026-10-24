"""Validate additive whole-run command and receipt wire fields."""

import pytest
from pydantic import ValidationError

from ora.contracts.models import LiveRunCancelReceipt, LiveRunCancelRequest


def test_run_cancel_request_and_receipt_are_exact():
    request = LiveRunCancelRequest.model_validate({
        "requestId": "stop-1", "runId": "run-1", "expectedRunVersion": 2,
        "reason": "Stop remaining work",
    })
    assert request.expected_run_version == 2
    for change in ({"expectedRunVersion": 0}, {"reason": " "}, {"unknown": 1}):
        with pytest.raises(ValidationError):
            LiveRunCancelRequest.model_validate({**request.model_dump(by_alias=True), **change})
    receipt = LiveRunCancelReceipt.model_validate({
        "requestId": "stop-1", "runId": "run-1", "status": "cancellation_requested",
        "cancellationRef": {"kind": "mission_run_cancellation", "id": "stop-1", "version": 1},
        "runRef": {"kind": "mission_run", "id": "run-1", "version": 3},
    })
    assert receipt.status == "cancellation_requested"
    with pytest.raises(ValidationError):
        LiveRunCancelReceipt.model_validate({
            **receipt.model_dump(by_alias=True),
            "runRef": {"kind": "mission_run", "id": "other-run", "version": 3},
        })
