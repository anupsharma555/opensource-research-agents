"""A peer can read checked retained HTML through the ordinary source window."""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from ora.contracts.models import LiveStartRequest
from tests.mission.test_service import drain, service, tool


def _last_tool_result(request):
    return json.loads(next(
        item["content"] for item in reversed(json.loads(request.data)["messages"])
        if item["role"] == "tool"
    ))


def test_peer_reads_saved_nonpublication_html_in_next_model_turn(tmp_path, monkeypatch):
    import ora.mission.service as service_module

    calls = []
    fetches = []
    selected = {}
    html = (b"<html><title>Program</title><main><h1>Program</h1>"
            b"<p>Poster 17: synthetic methods and findings.</p></main></html>")

    def fake_fetch(url, *, max_bytes):
        fetches.append(url)
        assert len(html) <= max_bytes
        return {
            "status": 200, "content": html,
            "content_sha256": hashlib.sha256(html).hexdigest(), "byte_length": len(html),
            "content_type": "text/html", "requested_url": url, "final_url": url,
            "redirect_chain": [], "access_basis": "public_https_unauthenticated",
            "authenticated": False, "retrieved_at": "2026-10-11T00:00:00Z",
        }

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        count = len(calls)
        if count == 1:
            return tool("request_capability", {"tool_id": "list_venues"}, count)
        if count == 2:
            return tool("list_venues", {"limit": 1}, count)
        if count == 3:
            selected["catalog_ref"] = _last_tool_result(request)["entries"][0]["catalog_ref"]
            return tool("request_capability", {"tool_id": "navigate_venue"}, count)
        if count == 4:
            return tool("navigate_venue", {"catalog_ref": selected["catalog_ref"]}, count)
        if count == 5:
            navigation = _last_tool_result(request)
            selected["navigation_ref"] = navigation["navigation_ref"]
            return tool("request_capability", {"tool_id": "inspect_navigation_document"}, count)
        if count == 6:
            return tool("inspect_navigation_document", {
                "navigation_ref": selected["navigation_ref"],
            }, count)
        if count == 7:
            inventory = next(
                json.loads(item["content"])["inventory"]
                for item in json.loads(request.data)["messages"]
                if item["role"] == "user" and "inventory" in json.loads(item["content"])
            )
            item = inventory["items"][0]
            assert item["model_input_status"] == "permitted"
            assert item["title"] == "Program"
            selected["copy_ref"] = item["ref"]
            assert "read_source_window" not in {
                entry["function"]["name"] for entry in calls[-1]["tools"]
            }
            return tool("request_capability", {"tool_id": "read_source_window"}, count)
        if count == 8:
            assert "read_source_window" in {
                entry["function"]["name"] for entry in calls[-1]["tools"]
            }
            return tool("read_source_window", {
                "source_id": selected["copy_ref"]["id"],
                "source_version": selected["copy_ref"]["version"],
                "block_offset": 1, "char_offset": 0, "char_limit": 200,
            }, count)
        assert count == 9
        visible = json.dumps(json.loads(request.data)["messages"])
        assert "Poster 17: synthetic methods and findings." in visible
        window = host.store.records("source_read_window")[0]
        passage = host.store.get(**window["body"]["passage_refs"][0])
        selected["span_id"] = passage["body"]["span_id"]
        assert selected["span_id"] in visible
        return tool("publish_result", {
            "title": "Program reviewed", "text": "The synthetic poster was listed.",
            "citations": [{"source_id": selected["copy_ref"]["id"],
                           "span_id": selected["span_id"]}],
            "limitations": ["A conference program is not a scientific study"],
        }, count)

    monkeypatch.setattr(service_module, "fetch_public_source", fake_fetch)
    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(
        request_id="saved-program-document", question="Read a conference program",
        agent_ids=["agent-synthesis"],
    ))
    drain(host, run.run_id, limit=35)
    assert host._run(run.run_id)["body"]["status"] == "completed"
    assert len(calls) == 9 and len(fetches) == 1
    assert len(host.store.records("source_copy")) == 1
    assert host.store.records("publication") == []
    snapshot = host.state(run.run_id).snapshot
    assert snapshot.sources[0].access_state == "navigation_text"
    assert snapshot.sources[0].title == "Program"
    assert snapshot.artifacts[0].citations[0].span_id == selected["span_id"]


@pytest.mark.parametrize(
    "scenario", ["resume", "revoked_use", "cancelled", "replacement_claim"],
)
def test_interrupted_local_inspection_recovers_only_original_current_work(
    tmp_path, monkeypatch, scenario,
):
    import ora.mission.service as service_module

    calls = []
    fetches = []
    selected = {}
    html = (b"<html><title>Program</title><main><h1>Program</h1>"
            b"<p>Poster 17: synthetic methods and findings.</p></main></html>")

    def fake_fetch(url, *, max_bytes):
        fetches.append(url)
        assert len(html) <= max_bytes
        return {
            "status": 200, "content": html,
            "content_sha256": hashlib.sha256(html).hexdigest(), "byte_length": len(html),
            "content_type": "text/html", "requested_url": url, "final_url": url,
            "redirect_chain": [], "access_basis": "public_https_unauthenticated",
            "authenticated": False, "retrieved_at": "2026-10-11T00:00:00Z",
        }

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        step = len(calls)
        if step == 1:
            return tool("request_capability", {"tool_id": "list_venues"}, step)
        if step == 2:
            return tool("list_venues", {"limit": 1}, step)
        if step == 3:
            selected["catalog_ref"] = _last_tool_result(request)["entries"][0]["catalog_ref"]
            return tool("request_capability", {"tool_id": "navigate_venue"}, step)
        if step == 4:
            return tool("navigate_venue", {"catalog_ref": selected["catalog_ref"]}, step)
        if step == 5:
            selected["navigation_ref"] = _last_tool_result(request)["navigation_ref"]
            return tool("request_capability", {"tool_id": "inspect_navigation_document"}, step)
        if step == 6:
            return tool("inspect_navigation_document", {
                "navigation_ref": selected["navigation_ref"],
            }, step)
        assert scenario in {"resume", "revoked_use"}
        if step == 7:
            if scenario == "revoked_use":
                assert "Poster 17: synthetic methods and findings." not in json.dumps(
                    json.loads(request.data)["messages"]
                )
                return tool("publish_result", {
                    "title": "Program unavailable for reading",
                    "text": "The saved program was not used for a finding.",
                    "citations": [],
                    "limitations": ["Current source-use permission was revoked"],
                }, step)
            inventory = next(
                json.loads(item["content"])["inventory"]
                for item in json.loads(request.data)["messages"]
                if item["role"] == "user" and "inventory" in json.loads(item["content"])
            )
            assert inventory["items"][0]["model_input_status"] == "permitted"
            selected["copy_ref"] = inventory["items"][0]["ref"]
            assert "read_source_window" not in {
                entry["function"]["name"] for entry in calls[-1]["tools"]
            }
            return tool("request_capability", {"tool_id": "read_source_window"}, step)
        if step == 8:
            assert "read_source_window" in {
                entry["function"]["name"] for entry in calls[-1]["tools"]
            }
            return tool("read_source_window", {
                "source_id": selected["copy_ref"]["id"],
                "source_version": selected["copy_ref"]["version"],
                "block_offset": 1, "char_offset": 0, "char_limit": 200,
            }, step)
        assert step == 9
        visible = json.dumps(json.loads(request.data)["messages"])
        assert "Poster 17: synthetic methods and findings." in visible
        window = reopened.store.records("source_read_window")[0]
        passage = reopened.store.get(**window["body"]["passage_refs"][0])
        return tool("publish_result", {
            "title": "Program reviewed", "text": "The synthetic poster was listed.",
            "citations": [{"source_id": selected["copy_ref"]["id"],
                           "span_id": passage["body"]["span_id"]}],
            "limitations": ["A conference program is not a scientific study"],
        }, step)

    monkeypatch.setattr(service_module, "fetch_public_source", fake_fetch)
    host = service(tmp_path, monkeypatch, transport)
    original_transact = host.store.transact
    interrupted = False

    def crash_before_use(operation_id, *args, **kwargs):
        nonlocal interrupted
        if operation_id.startswith("mission-navigation-document:") and operation_id.endswith(
            "-use"
        ) and not interrupted:
            interrupted = True
            raise RuntimeError("synthetic shutdown after local copy")
        return original_transact(operation_id, *args, **kwargs)

    monkeypatch.setattr(host.store, "transact", crash_before_use)
    run = host.start(LiveStartRequest(
        request_id=f"saved-program-recovery-{scenario}",
        question="Read a synthetic conference program after local interruption",
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
        raise AssertionError("Interrupted local inspection did not become uncertain")
    assert len(calls) == 6
    assert len(host.store.records("source_copy")) == 1
    assert host.store.records("source_use") == []
    original_work_id = work["body"]["spec"]["work_id"]
    cap_id = host.executor._capacity_id(host.executor._spec_from(work))
    host.close()

    reopened = service(tmp_path, monkeypatch, transport)
    try:
        control = reopened._control(run.run_id, "agent-synthesis")
        work = reopened._work_for(control)
        assert work["body"]["spec"]["work_id"] == original_work_id
        assert reopened.capacity.get(cap_id)["phase"] == "uncertain"
        if scenario == "revoked_use":
            original_operation = f"mission-navigation-document:{original_work_id}"
            granted = reopened.intake.inspect_navigation_document(
                original_operation, selected["navigation_ref"],
                mission_id=control["body"]["mission_id"],
                run_id=run.run_id, actor_id=control["body"]["actor_id"],
            )
            def revoke(tx):
                use = tx.get(**granted["source_use_ref"])
                tx.put("source_use", use["id"], {
                    **use["body"], "allowed_uses": ["discovery"],
                    "policy_status": "revoked",
                }, expected_version=use["version"])
                return {"revoked": True}
            reopened.store.transact("revoke-recovered-program-use", {}, revoke)
        if scenario == "cancelled":
            reopened.capacity.cancel(cap_id, reason="branch_cancel")
        if scenario == "replacement_claim":
            spec = reopened.executor._spec_from(work)
            old_claim = next(
                item["current_claim"] for item in reopened.board.open_work(spec.mission_id)
                if item["proposal_ref"] == spec.proposal_ref
            )
            reopened.executor._release_board(spec, old_claim)
            replacement = reopened.board.claim(
                "synthetic-replacement-inspection-claim",
                mission_id=spec.mission_id, proposal_id=spec.proposal_ref["id"],
                actor_id=spec.actor_id, ttl=60,
                expected_version=spec.proposal_ref["version"],
            )
            assert replacement["status"] == "accepted"
            assert replacement["fence"]["generation"] > old_claim["generation"]
        if scenario in {"resume", "revoked_use"}:
            drain(reopened, run.run_id, limit=35)
            assert reopened._run(run.run_id)["body"]["status"] == "completed"
            assert len(calls) == (9 if scenario == "resume" else 7)
            assert len(reopened.store.records("source_copy")) == 1
            assert len(reopened.store.records("source_use")) == 1
            assert len(reopened.store.records("publication")) == 0
            if scenario == "resume":
                assert reopened.state(run.run_id).snapshot.sources[0].access_state == (
                    "navigation_text"
                )
            else:
                assert reopened._control(run.run_id, "agent-synthesis")["body"]["source_refs"] == []
        else:
            for _ in range(3):
                reopened._drive_run(reopened._run(run.run_id))
            assert len(calls) == 6
            assert reopened.store.records("source_use") == []
            assert reopened.capacity.get(cap_id)["phase"] == "uncertain"
        assert len(fetches) == 1
    finally:
        reopened.close()
