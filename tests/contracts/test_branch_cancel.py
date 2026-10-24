"""Check the exact local operator cancellation wire shapes."""

import pytest
from pydantic import ValidationError

from ora.contracts.models import LiveBranchCancelReceipt, LiveBranchCancelRequest


def test_branch_cancel_request_requires_exact_task_and_version():
    body = {
        "requestId": "cancel-1", "runId": "run-1", "taskId": "task-1",
        "expectedTaskVersion": 2, "reason": "Stop this selected branch",
    }
    request = LiveBranchCancelRequest.model_validate(body)
    assert request.model_dump(by_alias=True) == body
    for changed in (
        {**body, "taskId": ""},
        {**body, "expectedTaskVersion": 0},
        {**body, "reason": ""},
        {**body, "reason": "   "},
        {**body, "agentName": "Synthetic Peer"},
    ):
        with pytest.raises(ValidationError):
            LiveBranchCancelRequest.model_validate(changed)


def test_branch_cancel_receipt_keeps_exact_saved_references():
    receipt = LiveBranchCancelReceipt(
        request_id="cancel-1", run_id="run-1", task_id="task-1",
        status="cancellation_requested", reused=False,
        cancellation_ref={"kind": "mission_branch_cancellation", "id": "cancel-1", "version": 1},
        task_ref={"kind": "board_proposal", "id": "task-1", "version": 2},
    )
    wire = receipt.model_dump(by_alias=True, mode="json")
    assert wire["status"] == "cancellation_requested"
    assert wire["cancellationRef"]["version"] == 1
    assert wire["taskRef"] == {"kind": "board_proposal", "id": "task-1", "version": 2}
    with pytest.raises(ValidationError):
        LiveBranchCancelReceipt.model_validate({**wire, "status": "remote_stopped"})
    with pytest.raises(ValidationError):
        LiveBranchCancelReceipt.model_validate({
            **wire, "taskRef": {"kind": "board_proposal", "id": "other-task", "version": 2},
        })
