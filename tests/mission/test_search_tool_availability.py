"""Attach saved-search readers only when this peer task can use them."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

from ora.agents.capabilities import content_hash
from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.tool_catalog import tool_contract_matches
from ora.retrieval.providers import HTTPResult
from tests.mission.test_service import answer, drain, service, tool


def _attached(packet: dict) -> set[str]:
    return {item["function"]["name"] for item in packet["tools"]}


def _tool_reply(packet: dict, name: str) -> dict:
    return next(json.loads(message["content"]) for message in reversed(packet["messages"])
                if message.get("role") == "tool" and message.get("name") == name)


def test_saved_operation_reader_and_eligible_fetch_are_attached_without_selection_turns(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("EXA_API_KEY", "synthetic-search-key")
    packets = []
    selected = {}

    def model(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        tools = _attached(packet)
        if step == 1:
            assert "read_search_results" not in tools
            assert "fetch_source" not in tools
            return tool("request_capability", {"tool_id": "search_web"}, step)
        if step == 2:
            assert "search_web" in tools
            return tool("search_web", {
                "mode": "quick", "information_need": "Find a synthetic public page",
                "queries": ["synthetic public page"],
            }, step)
        if step == 3:
            assert "read_search_results" in tools
            selected["operation_id"] = _tool_reply(packet, "search_web")["operation_ref"]["id"]
            return tool("read_search_results", {
                "operation_id": selected["operation_id"], "wait": True,
            }, step)
        assert step == 4
        result = _tool_reply(packet, "read_search_results")
        assert result["operation_ref"]["id"] == selected["operation_id"]
        assert result["available_results"][0]["result"]["hits"]
        assert "fetch_source" in tools
        selected["hit_id"] = result["available_results"][0]["result"]["hits"][0]["hit_id"]
        return answer("The saved lead still needs source inspection.", step)

    host = service(tmp_path, monkeypatch, model)
    calls = []

    def search(_method, _url, **_kwargs):
        calls.append(1)
        return HTTPResult(200, json.dumps({"results": [{
            "id": "synthetic-lead", "url": "https://example.org/public-page",
            "title": "Synthetic public page", "highlights": ["Uninspected candidate."],
        }]}).encode(), {})

    host.web_search.transport = search
    try:
        run = host.start(LiveStartRequest(
            request_id="auto-search-readers", question="Find a synthetic public page",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=20)
        assert len(packets) == 4
        assert len(calls) == 1
        assert host.state(run.run_id).snapshot.mission.status == "completed"
        assert selected["hit_id"]
    finally:
        host.close()


def test_pending_and_cancelled_operations_are_task_scoped_and_stale_leads_do_not_attach(
    tmp_path, monkeypatch,
):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        first = host.start(LiveStartRequest(
            request_id="scoped-reader-first", question="First synthetic task",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(first.run_id, "agent-synthesis")
        body = control["body"]
        config = host._configuration(control)
        initial, _ = host._exposed_tools(control, config)
        assert "read_search_results" not in initial and "fetch_source" not in initial

        operation_body = {
            "mission_id": body["mission_id"], "run_id": body["run_id"],
            "actor_id": body["actor_id"], "task_id": body["task_id"],
            "parent_spec": {}, "deliveries": [], "cancel_requested": False,
        }
        host.store.transact("pending-search-operation", {}, lambda tx: tx.put(
            "web_search_operation", "pending-search-operation", operation_body,
        ), mission_id=body["mission_id"])
        pending, _ = host._exposed_tools(control, config)
        assert "read_search_results" in pending and "fetch_source" not in pending

        saved = host.store.get("web_search_operation", "pending-search-operation")
        host.store.transact("cancel-search-operation", {}, lambda tx: tx.put(
            "web_search_operation", saved["id"],
            {**saved["body"], "cancel_requested": True},
            expected_version=saved["version"],
        ), mission_id=body["mission_id"])
        cancelled, _ = host._exposed_tools(control, config)
        assert "read_search_results" in cancelled and "fetch_source" not in cancelled

        def save_candidate(tx):
            delivery = tx.put("retrieval_delivery", "available-web-delivery", {})
            hit = tx.put("retrieval_hit", "available-web-hit", {
                "route": "exa.search", "mission_id": body["mission_id"],
                "task_id": body["task_id"], "agent_instance_id": body["actor_id"],
                "delivery_ref": ref(delivery),
                "source": {"origin": "original_source",
                           "original_url": "https://example.org/candidate",
                           "rights_status": "unknown", "content": None},
            })
            tx.link(hit, delivery, "derived_from")
            current = tx.get("web_search_operation", "pending-search-operation")
            tx.put("web_search_operation", current["id"], {
                **current["body"], "deliveries": [{"result": {"hits": [{"hit_ref": ref(hit)}]}}],
            }, expected_version=current["version"])
            return {"hit_ref": ref(hit)}

        candidate = host.store.transact(
            "save-eligible-web-candidate", {}, save_candidate, mission_id=body["mission_id"],
        )["hit_ref"]
        with_hit, _ = host._exposed_tools(control, config)
        assert "fetch_source" in with_hit
        # Releasing an explicit selection does not suppress a contextual reader.
        released = {**control, "body": {**body, "requested_capabilities": []}}
        after_release, _ = host._exposed_tools(released, config)
        assert "read_search_results" in after_release and "fetch_source" in after_release
        native = {**control, "body": {**body, "requested_capabilities": ["browser_search"]}}
        native_tools, _ = host._exposed_tools(native, config)
        assert native_tools == ["browser_search"]

        old_hit = host.store.get(**candidate)
        host.store.transact("supersede-web-hit", {}, lambda tx: tx.put(
            "retrieval_hit", old_hit["id"], old_hit["body"],
            expected_version=old_hit["version"],
        ), mission_id=body["mission_id"])
        stale, _ = host._exposed_tools(control, config)
        assert "read_search_results" in stale and "fetch_source" not in stale

        second = host.start(LiveStartRequest(
            request_id="scoped-reader-second", question="Second synthetic task",
            agent_ids=["agent-synthesis"],
        ))
        other = host._control(second.run_id, "agent-synthesis")
        foreign, _ = host._exposed_tools(other, host._configuration(other))
        assert "read_search_results" not in foreign and "fetch_source" not in foreign
    finally:
        host.close()


def test_old_prepared_search_schema_without_refresh_remains_exactly_compatible(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("EXA_API_KEY", "synthetic-search-key")
    old = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    tool_descriptor = old.catalog.tools["search_web"]
    schema = deepcopy(tool_descriptor.schema)
    schema["properties"].pop("refresh")
    old.catalog.tools["search_web"] = replace(tool_descriptor, schema=schema)
    old_descriptor = old.catalog.tools["search_web"].descriptor()
    current_descriptor = tool_descriptor.descriptor()
    assert tool_contract_matches(current_descriptor, old_descriptor)
    for changed in (
        {**old_descriptor, "permission": "different:permission"},
        {**old_descriptor, "schema": {**schema, "required": [*schema["required"], "limit"]}},
        {**old_descriptor, "schema": {**schema, "properties": {
            **schema["properties"], "limit": {"type": "string"},
        }}},
    ):
        changed = {**changed, "schema_sha256": content_hash(changed["schema"])}
        assert not tool_contract_matches(current_descriptor, changed)

    try:
        run = old.start(LiveStartRequest(
            request_id="saved-search-schema-before-refresh",
            question="Find one synthetic public page", agent_ids=["agent-synthesis"],
        ))
        control = old._control(run.run_id, "agent-synthesis")
        assert old._schedule_model(control)
        control = old._control(run.run_id, "agent-synthesis")
        pointer = control["body"]["call_ref"]
        intent = old.store.get(
            "model_request_intent", f"model-intent:{pointer['id']}:{pointer['version']}",
        )
        original_request = old.store.read_blob(intent["body"]["blob"]["sha256"])
    finally:
        old.close()

    packets = []

    def model(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            assert request.data == original_request
            return tool("request_capability", {"tool_id": "search_web"}, 1)
        if len(packets) == 2:
            assert "search_web" in _attached(packet), [_attached(item) for item in packets]
            attached = next(item["function"] for item in packet["tools"]
                            if item["function"]["name"] == "search_web")
            assert "refresh" in attached["parameters"]["properties"]
            return tool("search_web", {
                "mode": "quick", "information_need": "Find a synthetic page",
                "queries": ["synthetic page"],
            }, 2)
        return answer("The saved search operation is available.", len(packets))

    resumed = service(tmp_path, monkeypatch, model)
    resumed.web_search.transport = lambda *_args, **_kwargs: HTTPResult(
        200, json.dumps({"results": []}).encode(), {},
    )
    try:
        drain(resumed, run.run_id, limit=20)
        assert len(packets) >= 3
        operations = resumed.store.records("web_search_operation")
        assert len(operations) == 1
        assert operations[0]["body"]["arguments"].get("refresh", False) is False
        assert resumed.store.read_blob(intent["body"]["blob"]["sha256"]) == original_request
    finally:
        resumed.close()
