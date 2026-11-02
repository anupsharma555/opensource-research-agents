"""A provider-rejected argument type gets fresh feedback, never a tool effect."""

from __future__ import annotations

import json
import time
import urllib.error
from io import BytesIO

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import service

SOURCE = "Synthetic report: 40 participants compared activity with usual care at week eight."
TASK = "Compare the synthetic report without inventing citations."
TYPE_ERROR = (
    "tool call validation failed: parameters for tool publish_result did not match "
    "schema: errors: [`/citations`: expected array, but got string]"
)


def _captured_failure(tmp_path, monkeypatch, *, error: dict, generated: object = None):
    requests = []

    def rejected(request, *, timeout):
        requests.append(json.loads(request.data))
        body = {"error": {**error}}
        if generated is not None:
            body["error"]["failed_generation"] = generated
        raise urllib.error.HTTPError(
            "https://example.invalid", 400, "tool rejected", {},
            BytesIO(json.dumps(body).encode()),
        )

    host = service(tmp_path, monkeypatch, rejected)
    run = host.start(LiveStartRequest(
        request_id="wrong-type-provider-rejection", question=TASK,
        evidence_text=SOURCE, agent_ids=["agent-synthesis"],
    ))
    assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
    control = host._control(run.run_id, "agent-synthesis")
    work = host._work_for(control)
    spec = host.executor._spec_from(work)
    capacity_id = host.executor._capacity_id(spec)
    host.dispatch.poll(control["body"]["mission_id"])
    deadline = time.monotonic() + 3
    receipt = None
    capacity = None
    while time.monotonic() < deadline:
        receipt = host.store.get(
            "model_response_receipt",
            f"model-response:model-intent:{spec.call_id}:{spec.call_version}",
        )
        capacity = host.capacity.get(capacity_id)
        if receipt is not None and capacity is not None and capacity["phase"] == "failed":
            break
        time.sleep(0.01)
    assert receipt is not None and capacity is not None and capacity["phase"] == "failed"
    assert receipt["body"]["http_status"] == 400
    assert len(requests) == 1
    return host, run, control, work, capacity, receipt


def test_captured_wrong_type_gets_fresh_feedback_and_no_rejected_tool_effect(
    tmp_path, monkeypatch,
) -> None:
    error = {"type": "invalid_request_error", "code": "tool_use_failed",
             "message": TYPE_ERROR}
    generated = '{"name":"publish_result","arguments":{"citations":"[]"}}'
    host, run, control, work, capacity, receipt = _captured_failure(
        tmp_path, monkeypatch, error=error, generated=generated,
    )
    try:
        original_capacity = host.capacity.get(host.executor._capacity_id(
            host.executor._spec_from(work),
        ))
        assert original_capacity["attempts"][-1]["usage_known"] is False
        assert host.store.records("context_tool_result") == []
        assert host.store.records("report") == []
        assert host.store.records("mail_request") == []
        assert receipt["body"]["failed_generation_status"] == "returned"
        assert host._repair_tool_rejection(control, work, capacity)
        revised = host._control(run.run_id, "agent-synthesis")
        feedback = revised["body"]["feedback"]
        assert revised["body"]["phase"] == "ready_model"
        assert revised["body"]["work_id"] is None
        assert feedback["previous_response_ref"] == ref(receipt)
        assert {key: feedback[key] for key in (
            "status", "function_name", "field", "expected_type", "observed_type",
        )} == {
            "status": "provider_rejected_wrong_type_argument",
            "function_name": "publish_result", "field": "citations",
            "expected_type": "array", "observed_type": "string",
        }
        assert host._schedule_model(revised)
        successor = host._control(run.run_id, "agent-synthesis")
        assert successor["body"]["call_ref"] != control["body"]["call_ref"]
        next_call = host.store.get(**successor["body"]["call_ref"])
        next_intent = host.store.get(
            "model_request_intent", f"model-intent:{next_call['id']}:1",
        )
        packet = json.loads(host.store.read_blob(next_intent["body"]["blob"]["sha256"]))
        assert TYPE_ERROR not in json.dumps(packet)
        assert "provider_rejected_wrong_type_argument" in json.dumps(packet["messages"])
        assert TASK in json.loads(packet["messages"][-1]["content"])["task"]["objective"]
        source_packets = [
            source for message in packet["messages"]
            if message["role"] == "user" and message["content"].startswith("{")
            for source in json.loads(message["content"]).get("source_packets", [])
        ]
        assert len(source_packets) == 1
        assert source_packets[0]["text"].splitlines()[-1] == SOURCE
        assert f"chars=0:{len(SOURCE)}" in source_packets[0]["text"].splitlines()[0]
        original_call = host.store.get(**control["body"]["call_ref"])
        assert next_call["body"]["model_id"] == original_call["body"]["model_id"]
        assert host.store.records("context_tool_result") == []
        assert host.store.records("report") == []
        assert host.store.records("mail_request") == []
        assert host.store.get(**ref(receipt)) == receipt
        assert host.capacity.get(host.executor._capacity_id(
            host.executor._spec_from(work),
        ))["attempts"] == original_capacity["attempts"]
    finally:
        host.close()


@pytest.mark.parametrize("error", [
    {"type": "authentication_error", "code": "invalid_api_key",
     "message": TYPE_ERROR},
    {"type": "invalid_request_error", "code": "tool_use_failed",
     "message": TYPE_ERROR.replace("publish_result", "unregistered_tool")},
    {"type": "invalid_request_error", "code": "tool_use_failed",
     "message": TYPE_ERROR.replace("expected array", "expected object")},
])
def test_unqualified_400_does_not_get_type_repair(
    tmp_path, monkeypatch, error: dict,
) -> None:
    host, run, control, work, capacity, _receipt = _captured_failure(
        tmp_path, monkeypatch, error=error,
    )
    try:
        assert not host._repair_tool_rejection(control, work, capacity)
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == (
            "pending_model"
        )
        assert host.store.records("context_tool_result") == []
        assert host.store.records("report") == []
    finally:
        host.close()


def test_uncaptured_dispatch_marker_cannot_claim_wrong_type_repair(
    tmp_path, monkeypatch,
) -> None:
    host, run, control, work, capacity, receipt = _captured_failure(
        tmp_path, monkeypatch, error={
            "type": "invalid_request_error", "code": "tool_use_failed",
            "message": TYPE_ERROR,
        },
    )
    try:
        marker_id = f"model-dispatch:{receipt['body']['intent_ref']['id']}"
        marker = host.store.get("model_dispatch_attempt", marker_id)
        host.store.transact(
            "synthetic-uncaptured-marker", {},
            lambda tx: tx.put(
                "model_dispatch_attempt", marker_id,
                {**marker["body"], "status": "attempted_external_effect",
                 "receipt_ref": None},
                expected_version=marker["version"],
            ),
            mission_id=control["body"]["mission_id"],
        )
        assert not host._repair_tool_rejection(control, work, capacity)
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == (
            "pending_model"
        )
    finally:
        host.close()
