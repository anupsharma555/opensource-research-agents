"""Offline serialized-request checks for saved web-search navigation and sizing."""

import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import ContextError, WorkingContext
from ora.mission.tool_results import ResultProjectionChanged
from ora.mission.web_context import compact_search_result, require_search_operation
from ora.models.token_estimate import estimate_request_tokens
from ora.retrieval.providers import HTTPResult
from ora.state import StateError, StateStore
from ora.state.store import canonical
from tests.mission.test_context import fixture, save_source_free_search_intent
from tests.mission.test_service import answer, drain, service, tool


def _index(packet):
    return next(json.loads(item["content"]) for item in packet["messages"]
                if item.get("role") == "user" and item.get("content", "").startswith("{")
                and json.loads(item["content"]).get("schema") == "ora.working_context.v4")


def _result(packet, name):
    return next(json.loads(item["content"]) for item in reversed(packet["messages"])
                if item.get("role") == "tool" and item.get("name") == name)


def test_web_operation_and_five_candidates_survive_capability_turns(tmp_path, monkeypatch):
    """Read the next serialized packet; the fake model retains no private handle."""
    monkeypatch.setenv("EXA_API_KEY", "synthetic-search-key")
    packets = []
    input_estimates = []

    def model(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        input_estimates.append(estimate_request_tokens(request.data).tokens)
        step = len(packets)
        if step == 1:
            return tool("request_capability", {"tool_id": "search_web"}, step)
        if step == 2:
            return tool("search_web", {"mode": "quick", "limit": 5,
                                       "information_need": "Find synthetic public guidance",
                                       "queries": ["synthetic public guidance"]}, step)
        if step == 3:
            assert _result(packet, "search_web")["operation_ref"]["id"].startswith("web-search-")
            return tool("request_capability", {"tool_id": "read_search_results"}, step)
        if step == 4:
            operation = _index(packet)["latest_web_search_navigation"]["operation_id"]
            assert operation == _result(packets[2], "search_web")["operation_ref"]["id"]
            return tool("read_search_results", {"operation_id": operation}, step)
        if step == 5:
            result = _result(packet, "read_search_results")
            leads = [hit for entry in result["available_results"]
                     for hit in entry["result"]["hits"]]
            assert len(leads) == 5
            assert len({hit["hit_id"] for hit in leads}) == 5
            assert all(hit["hit_ref"]["id"] == hit["hit_id"] for hit in leads)
            assert all(hit["original_url"].startswith("https://example.org/") for hit in leads)
            assert result["result_status"] == "uninspected_discovery_leads_not_source_evidence"
            assert len(canonical(result)) <= 3600
            return tool("request_capability", {"tool_id": "fetch_source"}, step)
        assert step == 6
        navigation = _index(packet)["latest_web_search_navigation"]
        assert navigation["operation_id"] == _result(packets[4], "read_search_results")["operation_ref"]["id"]
        assert {lead["hit_id"] for lead in navigation["first_candidates"]} == {
            hit["hit_id"] for entry in _result(packets[4], "read_search_results")["available_results"]
            for hit in entry["result"]["hits"]}
        return answer("Discovery leads were saved for later source inspection.", step)

    host = service(tmp_path, monkeypatch, model)
    def search(method, url, **kwargs):
        return HTTPResult(200, json.dumps({"results": [{
            "id": f"synthetic-{n}", "url": f"https://example.org/{n}",
            "title": ("Synthetic guidance title " * 8) + str(n),
            "highlights": ["Uninspected candidate teaser " * 12],
        } for n in range(5)]}).encode(), {})
    host.web_search.transport = search
    try:
        run = host.start(LiveStartRequest(
            request_id="web-context-sizing", question="Find synthetic public guidance.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=20)
        assert len(packets) == 6, [
            (number, [message.get("name") for message in packet["messages"]
                      if message.get("role") == "tool"])
            for number, packet in enumerate(packets, 1)
        ]
        assert packets[4]["max_completion_tokens"] == 8192
        assert input_estimates[4] < 8000, input_estimates
        assert len(host.store.records("web_search_operation")) == 1
        saved = next(
            record for record in host.store.records("context_tool_result")
            if json.loads(host.store.read_blob(record["body"]["blob"]["sha256"]))
            .get("name") == "read_search_results"
        )
        full = json.loads(host.store.read_blob(saved["body"]["blob"]["sha256"]))
        assert len(canonical(json.loads(full["content"]))) > len(canonical(_result(packets[4], "read_search_results")))
        response = host.store.get(**saved["body"]["response_ref"])
        call = host.store.get(**response["body"]["call_ref"])
        scope = {"mission_id": saved["body"]["mission_id"],
                 "actor_id": call["body"]["instance_id"],
                 "task_id": call["body"]["task_id"]}
        chunks, offset, digest = [], 0, ""
        while True:
            page = host.context.read_tool_result(
                **scope,
                result_id=saved["id"], offset=offset, limit=1000,
                projection_sha256=digest,
            )
            assert len(canonical(page)) <= 3000
            chunks.append(page["text"])
            if page["next_request"] is None:
                break
            offset = page["next_request"]["offset"]
            digest = page["projection_sha256"]
        detail = json.loads("".join(chunks))
        assert len(detail["available_results"]) == 5
        visible_hits = {
            hit["hit_id"] for entry in _result(packets[4], "read_search_results")[
                "available_results"
            ] for hit in entry["result"]["hits"]
        }
        assert {entry["result"]["hits"][0]["hit_id"] for entry in detail[
            "available_results"
        ]} == visible_hits
        for entry in detail["available_results"]:
            assert entry["provider"] == "exa"
            assert entry["query"] == "synthetic public guidance"
            assert entry["result"]["shared_projection"] is True
            hit = entry["result"]["hits"][0]
            assert hit["route"] == "exa.search"
            saved_hit = host.store.get(**hit["hit_ref"])
            assert saved_hit["id"] == hit["hit_id"]
            assert host.store.get(**saved_hit["body"]["shared_origin_ref"]) is not None
        with pytest.raises(ContextError, match="another peer task"):
            host.context.read_tool_result(
                **{**scope, "actor_id": "different-peer"}, result_id=saved["id"],
                offset=0, limit=1000, projection_sha256="",
            )
        reopened = WorkingContext(StateStore(tmp_path))
        navigation = reopened._archived_tool_observation(saved["body"]["response_ref"])["web_search"]
        assert navigation["operation_id"] == _result(packets[4], "read_search_results")["operation_ref"]["id"]
        first_hit_ref = detail["available_results"][0]["result"]["hits"][0]["hit_ref"]
        stale_hit = host.store.get(**first_hit_ref)
        host.store.transact(
            "synthetic-hit-policy-change", {},
            lambda tx: tx.put("retrieval_hit", stale_hit["id"], stale_hit["body"],
                              expected_version=stale_hit["version"]),
            mission_id=scope["mission_id"],
        )
        with pytest.raises(ResultProjectionChanged):
            host.context.read_tool_result(
                **scope, result_id=saved["id"], offset=offset, limit=1000,
                projection_sha256=digest,
            )
        changed = host.context.read_tool_result(
            **scope, result_id=saved["id"], offset=0, limit=1000,
            projection_sha256="",
        )
        changed_text = changed["text"]
        while changed["next_request"]:
            changed = host.context.read_tool_result(
                **scope, **{key: changed["next_request"][key]
                            for key in ("result_id", "offset", "limit", "projection_sha256")},
            )
            changed_text += changed["text"]
        changed_detail = json.loads(changed_text)
        assert len(changed_detail["available_results"][0]["result"]["hits"]) == 0
        assert changed_detail["available_results"][0]["result"]["permission_status"] == (
            "some_candidates_withheld_due_current_source_use"
        )
    finally:
        host.close()


def test_oversized_first_candidate_keeps_exact_handle_and_page_request():
    pointer = {"kind": "retrieval_hit", "id": "synthetic-hit", "version": 1}
    value = {
        "operation_ref": {"kind": "web_search_operation", "id": "synthetic-operation", "version": 1},
        "status": "completed", "continuation_cursor": 1, "has_more": False,
        "pending": 0, "failed": 0, "available_results": [{"provider": "exa", "result": {
            "outcome": "success", "hits": [{"hit_id": "synthetic-hit", "hit_ref": pointer,
            "original_url": "https://example.org/" + "long" * 2000,
            "title": "Long synthetic title " * 200,
            "candidate_excerpt": "Uninspected teaser " * 200}],
        }}],
    }
    result_ref = {"kind": "context_tool_result", "id": "synthetic-result", "version": 1}
    preview = compact_search_result(value, result_ref=result_ref, max_chars=1200)
    assert len(canonical(preview)) <= 1200
    assert preview["available_results"][0]["result"]["hits"][0]["hit_ref"] == pointer
    assert preview["detail_request"]["result_id"] == result_ref["id"]


@pytest.mark.parametrize("status,outcome", [("completed", "empty"), ("failed", "provider_failure")])
def test_empty_or_failed_search_retains_operation_diagnostic(status, outcome):
    value = {"operation_ref": {"kind": "web_search_operation", "id": "synthetic-operation", "version": 1},
             "status": status, "continuation_cursor": 1, "has_more": False,
             "pending": 0, "failed": int(status == "failed"), "available_results": [{
                 "provider": "exa", "result": {"outcome": outcome, "reason": "synthetic-condition",
                                               "hits": []},
             }]}
    preview = compact_search_result(value, result_ref={
        "kind": "context_tool_result", "id": "synthetic-result", "version": 1,
    })
    assert preview["operation_ref"] == value["operation_ref"]
    assert preview["available_results"][0]["result"]["outcome"] == outcome
    assert preview["available_results"][0]["result"]["reason"] == "synthetic-condition"


def test_web_navigation_and_current_turn_bind_source_lineage_then_hide_on_revoke(tmp_path):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    def save_source(tx):
        copy = tx.put("source_copy", "web-source", {
            "mission_id": "m", "rights_status": "permitted",
        })
        use = tx.put("source_use", "web-use", {
            "copy_ref": ref(copy), "allowed_uses": ["model_input"],
            "policy_status": "granted",
        })
        tx.bind("source_use.copy", "web-source:1", "source_use", use["id"])
        operation = tx.put("web_search_operation", "synthetic-web-operation", {
            "mission_id": "m", "task_id": "task", "actor_id": "peer",
        })
        return {"copy_ref": ref(copy), "use_ref": ref(use), "operation_ref": ref(operation)}
    saved_source = store.transact(
        "web-source", {}, save_source, mission_id="m",
    )
    copy_ref, use_ref, operation_ref = (
        saved_source[key] for key in ("copy_ref", "use_ref", "operation_ref")
    )
    prior_call = save_source_free_search_intent(store, call, (copy_ref,))
    query = "SOURCE_DERIVED_SYNTHETIC_QUERY"
    private = store.put_blob(canonical({
        "content": None, "tool_calls": [{"id": "web-search-call", "type": "function",
            "function": {"name": "search_web", "arguments": canonical({
                "mode": "quick", "information_need": query, "queries": [query],
            })}}],
    }).encode())
    response_ref = store.transact("web-response", {}, lambda tx: ref(tx.put(
        "model_response_receipt", "web-response", {"mission_id": "m", "task_id": "task",
            "call_ref": prior_call, "status": "tool_requested", "blob": private,
        },
    )), mission_id="m")
    context = WorkingContext(store)
    context.save_tool_result(
        "web-result", response_ref=response_ref, tool_call_id="web-search-call",
        name="search_web", content=canonical({
            "operation_ref": operation_ref, "status": "completed",
            "available_results": [], "continuation_cursor": 0,
            "has_more": False, "pending": 0, "failed": 0,
        }),
    )
    current = context.assemble(call, task, tool_turn_refs=[response_ref])
    archived = context.assemble(call, task, archived_tool_turn_refs=[response_ref])
    assert context.context_source_dependency_refs(current) == (copy_ref,)
    assert context.context_source_dependency_refs(archived) == (copy_ref,)
    assert json.loads(archived.messages[-1]["content"])["latest_web_search_navigation"]["operation_id"] == operation_ref["id"]
    operation = store.get(**operation_ref)
    store.transact("advance-web-operation", {}, lambda tx: tx.put(
        "web_search_operation", operation["id"], operation["body"],
        expected_version=operation["version"],
    ), mission_id="m")
    assert context._archived_tool_observation(response_ref)["web_search"]["operation_id"] == operation_ref["id"]
    with pytest.raises(StateError, match="another peer task"):
        require_search_operation(store, {"operation_ref": operation_ref}, {
            "mission_id": "m", "actor_id": "other-peer", "task_id": "task",
        })
    with pytest.raises(StateError, match="unavailable"):
        require_search_operation(store, {"operation_ref": {
            **operation_ref, "version": 999,
        }}, {"mission_id": "m", "actor_id": "peer", "task_id": "task"})

    store.transact("revoke-web-use", {}, lambda tx: tx.put(
        "source_use", use_ref["id"], {
            **tx.get(**use_ref)["body"], "allowed_uses": [], "policy_status": "revoked",
        }, expected_version=use_ref["version"]), mission_id="m")
    after_current = context.assemble(call, task, tool_turn_refs=[response_ref])
    after_archived = context.assemble(call, task, archived_tool_turn_refs=[response_ref])
    assert query not in canonical(after_current.messages) + canonical(after_archived.messages)
    assert "latest_web_search_navigation" not in json.loads(after_archived.messages[-1]["content"])
