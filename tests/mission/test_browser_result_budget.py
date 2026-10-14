"""Budgeted discovery retains exact usable leads without promoting them to evidence."""

from __future__ import annotations

import json

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import MODEL, Response, drain, service, tool


def test_compact_browser_result_keeps_fitting_leads_and_executable_reader(tmp_path, monkeypatch):
    urls = [f"https://example.org/{number}/" + "long-path-" * 80 for number in range(16)]
    calls = []

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        if len(calls) == 1:
            return tool("request_capability", {"tool_id": "browser_search"}, 1)
        if len(calls) == 2:
            return Response({
                "id": "synthetic-many-browser-leads", "model": MODEL,
                "choices": [{"message": {
                    "role": "assistant", "content": "Uninspected leads: " + "\n".join(urls),
                }, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
            })
        return tool("publish_result", {
            "title": "Synthetic discovery", "text": "Uninspected links were found.",
            "citations": [], "limitations": ["No source passage inspected"],
        }, len(calls))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="compact-browser-leads", question="Find synthetic web leads",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=20)
        assert len(calls) == 3
        control = host._control(run.run_id, "agent-synthesis")
        task = host.store.get("board_proposal", control["body"]["task_id"])
        pack = host.context.assemble(
            control["body"]["call_ref"], ref(task), control_ref=ref(control),
            allowance=9000, estimate_tokens=len,
        )
        view = json.loads(pack.messages[-1]["content"])["browser_search"]
        assert view["status"] == "discovery_text_omitted_for_budget"
        assert "content" not in view
        assert view["source_inspection"] == "not_performed"
        assert view["access_surface"] == "metadata"
        assert 0 < len(view["hits"]) < view["available_hit_count"] == 16
        assert pack.estimated_tokens <= 9000
        request = {key: value for key, value in view["next_request"].items() if key != "tool"}
        original = host.context.browser_search_result(
            mission_id=control["body"]["mission_id"], actor_id=control["body"]["actor_id"],
            task_id=control["body"]["task_id"], **request,
        )
        assert len(original["hits"]) == 16
        assert view["hits"] == [
            {key: item[key] for key in ("hit_id", "original_url")}
            for item in original["hits"][:len(view["hits"])]
        ]
        assert len(calls) == 3  # Saved reading and packing make no model requests.
    finally:
        host.close()
