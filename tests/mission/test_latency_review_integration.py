"""One offline mission carries a peer-selected review notice through model delivery."""

from __future__ import annotations

import json
import time

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.latency import read_due_review, read_review_delivery_response
from tests.mission.test_service import answer, service, tool


def test_model_selected_review_is_delivered_once_then_research_finishes(
    tmp_path, monkeypatch,
) -> None:
    clock = [100.0]
    requests = []

    def transport(request, *, timeout):
        payload = json.loads(request.data)
        requests.append(payload)
        number = len(requests)
        if number == 1:
            return tool("request_capability", {"tool_id": "estimate_latency"}, number)
        if number == 2:
            assert any(item["function"]["name"] == "estimate_latency"
                       for item in payload["tools"])
            return tool("estimate_latency", {
                "category": "medium",
                "basis": "Check the saved question again after another step",
                "expected_version": 0,
                "review_after_seconds": 2,
            }, number)
        if number == 3:
            return tool("discover_capabilities", {}, number)
        if number == 4:
            return tool("publish_result", {
                "title": "Bounded assessment",
                "text": "The supplied question has no source, so this is a limited result.",
                "citations": [],
                "limitations": ["No source was supplied"],
            }, number)
        return answer("Unexpected extra call.", number)

    host = service(tmp_path, monkeypatch, transport)
    host.store.clock = lambda: clock[0]
    try:
        accepted = host.start(LiveStartRequest(
            request_id="latency-review-offline-integration",
            question="Give a bounded answer without supplied evidence",
            agent_ids=["agent-synthesis"],
        ))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            host._drive_run(host._run(accepted.run_id))
            control = host._control(accepted.run_id, "agent-synthesis")
            if (host.store.records("mission_latency_expectation")
                    and control["body"]["phase"] == "ready_model"):
                break
            time.sleep(0.005)
        else:
            raise AssertionError("Peer-selected review interval was not saved")
        assert len(requests) == 2
        assert not host.store.records("mission_host_diagnostic")
        clock[0] = 102.0

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            host._drive_run(host._run(accepted.run_id))
            if host._run(accepted.run_id)["body"]["status"] == "completed":
                break
            time.sleep(0.005)
        else:
            raise AssertionError("Mission did not complete its normal tool/result turns")

        assert len(requests) == 4
        review = host.store.records("mission_host_diagnostic")[0]
        assert review["body"]["category"] == "latency_review_due"
        third = json.dumps(requests[2]["messages"])
        fourth = json.dumps(requests[3]["messages"])
        assert review["id"] in third
        assert review["id"] not in fourth
        mission = host._run(accepted.run_id)
        control = host._control(accepted.run_id, "agent-synthesis")
        assert control["body"]["phase"] == "done"
        assert mission["body"]["status"] == "completed"
        assert control["body"]["report_ref"] is not None
        assert len(host.store.records("mission_host_diagnostic")) == 1

        actor_id = control["body"]["actor_id"]
        task_id = control["body"]["task_id"]
        # A closed task no longer has a current due review; the exact saved
        # diagnostic remains linked to the successful third call.
        assert read_due_review(host.store, mission["body"]["mission_id"],
                               actor_id, task_id) is None
        delivered = read_review_delivery_response(host.store, review)
        assert delivered is not None
        response = host.store.get(**delivered)
        assert response["body"]["status"] == "tool_requested"
        assert response["body"]["task_id"] == task_id
        assert all(item["model"] == requests[0]["model"] for item in requests)
        calls = host.store.records("agent_call")
        assert len(calls) == 4
        assert all(item["body"]["instance_id"] == actor_id
                   and item["body"]["task_id"] == task_id for item in calls)
        report = host.store.get(**control["body"]["report_ref"])
        assert report is not None
        assert ref(report) == control["body"]["report_ref"]
        assert report["body"]["title"] == "Bounded assessment"
        assert report["body"]["limitations"] == ["No source was supplied"]
    finally:
        host.close()
