"""The existing local executor carries a delayed venue tool without model polling."""

from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.evidence.intake import PublicSourceHTTPError
from ora.mission.tool_operations import read_navigation_operation, reconcile_late_navigation
from ora.retrieval.catalog_tools import list_catalog_leads
from ora.state import StateConflict
from ora.state.store import canonical
from tests.mission.test_service import Response, answer, drain, service, tool


def test_navigation_operation_is_pinned_before_fetch_and_finishes_once(tmp_path, monkeypatch):
    import ora.mission.service as service_module

    started = threading.Event()
    release = threading.Event()
    calls = []
    sibling_calls = []
    fetches = []
    selected = {}
    html = b"<html><title>Synthetic lead</title><a href='/one'>One</a></html>"

    def fetch(url, *, max_bytes):
        fetches.append(url)
        started.set()
        assert release.wait(5), "Controlled fetch was not released"
        assert max_bytes >= len(html)
        return {
            "status": 200,
            "content": html,
            "content_sha256": hashlib.sha256(html).hexdigest(),
            "byte_length": len(html),
            "content_type": "text/html",
            "requested_url": url,
            "final_url": url,
            "redirect_chain": [],
            "access_basis": "public_https_unauthenticated",
            "authenticated": False,
            "retrieved_at": "2026-10-11T00:00:00Z",
        }

    def transport(request, *, timeout):
        body = json.loads(request.data)
        if "Mxra" in body["messages"][0]["content"]:
            sibling_calls.append(body)
            return answer("A separate synthetic review completed.", 100)
        calls.append(body)
        count = len(calls)
        if count == 1:
            return tool("request_capability", {"tool_id": "navigate_venue"}, count)
        if count == 2:
            return tool("navigate_venue", {"catalog_ref": selected["catalog_ref"]}, count)
        assert count == 3
        prior = json.loads(
            next(item["content"] for item in reversed(body["messages"]) if item["role"] == "tool")
        )
        assert prior["status"] == "navigable"
        assert prior["operation_ref"] == selected["operation_ref"]
        return tool(
            "publish_result",
            {
                "title": "Lead inspected",
                "text": "A synthetic venue lead was inspected.",
                "citations": [],
                "limitations": ["No publication source was inspected"],
            },
            count,
        )

    monkeypatch.setattr(service_module, "fetch_public_source", fetch)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(
            LiveStartRequest(
                request_id="local-delayed-navigation",
                question="Inspect a synthetic venue lead",
                agent_ids=["agent-synthesis", "agent-critique"],
            )
        )
        control = host._control(run.run_id, "agent-synthesis")
        selected["catalog_ref"] = list_catalog_leads(
            host.store,
            mission_id=control["body"]["mission_id"],
            run_id=run.run_id,
            actor_id=control["body"]["actor_id"],
            limit=1,
        )["entries"][0]["catalog_ref"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = host._control(run.run_id, "agent-synthesis")
            if (
                current["body"]["phase"] == "ready_tool"
                and current["body"]["tool_call"]["name"] == "navigate_venue"
            ):
                break
            host._drive_run(host._run(run.run_id))
            time.sleep(0.005)
        else:
            raise AssertionError("Model did not request the local navigation tool")

        assert host._schedule_tool(current)
        pending = host._control(run.run_id, "agent-synthesis")
        pointer = pending["body"]["tool_operation_ref"]
        selected["operation_ref"] = pointer
        saved = host.store.get(**pointer)
        assert saved["body"]["response_ref"] == pending["body"]["response_ref"]
        assert saved["body"]["tool_call_id"] == pending["body"]["tool_call"]["id"]
        assert (
            saved["body"]["attempt_id"]
            == host.store.get(**pending["body"]["call_ref"])["body"]["attempt_id"]
        )
        scope = dict(
            mission_id=pending["body"]["mission_id"],
            actor_id=pending["body"]["actor_id"],
            task_id=pending["body"]["task_id"],
        )
        assert read_navigation_operation(host.store, pointer, **scope)["status"] == "prepared"
        assert (
            read_navigation_operation(host.store, pointer, **{**scope, "actor_id": "other"}) is None
        )
        assert (
            read_navigation_operation(host.store, pointer, **{**scope, "task_id": "other"}) is None
        )
        assert (
            host.store.records("context_tool_result")[-1]["body"]["tool_call_id"]
            != saved["body"]["tool_call_id"]
        )

        deadline = time.monotonic() + 5
        while not started.is_set() and time.monotonic() < deadline:
            host._drive_run(host._run(run.run_id))
            time.sleep(0.005)
        assert started.is_set()
        progress = read_navigation_operation(host.store, pointer, **scope)
        assert progress["status"] == "fetch_dispatch_recorded" and progress["dispatch_ref"]
        assert progress["result_ref"] is None and progress["partial_refs"] == []
        assert len(calls) == 2  # No model call polls the held fetch.
        assert (
            host.store.get(
                "context_tool_result", f"tool-result:{saved['body']['response_ref']['id']}"
            )
            is None
        )

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            host._drive_run(host._run(run.run_id))
            if host._control(run.run_id, "agent-critique")["body"]["phase"] == "done":
                break
            time.sleep(0.005)
        assert host._control(run.run_id, "agent-critique")["body"]["phase"] == "done"
        assert len(sibling_calls) == 1 and not release.is_set() and len(calls) == 2

        release.set()
        work_ref = saved["body"]["work_ref"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            host.dispatch.poll(scope["mission_id"])
            if host.store.get(work_ref["kind"], work_ref["id"])["body"]["phase"] == "completed":
                break
            time.sleep(0.005)
        assert host.store.get(work_ref["kind"], work_ref["id"])["body"]["phase"] == "completed"
        before_restart = read_navigation_operation(host.store, pointer, **scope)
        assert before_restart["status"] == "accepted"
        assert before_restart["tool_result_ref"] is None
        assert len(calls) == 2 and len(fetches) == 1
    finally:
        release.set()
        host.close()

    reopened = service(tmp_path, monkeypatch, transport)
    try:
        pending = reopened._control(run.run_id, "agent-synthesis")
        assert pending["body"]["phase"] == "pending_tool"
        exact_work = reopened.store.get(work_ref["kind"], work_ref["id"])
        assert reopened._advance_tool(pending, exact_work)
        ready = reopened._control(run.run_id, "agent-synthesis")
        assert ready["body"]["phase"] == "ready_model"
        assert ready["body"]["tool_operation_ref"] is None
        assert read_navigation_operation(reopened.store, pointer, **scope)["tool_result_ref"]
        assert len(calls) == 2
        drain(reopened, run.run_id, limit=30)
        assert reopened._run(run.run_id)["body"]["status"] == "completed"
        replay = read_navigation_operation(reopened.store, pointer, **scope)
        assert replay["status"] == "accepted"
        assert replay["navigation_ref"] and replay["result_ref"] and replay["tool_result_ref"]
        assert (
            reopened._control(run.run_id, "agent-synthesis")["body"]["tool_operation_ref"] is None
        )
        assert len(reopened.store.records("mission_tool_operation")) == 1
        assert len(reopened.store.records("venue_navigation")) == 1
        assert len(fetches) == 1 and len(calls) == 3
        real_get = reopened.store.get

        def hide_navigation(kind, id, version=None):
            if kind == "venue_navigation":
                return None
            return real_get(kind, id, version)

        monkeypatch.setattr(reopened.store, "get", hide_navigation)
        assert read_navigation_operation(reopened.store, pointer, **scope)["status"] == (
            "inconsistent_saved_result"
        )
        monkeypatch.setattr(reopened.store, "get", real_get)
        reopened._drive_run(reopened._run(run.run_id))
        assert len(calls) == 3
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("case", "expected_status"),
    [
        ("rejected_before_fetch", "rejected_before_fetch"),
        ("missing_catalog_ref", "rejected_before_fetch"),
        ("uncertain_dispatch", "uncertain_dispatch"),
    ],
)
def test_navigation_no_result_keeps_actual_cause_and_never_retries_http(
    tmp_path, monkeypatch, case, expected_status
):
    import ora.mission.service as service_module

    calls = []
    fetches = []
    selected = {}

    def forbidden_fetch(url, *, max_bytes):
        fetches.append(url)
        raise AssertionError("No HTTP fetch is authorized in this fixture")

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        count = len(calls)
        if count == 1:
            return tool("request_capability", {"tool_id": "navigate_venue"}, count)
        if count == 2:
            return tool("navigate_venue", selected["arguments"], count)
        assert count == 3
        prior = json.loads(
            next(
                item["content"]
                for item in reversed(calls[-1]["messages"])
                if item["role"] == "tool"
            )
        )
        selected["final_status"] = prior["status"]
        return tool(
            "publish_result",
            {
                "title": "Navigation did not produce a page",
                "text": "The synthetic navigation was unavailable.",
                "citations": [],
                "limitations": ["No publication source was inspected"],
            },
            count,
        )

    monkeypatch.setattr(service_module, "fetch_public_source", forbidden_fetch)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(
            LiveStartRequest(
                request_id=f"local-navigation-{case}",
                question="Inspect a synthetic venue lead",
                agent_ids=["agent-synthesis"],
            )
        )
        control = host._control(run.run_id, "agent-synthesis")
        entry = list_catalog_leads(
            host.store,
            mission_id=control["body"]["mission_id"],
            run_id=run.run_id,
            actor_id=control["body"]["actor_id"],
            limit=1,
        )["entries"][0]
        selected["catalog_ref"] = (
            entry["catalog_ref"]
            if case == "uncertain_dispatch"
            else {"kind": "venue_seed", "id": "missing", "version": 1, "sha256": "0" * 64}
        )
        selected["arguments"] = (
            {} if case == "missing_catalog_ref" else {"catalog_ref": selected["catalog_ref"]}
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = host._control(run.run_id, "agent-synthesis")
            if (
                current["body"]["phase"] == "ready_tool"
                and current["body"]["tool_call"]["name"] == "navigate_venue"
            ):
                break
            host._drive_run(host._run(run.run_id))
            time.sleep(0.005)
        else:
            raise AssertionError("Model did not request navigation")
        host._schedule_tool(current)
        pending = host._control(run.run_id, "agent-synthesis")
        pointer = pending["body"]["tool_operation_ref"]
        scope = dict(
            mission_id=pending["body"]["mission_id"],
            actor_id=pending["body"]["actor_id"],
            task_id=pending["body"]["task_id"],
        )
        if case == "uncertain_dispatch":
            operation_id = host.store.get(**pointer)["body"]["operation_id"]
            key = hashlib.sha256(
                canonical(
                    {
                        "mission_id": scope["mission_id"],
                        "operation_id": operation_id,
                    }
                ).encode()
            ).hexdigest()[:32]
            request = {
                "mission_id": scope["mission_id"],
                "run_id": run.run_id,
                "actor_id": scope["actor_id"],
                "catalog_ref": selected["catalog_ref"],
                "link_index": 0,
                "requested_url": entry["links"][0]["url"],
                "max_bytes": 2 * 1024 * 1024,
            }
            host.store.transact(
                "fixture-interrupted-navigation",
                request,
                lambda tx: tx.put(
                    "venue_navigation_dispatch",
                    f"venue-navigation-dispatch-{key}",
                    {
                        "schema": "ora.venue_navigation_dispatch.v1",
                        "request": request,
                        "dispatched_at": host.store.now(),
                    },
                ),
                mission_id=scope["mission_id"],
            )
            assert read_navigation_operation(host.store, pointer, **scope)["status"] == (
                "fetch_dispatch_recorded"
            )
            assert fetches == []  # The marker alone does not prove an HTTP call began.
        drain(host, run.run_id, limit=30)
        outcome = read_navigation_operation(host.store, pointer, **scope)
        assert outcome["status"] == expected_status
        assert outcome["tool_outcome"] == (
            "uncertain_dispatch" if case == "uncertain_dispatch" else "rejected"
        )
        assert outcome["navigation_ref"] is None
        assert outcome["tool_result_ref"] is not None
        assert selected["final_status"] == outcome["tool_outcome"]
        assert fetches == []
        if case == "uncertain_dispatch":
            real_get = host.store.get

            def hide_dispatch(kind, id, version=None):
                if kind == "venue_navigation_dispatch":
                    return None
                return real_get(kind, id, version)

            monkeypatch.setattr(host.store, "get", hide_dispatch)
            assert read_navigation_operation(host.store, pointer, **scope)["status"] == (
                "inconsistent_saved_result"
            )
            monkeypatch.setattr(host.store, "get", real_get)
    finally:
        host.close()


@pytest.mark.parametrize("raw_arguments", ["[]", "{malformed"])
def test_invalid_model_tool_arguments_stop_before_navigation_submission(
    tmp_path, monkeypatch, raw_arguments
):
    import ora.mission.service as service_module

    calls = []
    fetches = []

    def forbidden_fetch(url, *, max_bytes):
        fetches.append(url)
        raise AssertionError("Invalid model arguments must not reach HTTP")

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        count = len(calls)
        if count == 1:
            return tool("request_capability", {"tool_id": "navigate_venue"}, count)
        assert count == 2
        return Response(
            {
                "id": f"offline-{count}",
                "model": "openai/gpt-oss-120b",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"tool-{count}",
                                    "type": "function",
                                    "function": {
                                        "name": "navigate_venue",
                                        "arguments": raw_arguments,
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
            }
        )

    monkeypatch.setattr(service_module, "fetch_public_source", forbidden_fetch)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(
            LiveStartRequest(
                request_id=f"invalid-navigation-{hashlib.sha256(raw_arguments.encode()).hexdigest()[:8]}",
                question="Inspect a synthetic venue lead",
                agent_ids=["agent-synthesis"],
            )
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            responses = host.store.records("model_response_receipt")
            if len(responses) >= 2 and any(
                item["body"]["status"] == "failed" for item in responses
            ):
                break
            host._drive_run(host._run(run.run_id))
            time.sleep(0.005)
        else:
            raise AssertionError("Invalid tool response did not settle as failed")
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] != "ready_tool"
        assert host.store.records("mission_tool_operation") == []
        assert sum(
            work["body"]["spec"]["handler_id"] == "mission-tool"
            for work in host.store.records("executor_work")
        ) == 1  # Only the preceding request_capability call was submitted.
        assert fetches == []
        assert len(calls) == 2
    finally:
        host.close()


def test_saved_nonobject_navigation_arguments_reject_without_fetch_or_operation_claim(
    tmp_path, monkeypatch
):
    """A malformed old/current control must not turn local rejection into uncertainty."""
    import ora.mission.service as service_module

    calls = []
    fetches = []
    selected = {}

    def forbidden_fetch(url, *, max_bytes):
        fetches.append(url)
        raise AssertionError("Local schema rejection must not reach HTTP")

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        count = len(calls)
        if count == 1:
            return tool("request_capability", {"tool_id": "navigate_venue"}, count)
        assert count == 2
        return tool("navigate_venue", {"catalog_ref": selected["catalog_ref"]}, count)

    monkeypatch.setattr(service_module, "fetch_public_source", forbidden_fetch)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(
            LiveStartRequest(
                request_id="saved-nonobject-navigation",
                question="Inspect a synthetic venue lead",
                agent_ids=["agent-synthesis"],
            )
        )
        control = host._control(run.run_id, "agent-synthesis")
        selected["catalog_ref"] = list_catalog_leads(
            host.store,
            mission_id=control["body"]["mission_id"],
            run_id=run.run_id,
            actor_id=control["body"]["actor_id"],
            limit=1,
        )["entries"][0]["catalog_ref"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = host._control(run.run_id, "agent-synthesis")
            if current["body"].get("phase") == "ready_tool" and (
                current["body"].get("tool_call") or {}
            ).get("name") == "navigate_venue":
                break
            host._drive_run(host._run(run.run_id))
            time.sleep(0.005)
        else:
            raise AssertionError("Valid navigation request was not prepared")
        assert host._schedule_tool(current)
        pending = host._control(run.run_id, "agent-synthesis")
        malformed = {**pending["body"]["tool_call"], "arguments": []}
        host._update_control(pending, tool_call=malformed)
        work = next(
            item
            for item in host.store.records("executor_work")
            if item["body"]["spec"]["work_id"] == pending["body"]["work_id"]
        )
        result = host._tool_work(host.executor._spec_from(work))
        assert result.payload["result"]["status"] == "rejected"
        assert result.payload["result"]["external_action"] == "none"
        assert "operation_ref" not in result.payload["result"]
        assert fetches == []
    finally:
        host.close()


def test_late_navigation_after_task_revision_is_retained_unaccepted(tmp_path, monkeypatch):
    import ora.mission.service as service_module

    started = threading.Event()
    release = threading.Event()
    selected = {}
    fetches = []
    html = b"<html><title>Late synthetic lead</title></html>"

    def fetch(url, *, max_bytes):
        fetches.append(url)
        started.set()
        assert release.wait(5)
        return {
            "status": 200,
            "content": html,
            "content_sha256": hashlib.sha256(html).hexdigest(),
            "byte_length": len(html),
            "content_type": "text/html",
            "requested_url": url,
            "final_url": url,
            "redirect_chain": [],
            "access_basis": "public_https_unauthenticated",
            "authenticated": False,
            "retrieved_at": "2026-10-11T00:00:00Z",
        }

    calls = []

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        if len(calls) == 1:
            return tool("request_capability", {"tool_id": "navigate_venue"}, 1)
        assert len(calls) == 2
        return tool("navigate_venue", {"catalog_ref": selected["catalog_ref"]}, 2)

    monkeypatch.setattr(service_module, "fetch_public_source", fetch)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(
            LiveStartRequest(
                request_id="late-local-navigation",
                question="Inspect a synthetic venue lead",
                agent_ids=["agent-synthesis"],
            )
        )
        control = host._control(run.run_id, "agent-synthesis")
        selected["catalog_ref"] = list_catalog_leads(
            host.store,
            mission_id=control["body"]["mission_id"],
            run_id=run.run_id,
            actor_id=control["body"]["actor_id"],
            limit=1,
        )["entries"][0]["catalog_ref"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not started.is_set():
            host._drive_run(host._run(run.run_id))
            time.sleep(0.005)
        assert started.is_set()
        pending = host._control(run.run_id, "agent-synthesis")
        pointer = pending["body"]["tool_operation_ref"]
        scope = dict(
            mission_id=pending["body"]["mission_id"],
            actor_id=pending["body"]["actor_id"],
            task_id=pending["body"]["task_id"],
        )
        proposal = host.store.get("board_proposal", scope["task_id"])
        revised = host.board.revise(
            "fixture-revise-before-late-navigation",
            mission_id=scope["mission_id"],
            proposal_id=proposal["id"],
            actor_id=scope["actor_id"],
            expected_version=proposal["version"],
            reason="Synthetic task meaning changed while fetch was running",
        )
        assert revised["status"] == "accepted"
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            host.dispatch.poll(scope["mission_id"])
            observed = read_navigation_operation(host.store, pointer, **scope)
            if observed and observed["status"] == "result_unaccepted":
                break
            time.sleep(0.005)
        else:
            raise AssertionError("Late navigation was not retained as unaccepted")
        assert observed["navigation_ref"] and observed["dispatch_ref"]
        assert observed["tool_result_ref"] is None
        assert len(fetches) == 1 and len(calls) == 2
    finally:
        release.set()
        host.close()

    reopened = service(tmp_path, monkeypatch, transport)
    try:
        reopened._drive_run(reopened._run(run.run_id))
        assert (
            read_navigation_operation(reopened.store, pointer, **scope)["status"]
            == "result_unaccepted"
        )
        assert len(fetches) == 1 and len(calls) == 2
    finally:
        reopened.close()


def test_mismatched_saved_response_rejects_operation_and_work_atomically(tmp_path, monkeypatch):
    selected = {}
    calls = []

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        if len(calls) == 1:
            return tool("request_capability", {"tool_id": "navigate_venue"}, 1)
        assert len(calls) == 2
        return tool("navigate_venue", {"catalog_ref": selected["catalog_ref"]}, 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(
            LiveStartRequest(
                request_id="mismatched-local-operation",
                question="Inspect a venue lead",
                agent_ids=["agent-synthesis"],
            )
        )
        initial = host._control(run.run_id, "agent-synthesis")
        selected["catalog_ref"] = list_catalog_leads(
            host.store,
            mission_id=initial["body"]["mission_id"],
            run_id=run.run_id,
            actor_id=initial["body"]["actor_id"],
            limit=1,
        )["entries"][0]["catalog_ref"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = host._control(run.run_id, "agent-synthesis")
            if (
                current["body"].get("phase") == "ready_tool"
                and current["body"].get("tool_call", {}).get("name") == "navigate_venue"
            ):
                break
            host._drive_run(host._run(run.run_id))
            time.sleep(0.005)
        else:
            raise AssertionError("Model did not request navigation")
        old_response = current["body"]["tool_turn_refs"][0]
        corrupted = host._update_control(current, response_ref=old_response)
        changed = host.store.get(**corrupted)
        work_id = host._tool_id(changed, changed["body"]["tool_call"]["id"])
        with pytest.raises(StateConflict, match="lineage changed"):
            host._schedule_tool(changed)
        assert host.store.get("mission_task_control", changed["id"]) == changed
        assert not any(
            item["body"]["spec"]["work_id"] == work_id
            for item in host.store.records("executor_work")
        )
        assert host.store.records("mission_tool_operation") == []
    finally:
        host.close()


def test_http_block_is_accepted_as_blocked_navigation_without_source_claim(tmp_path, monkeypatch):
    import ora.mission.service as service_module

    selected = {}
    fetches = []
    calls = []

    def blocked_fetch(url, *, max_bytes):
        fetches.append(url)
        raise PublicSourceHTTPError(403, requested_url=url, final_url=url, redirect_chain=[])

    def transport(request, *, timeout):
        body = json.loads(request.data)
        calls.append(body)
        count = len(calls)
        if count == 1:
            return tool("request_capability", {"tool_id": "navigate_venue"}, count)
        if count == 2:
            return tool("navigate_venue", {"catalog_ref": selected["catalog_ref"]}, count)
        assert count == 3
        prior = json.loads(
            next(item["content"] for item in reversed(body["messages"]) if item["role"] == "tool")
        )
        assert prior["status"] == "blocked" and prior["evidence_status"] == "navigation_lead_only"
        return tool(
            "publish_result",
            {
                "title": "Blocked venue lead",
                "text": "The venue page was inaccessible.",
                "citations": [],
                "limitations": ["No page or publication text was retrieved"],
            },
            count,
        )

    monkeypatch.setattr(service_module, "fetch_public_source", blocked_fetch)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(
            LiveStartRequest(
                request_id="blocked-navigation-operation",
                question="Inspect a venue lead",
                agent_ids=["agent-synthesis"],
            )
        )
        control = host._control(run.run_id, "agent-synthesis")
        selected["catalog_ref"] = list_catalog_leads(
            host.store,
            mission_id=control["body"]["mission_id"],
            run_id=run.run_id,
            actor_id=control["body"]["actor_id"],
            limit=1,
        )["entries"][0]["catalog_ref"]
        drain(host, run.run_id, limit=30)
        record = host.store.records("mission_tool_operation")[0]
        scope = dict(
            mission_id=record["body"]["mission_id"],
            actor_id=record["body"]["actor_id"],
            task_id=record["body"]["task_id"],
        )
        outcome = read_navigation_operation(host.store, ref(record), **scope)
        assert outcome["status"] == "accepted" and outcome["tool_outcome"] == "blocked"
        assert outcome["navigation_ref"] and outcome["tool_result_ref"]
        navigation = host.store.get(**outcome["navigation_ref"])
        assert navigation["body"].get("blob") is None
        assert navigation["body"]["status_code"] == 403
        assert len(fetches) == 1 and host.store.records("source_copy") == []
    finally:
        host.close()


def test_legacy_pending_navigation_finishes_after_restart_without_new_operation_claim(
    tmp_path, monkeypatch
):
    import ora.mission.service as service_module

    selected = {}
    calls = []
    fetches = []
    html = b"<html><title>Legacy synthetic lead</title></html>"

    def fetch(url, *, max_bytes):
        fetches.append(url)
        return {
            "status": 200,
            "content": html,
            "content_sha256": hashlib.sha256(html).hexdigest(),
            "byte_length": len(html),
            "content_type": "text/html",
            "requested_url": url,
            "final_url": url,
            "redirect_chain": [],
            "access_basis": "public_https_unauthenticated",
            "authenticated": False,
            "retrieved_at": "2026-10-11T00:00:00Z",
        }

    def transport(request, *, timeout):
        body = json.loads(request.data)
        calls.append(body)
        count = len(calls)
        if count == 1:
            return tool("request_capability", {"tool_id": "navigate_venue"}, count)
        if count == 2:
            return tool("navigate_venue", {"catalog_ref": selected["catalog_ref"]}, count)
        assert count == 3
        prior = json.loads(
            next(item["content"] for item in reversed(body["messages"]) if item["role"] == "tool")
        )
        assert prior["status"] == "navigable"
        assert "operation_ref" not in prior
        return tool(
            "publish_result",
            {
                "title": "Legacy venue lead",
                "text": "A saved page was inspected.",
                "citations": [],
                "limitations": ["No publication source was inspected"],
            },
            count,
        )

    monkeypatch.setattr(service_module, "fetch_public_source", fetch)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(
            LiveStartRequest(
                request_id="legacy-pending-navigation",
                question="Inspect a venue lead",
                agent_ids=["agent-synthesis"],
            )
        )
        control = host._control(run.run_id, "agent-synthesis")
        selected["catalog_ref"] = list_catalog_leads(
            host.store,
            mission_id=control["body"]["mission_id"],
            run_id=run.run_id,
            actor_id=control["body"]["actor_id"],
            limit=1,
        )["entries"][0]["catalog_ref"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = host._control(run.run_id, "agent-synthesis")
            if (
                current["body"].get("phase") == "ready_tool"
                and current["body"].get("tool_call", {}).get("name") == "navigate_venue"
            ):
                break
            host._drive_run(host._run(run.run_id))
            time.sleep(0.005)
        else:
            raise AssertionError("Model did not request navigation")
        # Recreate a pre-upgrade pending submission atomically: runnable work
        # and control exist, but no operation metadata was saved or claimed.
        monkeypatch.setattr(service_module, "save_navigation_operation_tx", lambda *_a, **_k: None)
        assert host._schedule_tool(current)
        pending = host._control(run.run_id, "agent-synthesis")
        work_id = pending["body"]["work_id"]

        def old_control(tx):
            existing = tx.get("mission_task_control", pending["id"])
            body = {**existing["body"]}
            body.pop("tool_operation_ref")
            return ref(
                tx.put(
                    "mission_task_control",
                    pending["id"],
                    body,
                    expected_version=existing["version"],
                )
            )

        host.store.transact(
            "fixture-legacy-control",
            {"control_ref": ref(pending)},
            old_control,
            mission_id=pending["body"]["mission_id"],
        )
        assert host.store.records("mission_tool_operation") == []
        assert fetches == []
        assert any(
            item["body"]["spec"]["work_id"] == work_id
            for item in host.store.records("executor_work")
        )
    finally:
        host.close()

    reopened = service(tmp_path, monkeypatch, transport)
    try:
        drain(reopened, run.run_id, limit=30)
        assert reopened._run(run.run_id)["body"]["status"] == "completed"
        assert len(fetches) == 1 and len(calls) == 3
        assert reopened.store.records("mission_tool_operation") == []
        assert len(reopened.store.records("venue_navigation")) == 1
        tool_result = next(
            item
            for item in reopened.store.records("context_tool_result")
            if item["body"]["tool_call_id"] == "tool-2"
        )
        message = json.loads(reopened.store.read_blob(tool_result["body"]["blob"]["sha256"]))
        assert "operation_ref" not in json.loads(message["content"])
        reopened._drive_run(reopened._run(run.run_id))
        assert len(fetches) == 1 and len(calls) == 3
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "scenario", ["late_result", "late_after_reopen", "accepted_then_reopened",
                 "no_result", "revised_task", "cancelled_uncertain", "cancel_during_accept",
                 "new_board_claim", "capacity_claim_race"]
)
def test_interrupted_navigation_reconciles_only_a_current_saved_result(
    tmp_path, monkeypatch, scenario
):
    """Shutdown during the fetch cannot lose a known page or repeat its effect."""
    import ora.mission.service as service_module

    started = threading.Event()
    release = threading.Event()
    selected = {}
    calls = []
    fetches = []
    html = b"<html><title>Synthetic late page</title></html>"

    def fetch(url, *, max_bytes):
        fetches.append(url)
        started.set()
        assert release.wait(10), "Controlled fetch was not released"
        return {
            "status": 200,
            "content": html,
            "content_sha256": hashlib.sha256(html).hexdigest(),
            "byte_length": len(html),
            "content_type": "text/html",
            "requested_url": url,
            "final_url": url,
            "redirect_chain": [],
            "access_basis": "public_https_unauthenticated",
            "authenticated": False,
            "retrieved_at": "2026-10-12T00:00:00Z",
        }

    def transport(request, *, timeout):
        body = json.loads(request.data)
        calls.append(body)
        if "Mxra" in body["messages"][0]["content"]:
            return answer("An independent synthetic sibling result.", 100)
        count = len([item for item in calls if "Mxra" not in item["messages"][0]["content"]])
        if count == 1:
            return tool("request_capability", {"tool_id": "navigate_venue"}, count)
        if count == 2:
            return tool("navigate_venue", {"catalog_ref": selected["catalog_ref"]}, count)
        return answer("Synthetic continuation after navigation.", count)

    monkeypatch.setattr(service_module, "fetch_public_source", fetch)
    host = service(tmp_path, monkeypatch, transport)
    reopened = None
    try:
        run = host.start(
            LiveStartRequest(
                request_id=f"interrupted-navigation-{scenario}",
                question="Inspect a synthetic venue lead",
                agent_ids=["agent-synthesis", "agent-critique"],
            )
        )
        initial = host._control(run.run_id, "agent-synthesis")
        selected["catalog_ref"] = list_catalog_leads(
            host.store,
            mission_id=initial["body"]["mission_id"],
            run_id=run.run_id,
            actor_id=initial["body"]["actor_id"],
            limit=1,
        )["entries"][0]["catalog_ref"]
        deadline = time.monotonic() + 5
        while not started.is_set() and time.monotonic() < deadline:
            host._drive_run(host._run(run.run_id))
            time.sleep(0.005)
        assert started.is_set()
        pending = host._control(run.run_id, "agent-synthesis")
        pointer = pending["body"]["tool_operation_ref"]
        scope = dict(
            mission_id=pending["body"]["mission_id"],
            actor_id=pending["body"]["actor_id"],
            task_id=pending["body"]["task_id"],
        )
        deadline = time.monotonic() + 5
        while host._control(run.run_id, "agent-critique")["body"]["phase"] != "done":
            assert time.monotonic() < deadline
            host._drive_run(host._run(run.run_id))
            time.sleep(0.005)
        assert read_navigation_operation(host.store, pointer, **scope)["status"] == (
            "fetch_dispatch_recorded"
        )
        operation = host.store.get(**pointer)
        cap_id = operation["body"]["capacity_operation_id"]
        old_cap = host.capacity.get(cap_id)
        assert len(fetches) == 1 and len(calls) == 3
        host.close()
        reopened = service(tmp_path, monkeypatch, transport)
        assert reopened.capacity.get(cap_id)["phase"] == "uncertain"
        current = reopened._control(run.run_id, "agent-synthesis")
        spec = reopened.executor._spec_from(reopened._work_for(current))
        if scenario == "revised_task":
            proposal = reopened.store.get("board_proposal", scope["task_id"])
            assert reopened.board.revise(
                "fixture-revise-interrupted-navigation",
                mission_id=scope["mission_id"],
                proposal_id=scope["task_id"],
                actor_id=scope["actor_id"],
                expected_version=proposal["version"],
                reason="Synthetic task meaning changed after shutdown",
            )["status"] == "accepted"
        if scenario == "cancelled_uncertain":
            assert reopened.capacity.cancel(cap_id, reason="branch_cancel")["cancel_requested"]
        if scenario == "cancel_during_accept":
            original_claim = reopened.capacity.claim_uncertain

            def cancel_after_claim(*args, **kwargs):
                claimed = original_claim(*args, **kwargs)
                assert reopened.capacity.cancel(cap_id, reason="branch_cancel")["cancel_requested"]
                return claimed

            monkeypatch.setattr(reopened.capacity, "claim_uncertain", cancel_after_claim)
        if scenario in {"new_board_claim", "capacity_claim_race"}:
            old_claim = next(
                item["current_claim"] for item in reopened.board.open_work(scope["mission_id"])
                if item["proposal_ref"] == spec.proposal_ref
            )
            reopened.executor._release_board(spec, old_claim)
            if scenario == "new_board_claim":
                fresh = reopened.board.claim(
                    "fixture-fresh-claim-for-other-work",
                    mission_id=spec.mission_id, proposal_id=spec.proposal_ref["id"],
                    actor_id=spec.actor_id, ttl=60,
                    expected_version=spec.proposal_ref["version"],
                )
                assert fresh["status"] == "accepted"
                assert fresh["fence"]["generation"] > old_claim["generation"]
            else:
                def unavailable_capacity(*_args, **_kwargs):
                    raise StateConflict("Synthetic concurrent capacity claimant")

                monkeypatch.setattr(reopened.capacity, "claim_uncertain", unavailable_capacity)
        if scenario == "late_after_reopen":
            reopened._drive_run(reopened._run(run.run_id))
            assert reopened._control(run.run_id, "agent-synthesis")["body"]["phase"] == "waiting"
        if scenario != "no_result":
            release.set()
            deadline = time.monotonic() + 5
            while not reopened.store.records("venue_navigation"):
                assert time.monotonic() < deadline
                time.sleep(0.005)
        if scenario == "accepted_then_reopened":
            current = reopened._control(run.run_id, "agent-synthesis")
            assert reconcile_late_navigation(
                reopened.executor, current, reopened._work_for(current),
            )["status"] == "accepted"
            assert read_navigation_operation(reopened.store, pointer, **scope)["tool_result_ref"] is None
            reopened.close()
            reopened = service(tmp_path, monkeypatch, transport)
        for _ in range(4):
            reopened._drive_run(reopened._run(run.run_id))
            observed = read_navigation_operation(reopened.store, pointer, **scope)
            if scenario in {"no_result", "capacity_claim_race"} or (
                observed["status"] in {"accepted", "result_unaccepted"}
                and (
                    scenario in {"revised_task", "cancelled_uncertain", "cancel_during_accept"}
                    or observed["tool_result_ref"]
                )
            ):
                break
        if scenario == "no_result":
            assert reopened.store.records("venue_navigation") == []
            assert reopened.capacity.get(cap_id)["phase"] == "uncertain"
            assert reopened._control(run.run_id, "agent-synthesis")["body"]["phase"] == "waiting"
        elif scenario in {"revised_task", "cancelled_uncertain", "cancel_during_accept"}:
            assert observed["status"] == "result_unaccepted"
            assert observed["navigation_ref"] and observed["tool_result_ref"] is None
            assert reopened.capacity.get(cap_id)["phase"] == "known_unaccepted"
            if scenario != "revised_task":
                source_proposal = reopened.store.get(spec.proposal_ref["kind"], spec.proposal_ref["id"])
                assert source_proposal["body"]["status"] == "open"
                claim_view = next(
                    item for item in reopened.board.open_work(spec.mission_id)
                    if item["proposal_ref"] == spec.proposal_ref
                )
                assert claim_view["current_claim"] is None
                other_actor = reopened._control(run.run_id, "agent-critique")["body"]["actor_id"]
                assert reopened.board.claim(
                    f"fixture-other-peer-after-{scenario}",
                    mission_id=spec.mission_id, proposal_id=spec.proposal_ref["id"],
                    actor_id=other_actor, ttl=60,
                    expected_version=spec.proposal_ref["version"],
                )["status"] == "accepted"
        elif scenario == "new_board_claim":
            assert observed["navigation_ref"] and observed["tool_result_ref"] is None
            assert reopened.capacity.get(cap_id)["phase"] == "uncertain"
            current_claim = next(
                item["current_claim"] for item in reopened.board.open_work(spec.mission_id)
                if item["proposal_ref"] == spec.proposal_ref
            )
            assert current_claim == fresh["fence"]
        elif scenario == "capacity_claim_race":
            assert observed["navigation_ref"] and observed["tool_result_ref"] is None
            assert reopened.capacity.get(cap_id)["phase"] == "uncertain"
            claim_view = next(
                item for item in reopened.board.open_work(spec.mission_id)
                if item["proposal_ref"] == spec.proposal_ref
            )
            assert claim_view["current_claim"] is None
            other_actor = reopened._control(run.run_id, "agent-critique")["body"]["actor_id"]
            assert reopened.board.claim(
                "fixture-other-peer-after-capacity-race",
                mission_id=spec.mission_id, proposal_id=spec.proposal_ref["id"],
                actor_id=other_actor, ttl=60,
                expected_version=spec.proposal_ref["version"],
            )["status"] == "accepted"
        else:
            assert observed["status"] == "accepted"
            assert observed["tool_result_ref"] and observed["navigation_ref"]
            assert reopened.capacity.get(cap_id)["phase"] == "completed"
            with pytest.raises(StateConflict, match="owner generation"):
                reopened.capacity.settle(
                    cap_id, host.executor.owner, old_cap["generation"],
                    used_tokens=None, outcome="completed", output_ref="stale",
                )
            drain(reopened, run.run_id, limit=30)
            synthesis_calls = [
                item for item in calls if "Mxra" not in item["messages"][0]["content"]
            ]
            assert len(synthesis_calls) == 3
            prior = json.loads(
                next(
                    item["content"] for item in reversed(synthesis_calls[-1]["messages"])
                    if item["role"] == "tool"
                )
            )
            assert prior["operation_ref"] == pointer
            assert prior["navigation_ref"] == observed["navigation_ref"]
            assert reopened._run(run.run_id)["body"]["status"] == "completed"
        assert len(fetches) == 1
        assert len(calls) == (
            4 if scenario in {"late_result", "late_after_reopen", "accepted_then_reopened"} else 3
        )
        assert len(reopened.store.records("venue_navigation")) <= 1
    finally:
        release.set()
        host.close()
        if reopened is not None:
            reopened.close()
