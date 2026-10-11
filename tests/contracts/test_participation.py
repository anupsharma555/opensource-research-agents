"""Starting peers and required contributions have separate command semantics."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.live.engine import ResearchHost


@pytest.mark.parametrize("model,extra", [
    (LiveStartRequest, {}),
    (LiveFollowupRequest, {
        "parent_run_id": "parent", "parent_snapshot_id": "snapshot",
        "target_id": "report", "target_version": 1,
    }),
])
def test_required_participants_are_an_explicit_subset(model, extra):
    body = {"request_id": "request", "question": "Review the evidence",
            "agent_ids": ["peer-a", "peer-b"], **extra}
    assert model(**body).required_agent_ids == []
    assert model(**body, required_agent_ids=["peer-b"]).required_agent_ids == ["peer-b"]
    for invalid in (["unselected-peer"], ["peer-a", "peer-a"]):
        with pytest.raises(ValidationError, match="requiredAgentIds"):
            model(**body, required_agent_ids=invalid)


def test_legacy_request_fingerprint_does_not_change_for_empty_new_field():
    old_body = {
        "requestId": "request", "question": "Review the evidence", "scope": "",
        "evidenceText": "A saved passage", "agentIds": ["peer-a"],
    }
    old_hash = hashlib.sha256(json.dumps(
        old_body, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode()).hexdigest()
    request = LiveStartRequest.model_validate(old_body)
    assert ResearchHost._fingerprint(request) == old_hash
