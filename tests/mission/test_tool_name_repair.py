"""A saved route-as-function rejection can lead to a real corrected tool turn."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
from io import BytesIO

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission import MissionService
from ora.retrieval.providers import HTTPResult
from tests.mission.test_service import MODEL, ROOT, answer, drain, service, tool


def test_saved_route_name_400_recovers_through_registered_pmid_tool(tmp_path, monkeypatch):
    model_requests = []
    discovery_calls = []

    def model_transport(request, *, timeout):
        body = json.loads(request.data)
        model_requests.append(body)
        turn = len(model_requests)
        if turn == 1:
            error = {"error": {
                "type": "invalid_request_error", "code": "tool_use_failed",
                "message": "attempted to call tool 'pubmed.pmid' which was not in request.tools",
            }}
            raise urllib.error.HTTPError(
                "https://example.invalid", 400, "bad tool", {},
                BytesIO(json.dumps(error).encode()),
            )
        if turn == 2:
            messages = json.dumps(body["messages"])
            assert "provider_rejected_unregistered_function" in messages
            assert "Use search_scholarly with a route argument" in messages
            offered = {item["function"]["name"] for item in body["tools"]}
            assert "search_scholarly" in offered
            assert "pubmed.pmid" not in offered
            return tool("search_scholarly", {
                "route": "pubmed.pmid", "selected_id": 12345, "limit": 1,
            }, turn)
        assert turn == 3
        completed_tool = next(
            item for item in reversed(body["messages"]) if item["role"] == "tool"
        )
        result = json.loads(completed_tool["content"])
        assert completed_tool["name"] == "search_scholarly"
        assert result["route"] == "pubmed.pmid"
        assert result["outcome"] == "success"
        return answer("The exact synthetic PMID metadata was retrieved.", turn)

    def discovery_transport(method, url, *, body=None, headers=None):
        discovery_calls.append((method, url))
        return HTTPResult(200, json.dumps({
            "result": {
                "uids": ["12345"],
                "12345": {"uid": "12345", "title": "Synthetic PMID"},
            },
        }).encode(), {})

    host = service(tmp_path, monkeypatch, model_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="route-as-function-corrected-tool", question="Inspect an exact PMID",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        control = host._control(run.run_id, "agent-synthesis")
        old_call_ref = control["body"]["call_ref"]
        work = host._work_for(control)
        spec = host.executor._spec_from(work)
        capacity_id = host.executor._capacity_id(spec)
        host.dispatch.poll(control["body"]["mission_id"])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            failed = host.store.get(
                "model_response_receipt",
                f"model-response:model-intent:{spec.call_id}:{spec.call_version}",
            )
            capacity = host.capacity.get(capacity_id)
            if failed and capacity and capacity["phase"] == "failed":
                break
            time.sleep(.01)
        assert failed and failed["body"]["http_status"] == 400
        assert failed["body"]["error_kind"] == "route_used_as_function_name"
        assert len(model_requests) == 1
        assert discovery_calls == []
        assert host.store.records("context_tool_result") == []
        host._update_control(
            host._control(run.run_id, "agent-synthesis"),
            phase="waiting", waiting_reason="Saved old host left provider 400 waiting",
        )

        resumed = MissionService(
            ROOT, tmp_path, model_session=host.session,
            discovery_transport=discovery_transport,
            execution_mode="offline_test", start_supervisor=False,
        )
        try:
            drain(resumed, run.run_id, limit=20)
            assert resumed.state(run.run_id).snapshot.mission.status == "completed"
            assert len(model_requests) == 3
            assert len(discovery_calls) == 1
            assert discovery_calls[0][0] == "GET"
            assert "id=12345" in discovery_calls[0][1]
            assert {(item["model"], item["reasoning_effort"]) for item in model_requests} == {
                (MODEL, "high")
            }

            assert resumed.store.get(**ref(failed)) == failed
            responses = resumed.store.records("model_response_receipt")
            assert sorted(item["body"]["status"] for item in responses) == [
                "complete", "failed", "tool_requested",
            ]
            corrected = next(
                item for item in responses if item["body"]["status"] == "tool_requested"
            )
            assert corrected["body"]["call_ref"] != old_call_ref
            intent = resumed.store.get(**corrected["body"]["intent_ref"])
            assert intent["body"]["call_ref"] == corrected["body"]["call_ref"]
            repaired_bytes = resumed.store.read_blob(intent["body"]["blob"]["sha256"])
            assert hashlib.sha256(repaired_bytes).hexdigest() == intent["body"][
                "request_sha256"
            ]
            assert json.loads(repaired_bytes) == model_requests[1]
            saved_tool = resumed.store.records("context_tool_result")
            assert len(saved_tool) == 1
            message = json.loads(resumed.store.read_blob(saved_tool[0]["body"][
                "blob"
            ]["sha256"]))
            result = json.loads(message["content"])
            assert message["name"] == "search_scholarly"
            assert result["route"] == "pubmed.pmid"
            assert result["outcome"] == "success"
        finally:
            resumed.close()
    finally:
        host.close()
