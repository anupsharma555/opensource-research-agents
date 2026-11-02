"""Offline mission path for native browser discovery and its evidence boundary."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
from io import BytesIO

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.evidence import EvidenceError, EvidenceLibrary, read_source_window
from ora.mission import MissionService
from ora.mission.browser_search import save_browser_search_hits
from ora.mission.context import ContextError
from tests.mission.test_service import MODEL, Response, drain, service, tool

_URL = "https://example.org/paper"
_MARKER = "〖turn0search0〗"
_PRIVATE_REASONING = "PRIVATE_REASONING_SHOULD_NOT_REPLAY"


def _native_result(index: int) -> Response:
    return Response({
        "id": f"offline-browser-{index}", "model": MODEL,
        "choices": [{"message": {
            "role": "assistant",
            "content": f"A discovery lead is {_URL} {_MARKER}. Source text was not inspected.",
            "reasoning": _PRIVATE_REASONING,
        }, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
    })


def _index(packet: dict) -> dict:
    for message in reversed(packet["messages"]):
        if message["role"] != "user":
            continue
        try:
            value = json.loads(message["content"])
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and "browser_search" in value:
            return value
    raise AssertionError("Saved browser discovery was absent from the next model input")


def _last_result(packet: dict, name: str) -> dict:
    return json.loads(next(
        message["content"] for message in reversed(packet["messages"])
        if message["role"] == "tool" and message["name"] == name
    ))


def _wait_for_revised_document_completion(host: MissionService, run_id: str) -> None:
    """Let the interrupted fetch callback settle under a finite wall-clock bound."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if errors := host._fixture_transport_errors:
            raise errors[0]
        host._drive_run(host._run(run_id))
        if errors := host._fixture_transport_errors:
            raise errors[0]
        status = host._run(run_id)["body"]["status"]
        if status == "completed":
            return
        if status != "active":
            pytest.fail(f"Revised document run stopped as {status} before completion")
        time.sleep(.005)
    controls = host.store.records("mission_task_control")
    work = host.store.records("executor_work")
    pytest.fail(
        "Revised document callback did not settle before deadline: "
        f"controls={[item['body'].get('phase') for item in controls]}, "
        f"work={[item['body'].get('phase') for item in work]}"
    )


def test_native_browser_discovery_retains_metadata_lead_then_scoped_result(tmp_path, monkeypatch):
    packets = []
    receipt_id = None
    hit_id = None

    def transport(request, *, timeout):
        nonlocal receipt_id, hit_id
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        assert _PRIVATE_REASONING not in json.dumps(packet["messages"])
        if step == 1:
            catalog_text = packet["messages"][0]["content"]
            assert '"browser_search"' in catalog_text
            assert '"general-web-discovery"' in catalog_text
            assert any(boundary in catalog_text for boundary in (
                "not a local function", "never a local function",
            ))
            assert "Never emit a function call named browser_search" not in catalog_text
            assert {"type": "browser_search"} not in packet["tools"]
            assert packet["reasoning_effort"] == "medium"
            return tool("request_capability", {"tool_id": "browser_search"}, step)
        if step == 2:
            assert packet["tools"] == [{"type": "browser_search"}]
            assert packet["reasoning_effort"] == "low"
            instructions = packet["messages"][0]["content"]
            assert "Never emit a function call named browser_search" in instructions
            assert "Groq may run browser.search and browser.open internally" in instructions
            return _native_result(step)
        assert {"type": "browser_search"} not in packet["tools"]
        assert packet["reasoning_effort"] == "medium"
        assert "Never emit a function call named browser_search" not in packet["messages"][0]["content"]
        assert "read_browser_search_result" in {
            item["function"]["name"] for item in packet["tools"]
            if item.get("type") == "function"
        }
        if step == 3:
            assert "fetch_source" in {
                entry["function"]["name"] for entry in packet["tools"]
                if entry.get("type") == "function"
            }
            discovery = _index(packet)["browser_search"]
            receipt_id = discovery["response_receipt_ref"]["id"]
            assert discovery["kind"] == "browser_search_discovery"
            assert discovery["explicit_urls"] == [_URL]
            assert discovery["explicit_citation_markers"] == [_MARKER]
            assert discovery["source_inspection"] == "not_performed"
            assert discovery["search_execution"] == "not_independently_confirmed"
            assert discovery["hits"][0]["original_url"] == _URL
            assert discovery["hits"][0]["access_surface"] == "metadata"
            hit_id = discovery["hits"][0]["hit_id"]
            return tool("inspect_hit", {"hit_id": hit_id}, step)
        assert step == 4
        inspected = _last_result(packet, "inspect_hit")
        assert inspected["status"] == "metadata_only"
        assert inspected["hit_id"] == hit_id
        return tool("publish_result", {
            "title": "Unverified web discovery lead",
            "text": "A possible source URL was found; no source passage was inspected.",
            "citations": [],
            "limitations": ["Browser output is discovery only; source content unverified"],
        }, step)

    host = service(tmp_path, monkeypatch, transport)
    host.discovery_transport = lambda *_args, **_kwargs: pytest.fail(
        "Native browser discovery or metadata inspection made an unexpected retrieval call"
    )
    try:
        run = host.start(LiveStartRequest(
            request_id="browser-search-native-offline",
            question="Locate a possible synthetic web research lead",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=25)
        assert host.state(run.run_id).snapshot.mission.status == "completed", (
            len(packets), host._control(run.run_id, "agent-synthesis")["body"]["waiting_reason"]
        )
        assert len(packets) == 4
        assert receipt_id is not None and hit_id is not None
        control = host._control(run.run_id, "agent-synthesis")["body"]
        assert len(control["browser_search_response_refs"]) == 1
        assert control["browser_search_response_refs"][0]["id"] == receipt_id
        hit = host.store.get("retrieval_hit", hit_id)
        assert hit["body"]["source"]["original_url"] == _URL
        assert hit["body"]["source"]["access_surface"] == "metadata"
        assert hit["body"]["source"]["content"] is None
        current = host._control(run.run_id, "agent-synthesis")
        assert host._has_inspectable_hit(current)
        assert not host._has_inspectable_hit({
            **current, "body": {**current["body"], "actor_id": "another-actor"},
        })
        assert host.store.records("source_copy") == []
        assert len(host.store.records("browser_search_leads")) == 1

        # Re-reading and remapping the saved response cannot create duplicate hits.
        original_hits = [item["id"] for item in host.store.records("retrieval_hit")]
        replay = save_browser_search_hits(host.store, control["browser_search_response_refs"][0])
        assert [item["id"] for item in host.store.records("retrieval_hit")] == original_hits
        assert replay["hits"][0]["hit_id"] == hit_id

        first = host.context.browser_search_result(
            mission_id=control["mission_id"], actor_id=control["actor_id"],
            task_id=control["task_id"], response_id=receipt_id, limit=30,
        )
        assert first["next_request"] is not None
        following = host.context.browser_search_result(
            mission_id=control["mission_id"], actor_id=control["actor_id"],
            task_id=control["task_id"], response_id=receipt_id,
            offset=first["next_offset"], limit=30,
            content_sha256=first["content_sha256"],
        )
        assert first["content"] + following["content"] in packets[2]["messages"][-1]["content"]
        with pytest.raises(ContextError, match="outside this peer's task"):
            host.context.browser_search_result(
                mission_id="another-mission", actor_id=control["actor_id"],
                task_id=control["task_id"], response_id=receipt_id,
            )
    finally:
        host.close()


def test_browser_url_fetch_reads_checked_nonpublication_html_and_cites_window(tmp_path, monkeypatch):
    html = (
        b"<html><title>Browser Search Documentation</title><main>"
        b"<h1>Browser Search Documentation</h1><p>Native browser search returns URL leads. "
        b"Read this exact synthetic document before citing the operational detail.</p>"
        b"</main></html>"
    )
    digest = hashlib.sha256(html).hexdigest()
    packets = []
    fetches = []
    hit_id = None

    def fetch(url):
        fetches.append(url)
        assert url == _URL
        return {
            "status": 200, "content": html, "content_sha256": digest,
            "byte_length": len(html), "content_type": "text/html; charset=utf-8",
            "requested_url": url, "final_url": url, "redirect_chain": [],
            "access_basis": "public_https_unauthenticated", "authenticated": False,
            "retrieved_at": "2026-10-13T00:00:00Z",
        }

    def transport(request, *, timeout):
        nonlocal hit_id
        packet = json.loads(request.data)
        packets.append(packet)
        turn = len(packets)
        if turn == 1:
            return tool("request_capability", {"tool_id": "browser_search"}, turn)
        if turn == 2:
            return _native_result(turn)
        if turn == 3:
            lead = _index(packet)["browser_search"]["hits"][0]
            hit_id = lead["hit_id"]
            assert lead["access_surface"] == "metadata"
            return tool("request_capability", {"tool_id": "fetch_source"}, turn)
        if turn == 4:
            return tool("fetch_source", {"hit_id": hit_id}, turn)
        if turn == 5:
            inventory = next(
                json.loads(item["content"])["inventory"]
                for item in packet["messages"] if item["role"] == "user"
                and "inventory" in json.loads(item["content"])
            )
            selected = inventory["items"][0]
            assert selected["model_input_status"] == "permitted"
            return tool("read_source_window", {
                "source_id": selected["ref"]["id"],
                "source_version": selected["ref"]["version"],
                "block_offset": 1, "char_offset": 0, "char_limit": 200,
            }, turn)
        assert turn == 6
        window = host.store.records("source_read_window")[0]
        exact = host.store.get(**window["body"]["passage_refs"][0])
        assert "Native browser search returns URL leads." in exact["body"]["text"]
        source_packets = json.loads(packet["messages"][1]["content"])["source_packets"]
        assert exact["body"]["text"] in source_packets[0]["text"]
        return tool("publish_result", {
            "title": "Synthetic browser documentation",
            "text": "Native browser search returns URL leads.",
            "citations": [{"source_id": window["body"]["copy_ref"]["id"],
                           "span_id": exact["body"]["span_id"]}],
            "limitations": ["This injected document is synthetic"],
        }, turn)

    monkeypatch.setattr("ora.mission.service.fetch_public_source", fetch)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="browser-document-offline", question="Read a synthetic browser documentation lead",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=40)
        assert len(packets) == 6
        assert fetches == [_URL]
        assert host.state(run.run_id).snapshot.mission.status == "completed"
        assert host.store.records("publication") == []
        document = host.store.records("public_document_fetch")[0]
        copy = host.store.records("source_copy")[0]
        assert copy["body"]["publication"] is None
        assert copy["body"]["document_ref"] == {
            "kind": "public_document_fetch", "id": document["id"], "version": 1,
        }
        assert host.store.read_blob(copy["body"]["blob"]["sha256"]) == html
        assert copy["body"]["mime_type"] == "text/html"
        snapshot = host.state(run.run_id).snapshot
        assert snapshot.sources[0].access_state == "document_text"
        window = host.store.records("source_read_window")[0]
        passage = host.store.get(**window["body"]["passage_refs"][0])
        assert snapshot.artifacts[0].citations[0].span_id == passage["body"]["span_id"]
        with pytest.raises(EvidenceError, match="another research peer or task"):
            host.intake.inspect_public_document(
                "wrong-task-document-inspection", ref(document),
                mission_id=document["body"]["mission_id"],
                run_id=document["body"]["run_id"],
                actor_id=document["body"]["actor_id"], task_id="another-task",
            )
        with pytest.raises(EvidenceError, match="receipt disagrees"):
            host.intake.save_public_document_fetch(
                "wrong-hash-fetch", document["body"]["hit_ref"],
                document["body"]["response_ref"],
                mission_id=document["body"]["mission_id"],
                run_id=document["body"]["run_id"],
                actor_id=document["body"]["actor_id"],
                task_id=document["body"]["task_id"],
                fetched={
                    "status": 200, "content": html, "content_sha256": "0" * 64,
                    "byte_length": len(html), "content_type": "text/html",
                    "requested_url": _URL, "final_url": _URL, "redirect_chain": [],
                    "access_basis": "public_https_unauthenticated", "authenticated": False,
                    "retrieved_at": "2026-10-13T00:00:00Z",
                },
            )
    finally:
        host.close()


@pytest.mark.parametrize("scenario", [
    "missing_auth", "authenticated", "page_prohibition", "private_redirect",
    "unsupported_type",
])
def test_browser_html_fetch_without_public_use_basis_remains_unreadable(
    tmp_path, monkeypatch, scenario,
):
    html = (
        b"<html><main><p>Synthetic documentation details.</p></main>"
        b"<footer>No text and data mining.</footer></html>"
        if scenario == "page_prohibition" else
        b"PK\x03\x04 synthetic unsupported archive"
        if scenario == "unsupported_type" else
        b"<html><main><p>Synthetic documentation details.</p></main></html>"
    )
    digest = hashlib.sha256(html).hexdigest()
    calls = []
    selected = {}

    def fetch(url):
        receipt = {
            "status": 200, "content": html, "content_sha256": digest,
            "byte_length": len(html),
            "content_type": "application/zip" if scenario == "unsupported_type" else "text/html",
            "requested_url": url, "final_url": url, "redirect_chain": [],
            "access_basis": "public_https_unauthenticated",
            "retrieved_at": "2026-10-13T00:00:00Z",
        }
        if scenario != "missing_auth":
            receipt["authenticated"] = scenario == "authenticated"
        if scenario == "private_redirect":
            receipt["final_url"] = "https://127.0.0.1/private"
            receipt["redirect_chain"] = [{
                "from_url": url, "to_url": receipt["final_url"], "status": 302,
            }]
        return receipt

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        calls.append(packet)
        turn = len(calls)
        if turn == 1:
            return tool("request_capability", {"tool_id": "browser_search"}, turn)
        if turn == 2:
            return _native_result(turn)
        if turn == 3:
            selected["hit_id"] = _index(packet)["browser_search"]["hits"][0]["hit_id"]
            return tool("request_capability", {"tool_id": "fetch_source"}, turn)
        if turn == 4:
            return tool("fetch_source", {"hit_id": selected["hit_id"]}, turn)
        inspected = _last_result(packet, "fetch_source")
        assert inspected["status"] == "source_unavailable"
        if scenario == "unsupported_type":
            assert inspected["classification"] == "unsupported_public_document_type"
        else:
            assert inspected["model_input_status"] == "not_granted"
        return tool("publish_result", {
            "title": "No checked source", "text": "The URL remains a discovery lead.",
            "citations": [], "limitations": ["No permitted original document text was read"],
        }, turn)

    monkeypatch.setattr("ora.mission.service.fetch_public_source", fetch)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"browser-document-{scenario}", question="Inspect a synthetic web lead",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=35)
        assert host._run(run.run_id)["body"]["status"] == "completed"
        if scenario in {"missing_auth", "authenticated"}:
            # Shared public retrieval rejects these receipts before retaining
            # bytes for reuse. Keep the failed attempt, with no source grant.
            assert host.store.records("public_document_fetch") == []
            failed = host.store.records("shared_public_fetch")[0]["body"]
            assert failed["phase"] == "failed"
            assert failed["reason"] == "invalid_public_fetch_receipt"
            assert "blob" not in failed
        else:
            assert host.store.records("public_document_fetch")
        assert host.store.records("source_copy") == []
        assert host.store.records("source_use") == []
        if scenario in {"unsupported_type", "missing_auth", "authenticated"}:
            assert host.store.records("intake_quality") == []
        else:
            quality = host.store.records("intake_quality")[0]["body"]
            assert quality["direct_source_use_policy"]["decision"] == "not_granted"
            assert quality["page_restriction"] is (scenario == "page_prohibition")
    finally:
        host.close()


@pytest.mark.parametrize("scenario", [
    "resume", "after_fetch", "revoked_use", "cancelled", "replacement_claim",
    "revised_proposal",
])
def test_interrupted_browser_document_use_reconciles_only_original_work(
    tmp_path, monkeypatch, scenario,
):
    import ora.mission.service as service_module

    html = (
        b"<html><title>Browser Search Documentation</title><main>"
        b"<h1>Browser Search Documentation</h1>"
        b"<p>Native browser search returns URL leads.</p></main></html>"
    )
    calls = []
    fetches = []
    selected = {}

    def fetch(url):
        fetches.append(url)
        return {
            "status": 200, "content": html, "content_sha256": hashlib.sha256(html).hexdigest(),
            "byte_length": len(html), "content_type": "text/html",
            "requested_url": url, "final_url": url, "redirect_chain": [],
            "access_basis": "public_https_unauthenticated", "authenticated": False,
            "retrieved_at": "2026-10-13T00:00:00Z",
        }

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        calls.append(packet)
        turn = len(calls)
        if turn == 1:
            return tool("request_capability", {"tool_id": "browser_search"}, turn)
        if turn == 2:
            return _native_result(turn)
        if turn == 3:
            selected["hit_id"] = _index(packet)["browser_search"]["hits"][0]["hit_id"]
            return tool("request_capability", {"tool_id": "fetch_source"}, turn)
        if turn == 4:
            return tool("fetch_source", {"hit_id": selected["hit_id"]}, turn)
        if scenario in {"cancelled", "revised_proposal"}:
            assert turn == 5
            assert "Native browser search returns URL leads." not in json.dumps(packet["messages"])
            assert not any(
                item["role"] == "tool" and item.get("name") == "fetch_source"
                for item in packet["messages"]
            )
            contexts = [
                json.loads(item["content"]) for item in packet["messages"]
                if item["role"] == "user"
            ]
            feedback = next(
                item["host_feedback"] for item in contexts
                if (item.get("host_feedback") or {}).get("status") == "wait_needs_peer_replan"
            )
            assert feedback["stranded_waits"][0]["waiting_reason"].startswith(
                "Saved fetch_source result was not accepted after "
                + ("cancellation" if scenario == "cancelled" else "task revision")
            )
            current_context = next(item for item in contexts if "task_ref" in item)
            assert current_context["task_ref"]["version"] == (
                1 if scenario == "cancelled" else 2
            )
            return tool("publish_result", {
                "title": "Document result unavailable",
                "text": "The saved document fetch did not supply accepted source evidence.",
                "citations": [], "limitations": ["No permitted passage was read"],
            }, turn)
        if scenario == "revoked_use":
            assert "Native browser search returns URL leads." not in json.dumps(packet["messages"])
            assert _last_result(packet, "fetch_source")["status"] == "source_unavailable"
            return tool("publish_result", {
                "title": "Document use revoked", "text": "No source claim was made.",
                "citations": [], "limitations": ["Document model-use grant was revoked"],
            }, turn)
        if turn == 5:
            inventory = next(
                json.loads(item["content"])["inventory"]
                for item in packet["messages"] if item["role"] == "user"
                and "inventory" in json.loads(item["content"])
            )
            copy_ref = inventory["items"][0]["ref"]
            selected["copy_ref"] = copy_ref
            return tool("read_source_window", {
                "source_id": copy_ref["id"], "source_version": copy_ref["version"],
                "block_offset": 1, "char_offset": 0, "char_limit": 200,
            }, turn)
        assert turn == 6
        window = reopened.store.records("source_read_window")[0]
        passage = reopened.store.get(**window["body"]["passage_refs"][0])
        assert passage["body"]["text"] in json.dumps(packet["messages"])
        return tool("publish_result", {
            "title": "Documentation read", "text": "Native browser search returns URL leads.",
            "citations": [{"source_id": selected["copy_ref"]["id"],
                           "span_id": passage["body"]["span_id"]}],
            "limitations": ["Synthetic document"],
        }, turn)

    monkeypatch.setattr("ora.mission.service.fetch_public_source", fetch)
    original_reconcile = service_module.reconcile_late_public_document_fetch
    monkeypatch.setattr(service_module, "reconcile_late_public_document_fetch", lambda *_args: None)
    host = service(tmp_path, monkeypatch, transport)
    original_transact = host.store.transact
    interrupted = False

    def crash_before_use(operation_id, *args, **kwargs):
        nonlocal interrupted
        if (operation_id.startswith("mission-public-document-inspect:")
                and operation_id.endswith("-quality" if scenario == "after_fetch" else "-use")
                and not interrupted):
            interrupted = True
            raise RuntimeError("synthetic shutdown after public-document copy")
        return original_transact(operation_id, *args, **kwargs)

    monkeypatch.setattr(host.store, "transact", crash_before_use)
    run = host.start(LiveStartRequest(
        request_id=f"browser-document-recovery-{scenario}",
        question="Read a synthetic browser documentation lead after interruption",
        agent_ids=["agent-synthesis"],
    ))
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        host._drive_run(host._run(run.run_id))
        control = host._control(run.run_id, "agent-synthesis")
        work = host._work_for(control)
        if interrupted and work and host.capacity.get(
            host.executor._capacity_id(host.executor._spec_from(work))
        )["phase"] == "uncertain":
            break
        time.sleep(.01)
    else:
        raise AssertionError("Interrupted public-document inspection did not become uncertain")
    assert len(calls) == 4 and len(fetches) == 1
    assert len(host.store.records("public_document_fetch")) == 1
    assert len(host.store.records("source_copy")) == (0 if scenario == "after_fetch" else 1)
    assert host.store.records("source_use") == []
    original_work_id = work["body"]["spec"]["work_id"]
    cap_id = host.executor._capacity_id(host.executor._spec_from(work))
    host.close()

    monkeypatch.setattr(service_module, "reconcile_late_public_document_fetch", original_reconcile)
    reopened = service(tmp_path, monkeypatch, transport)
    try:
        control = reopened._control(run.run_id, "agent-synthesis")
        work = reopened._work_for(control)
        assert work["body"]["spec"]["work_id"] == original_work_id
        document = reopened.store.records("public_document_fetch")[0]
        document_ref = ref(document)
        if scenario == "revoked_use":
            granted = reopened.intake.inspect_public_document(
                f"mission-public-document-inspect:{original_work_id}", document_ref,
                mission_id=control["body"]["mission_id"], run_id=run.run_id,
                actor_id=control["body"]["actor_id"], task_id=control["body"]["task_id"],
            )

            def revoke(tx):
                use = tx.get(**granted["source_use_ref"])
                tx.put("source_use", use["id"], {
                    **use["body"], "allowed_uses": ["discovery"], "policy_status": "revoked",
                }, expected_version=use["version"])
                return {"revoked": True}

            reopened.store.transact("revoke-public-document-use", {}, revoke)
        if scenario == "cancelled":
            reopened.capacity.cancel(cap_id, reason="branch_cancel")
        if scenario == "revised_proposal":
            spec = reopened.executor._spec_from(work)
            revised = reopened.board.revise(
                "synthetic-revised-browser-document-task",
                mission_id=spec.mission_id, proposal_id=spec.proposal_ref["id"],
                actor_id=spec.actor_id, expected_version=spec.proposal_ref["version"],
                reason="Synthetic task changed before saved fetch acceptance",
            )
            assert revised["status"] == "accepted"
        if scenario == "replacement_claim":
            spec = reopened.executor._spec_from(work)
            old_claim = next(
                item["current_claim"] for item in reopened.board.open_work(spec.mission_id)
                if item["proposal_ref"] == spec.proposal_ref
            )
            reopened.executor._release_board(spec, old_claim)
            replacement = reopened.board.claim(
                "synthetic-replacement-document-claim",
                mission_id=spec.mission_id, proposal_id=spec.proposal_ref["id"],
                actor_id=spec.actor_id, ttl=60,
                expected_version=spec.proposal_ref["version"],
            )
            assert replacement["status"] == "accepted"
        if scenario in {"resume", "after_fetch", "revoked_use"}:
            drain(reopened, run.run_id, limit=35)
            assert reopened._run(run.run_id)["body"]["status"] == "completed"
            assert len(calls) == (5 if scenario == "revoked_use" else 6)
            assert len(reopened.store.records("source_copy")) == 1
            assert len(reopened.store.records("source_use")) == 1
            if scenario == "revoked_use":
                assert reopened._control(run.run_id, "agent-synthesis")["body"]["source_refs"] == []
                with pytest.raises(EvidenceError, match="does not grant model input"):
                    read_source_window(
                        EvidenceLibrary(reopened.store), "revoked-browser-document-window",
                        ref(reopened.store.records("source_copy")[0]),
                    )
        elif scenario == "replacement_claim":
            for _ in range(4):
                reopened._drive_run(reopened._run(run.run_id))
            assert len(calls) == 4
            assert reopened.store.records("source_use") == []
        else:
            if scenario == "revised_proposal":
                _wait_for_revised_document_completion(reopened, run.run_id)
            else:
                drain(reopened, run.run_id, limit=35)
            assert reopened._run(run.run_id)["body"]["status"] == "completed"
            assert len(calls) == 5
            stale_work = reopened.store.get("executor_work", work["id"])
            assert stale_work["body"]["phase"] == "result_unaccepted"
            observation = reopened.store.get(**stale_work["body"]["result_ref"])
            assert observation["body"]["reason_code"] == (
                "saved_local_tool_after_cancel" if scenario == "cancelled"
                else "saved_local_tool_after_task_revision"
            )
            assert reopened.store.records("source_use") == []
            assert reopened._control(run.run_id, "agent-synthesis")["body"][
                "source_refs"] == []
        assert len(fetches) == 1
    finally:
        reopened.close()


def test_browser_turn_keeps_other_selected_local_capability_for_later_turn(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: pytest.fail("no model dispatch"))
    try:
        run = host.start(LiveStartRequest(
            request_id="browser-selected-turn-tools",
            question="Inspect a synthetic web lead",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        host._update_control(control, requested_capabilities=["browser_search", "calculate"])
        selected = host._control(run.run_id, "agent-synthesis")
        config = host._configuration(selected)
        exposed, unavailable = host._exposed_tools(selected, config)
        assert exposed == ["browser_search"] and unavailable == []
        assert selected["body"]["requested_capabilities"] == ["browser_search", "calculate"]

        host._update_control(selected, requested_capabilities=["calculate"])
        later = host._control(run.run_id, "agent-synthesis")
        restored, unavailable = host._exposed_tools(later, config)
        assert "calculate" in restored and "publish_result" in restored
        assert "browser_search" not in restored and unavailable == []
    finally:
        host.close()


@pytest.mark.parametrize("old_kind", ["unregistered_tool_function", "http_error"])
def test_saved_native_browser_400_repairs_legacy_label_into_fresh_native_turn(
    tmp_path, monkeypatch, old_kind,
):
    import ora.models.groq as groq_module
    packets = []
    error = {"error": {"type": "invalid_request_error", "code": "tool_use_failed",
                       "message": "attempted to call tool 'browser_search' which was not in request.tools"}}

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        if step == 1:
            return tool("request_capability", {"tool_id": "browser_search"}, step)
        if step == 2:
            assert packet["tools"] == [{"type": "browser_search"}]
            assert packet["reasoning_effort"] == "low"
            selected = _last_result(packet, "request_capability")
            assert selected["execution_model"] == "provider_builtin"
            assert "not a local function" in selected["next_action"]
            raise urllib.error.HTTPError(
                "https://example.invalid", 400, "tool rejected", {},
                BytesIO(json.dumps(error).encode()),
            )
        if step == 3:
            assert {"type": "browser_search"} not in packet["tools"]
            assert packet["reasoning_effort"] == "medium"
            assert "Never emit a function call named browser_search" not in (
                packet["messages"][0]["content"]
            )
            visible = json.dumps(packet["messages"])
            assert "provider_rejected_native_browser_call" in visible
            assert "provider-managed browsing" in visible
            assert "provider_rejected_unregistered_function" not in visible
            return tool("request_capability", {"tool_id": "browser_search"}, step)
        if step == 4:
            assert packet["tools"] == [{"type": "browser_search"}]
            assert packet["reasoning_effort"] == "low"
            return _native_result(step)
        assert step == 5
        assert {"type": "browser_search"} not in packet["tools"]
        assert packet["reasoning_effort"] == "medium"
        return tool("publish_result", {
            "title": "Bounded discovery lead", "text": "A candidate URL was returned.",
            "citations": [], "limitations": ["No source passage was inspected"],
        }, step)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="native-browser-saved-400-repair",
            question="Find a possible synthetic web research lead",
            agent_ids=["agent-synthesis"],
        ))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            if (control["body"]["phase"] == "ready_model"
                    and "browser_search" in control["body"].get("requested_capabilities", [])):
                break
            time.sleep(.01)
        else:
            raise AssertionError("Native capability was not selected")

        # Model an older saved receipt whose classifier treated a native name
        # as an unregistered local function. The receipt itself stays immutable.
        original_classifier = groq_module._tool_failure_kind
        monkeypatch.setattr(groq_module, "_tool_failure_kind", lambda *_args: old_kind)
        assert host._schedule_model(control)
        control = host._control(run.run_id, "agent-synthesis")
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
            cap = host.capacity.get(capacity_id)
            if failed and cap and cap["phase"] == "failed":
                break
            time.sleep(.01)
        assert failed and failed["body"]["error_kind"] == old_kind
        assert len(packets) == 2
        assert host.store.records("browser_search_leads") == []

        monkeypatch.setattr(groq_module, "_tool_failure_kind", original_classifier)
        control = host._control(run.run_id, "agent-synthesis")
        host._update_control(control, phase="waiting", waiting_reason="Older host left 400 waiting")
        control = host._control(run.run_id, "agent-synthesis")
        assert host._repair_tool_rejection(control, work, cap)
        repaired = host._control(run.run_id, "agent-synthesis")
        assert repaired["body"]["phase"] == "ready_model"
        assert repaired["body"]["feedback"]["status"] == "provider_rejected_native_browser_call"
        assert "browser_search" not in repaired["body"].get("requested_capabilities", [])
        assert host.store.get(**ref(failed)) == failed
        assert len(packets) == 2  # Saved repair does not retry by itself.

        resumed = MissionService(
            host.project_root, tmp_path, model_session=host.session,
            execution_mode="offline_test", start_supervisor=False,
        )
        try:
            drain(resumed, run.run_id, limit=20)
            assert resumed.state(run.run_id).snapshot.mission.status == "completed"
            assert len(packets) == 5
            assert len(resumed.store.records("browser_search_leads")) == 1
        finally:
            resumed.close()
    finally:
        host.close()
