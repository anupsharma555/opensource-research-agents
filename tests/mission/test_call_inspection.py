"""The call inspector uses saved response bytes and exact actor lineage."""

from __future__ import annotations

import json
import time
import urllib.error
from io import BytesIO

import pytest

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.mission.call_inspection import (
    CallInspectionError,
    inspect_calls,
    inspect_provider_token_quota,
    provider_token_quota_explanation,
)
from ora.mission.service import TOOL_SCHEMAS
from tests.mission.test_service import answer, drain, service, tool


def test_inspector_shows_saved_output_and_result_without_private_instructions(tmp_path, monkeypatch):
    requests = []

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        if len(requests) == 1:
            return tool("discover_capabilities", {}, 1)
        return answer("A bounded answer; token=synthetic-secret is redacted.", 2)

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(LiveStartRequest(
        request_id="inspection-tools", question="What can be assessed?",
        agent_ids=["agent-synthesis"],
    ))
    drain(host, receipt.run_id)
    inspected = inspect_calls(host.store, receipt.run_id)
    assert len(inspected) == 2
    first, second = inspected
    assert first["response"]["tool_calls"][0]["function"]["name"] == "discover_capabilities"
    assert first["toolResults"][0]["role"] == "tool"
    assert first["trace"]["toolResultReferences"]
    assert second["response"]["content"].endswith("token=[REDACTED] is redacted.")
    assert second["trace"]["author"]["name"] == "Sage"
    assert second["trace"]["selectedModel"] == "openai/gpt-oss-120b"
    assert second["trace"]["providerProfile"] == "primary"
    assert second["trace"]["quotaDomain"] == "groq-selected"
    assert second["trace"]["intentReference"]
    assert second["trace"]["inclusionReference"]
    assert second["returnedReasoning"]["availability"] in {"absent", "restricted", "truncated"}
    serialized = json.dumps(inspected)
    assert "synthetic-secret" not in serialized
    # Public capability descriptions can legitimately say "instructions".
    # Protect actual request fields and private prompt content, not that word.
    for field in ("instructions", "system_prompt", "developer_instructions"):
        assert f'"{field}":' not in serialized
    private_sentences = {
        sentence.strip()
        for request in requests
        for message in request["messages"] if message["role"] in {"system", "developer"}
        for paragraph in message["content"].split("\n\n")
        for sentence in paragraph.split(". ")
        if len(sentence.strip()) >= 40
    }
    assert private_sentences
    assert any("Link a narrow claim" in sentence for sentence in private_sentences)
    for sentence in private_sentences:
        assert json.dumps(sentence)[1:-1] not in serialized
    assert len(inspect_calls(host.store, receipt.run_id, second["attemptId"])) == 1
    assert inspect_calls(host.store, receipt.run_id, "absent-attempt") == []


def test_exact_saved_required_argument_rejection_has_safe_diagnostic_after_schema_change(
    tmp_path, monkeypatch,
):
    schema = TOOL_SCHEMAS["search_scholarly"]
    monkeypatch.setitem(schema, "required", sorted(set([*schema["required"], "query"])))
    error = {"type": "invalid_request_error", "code": "tool_use_failed", "message": (
        "Tool call validation failed: tool call validation failed: parameters for tool "
        "search_scholarly did not match schema: errors: [missing properties: 'query']"
    ), "private_account": "SYNTHETIC-SECRET"}

    def transport(_request, *, timeout):
        raise urllib.error.HTTPError(
            "https://example.invalid", 400, "synthetic rejection", {},
            BytesIO(json.dumps({"error": error}).encode()),
        )

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="inspector-required-query", question="Synthetic saved validation",
            agent_ids=["agent-synthesis"],
        ))
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        control = host._control(run.run_id, "agent-synthesis")
        work = host._work_for(control)
        spec = host.executor._spec_from(work)
        host.dispatch.poll(host._run(run.run_id)["body"]["mission_id"])
        deadline = time.monotonic() + 3
        response_id = f"model-response:model-intent:{spec.call_id}:{spec.call_version}"
        while time.monotonic() < deadline and not host.store.get("model_response_receipt", response_id):
            time.sleep(.01)
        receipt = host.store.get("model_response_receipt", response_id)
        assert receipt and receipt["body"]["http_status"] == 400
        assert receipt["body"]["error_kind"] == "http_error"
        assert host.store.records("context_tool_result") == []

        # The current catalog may change; diagnosis must use this call's pinned request.
        monkeypatch.setitem(schema, "required", [])
        inspected = inspect_calls(host.store, run.run_id, spec.attempt_id)
        assert len(inspected) == 1
        assert inspected[0]["providerValidation"] == {
            "kind": "missing_required_argument",
            "function_name": "search_scholarly", "field": "query",
        }
        assert "SYNTHETIC-SECRET" not in json.dumps(inspected)
        assert error["message"] not in json.dumps(inspected)
        host._refresh_snapshot(run.run_id)
        snapshot = host.state(run.run_id).snapshot
        provenance = next(item for item in snapshot.inspection.attempts
                          if item.attempt_id == spec.attempt_id)
        assert provenance.explanation.startswith("Saved provider tool validation:")
        assert "required query was missing" in provenance.explanation
        assert "SYNTHETIC-SECRET" not in provenance.explanation
        assert host.store.get("model_response_receipt", response_id) == receipt
    finally:
        host.close()


@pytest.mark.parametrize("bucket,label", [("day", "daily"), ("minute", "minute")])
def test_exact_saved_token_429_has_sanitized_request_specific_bucket(
    tmp_path, monkeypatch, bucket, label,
):
    message = (
        f"Rate limit in synthetic account on tokens per {bucket} "
        f"({'TPD' if bucket == 'day' else 'TPM'}): Limit 200000, Used 195540, "
        "Requested 5026. Private marker SYNTHETIC-ACCOUNT-SECRET."
    )

    def transport(_request, *, timeout):
        raise urllib.error.HTTPError(
            "https://example.invalid", 429, "synthetic rate limit",
            {"Retry-After": "245", "x-ratelimit-limit-tokens": "8000"},
            BytesIO(json.dumps({"error": {
                "code": "rate_limit_exceeded", "type": "tokens", "message": message,
            }}).encode()),
        )

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"inspector-{label}-quota", question="Synthetic quota inspection",
            agent_ids=["agent-synthesis"],
        ))
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        control = host._control(run.run_id, "agent-synthesis")
        work = host._work_for(control)
        spec = host.executor._spec_from(work)
        host.dispatch.poll(control["body"]["mission_id"])
        response_id = f"model-response:model-intent:{spec.call_id}:{spec.call_version}"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            receipt = host.store.get("model_response_receipt", response_id)
            if receipt:
                break
            time.sleep(.01)
        assert receipt and receipt["body"]["http_status"] == 429
        call = host.store.get("agent_call", spec.call_id, spec.call_version)
        attempt = host.store.get("agent_attempt", spec.attempt_id)
        quota = inspect_provider_token_quota(host.store, call, attempt, receipt)
        assert quota == {
            "bucket": label, "limit": 200000, "used": 195540,
            "requested": 5026, "retry_after_seconds": 245.0,
        }
        safe = provider_token_quota_explanation(quota)
        allowance = "daily" if label == "daily" else "per-minute"
        assert f"{allowance} token allowance (HTTP 429)" in safe
        assert "limit 200,000, used 195,540, remaining 4,460, requested 5,026" in safe
        assert "245 seconds for the rejected request size" in safe
        assert "not a full allowance reset" in safe
        assert "SYNTHETIC-ACCOUNT-SECRET" not in safe
        assert message not in safe
        assert host.store.get("model_response_receipt", response_id) == receipt
        host._refresh_snapshot(run.run_id)
        snapshot = host.state(run.run_id).snapshot
        provenance = next(item for item in snapshot.inspection.attempts
                          if item.attempt_id == attempt["id"])
        assert provenance.explanation == safe
        assert "SYNTHETIC-ACCOUNT-SECRET" not in provenance.explanation
        forged = {**receipt, "body": {**receipt["body"], "attempt_id": "foreign-attempt"}}
        assert inspect_provider_token_quota(host.store, call, attempt, forged) is None
        assert inspect_provider_token_quota(host.store, call, attempt, {
            **receipt, "body": {**receipt["body"], "request_sha256": "wrong-hash"},
        }) is None
    finally:
        host.close()


@pytest.mark.parametrize("message,code,error_type", [
    ("Rate limit reached; please try again later.", "rate_limit_exceeded", "tokens"),
    ("on tokens per day (TPM): Limit 200000, Used 195540, Requested 5026", "rate_limit_exceeded", "tokens"),
    ("on tokens per day (TPD): Limit 200000, Used unknown, Requested 5026", "rate_limit_exceeded", "tokens"),
    ("on tokens per day (TPD): Limit 200000, Used 195540, Requested 0", "rate_limit_exceeded", "tokens"),
    ("on tokens per day (TPD): Limit 200000, Used 195540, Requested 5026", "other", "tokens"),
    ("on tokens per day (TPD): Limit 200000, Used 195540, Requested 5026", "rate_limit_exceeded", "requests"),
])
def test_unclassified_or_malformed_429_remains_generic(
    tmp_path, monkeypatch, message, code, error_type,
):
    def transport(_request, *, timeout):
        raise urllib.error.HTTPError(
            "https://example.invalid", 429, "synthetic rate limit", {},
            BytesIO(json.dumps({"error": {
                "code": code, "type": error_type, "message": message,
            }}).encode()),
        )

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="inspector-unclassified-quota", question="Synthetic quota inspection",
            agent_ids=["agent-synthesis"],
        ))
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        control = host._control(run.run_id, "agent-synthesis")
        work = host._work_for(control)
        spec = host.executor._spec_from(work)
        host.dispatch.poll(control["body"]["mission_id"])
        response_id = f"model-response:model-intent:{spec.call_id}:{spec.call_version}"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            receipt = host.store.get("model_response_receipt", response_id)
            if receipt:
                break
            time.sleep(.01)
        assert receipt and receipt["body"]["http_status"] == 429
        call = host.store.get("agent_call", spec.call_id, spec.call_version)
        attempt = host.store.get("agent_attempt", spec.attempt_id)
        assert inspect_provider_token_quota(host.store, call, attempt, receipt) is None
        host._refresh_snapshot(run.run_id)
        snapshot = host.state(run.run_id).snapshot
        provenance = next(item for item in snapshot.inspection.attempts
                          if item.attempt_id == attempt["id"])
        assert not provenance.explanation.startswith("Provider reported a daily token allowance")
        assert not provenance.explanation.startswith("Provider reported a per-minute token allowance")
    finally:
        host.close()


def test_inspector_parent_attempt_uses_originating_run(tmp_path, monkeypatch):
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        return answer("Saved parent answer.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    parent = host.start(LiveStartRequest(
        request_id="inspection-parent", question="Parent", agent_ids=["agent-synthesis"],
    ))
    drain(host, parent.run_id)
    snapshot = host.state(parent.run_id).snapshot
    child = host.followup(LiveFollowupRequest(
        request_id="inspection-child", parent_run_id=parent.run_id,
        parent_snapshot_id=snapshot.snapshot_id, target_id=snapshot.artifacts[0].id,
        target_version=snapshot.artifacts[0].version, question="Follow up",
        agent_ids=["agent-critique"],
    ))
    call = inspect_calls(host.store, child.run_id, snapshot.attempts[0].id)[0]
    assert call["runId"] == parent.run_id
    assert call["trace"]["author"]["name"] == "Sage"


def test_inspector_exposes_only_validated_backup_routing_labels(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY_BACKUP", "synthetic-backup-secret")
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("Saved.", 1))
    try:
        receipt = host.start(LiveStartRequest(
            request_id="inspection-backup", question="Synthetic backup selection",
            agent_ids=["agent-synthesis"], credential_profile="backup",
        ))
        drain(host, receipt.run_id)
        call = inspect_calls(host.store, receipt.run_id)[0]
        assert call["trace"]["providerProfile"] == "backup"
        assert call["trace"]["quotaDomain"] == "groq-selected"
        assert "synthetic-backup-secret" not in json.dumps(call)
    finally:
        host.close()


def test_exact_diagnostic_read_separates_returned_reasoning_from_calls_and_state(
    tmp_path, monkeypatch
):
    count = 0

    def transport(request, *, timeout):
        nonlocal count
        count += 1
        result = answer("Synthetic answer.", count)
        if count == 1:
            result.payload["choices"][0]["message"]["reasoning"] = (
                "SYNTHETIC_RETURNED_REASONING"
            )
        return result

    host = service(tmp_path, monkeypatch, transport)
    try:
        first = host.start(LiveStartRequest(
            request_id="reasoning-first", question="Synthetic first task",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, first.run_id)
        call = host.calls(first.run_id)[0]
        with pytest.raises(PermissionError):
            host.returned_reasoning(first.run_id, call["id"])
        result = host.returned_reasoning(
            first.run_id, call["id"], diagnostic_granted=True
        )
        assert result == {
            "runId": first.run_id, "callId": call["id"],
            "agentId": call["agentId"], "attemptId": call["attemptId"],
            "status": "available", "text": "SYNTHETIC_RETURNED_REASONING",
            "origin": "provider_returned", "redacted": False, "reason": "returned",
        }
        assert "SYNTHETIC_RETURNED_REASONING" not in json.dumps(host.calls(first.run_id))
        assert "SYNTHETIC_RETURNED_REASONING" not in host.state(first.run_id).model_dump_json()

        second = host.start(LiveStartRequest(
            request_id="reasoning-second", question="Synthetic second task",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, second.run_id)
        other_call = host.calls(second.run_id)[0]
        absent = host.returned_reasoning(
            second.run_id, other_call["id"], diagnostic_granted=True
        )
        assert absent["status"] == "unavailable" and absent["reason"] == "absent"
        assert "text" not in absent
        with pytest.raises(CallInspectionError):
            host.returned_reasoning(second.run_id, call["id"], diagnostic_granted=True)
        with pytest.raises(CallInspectionError):
            host.returned_reasoning(first.run_id, "absent-call", diagnostic_granted=True)
    finally:
        host.close()
