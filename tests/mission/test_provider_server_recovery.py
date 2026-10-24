"""Captured provider 5xx errors get a new decision without replaying old work."""

from __future__ import annotations

import json
import time
import urllib.error
from datetime import datetime
from io import BytesIO

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission import MissionService
from tests.mission.test_service import ROOT, answer, drain, revoke_source, service


def _failed_call(
    tmp_path, monkeypatch, *, status: int, native: bool = False, retry_after: str | None = None,
):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            raise urllib.error.HTTPError(
                "https://example.invalid", status, "synthetic provider failure",
                {"Retry-After": retry_after} if retry_after is not None else {}, BytesIO(b""),
            )
        return answer("A fresh synthetic answer after the saved failure.", len(packets))

    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(
        request_id=f"captured-server-{status}-{'native' if native else 'ordinary'}",
        question="Assess a synthetic retained source after provider interruption",
        evidence_text="SYNTHETIC_RETAINED_TEXT: verified source content.",
        agent_ids=["agent-synthesis"],
    ))
    control = host._control(run.run_id, "agent-synthesis")
    source_ref = control["body"]["source_refs"][0]
    if native:
        host._update_control(control, requested_capabilities=["browser_search"])
        control = host._control(run.run_id, "agent-synthesis")
    assert host._schedule_model(control)
    control = host._control(run.run_id, "agent-synthesis")
    work = host._work_for(control)
    spec = host.executor._spec_from(work)
    cap_id = host.executor._capacity_id(spec)
    host.dispatch.poll(control["body"]["mission_id"])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        failed = host.store.get(
            "model_response_receipt", f"model-response:model-intent:{spec.call_id}:{spec.call_version}"
        )
        cap = host.capacity.get(cap_id)
        if failed and cap and cap["phase"] == ("waiting" if status == 429 else "failed"):
            break
        time.sleep(.01)
    else:
        pytest.fail("Injected provider error was not captured")
    assert len(packets) == 1
    control = host._control(run.run_id, "agent-synthesis")
    work = host._work_for(control)
    return host, run.run_id, control, work, cap, failed, packets, source_ref


def test_native_500_restart_creates_fresh_ordinary_decision_with_current_source_use(
    tmp_path, monkeypatch,
):
    host, run_id, control, work, cap, failed, packets, source_ref = _failed_call(
        tmp_path, monkeypatch, status=500, native=True,
    )
    try:
        assert packets[0]["tools"] == [{"type": "browser_search"}]
        assert packets[0]["reasoning_effort"] == "low"
        assert "SYNTHETIC_RETAINED_TEXT" in json.dumps(packets[0]["messages"])
        old_intent = host.store.get(**failed["body"]["intent_ref"])
        old_marker = host.store.get("model_dispatch_attempt", f"model-dispatch:{old_intent['id']}")
        old_capacity = host.store.get("capacity_operation", host.executor._capacity_id(
            host.executor._spec_from(work)
        ))
        assert old_marker["body"]["status"] == "captured"
        assert failed["body"]["usage"] == {}
        assert old_capacity["body"]["attempts"][-1]["usage_known"] is False

        # Revocation between the failed request and new preparation must keep
        # the old record but exclude the original text from the fresh input.
        revoke_source(host, source_ref, control["body"]["mission_id"])
        host._update_control(
            control, phase="waiting", waiting_reason="Older host left captured 500 waiting",
        )
        host.close()
        resumed = MissionService(
            ROOT, tmp_path, model_session=host.session,
            execution_mode="offline_test", start_supervisor=False,
        )
        try:
            assert resumed._drive_run(resumed._run(run_id))[0]
            repaired = resumed._control(run_id, "agent-synthesis")
            assert repaired["body"]["phase"] == "retry_at"
            assert repaired["body"]["work_id"] is None
            assert repaired["body"]["feedback"]["status"] == "provider_transient_failure"
            assert repaired["body"]["feedback"]["remote_browser_effect"] == "unknown"
            assert "browser_search" not in repaired["body"]["requested_capabilities"]
            assert resumed._repair_server_failure(repaired, work, cap) is False
            assert len(packets) == 1
            drain(resumed, run_id, limit=30)
            assert resumed.state(run_id).snapshot.mission.status == "completed"
            assert len(packets) == 2
            assert {"type": "browser_search"} not in packets[1]["tools"]
            assert packets[1]["reasoning_effort"] == "medium"
            visible = json.dumps(packets[1]["messages"])
            assert "provider_transient_failure" in visible
            assert "SYNTHETIC_RETAINED_TEXT" not in visible
            assert len(resumed.store.records("agent_call")) == 2
            assert resumed.store.get(**ref(failed)) == failed
            assert resumed.store.get(**ref(old_intent)) == old_intent
            assert resumed.store.get(**ref(old_marker)) == old_marker
            assert resumed.store.get(**ref(old_capacity)) == old_capacity
        finally:
            resumed.close()
    finally:
        host.close()


def test_ordinary_503_restarts_with_distinct_intent_and_saved_backoff(tmp_path, monkeypatch):
    host, run_id, control, work, cap, failed, packets, _ = _failed_call(
        tmp_path, monkeypatch, status=503,
    )
    try:
        assert packets[0]["reasoning_effort"] == "medium"
        assert host._repair_server_failure(control, work, cap)
        repaired = host._control(run_id, "agent-synthesis")
        assert repaired["body"]["phase"] == "retry_at"
        assert repaired["body"]["retry_at"] > time.time()
        assert repaired["body"]["requested_capabilities"] == []
        assert len(packets) == 1
        drain(host, run_id, limit=30)
        assert host.state(run_id).snapshot.mission.status == "completed"
        assert len(packets) == 2
        intents = host.store.records("model_request_intent")
        assert len(intents) == 2
        assert intents[0]["body"]["request_sha256"] != intents[1]["body"]["request_sha256"]
        assert host.store.get(**ref(failed)) == failed
    finally:
        host.close()


def test_provider_retry_after_extends_server_failure_backoff(tmp_path, monkeypatch):
    host, run_id, control, work, cap, failed, packets, _ = _failed_call(
        tmp_path, monkeypatch, status=503, retry_after="5",
    )
    try:
        assert host._repair_server_failure(control, work, cap)
        repaired = host._control(run_id, "agent-synthesis")
        observed_at = datetime.fromisoformat(
            failed["created_at"].replace("Z", "+00:00")
        ).timestamp()
        assert repaired["body"]["retry_at"] >= observed_at + 5
        assert len(packets) == 1
    finally:
        host.close()


@pytest.mark.parametrize("status", [400, 401, 429, 501])
def test_unrelated_http_failures_are_not_server_repaired(tmp_path, monkeypatch, status):
    host, _run_id, control, work, cap, _failed, packets, _ = _failed_call(
        tmp_path, monkeypatch, status=status,
    )
    try:
        assert host._repair_server_failure(control, work, cap) is False
        assert len(packets) == 1
    finally:
        host.close()


def test_uncertain_or_missing_receipt_never_uses_captured_server_repair(tmp_path, monkeypatch):
    host, _run_id, control, work, cap, receipt, _packets, _ = _failed_call(
        tmp_path, monkeypatch, status=500,
    )
    try:
        assert host._repair_server_failure(control, work, {**cap, "phase": "uncertain"}) is False
        original_get = host.store.get

        def no_receipt(*args, **kwargs):
            kind = args[0] if args else kwargs.get("kind")
            record_id = args[1] if len(args) > 1 else kwargs.get("id")
            if kind == "model_response_receipt" and record_id == receipt["id"]:
                return None
            return original_get(*args, **kwargs)

        monkeypatch.setattr(host.store, "get", no_receipt)
        assert host._repair_server_failure(control, work, cap) is False
    finally:
        host.close()


@pytest.mark.parametrize("changed_record", ["receipt_sha", "marker_status"])
def test_captured_failure_requires_exact_saved_lineage(tmp_path, monkeypatch, changed_record):
    host, _run_id, control, work, cap, receipt, _packets, _ = _failed_call(
        tmp_path, monkeypatch, status=500,
    )
    try:
        original_get = host.store.get

        def changed_get(*args, **kwargs):
            item = original_get(*args, **kwargs)
            if item is None:
                return None
            if changed_record == "receipt_sha" and item["id"] == receipt["id"]:
                return {**item, "body": {**item["body"], "request_sha256": "0" * 64}}
            if changed_record == "marker_status" and item["kind"] == "model_dispatch_attempt":
                return {**item, "body": {**item["body"], "status": "attempted_external_effect"}}
            return item

        monkeypatch.setattr(host.store, "get", changed_get)
        assert host._repair_server_failure(control, work, cap) is False
        assert host._control(control["body"]["run_id"], "agent-synthesis")["body"]["work_id"] == (
            control["body"]["work_id"]
        )
    finally:
        host.close()


def test_repeated_captured_5xx_uses_later_backoff_without_global_round_cutoff(
    tmp_path, monkeypatch,
):
    packets = []

    def transport(request, *, timeout):
        packets.append(json.loads(request.data))
        if len(packets) <= 2:
            raise urllib.error.HTTPError(
                "https://example.invalid", 500, "synthetic provider failure", {}, BytesIO(b"")
            )
        return answer("A third, fresh synthetic answer.", len(packets))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="captured-repeated-server-error", question="Complete a synthetic answer",
            agent_ids=["agent-synthesis"],
        ))
        observed_streaks = {}
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            streak = control["body"].get("provider_server_failure_streak")
            if streak in {1, 2}:
                observed_streaks.setdefault(streak, control["body"].get("retry_at"))
            if host._run(run.run_id)["body"]["status"] == "completed":
                break
            time.sleep(.01)
        assert host.state(run.run_id).snapshot.mission.status == "completed"
        assert len(packets) == 3
        assert observed_streaks[2] > observed_streaks[1] + 1
        assert len({item["id"] for item in host.store.records("model_request_intent")}) == 3
    finally:
        host.close()
