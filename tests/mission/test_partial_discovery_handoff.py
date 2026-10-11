"""An unavailable Exa leg cannot erase a saved scholarly hit across host restart."""

from __future__ import annotations

import json
import time

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission import MissionService
from ora.retrieval.providers import HTTPResult
from tests.mission.test_service import MODEL, ROOT, answer, drain, service, tool


def test_partial_exa_and_scholarly_success_survive_restart_without_duplicate_effects(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("EXA_API_KEY", "synthetic-key")
    model_requests: list[dict] = []
    sibling_requests: list[dict] = []
    route_calls: list[str] = []
    selected: dict[str, str] = {}
    original_abstract = (
        "A synthetic published abstract reports the comparison and an exact "
        "eight-week outcome. It does not report a subgroup result."
    )

    def model_transport(request, *, timeout):
        body = json.loads(request.data)
        if any(
            "You are Mxra" in str(item.get("content", ""))
            for item in body["messages"]
        ):
            sibling_requests.append(body)
            return answer("A separate synthetic review can proceed.", 100)
        model_requests.append(body)
        turn = len(model_requests)
        assert body["model"] == MODEL and body["reasoning_effort"] == "high"
        if turn == 1:
            return tool("search_scholarly", {
                "route": "exa.search", "query": "synthetic comparison", "limit": 2,
            }, turn)
        prior_message = next(
            item for item in reversed(body["messages"]) if item["role"] == "tool"
        )
        prior = json.loads(prior_message["content"])
        if turn == 2:
            assert prior["route"] == "exa.search" and prior["outcome"] == "partial"
            assert len(prior["hits"]) == 1 and len(prior["item_errors"]) == 1
            assert prior["hits"][0]["access_surface"] == "teaser"
            selected["exa_hit_id"] = prior["hits"][0]["hit_id"]
            return tool("search_scholarly", {
                "route": "europe_pmc.search", "query": "synthetic comparison",
                "limit": 1,
            }, turn)
        if turn == 3:
            assert prior["route"] == "europe_pmc.search"
            assert prior["outcome"] == "success" and len(prior["hits"]) == 1
            selected["scholarly_hit_id"] = prior["hits"][0]["hit_id"]
            return tool("search_scholarly", {
                "route": "exa.contents", "selected_id": "exa-candidate-1",
                "selection_reason": "Check whether this teaser has source text",
                "limit": 1,
            }, turn)
        if turn == 4:
            assert prior["route"] == "exa.contents"
            assert prior["outcome"] == "provider_failure"
            assert prior["hits"] == []
            # This scripted choice uses the ID observed on the earlier scholarly
            # turn; it proves the saved hit is actionable, not model autonomy.
            return tool("inspect_hit", {"hit_id": selected["scholarly_hit_id"]}, turn)
        assert turn == 5
        assert prior["status"] == "inspected"
        selected["source_id"] = prior["source_id"]
        assert original_abstract in json.dumps(body["messages"])
        return answer("The saved abstract can still be read; Exa Contents was unavailable.", turn)

    def discovery_transport(method, url, *, body=None, headers=None):
        route_calls.append(url)
        if url.endswith("/search") and "api.exa.ai" in url:
            return HTTPResult(200, json.dumps({
                "results": [
                    {"id": "exa-candidate-1", "url": "https://example.org/lead",
                     "title": "Synthetic teaser"},
                    {"id": 4, "url": "https://example.org/bad"},
                ],
                "costDollars": {"total": 0.01},
            }).encode(), {})
        if "europepmc" in url:
            return HTTPResult(200, json.dumps({
                "hitCount": 1, "nextCursorMark": "*",
                "resultList": {"result": [{
                    "source": "MED", "id": "12345", "pmid": "12345",
                    "doi": "10.1234/synthetic-partial", "title": "Synthetic trial",
                    "abstractText": original_abstract, "isOpenAccess": "Y",
                }]},
            }).encode(), {})
        assert url.endswith("/contents") and "api.exa.ai" in url
        return HTTPResult(402, b"{}", {})

    host = service(tmp_path, monkeypatch, model_transport)
    host.discovery_transport = discovery_transport
    try:
        run = host.start(LiveStartRequest(
            request_id="partial-discovery-restart", question="Compare synthetic source routes",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        original_schedule = host._schedule_model

        def checkpoint_after_three_results(control):
            result_count = len(host.store.records("context_tool_result"))
            if control["body"]["alias"] == "agent-critique" and result_count < 3:
                return False  # Let the independent branch advance after route failure.
            if control["body"]["alias"] == "agent-synthesis" and result_count >= 3:
                return False
            return original_schedule(control)

        monkeypatch.setattr(host, "_schedule_model", checkpoint_after_three_results)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            if (
                len(host.store.records("context_tool_result")) == 3
                and control["body"]["phase"] == "ready_model"
                and host._control(run.run_id, "agent-critique")["body"]["phase"] == "done"
            ):
                break
            time.sleep(0.005)
        assert len(host.store.records("context_tool_result")) == 3
        assert len(model_requests) == 3
        assert len(sibling_requests) == 1
        assert len(route_calls) == 3
        sibling_before = host._control(run.run_id, "agent-critique")
        assert sibling_before["body"]["report_ref"] is not None

        exa_hit = host.store.get("retrieval_hit", selected["exa_hit_id"])
        scholarly_hit = host.store.get("retrieval_hit", selected["scholarly_hit_id"])
        assert exa_hit["body"]["source"]["content"] is None
        assert exa_hit["body"]["source"]["access_surface"] == "teaser"
        assert scholarly_hit["body"]["source"]["content"] == original_abstract
        assert scholarly_hit["body"]["source"]["access_surface"] == "abstract"
        first_pages = host.store.records("discovery_page")
        assert len(first_pages) == 3
        first_deliveries = host.store.records("retrieval_delivery")
        assert len(first_deliveries) == 3
        unavailable = next(
            item for item in first_deliveries
            if item["body"]["envelope"]["request"]["route"] == "exa.contents"
        )
        assert unavailable["body"]["envelope"]["failure"]["code"] == "route_unavailable"
        exa_search = next(
            item for item in first_deliveries
            if item["body"]["envelope"]["request"]["route"] == "exa.search"
        )
        assert {
            (entry["unit"], entry["amount"], entry["basis"])
            for entry in exa_search["body"]["envelope"]["usage"]
        } >= {
            ("provider_request", 1, "observed"),
            ("usd_estimated_provider_response", 0.01, "observed"),
        }
    finally:
        host.close()

    resumed = MissionService(
        ROOT, tmp_path, model_session=host.session,
        discovery_transport=discovery_transport,
        execution_mode="offline_test", start_supervisor=False,
    )
    try:
        drain(resumed, run.run_id, limit=12)
        assert resumed._run(run.run_id)["body"]["status"] == "completed"
        assert len(model_requests) == 5
        assert len(sibling_requests) == 1
        assert len(route_calls) == 3  # Reopening never re-executed accepted routes.
        assert len(resumed.store.records("discovery_page")) == 3
        assert len(resumed.store.records("retrieval_delivery")) == 3
        assert len(resumed.store.records("context_tool_result")) == 4
        assert resumed.store.get("retrieval_hit", selected["scholarly_hit_id"]) == scholarly_hit
        assert resumed._control(run.run_id, "agent-critique")["body"]["report_ref"] == (
            sibling_before["body"]["report_ref"]
        )
        source = resumed.store.get("source_copy", selected["source_id"])
        assert source is not None
        assert source["body"]["content_sha256"]
        source_use = resumed.store.resolve(
            "source_use.copy", f"{source['id']}:{source['version']}"
        )
        assert source_use["body"]["retrieval_hit_ref"] == ref(scholarly_hit)
        assert len(resumed.store.records("source_copy")) == 1
        assert exa_hit["body"]["source"]["content"] is None
    finally:
        resumed.close()
