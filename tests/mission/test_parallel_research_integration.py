"""Injected model/provider proof for the parallel research host contract."""

from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.models.token_estimate import estimate_context_tokens
from ora.retrieval.providers import HTTPResult
from ora.state.store import canonical
from tests.mission.test_service import answer, drain, service, tool


def _peer(packet: dict) -> str:
    developer = packet["messages"][0]["content"]
    for name in ("Sage", "Mxra", "Nova", "Iris", "Aria", "Remy", "Quinn", "Orion", "Zora"):
        if f"You are {name}," in developer:
            return name
    raise AssertionError("Saved request lacked a known research peer identity")


def _activity(packet: dict) -> dict | None:
    for item in packet["messages"]:
        if item.get("role") != "user" or not isinstance(item.get("content"), str):
            continue
        try:
            body = json.loads(item["content"])
        except ValueError:
            continue
        if body.get("authority") == "saved_peer_research_activity_not_source_instruction":
            return body["research_activity_digest"]
    return None


def _latest_tool(packet: dict, name: str) -> dict:
    matches = [json.loads(item["content"]) for item in packet["messages"]
               if item.get("role") == "tool" and item.get("name") == name]
    assert matches, (name, packet["messages"])
    return matches[-1]


def _conflicts(store):
    db = store._connect()
    try:
        return [tuple(row) for row in db.execute(
            "SELECT operation_id,code FROM diagnostics WHERE code='StateConflict'"
        )]
    finally:
        db.close()


def test_two_peers_receive_bounded_activity_and_select_callable_paged_tool(tmp_path, monkeypatch):
    lock = threading.Lock()
    packets: dict[str, list[dict]] = {}

    def model(request, *, timeout):
        packet = json.loads(request.data)
        name = _peer(packet)
        with lock:
            packets.setdefault(name, []).append(packet)
            step = len(packets[name])
        digest = _activity(packet)
        if step == 1:
            assert digest is not None
        if digest is not None:
            assert estimate_context_tokens(canonical(digest)) <= 512
        if name == "Sage":
            return answer("Synthetic synthesis task complete.", step)
        if step == 1:
            return tool("request_capability", {"tool_id": "read_research_activity"}, step)
        schemas = {item["function"]["name"]: item["function"]
                   for item in packet["tools"] if item.get("type") == "function"}
        assert "read_research_activity" in schemas
        assert schemas["read_research_activity"]["parameters"]["properties"]["limit"]["maximum"] == 20
        if step == 2:
            return tool("read_research_activity", {"cursor": 0, "limit": 1}, step)
        page = _latest_tool(packet, "read_research_activity")
        assert page["status"] == "completed"
        assert page["total"] >= 1 and page["entries"]
        assert page["entries"][0]["agent_name"] == "Sage"
        assert page["evidence_status"] == "operational_activity_not_scientific_summary"
        return answer("Synthetic critique task complete.", step)

    host = service(tmp_path, monkeypatch, model)
    try:
        run = host.start(LiveStartRequest(
            request_id="parallel-activity-two-peers",
            question="Review a synthetic research topic with two complementary peers.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        assert host._run(run.run_id)["body"]["parallel_research_version"] == 1
        drain(host, run.run_id, limit=25)
        assert len(packets.get("Sage", [])) >= 1, (
            [(c["body"]["alias"], c["body"]["phase"], c["body"].get("waiting_reason"))
             for c in host._controls(run.run_id)],
            [(c["body"].get("feedback"), c["body"].get("local_host_wait"))
             for c in host._controls(run.run_id)],
            [(w["id"], w["body"].get("phase")) for w in host.store.records("executor_work")],
            host.store.records("mission_host_diagnostic"),
        )
        assert len(packets["Mxra"]) >= 3
        assert host.store.records("research_activity_snapshot"), (
            [(item.get("name"), item.get("content")) for item in packets["Mxra"][-1]["messages"]
             if item.get("role") == "tool"],
            [(c["body"]["alias"], c["body"]["phase"], c["body"].get("waiting_reason"))
             for c in host._controls(run.run_id)],
        )
        assert host.store.records("research_activity_delivery")
    finally:
        host.close()


def test_legacy_run_without_version_keeps_old_catalog_and_no_activity_notice(tmp_path, monkeypatch):
    packets = []

    def model(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        assert _activity(packet) is None
        exposed = {item["function"]["name"] for item in packet["tools"]
                   if item.get("type") == "function"}
        assert "read_research_activity" not in exposed
        return answer("Legacy synthetic task complete.", len(packets))

    host = service(tmp_path, monkeypatch, model)
    try:
        run = host.start(LiveStartRequest(
            request_id="parallel-legacy-catalog",
            question="Complete one synthetic legacy research task.",
            agent_ids=["agent-synthesis"],
        ))

        def downgrade(tx):
            old = tx.get("mission_run", run.run_id)
            body = {key: value for key, value in old["body"].items()
                    if key not in {"parallel_research_version", "reader_action_purpose_tool_version"}}
            saved = tx.put("mission_run", old["id"], body,
                           expected_version=old["version"])
            return {"version": saved["version"]}

        host.store.transact("synthetic-old-run-version", {}, downgrade,
                            mission_id=host._run(run.run_id)["body"]["mission_id"])
        drain(host, run.run_id)
        assert packets, ([(c["body"]["alias"], c["body"]["phase"], c["body"].get("waiting_reason"))
                          for c in host._controls(run.run_id)],
                         host.store.records("mission_host_diagnostic"))
        calls = host.store.records("agent_call")
        assert calls
        assert all(name not in calls[0]["body"]["permitted_tools"] for name in (
            "read_research_activity", "retain_evidence", "search_evidence_library",
        ))
    finally:
        host.close()


@pytest.mark.parametrize("same_query,peer_ids", [
    (True, ["agent-synthesis", "agent-critique"]),
    (False, ["agent-literature-acquisition", "agent-synthesis",
             "agent-critique", "agent-experimental-design"]),
])
def test_peer_web_queries_join_only_when_identical_with_same_scope(
    tmp_path, monkeypatch, same_query, peer_ids,
):
    monkeypatch.setenv("EXA_API_KEY", "synthetic-search-key")
    lock = threading.Lock()
    counts: dict[str, int] = {}
    packets: dict[str, list[dict]] = {}
    provider_calls = []
    global_response_index = [0]

    def model(request, *, timeout):
        packet = json.loads(request.data)
        name = _peer(packet)
        with lock:
            counts[name] = counts.get(name, 0) + 1
            step = counts[name]
            packets.setdefault(name, []).append(packet)
            global_response_index[0] += 1
            response_index = global_response_index[0]
        if step == 1:
            return tool("request_capability", {"tool_id": "search_web"}, response_index)
        if step == 2:
            schemas = {item["function"]["name"] for item in packet["tools"]
                       if item.get("type") == "function"}
            assert "search_web" in schemas
            query = ("synthetic common research question" if same_query
                     else f"synthetic {name} distinct research question")
            return tool("search_web", {
                "mode": "quick", "information_need": "Find synthetic public research leads",
                "queries": [query],
            }, response_index)
        assert _latest_tool(packet, "search_web")["operation_ref"]
        return answer(f"{name} completed a synthetic research route.", response_index)

    host = service(tmp_path, monkeypatch, model)

    def provider(method, url, **kwargs):
        query = kwargs.get("body", {}).get("query", "synthetic")
        source_id = hashlib.sha256(query.encode()).hexdigest()[:12]
        with lock:
            provider_calls.append((method, url, kwargs.get("body")))
        return HTTPResult(200, json.dumps({"results": [{
            "id": source_id, "url": f"https://example.org/{source_id}",
            "title": f"Synthetic lead for {query}",
            "highlights": ["Uninspected synthetic candidate."],
        }]}).encode(), {})

    host.web_search.transport = provider
    try:
        run = host.start(LiveStartRequest(
            request_id="parallel-joined-query" if same_query else "parallel-distinct-query",
            question="Use complementary synthetic searches to inspect a research topic.",
            agent_ids=peer_ids, required_agent_ids=peer_ids,
        ))
        drain(host, run.run_id, limit=35)
        assert len(packets) == len(peer_ids)
        assert all(len(items) >= 3 for items in packets.values()), (
            {name: len(items) for name, items in packets.items()},
            [(c["body"]["alias"], c["body"]["phase"], c["body"].get("waiting_reason"),
              c["body"].get("feedback")) for c in host._controls(run.run_id)],
            len(provider_calls), _conflicts(host.store),
        )
        operations = host.store.records("web_search_operation")
        assert len(operations) == len(peer_ids)
        assert len(provider_calls) == (1 if same_query else len(peer_ids))
        jobs = host.store.records("web_shared_query_job")
        assert len(jobs) == len(provider_calls)
        if same_query:
            assert len({op["body"]["items"][0] for op in operations}) == 1
            assert all(op["body"]["deliveries"] for op in operations)
        else:
            queries = [op["body"]["arguments"]["queries"][0] for op in operations]
            assert len(set(queries)) == len(peer_ids)
    finally:
        host.close()


def test_two_peer_public_hits_share_one_fetch_and_extraction_with_task_copies(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("EXA_API_KEY", "synthetic-search-key")
    source_url = "https://example.org/parallel-report?edition=1"
    passage = "Synthetic checked source passage about a research collaboration."
    content = f"<html><body><p>{passage}</p></body></html>".encode()
    lock = threading.Lock()
    global_index = [0]
    per_peer: dict[str, int] = {}
    copies = {}
    repeated_same_hit = [False]
    fetch_urls = []
    extraction_calls = []
    from ora.evidence import shared_fetch as shared_module

    original_extract = shared_module.extract_blocks

    def extract(*args, **kwargs):
        extraction_calls.append(args[1])
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(shared_module, "extract_blocks", extract)

    def fetch(url):
        with lock:
            fetch_urls.append(url)
        return {
            "status": 200, "content": content,
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "byte_length": len(content), "content_type": "text/html",
            "requested_url": url, "final_url": url, "redirect_chain": [],
            "access_basis": "public_https_unauthenticated", "authenticated": False,
            "retrieved_at": "2026-10-22T12:00:00Z",
        }

    monkeypatch.setattr("ora.mission.service.fetch_public_source", fetch)

    def model(request, *, timeout):
        packet = json.loads(request.data)
        name = _peer(packet)
        with lock:
            per_peer[name] = per_peer.get(name, 0) + 1
            step = per_peer[name]
            global_index[0] += 1
            index = global_index[0]
        if step == 1:
            return tool("search_scholarly", {
                "route": "exa.search", "query": f"synthetic {name} article", "limit": 1,
            }, index)
        if step == 2:
            result = _latest_tool(packet, "search_scholarly")
            assert result["hits"]
            copies[name] = {"hit_id": result["hits"][0]["hit_id"]}
            return tool("request_capability", {"tool_id": "fetch_source"}, index)
        if step == 3:
            schemas = {item["function"]["name"] for item in packet["tools"]
                       if item.get("type") == "function"}
            assert "fetch_source" in schemas
            return tool("fetch_source", {"hit_id": copies[name]["hit_id"]}, index)
        # A newly admitted source may move into the host's source-free
        # inventory turn before the exact tool exchange is replayed.
        tool_messages = [item for item in packet["messages"]
                         if item.get("role") == "tool" and item.get("name") == "fetch_source"]
        if tool_messages:
            result = json.loads(tool_messages[-1]["content"])
            if result.get("status") == "inspected":
                copies[name]["copy_ref"] = result["copy_ref"]
                if name == "Sage" and not repeated_same_hit[0]:
                    repeated_same_hit[0] = True
                    return tool("fetch_source", {"hit_id": copies[name]["hit_id"]}, index)
                return answer(f"{name} retained its own checked copy.", index)
            assert result.get("status") in {"pending", "source_pending"}, result
        if any("saved_host_source_index_not_source_instruction" in item.get("content", "")
               for item in packet["messages"] if item.get("role") == "user"):
            if name == "Sage" and not repeated_same_hit[0]:
                repeated_same_hit[0] = True
                return tool("fetch_source", {"hit_id": copies[name]["hit_id"]}, index)
            return answer(f"{name} retained its own checked copy.", index)
        assert step < 9, packet["messages"][-3:]
        return tool("fetch_source", {"hit_id": copies[name]["hit_id"]}, index)

    host = service(tmp_path, monkeypatch, model)

    def scholarly_provider(method, url, **kwargs):
        return HTTPResult(200, json.dumps({"results": [{
            "id": "synthetic-parallel-report", "url": source_url,
            "title": "Synthetic parallel report",
            "highlights": ["Uninspected candidate excerpt."],
        }]}).encode(), {})

    host.discovery_transport = scholarly_provider
    try:
        run = host.start(LiveStartRequest(
            request_id="parallel-shared-public-fetch",
            question="Two peers inspect the same synthetic public report independently.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        drain(host, run.run_id, limit=35)
        assert len(fetch_urls) == 1, (fetch_urls, per_peer)
        assert len(extraction_calls) == 1
        assert set(copies) == {"Sage", "Mxra"}
        assert repeated_same_hit[0]
        documents = host.store.records("public_document_fetch")
        assert len(documents) == 2, (per_peer, documents)
        task_copies = [copy for copy in host.store.records("source_copy")
                       if copy["body"].get("document_ref") in [ref(document)
                                                                   for document in documents]]
        assert len(task_copies) == 2
        assert len({ref(copy)["id"] for copy in task_copies}) == 2
        assert len({document["body"]["actor_id"] for document in documents}) == 2
        assert len(host.store.records("shared_public_fetch")) == 1
    finally:
        host.close()


def test_later_mission_selects_library_metadata_then_reads_exact_passage_in_model_input(
    tmp_path, monkeypatch,
):
    phrase = "Synthetic cytokine response followed a distinct intervention period."
    stage = ["curate"]
    calls = {"curate": 0, "reuse": 0}
    global_index = [0]
    selected = {}

    def model(request, *, timeout):
        packet = json.loads(request.data)
        current = stage[0]
        calls[current] += 1
        step = calls[current]
        global_index[0] += 1
        index = global_index[0]
        if current == "curate":
            if step == 1:
                return tool("request_capability", {"tool_id": "retain_evidence"}, index)
            assert "retain_evidence" in {
                item["function"]["name"] for item in packet["tools"]
                if item.get("type") == "function"
            }
            if step == 2:
                return tool("retain_evidence", {
                    "source_refs": [selected["curator_copy"]],
                    "relevance_note": "Synthetic cytokine intervention finding",
                }, index)
            assert _latest_tool(packet, "retain_evidence")["status"] == "retained"
            return answer("The source was curated for later work.", index)
        if step == 1:
            return tool("request_capability", {"tool_id": "search_evidence_library"}, index)
        if step == 2:
            assert "search_evidence_library" in {
                item["function"]["name"] for item in packet["tools"]
                if item.get("type") == "function"
            }
            return tool("search_evidence_library", {"query": "cytokine"}, index)
        if step == 3:
            metadata = _latest_tool(packet, "search_evidence_library")
            assert metadata["status"] == "found" and metadata["entries"]
            assert phrase not in canonical(metadata)
            selected["entry_id"] = metadata["entries"][0]["entry_id"]
            return tool("search_evidence_library", {"entry_id": selected["entry_id"]}, index)
        if step == 4:
            chosen = _latest_tool(packet, "search_evidence_library")
            assert chosen["status"] == "selected_for_source_reader"
            selected["source_ref"] = chosen["entry"]["source_ref"]
            return tool("request_capability", {"tool_id": "read_source_window"}, index)
        if step == 5:
            assert "read_source_window" in {
                item["function"]["name"] for item in packet["tools"]
                if item.get("type") == "function"
            }
            return tool("read_source_window", {
                "source_id": selected["source_ref"]["id"],
                "source_version": selected["source_ref"]["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 500,
            }, index)
        assert phrase in canonical(packet)
        selected["serialized_passage_seen"] = True
        return answer("The checked synthetic passage was read.", index)

    host = service(tmp_path, monkeypatch, model)
    try:
        first = host.start(LiveStartRequest(
            request_id="parallel-library-curate",
            question="Curate the supplied synthetic intervention finding.",
            evidence_text=phrase,
            agent_ids=["agent-synthesis"],
        ))
        selected["curator_copy"] = host._control(first.run_id, "agent-synthesis")["body"][
            "source_refs"
        ][0]
        drain(host, first.run_id, limit=20)
        assert host.store.records("evidence_library_entry")
        stage[0] = "reuse"
        second = host.start(LiveStartRequest(
            request_id="parallel-library-reuse",
            question="Find a previous synthetic cytokine source, select it, and inspect its text.",
            agent_ids=["agent-critique"],
        ))
        drain(host, second.run_id, limit=30)
        assert selected.get("serialized_passage_seen")
        assert selected["source_ref"] == selected["curator_copy"]
        assert calls["reuse"] >= 6
        assert host.store.records("evidence_library_task_binding")
        assert host.store.records("source_read_window")
    finally:
        host.close()


def test_completed_checked_fetch_notifies_only_active_peer_once(tmp_path, monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "synthetic-search-key")
    source_url = "https://example.org/notice-report"
    content = b"<html><body><p>Synthetic checked notice source.</p></body></html>"
    global_index = [0]
    producer_steps = [0]
    selected_hit = [None]

    def fetch(url):
        return {"status": 200, "content": content,
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "byte_length": len(content), "content_type": "text/html",
                "requested_url": url, "final_url": url, "redirect_chain": [],
                "access_basis": "public_https_unauthenticated", "authenticated": False,
                "retrieved_at": "2026-10-22T12:00:00Z"}

    monkeypatch.setattr("ora.mission.service.fetch_public_source", fetch)

    def model(request, *, timeout):
        packet = json.loads(request.data)
        name = _peer(packet)
        assert name == "Sage"  # The other peer stays in an explicit local wait.
        global_index[0] += 1
        index = global_index[0]
        producer_steps[0] += 1
        step = producer_steps[0]
        if step == 1:
            return tool("search_scholarly", {
                "route": "exa.search", "query": "synthetic notice source", "limit": 1,
            }, index)
        if step == 2:
            selected_hit[0] = _latest_tool(packet, "search_scholarly")["hits"][0]["hit_id"]
            return tool("request_capability", {"tool_id": "fetch_source"}, index)
        if step == 3:
            return tool("fetch_source", {"hit_id": selected_hit[0]}, index)
        return answer("The source was checked and saved.", index)

    host = service(tmp_path, monkeypatch, model)
    host.discovery_transport = lambda *_args, **_kwargs: HTTPResult(
        200, json.dumps({"results": [{"id": "notice-source", "url": source_url,
                                     "title": "Synthetic notice source",
                                     "highlights": ["Candidate only."]}]}).encode(), {},
    )
    try:
        run = host.start(LiveStartRequest(
            request_id="parallel-checked-notice",
            question="Check a synthetic source while another peer is active.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        recipient = host._control(run.run_id, "agent-critique")
        host._update_control(
            recipient, phase="waiting", waiting_reason="Synthetic active-peer hold",
            local_host_wait=host._local_host_wait_marker(recipient, "SyntheticHold"),
        )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            changed, _wake = host._drive_run(host._run(run.run_id))
            notices = host.store.records("research_peer_notice")
            if notices:
                break
            if not changed:
                time.sleep(0.01)
        assert len(notices) == 1, (
            producer_steps[0],
            [(c["body"]["alias"], c["body"]["phase"], c["body"].get("waiting_reason"))
             for c in host._controls(run.run_id)],
        )
        assert host.store.records("public_document_fetch")
        notice = notices[0]["body"]
        assert notice["kind"] == "checked_evidence"
        assert notice["subject_ref"]["kind"] == "source_copy"
        assert notice["recipient_actor_id"] == host._control(
            run.run_id, "agent-critique"
        )["body"]["actor_id"]
        assert "Synthetic checked notice source" not in canonical(notice)
        for _ in range(2):
            host._drive_run(host._run(run.run_id))
        assert len(host.store.records("research_peer_notice")) == 1
        assert len([event for event in host.store.events(notice["mission_id"])
                    if event["type"] == "research_peer_notice"]) == 1
    finally:
        host.close()


def test_plan_notice_only_tracks_new_uncertainty_not_other_plan_edits(tmp_path, monkeypatch):
    producer_steps = [0]
    global_index = [0]
    plan = {
        "goal": "Compare a synthetic research question",
        "completion_criteria": ["State a bounded result"],
        "uncertainties": ["Initial synthetic gap"],
        "status": "active",
        "steps": [{"id": "inspect", "description": "Inspect one source",
                   "status": "todo", "depends_on": [], "linked_refs": []}],
        "linked_refs": [], "revision_reason": "Initial bounded plan",
    }

    def model(request, *, timeout):
        packet = json.loads(request.data)
        name = _peer(packet)
        assert name == "Sage"  # The other peer stays in an explicit local wait.
        global_index[0] += 1
        index = global_index[0]
        producer_steps[0] += 1
        step = producer_steps[0]
        if step == 1:
            return tool("activate_skill", {"skill_id": "task-planning"}, index)
        if step == 2:
            return tool("save_plan", {"expected_version": 0, "plan": plan}, index)
        if step == 3:
            return tool("modify_plan", {
                "expected_version": 1, "revision_reason": "Refine next action without new gap",
                "changes": {"goal": "Compare the synthetic research question carefully"},
            }, index)
        if step == 4:
            return tool("modify_plan", {
                "expected_version": 2, "revision_reason": "New gap discovered",
                "changes": {"uncertainties": ["Initial synthetic gap", "New synthetic gap"]},
            }, index)
        return answer("Plan revisions recorded.", index)

    host = service(tmp_path, monkeypatch, model)
    try:
        run = host.start(LiveStartRequest(
            request_id="parallel-plan-gap-notices",
            question="Track synthetic plan gap changes while another peer is active.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        recipient = host._control(run.run_id, "agent-critique")
        host._update_control(
            recipient, phase="waiting", waiting_reason="Synthetic active-peer hold",
            local_host_wait=host._local_host_wait_marker(recipient, "SyntheticHold"),
        )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            changed, _wake = host._drive_run(host._run(run.run_id))
            notices = host.store.records("research_peer_notice")
            if notices and notices[0]["version"] == 2:
                break
            if not changed:
                time.sleep(0.01)
        assert notices and notices[0]["version"] == 2, (
            producer_steps[0],
            [(c["body"]["alias"], c["body"]["phase"], c["body"].get("waiting_reason"))
             for c in host._controls(run.run_id)],
        )
        assert notices[0]["body"]["kind"] == "new_gap"
        assert notices[0]["body"]["subject_ref"]["version"] == 3
        events = [event for event in host.store.events(notices[0]["body"]["mission_id"])
                  if event["type"] == "research_peer_notice"]
        assert len(events) == 2  # Plan v2 changed the goal but introduced no gap.
        assert len(host.store.records("working_note")) == 1
        assert host.store.records("working_note")[0]["version"] == 3
    finally:
        host.close()
