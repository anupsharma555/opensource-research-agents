"""A malformed provider tool draft may yield feedback, never an executable action."""

from __future__ import annotations

import json

import pytest

from ora.coordination.board import ref
from tests.mission.test_wrong_type_tool_recovery import (
    SOURCE,
    TASK,
    _captured_failure,
)

GENERIC_ERROR = {
    "type": "invalid_request_error", "code": "tool_use_failed",
    "message": "Failed to call a function. Please adjust your prompt. See "
               "'failed_generation' for more details.",
}
REJECTED_DRAFT = "<tool_call><function=publish_result><parameter=text>Incomplete"


def test_captured_generic_400_replans_with_private_draft_unexecuted(
    tmp_path, monkeypatch,
) -> None:
    host, run, control, work, capacity, receipt = _captured_failure(
        tmp_path, monkeypatch, error=GENERIC_ERROR, generated=REJECTED_DRAFT,
    )
    try:
        rejected_ref = receipt["body"]["failed_generation_ref"]
        rejected = host.store.get(**rejected_ref)
        assert rejected["body"]["status"] == "returned"
        assert host.store.read_blob(rejected["body"]["blob"]["sha256"]).decode() == (
            REJECTED_DRAFT
        )
        assert host._captured_malformed_tool_generation(receipt, host.store.get(
            **control["body"]["call_ref"])) is True
        assert host._repair_tool_rejection(control, work, capacity)
        revised = host._control(run.run_id, "agent-synthesis")
        feedback = revised["body"]["feedback"]
        assert feedback["status"] == "provider_rejected_malformed_tool_generation"
        assert feedback["previous_response_ref"] == ref(receipt)
        assert "no tool ran" in feedback["next_action"]
        assert REJECTED_DRAFT not in json.dumps(feedback)
        assert revised["body"]["phase"] == "ready_model"
        assert host._schedule_model(revised)
        successor = host._control(run.run_id, "agent-synthesis")
        assert successor["body"]["call_ref"] != control["body"]["call_ref"]
        next_call = host.store.get(**successor["body"]["call_ref"])
        intent = host.store.get("model_request_intent", f"model-intent:{next_call['id']}:1")
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        assert "provider_rejected_malformed_tool_generation" in json.dumps(packet["messages"])
        assert REJECTED_DRAFT not in json.dumps(packet)
        assert TASK == json.loads(packet["messages"][-1]["content"])["task"]["objective"]
        sources = [
            item for message in packet["messages"]
            if message["role"] == "user" and message["content"].startswith("{")
            for item in json.loads(message["content"]).get("source_packets", [])
        ]
        assert len(sources) == 1
        assert sources[0]["text"].splitlines()[-1] == SOURCE
        assert host.store.records("context_tool_result") == []
        assert host.store.records("report") == []
        assert host.store.records("mail_request") == []
        assert host.store.get(**ref(receipt)) == receipt
    finally:
        host.close()


@pytest.mark.parametrize(("error", "draft"), [
    (GENERIC_ERROR, None),
    (GENERIC_ERROR, {"name": "publish_result"}),
    ({**GENERIC_ERROR, "type": "authentication_error"}, REJECTED_DRAFT),
    ({**GENERIC_ERROR, "message": GENERIC_ERROR["message"] + " extra"},
     REJECTED_DRAFT),
])
def test_generic_400_without_exact_capture_is_not_repaired(
    tmp_path, monkeypatch, error: dict, draft: object,
) -> None:
    host, run, control, work, capacity, receipt = _captured_failure(
        tmp_path, monkeypatch, error=error, generated=draft,
    )
    try:
        assert not host._repair_tool_rejection(control, work, capacity)
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == (
            "pending_model"
        )
        assert host.store.records("report") == []
        assert host.store.records("context_tool_result") == []
        assert receipt["body"]["status"] == "failed"
    finally:
        host.close()


def test_generic_400_does_not_repair_after_source_use_revocation(
    tmp_path, monkeypatch,
) -> None:
    host, run, control, work, capacity, receipt = _captured_failure(
        tmp_path, monkeypatch, error=GENERIC_ERROR, generated=REJECTED_DRAFT,
    )
    try:
        copy_ref = host.store.get(**receipt["body"]["intent_ref"])["body"][
            "source_refs"
        ][0]
        use_ref = host.context._source_status(host.store.get(**copy_ref))[
            "current_source_use_ref"
        ]

        def revoke(tx):
            prior = tx.get(**use_ref)
            return tx.put(
                prior["kind"], prior["id"],
                {**prior["body"], "allowed_uses": [],
                 "policy_status": "requires_source_policy_resolution"},
                expected_version=prior["version"],
            )

        host.store.transact("synthetic-revoke-after-error", {}, revoke,
                            mission_id=control["body"]["mission_id"])
        assert not host._repair_tool_rejection(control, work, capacity)
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == (
            "pending_model"
        )
    finally:
        host.close()
