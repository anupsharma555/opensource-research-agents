"""A peer continues an exact saved venue page across new model tool calls."""

from __future__ import annotations

import hashlib
import json

from ora.contracts.models import LiveStartRequest
from ora.retrieval.catalog_tools import page_venue_navigation
from ora.state import StateStore
from tests.mission.test_service import drain, service, tool


def test_model_pages_retained_venue_links_without_refetch_after_reopen(tmp_path, monkeypatch):
    import ora.mission.service as service_module

    calls = []
    fetched = []
    selected = {}
    html = ("<html><title>Synthetic issue</title>" + "".join(
        f"<a href='/article/{index}'>Article {index}</a>" for index in range(58)
    ) + "</html>").encode()

    def fake_fetch(url, *, max_bytes):
        fetched.append(url)
        assert len(html) <= max_bytes
        return {
            "status": 200, "content": html,
            "content_sha256": hashlib.sha256(html).hexdigest(), "byte_length": len(html),
            "content_type": "text/html", "requested_url": url, "final_url": url,
            "redirect_chain": [], "access_basis": "public_https_unauthenticated",
            "authenticated": False, "retrieved_at": "2026-10-11T00:00:00Z",
        }

    def last_result(request):
        return json.loads(next(
            item["content"] for item in reversed(json.loads(request.data)["messages"])
            if item["role"] == "tool"
        ))

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        count = len(calls)
        if count == 1:
            return tool("request_capability", {"tool_id": "list_venues"}, count)
        if count == 2:
            return tool("list_venues", {"limit": 1}, count)
        if count == 3:
            selected["catalog_ref"] = last_result(request)["entries"][0]["catalog_ref"]
            return tool("request_capability", {"tool_id": "navigate_venue"}, count)
        if count == 4:
            return tool("navigate_venue", {"catalog_ref": selected["catalog_ref"]}, count)
        if count == 5:
            first = last_result(request)
            assert first["status"] == "navigable"
            assert len(first["links"]) == 25
            assert first["evidence_status"] == "navigation_lead_only"
            assert first["next_request"]["tool"] == "page_venue_navigation"
            selected["first"] = first
            return tool("request_capability", {"tool_id": "page_venue_navigation"}, count)
        if count == 6:
            args = {key: value for key, value in selected["first"]["next_request"].items()
                    if key != "tool"}
            return tool("page_venue_navigation", args, count)
        if count == 7:
            page = last_result(request)
            assert page["navigation_ref"] == selected["first"]["navigation_ref"]
            assert [item["text"] for item in page["links"]] == [
                f"Article {index}" for index in range(25, 50)
            ]
            assert page["next_request"]["link_offset"] == 50
            selected["second"] = page
            return tool("page_venue_navigation", {
                "navigation_ref": page["navigation_ref"], "link_offset": 999,
                "link_limit": 25,
            }, count)
        if count == 8:
            rejected = last_result(request)
            assert rejected["status"] == "rejected"
            assert rejected["external_action"] == "none"
            assert rejected["field"] == "navigation_ref/link_offset/link_limit"
            args = {key: value for key, value in selected["second"]["next_request"].items()
                    if key != "tool"}
            return tool("page_venue_navigation", args, count)
        assert count == 9
        last = last_result(request)
        assert [item["text"] for item in last["links"]] == [
            f"Article {index}" for index in range(50, 58)
        ]
        assert last["next_request"] is None
        return tool("publish_result", {
            "title": "Venue links reviewed",
            "text": "The synthetic issue has later navigation links.",
            "citations": [], "limitations": ["No publication source was inspected"],
        }, count)

    monkeypatch.setattr(service_module, "fetch_public_source", fake_fetch)
    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(
        request_id="saved-catalog-paging", question="Review later venue links",
        agent_ids=["agent-synthesis"],
    ))
    drain(host, run.run_id, limit=35)
    assert host._run(run.run_id)["body"]["status"] == "completed"
    assert len(calls) == 9 and len(fetched) == 1
    assert len(host.store.records("venue_navigation")) == 1
    assert host.store.records("source_copy") == []
    assert host.store.records("publication") == []
    reopened = StateStore(tmp_path)
    result = page_venue_navigation(
        reopened, mission_id=host._run(run.run_id)["body"]["mission_id"],
        run_id=run.run_id,
        actor_id=host._control(run.run_id, "agent-synthesis")["body"]["actor_id"],
        navigation_ref=selected["first"]["navigation_ref"], link_offset=25,
        link_limit=25,
    )
    assert result["links"] == selected["second"]["links"]
    assert len(fetched) == 1
