"""A rejected browser choice can select a granted native turn without executing arguments."""

from __future__ import annotations

import json
import time
import urllib.error
from io import BytesIO

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission import MissionService
from ora.mission.browser_search import rejected_browser_selection
from tests.mission.test_browser_search import _native_result
from tests.mission.test_service import drain, service, tool


def _error():
    return {
        "type": "invalid_request_error", "code": "tool_use_failed",
        "message": "Tool call validation failed: tool call validation failed: "
        "attempted to call tool 'browser_search' which was not in request.tools",
        "failed_generation": json.dumps({
            "name": "browser_search", "arguments": {"query": "synthetic discovery"},
        }),
    }


@pytest.mark.parametrize("change", [
    "native_exposed", "function_exposed", "missing_message", "different_name",
    "different_error", "different_type", "extra_message",
])
def test_rejected_browser_selection_requires_exact_unexposed_choice(change):
    request = {"tools": []}
    error = _error()
    assert rejected_browser_selection(request, error)
    if change == "native_exposed":
        request["tools"] = [{"type": "browser_search"}]
    elif change == "function_exposed":
        request["tools"] = [{"type": "function", "function": {"name": "browser_search"}}]
    elif change == "missing_message":
        error.pop("message")
    elif change == "different_name":
        error["message"] = error["message"].replace("browser_search", "execute")
    elif change == "different_error":
        error["code"] = "rate_limit_exceeded"
    elif change == "different_type":
        error["type"] = "transport_error"
    else:
        error["message"] += " Arbitrary extra content"
    assert not rejected_browser_selection(request, error)


@pytest.mark.parametrize("grant_available,interrupted", [(True, False), (False, False), (True, True)])
def test_saved_rejected_choice_recovery_respects_grant_and_restart(
    tmp_path, monkeypatch, grant_available, interrupted,
):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            assert {"type": "browser_search"} not in packet["tools"]
            assert packet["reasoning_effort"] == "high"
            raise urllib.error.HTTPError(
                "https://example.invalid", 400, "tool rejected", {},
                BytesIO(json.dumps({"error": _error()}).encode()),
            )
        if grant_available and len(packets) == 2:
            assert packet["tools"] == [{"type": "browser_search"}]
            assert packet["reasoning_effort"] == "low"
            assert "provider_rejected_unselected_browser_call" in json.dumps(packet["messages"])
            return _native_result(2)
        assert {"type": "browser_search"} not in packet["tools"]
        assert packet["reasoning_effort"] == "high"
        return tool("publish_result", {
            "title": "Synthetic discovery", "text": "Only a discovery lead is available.",
            "citations": [], "limitations": ["No inspected source text"],
        }, len(packets))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="rejected-browser-selection", question="Find a synthetic web lead",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        control = host._control(run.run_id, "agent-synthesis")
        work = host._work_for(control)
        spec = host.executor._spec_from(work)
        cap_id = host.executor._capacity_id(spec)
        host.dispatch.poll(control["body"]["mission_id"])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            failed = host.store.get("model_response_receipt",
                                    f"model-response:model-intent:{spec.call_id}:{spec.call_version}")
            cap = host.capacity.get(cap_id)
            if failed and cap and cap["phase"] == "failed":
                break
            time.sleep(.01)
        assert failed and cap["phase"] == "failed"
        if not grant_available:
            # Current availability no longer matches the pinned descriptor.
            monkeypatch.delitem(host.catalog.tools, "browser_search")
        control = host._control(run.run_id, "agent-synthesis")
        if interrupted:
            original_update = host._update_control
            with monkeypatch.context() as patch:
                def interrupt_update(*args, **kwargs):
                    raise RuntimeError("injected interruption after selection receipt")
                patch.setattr(host, "_update_control", interrupt_update)
                with pytest.raises(RuntimeError, match="injected interruption"):
                    host._repair_tool_rejection(control, work, cap)
            assert host._update_control == original_update
            assert len(host.store.records("mission_capability_selection")) == 1
        assert host._repair_tool_rejection(control, work, cap)
        repaired = host._control(run.run_id, "agent-synthesis")
        assert ("browser_search" in repaired["body"].get("requested_capabilities", [])) is grant_available
        assert len(packets) == 1
        assert host.store.records("context_tool_result") == []
        assert host.store.records("browser_search_leads") == []
        assert host.store.get(**ref(failed)) == failed
        selections = host.store.records("mission_capability_selection")
        assert len(selections) == int(grant_available)
        if grant_available:
            assert selections[0]["body"]["action"] == "recover_rejected_capability_choice"
            assert selections[0]["body"]["source_response_ref"] == ref(failed)
            assert selections[0]["body"]["rejected_arguments_executed"] is False
            assert repaired["body"]["capability_selection_refs"] == [ref(selections[0])]
        resumed = MissionService(host.project_root, tmp_path, model_session=host.session,
                                 execution_mode="offline_test", start_supervisor=False)
        try:
            drain(resumed, run.run_id, limit=20)
            assert resumed.state(run.run_id).snapshot.mission.status == "completed"
            assert len(packets) == (3 if grant_available else 2)
            assert len(resumed.store.records("browser_search_leads")) == int(grant_available)
        finally:
            resumed.close()
    finally:
        host.close()
