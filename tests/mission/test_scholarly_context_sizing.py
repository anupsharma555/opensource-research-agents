"""Saved scholarly leads stay navigable without repeating a large tool reply."""

from __future__ import annotations

import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import ContextError, WorkingContext
from ora.mission.tool_results import ResultProjectionChanged
from ora.models.token_estimate import estimate_request_tokens
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import fixture, save_source_free_search_intent
from tests.mission.test_service import answer, drain, service, tool
from tests.mission.test_tool_result_paging import save_result


def _leads(store: StateStore, *, mission_id: str = "m", task_id: str = "task",
           actor_id: str = "peer") -> tuple[list[dict], list[dict]]:
    def save(tx):
        leads, pointers = [], []
        for index in range(9):
            delivery = tx.put("retrieval_delivery", f"scholarly-delivery-{index}", {})
            actor = "other-peer" if index == 8 else actor_id
            hit = tx.put("retrieval_hit", f"scholarly-hit-{index}", {
                "route": "pubmed.search", "mission_id": mission_id, "task_id": task_id,
                "agent_instance_id": actor, "delivery_ref": ref(delivery),
                "source": {"origin": "original_source",
                           "original_url": f"https://example.org/abstract/{index}",
                           "rights_status": "unknown", "content": None},
            })
            tx.link(hit, delivery, "derived_from")
            pointers.append(ref(hit))
            leads.append({
                "hit_id": hit["id"], "hit_ref": ref(hit),
                "title": f"Synthetic study {index} " + "long title " * 28,
                "url": f"https://example.org/abstract/{index}",
                "rights_status": "unknown", "access_surface": "metadata",
                "identifiers": {"synthetic_metadata": "candidate detail " * 35},
            })
        return {"leads": leads, "refs": pointers}

    result = store.transact("save-scholarly-leads", {}, save, mission_id=mission_id)
    return result["leads"], result["refs"]


def _collect(context: WorkingContext, result_id: str) -> tuple[dict, dict]:
    pieces = []
    request = {"result_id": result_id, "offset": 0, "limit": 1000,
               "projection_sha256": ""}
    first = None
    while True:
        page = context.read_tool_result(
            mission_id="m", actor_id="peer", task_id="task", **request,
        )
        if first is None:
            first = page
        assert len(canonical(page)) <= 3000
        pieces.append(page["text"])
        if page["next_request"] is None:
            return json.loads("".join(pieces)), first
        request = {key: page["next_request"][key]
                   for key in ("result_id", "offset", "limit", "projection_sha256")}


def test_large_scholarly_listing_is_compact_and_full_detail_is_scoped(tmp_path):
    store = StateStore(tmp_path)
    call, _task = fixture(store)
    call = save_source_free_search_intent(store, call, ())
    leads, refs = _leads(store)
    original = {"route": "pubmed.search", "requested_route": "pubmed.search",
                "outcome": "success", "hits": leads}
    assert len(canonical(original)) > 3000
    response, result_ref = save_result(store, call, "search_scholarly", original)
    context = WorkingContext(store)

    exchange, _receipt = context._tool_exchange(response)
    preview = json.loads(exchange[1]["content"])
    assert len(canonical(preview)) <= 3600
    assert preview["result_status"] == "uninspected_discovery_leads_not_source_evidence"
    assert preview["detail_request"]["result_id"] == result_ref["id"]
    assert preview["hits"][0]["hit_id"] == refs[0]["id"]
    assert all(hit["hit_id"] != refs[-1]["id"] for hit in preview["hits"])

    detail, first = _collect(context, result_ref["id"])
    assert len(detail["hits"]) == 8
    assert detail["hits"][0]["identifiers"] == leads[0]["identifiers"]
    assert detail["permission_status"] == "some_candidates_withheld_due_current_source_use"
    with pytest.raises(ContextError, match="another peer task"):
        context.read_tool_result(
            mission_id="m", actor_id="other-peer", task_id="task",
            result_id=result_ref["id"], offset=0, limit=1000, projection_sha256="",
        )

    old = store.get(**refs[0])
    store.transact("revise-scholarly-hit", {}, lambda tx: tx.put(
        "retrieval_hit", old["id"], old["body"], expected_version=old["version"],
    ), mission_id="m")
    with pytest.raises(ResultProjectionChanged):
        context.read_tool_result(
            mission_id="m", actor_id="peer", task_id="task",
            result_id=result_ref["id"], offset=0, limit=1000,
            projection_sha256=first["projection_sha256"],
        )
    revised, _ = _collect(context, result_ref["id"])
    assert refs[0]["id"] not in {hit["hit_id"] for hit in revised["hits"]}
    assert len(revised["hits"]) == 7


def test_short_scholarly_reply_rechecks_exact_hit_before_current_delivery(tmp_path):
    store = StateStore(tmp_path)
    call, _task = fixture(store)
    call = save_source_free_search_intent(store, call, ())
    leads, refs = _leads(store)
    response, result_ref = save_result(store, call, "search_scholarly", {
        "route": "pubmed.search", "outcome": "success", "hits": [leads[0]],
    })
    context = WorkingContext(store)
    current = json.loads(context._tool_exchange(response)[0][1]["content"])
    assert [hit["hit_id"] for hit in current["hits"]] == [refs[0]["id"]]

    old = store.get(**refs[0])
    store.transact("revise-short-scholarly-hit", {}, lambda tx: tx.put(
        "retrieval_hit", old["id"], old["body"], expected_version=old["version"],
    ), mission_id="m")
    updated = json.loads(context._tool_exchange(response)[0][1]["content"])
    assert updated["hits"] == []
    assert updated["permission_status"] == "some_candidates_withheld_due_current_source_use"
    saved = store.get(**result_ref)
    assert json.loads(store.read_blob(saved["body"]["blob"]["sha256"]))["content"] == canonical({
        "route": "pubmed.search", "outcome": "success", "hits": [leads[0]],
    })


@pytest.mark.parametrize("route", ["pubmed.search", "crossref.search"])
def test_large_scholarly_reply_is_bounded_in_next_serialized_request(
    tmp_path, monkeypatch, route,
):
    packets = []

    def model(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            return tool("search_scholarly", {"route": route, "query": "synthetic", "limit": 9}, 1)
        assert len(packets) == 2
        reply = next(json.loads(message["content"]) for message in packet["messages"]
                     if message.get("role") == "tool"
                     and message.get("name") == "search_scholarly")
        assert reply["result_status"] == "uninspected_discovery_leads_not_source_evidence"
        assert len(canonical(reply)) <= 3600
        assert reply["hits"] and reply["hits"][0]["hit_ref"]["version"] == 1
        assert "read_tool_result" in {item["function"]["name"] for item in packet["tools"]}
        assert estimate_request_tokens(request.data).tokens < 8000
        return answer("The synthetic leads require source inspection.", 2)

    host = service(tmp_path, monkeypatch, model)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"scholarly-compact-{route}", question="Find synthetic studies",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        leads, refs = _leads(
            host.store, mission_id=control["body"]["mission_id"],
            task_id=control["body"]["task_id"], actor_id=control["body"]["actor_id"],
        )

        def discovery(_store, _request, **_kwargs):
            return {"outcome": "success", "hits": [{
                "hit_ref": lead["hit_ref"], "title": lead["title"],
                "original_url": lead["url"], "access_surface": lead["access_surface"],
                "rights_status": lead["rights_status"],
                "scientific_metadata": lead["identifiers"],
            } for lead in leads[:8]], "window": None}

        monkeypatch.setattr("ora.mission.service.run_discovery_page", discovery)
        drain(host, run.run_id)
        assert len(packets) == 2
        assert host.state(run.run_id).snapshot.mission.status == "completed"
        assert len(refs) == 9
    finally:
        host.close()
