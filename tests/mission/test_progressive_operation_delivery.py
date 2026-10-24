"""Early search results reach an ordinary peer before the search finishes."""

import hashlib
import json
import threading
import time

from ora.contracts.models import LiveStartRequest
from ora.retrieval.providers import HTTPResult
from tests.mission.test_service import answer, serialized_peer_name, service, tool


def test_progressive_search_result_reaches_model_and_provisional_note(tmp_path, monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "synthetic-search-key")
    slow_entered = threading.Event()
    release_slow = threading.Event()
    packets = {"Sage": [], "Mxra": []}
    failures = []
    chosen = {}
    provider_queries = []

    def latest(packet, name):
        matches = [json.loads(item["content"]) for item in packet["messages"]
                   if item.get("role") == "tool" and item.get("name") == name]
        assert matches, name
        return matches[-1]

    def model(request, *, timeout):
        packet = json.loads(request.data)
        name = serialized_peer_name(packet)
        packets[name].append(packet)
        step = len(packets[name])
        try:
            if name == "Mxra":
                assert step == 1
                return answer("Independent synthetic critique completed.", step)
            if step == 1:
                return tool("request_capability", {"tool_id": "search_web"}, step)
            if step == 2:
                return tool("search_web", {
                    "mode": "research", "information_need": "Locate two synthetic leads for a provisional plan",
                    "queries": ["slow first query", "fast second query"],
                }, step)
            if step == 3:
                chosen["operation_id"] = latest(packet, "search_web")["operation_ref"]["id"]
                return tool("request_capability", {"tool_id": "read_search_results"}, step)
            if step == 4:
                return tool("read_search_results", {
                    "operation_id": chosen["operation_id"], "cursor": 0, "limit": 1, "wait": True,
                }, step)
            if step == 5:
                # This request follows a host wake, not a model polling loop.
                assert slow_entered.is_set()
                return tool("read_search_results", {
                    "operation_id": chosen["operation_id"], "cursor": 0, "limit": 1,
                }, step)
            if step == 6:
                raw = request.data if isinstance(request.data, bytes) else request.data.encode()
                chosen["early_sent_sha256"] = hashlib.sha256(raw).hexdigest()
                early = latest(packet, "read_search_results")
                assert early["operation_ref"]["id"] == chosen["operation_id"]
                assert early["status"] == "pending" and early["pending"] == 1
                assert early["available_results"][0]["provider"] == "exa"
                hit = early["available_results"][0]["result"]["hits"][0]
                assert hit["excerpt_status"] == "uninspected_candidate"
                assert hit["candidate_excerpt_preview"] == "Synthetic fast lead, not checked evidence."
                assert hit["original_url"] == "https://example.org/fast-lead"
                chosen["early_cursor"] = early["continuation_cursor"]
                chosen["early_hit"] = hit["hit_id"]
                return tool("request_capability", {"tool_id": "write_note"}, step)
            if step == 7:
                assert "write_note" in {item["function"]["name"] for item in packet["tools"]}
                return tool("write_note", {
                    "note_id": "provisional-fast-lead", "category": "hypothesis",
                    "text": "Provisional lead from the fast query; the slow query is still pending and the candidate is uninspected.",
                    "expected_version": 0,
                }, step)
            if step == 8:
                assert latest(packet, "write_note")["status"] == "saved"
                return tool("read_search_results", {
                    "operation_id": chosen["operation_id"], "cursor": chosen["early_cursor"],
                    "limit": 1, "wait": True,
                }, step)
            # The test ends with this peer waiting for the held second result.
            raise AssertionError(f"unexpected Sage model request {step}")
        except Exception as exc:
            failures.append((name, step, repr(exc)))
            raise

    def search(method, url, **kwargs):
        query = kwargs["body"]["query"]
        provider_queries.append(query)
        if query == "slow first query":
            slow_entered.set()
            if not release_slow.wait(30):
                raise AssertionError("held synthetic provider query was not released")
        source_id = "slow-lead" if query == "slow first query" else "fast-lead"
        return HTTPResult(200, json.dumps({"results": [{
            "id": source_id, "url": f"https://example.org/{source_id}",
            "title": source_id,
            "highlights": ["Synthetic fast lead, not checked evidence."],
        }]}).encode(), {})

    host = service(tmp_path, monkeypatch, model)
    host.web_search.transport = search
    try:
        run = host.start(LiveStartRequest(
            request_id="progressive-search-delivery",
            question="Find two synthetic leads and record a provisional interpretation while work continues.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))

        def drive_until(predicate, *, seconds=12):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                host._drive_run(host._run(run.run_id))
                if failures:
                    raise AssertionError(failures)
                if predicate():
                    return
                time.sleep(0.01)
            raise AssertionError((
                "condition not reached", {name: len(items) for name, items in packets.items()},
                [(control["body"]["alias"], control["body"]["phase"],
                  control["body"].get("waiting_reason")) for control in host._controls(run.run_id)],
                host.store.records("mission_host_diagnostic"),
            ))

        drive_until(lambda: slow_entered.is_set()
                    and host._control(run.run_id, "agent-critique")["body"]["phase"] == "done")
        assert slow_entered.is_set()
        operation = host.store.records("web_search_operation")[0]
        assert len(operation["body"]["deliveries"]) < len(operation["body"]["items"])

        # The fast provider request finishes while the slow callback stays held.
        # Sage uses the normal reader before writing a note.
        drive_until(lambda: host.store.get("working_note", "provisional-fast-lead") is not None
                    and host._control(run.run_id, "agent-synthesis")["body"].get("search_wait"))

        note = host.store.get("working_note", "provisional-fast-lead")
        assert note["body"]["task_id"] == host._control(run.run_id, "agent-synthesis")["body"]["task_id"]
        assert note["body"]["category"] == "hypothesis"
        assert chosen["early_hit"]
        assert len(packets["Sage"]) == 8 and len(packets["Mxra"]) == 1
        assert any(intent["body"]["blob"]["sha256"] == chosen["early_sent_sha256"]
                   for intent in host.store.records("model_request_intent"))
        operation = host.store.get("web_search_operation", chosen["operation_id"])
        assert len(operation["body"]["deliveries"]) == 1
        assert len(operation["body"]["items"]) == 2
        intent_count = len(host.store.records("model_request_intent"))
        for _ in range(4):
            host._drive_run(host._run(run.run_id))
        assert len(packets["Sage"]) == 8
        assert len(host.store.records("model_request_intent")) == intent_count
        assert len(host.store.records("source_copy")) == 0
        assert not failures
    finally:
        release_slow.set()
        host.close()

    assert sorted(provider_queries) == ["fast second query", "slow first query"]
