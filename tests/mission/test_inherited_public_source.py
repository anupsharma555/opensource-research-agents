"""An accepted follow-up can reuse one exact checked public document."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.coordination.board import BoardError, ref
from ora.coordination.traces import TraceContext, TraceJournal
from ora.mission.context import ContextError
from ora.mission.source_context import save_source_inventory, source_inventory_page
from ora.state import StateError
from ora.state.snapshot_storage import read_snapshot
from ora.state.source_lineage import inherited_public_source
from ora.state.store import canonical
from tests.mission.test_service import MODEL, Response, drain, service, tool

URL = "https://example.org/synthetic-followup-guide"
HTML = (b"<html><title>Synthetic guide</title><main><h1>Synthetic guide</h1>"
        b"<p>One checked sentence supports an offline report.</p></main></html>")


def _user_value(packet: dict, key: str) -> dict:
    for message in reversed(packet["messages"]):
        if message.get("role") != "user":
            continue
        try:
            value = json.loads(message["content"])
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and key in value:
            return value[key]
    raise AssertionError(f"{key} is absent from the actual model request")


def _followup(host, parent, request_id: str):
    view = host.state(parent.run_id).snapshot
    assert view.mission.status == "completed" and view.artifacts
    artifact = view.artifacts[0]
    return host.followup(LiveFollowupRequest(
        request_id=request_id, parent_run_id=parent.run_id,
        parent_snapshot_id=view.snapshot_id, target_id=artifact.id,
        target_version=artifact.version,
        question="Recheck the accepted result against its exact saved source.",
        agent_ids=["agent-synthesis"],
    ))


def test_checked_public_document_survives_two_accepted_followups(
    tmp_path, monkeypatch,
):
    import ora.mission.service as service_module

    requests = []
    fetched = []
    selected = {}
    host = None

    def fetch(url, **_kwargs):
        assert url == URL
        fetched.append(url)
        return {
            "status": 200, "content": HTML,
            "content_sha256": hashlib.sha256(HTML).hexdigest(),
            "byte_length": len(HTML), "content_type": "text/html",
            "requested_url": url, "final_url": url, "redirect_chain": [],
            "access_basis": "public_https_unauthenticated", "authenticated": False,
            "retrieved_at": "2026-10-13T00:00:00Z",
        }

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        requests.append(packet)
        turn = len(requests)
        if turn == 1:
            return tool("request_capability", {"tool_id": "browser_search"}, turn)
        if turn == 2:
            return Response({
                "id": "synthetic-browser", "model": MODEL,
                "choices": [{"message": {"role": "assistant",
                                         "content": f"Uninspected lead: {URL}"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6,
                          "total_tokens": 11},
            })
        if turn == 3:
            hit_id = _user_value(packet, "browser_search")["hits"][0]["hit_id"]
            return tool("fetch_source", {"hit_id": hit_id}, turn)
        if turn in {4, 6, 8}:
            item = _user_value(packet, "inventory")["items"][0]
            selected["copy_ref"] = item["ref"]
            assert item["source_url"] == URL
            assert item["model_input_status"] == "permitted"
            assert item["fetch"]["tool"] == "read_source_window"
            return tool("read_source_window", {
                "source_id": item["ref"]["id"],
                "source_version": item["ref"]["version"],
                "block_offset": 1, "char_offset": 0, "char_limit": 200,
            }, turn)
        assert turn in {5, 7, 9}
        visible = json.dumps(packet["messages"])
        assert "One checked sentence supports an offline report." in visible
        windows = host.store.records("source_read_window")
        passage = host.store.get(**windows[0]["body"]["passage_refs"][0])
        selected["passage_ref"] = ref(passage)
        return tool("publish_result", {
            "title": f"Synthetic accepted report {turn}",
            "text": "The exact saved page supports this offline sentence.",
            "citations": [{"source_id": selected["copy_ref"]["id"],
                           "span_id": passage["body"]["span_id"]}],
            "limitations": ["Synthetic source used only for an offline test"],
        }, turn)

    monkeypatch.setattr(service_module, "fetch_public_source", fetch)
    host = service(tmp_path, monkeypatch, transport)
    try:
        parent = host.start(LiveStartRequest(
            request_id="public-parent", question="Read the synthetic web guide.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, parent.run_id, limit=40)
        child = _followup(host, parent, "public-child")
        drain(host, child.run_id, limit=40)
        grandchild = _followup(host, child, "public-grandchild")
        drain(host, grandchild.run_id, limit=40)
        assert len(requests) == 9 and fetched == [URL]
        assert len(host.store.records("source_copy")) == 1
        assert len(host.store.records("mission_source_inventory")) == 3
        for run in (child, grandchild):
            view = host.state(run.run_id).snapshot
            assert view.mission.status == "completed"
            assert view.sources[0].url == URL
            assert view.sources[0].content_hash == hashlib.sha256(HTML).hexdigest()
            assert view.artifacts[0].citations[0].source_id == selected["copy_ref"]["id"]
            control = host._control(run.run_id, "agent-synthesis")
            assert inherited_public_source(
                host.store, mission_id=host._run(run.run_id)["body"]["mission_id"],
                run_id=run.run_id, copy_ref=selected["copy_ref"],
                actor_id=control["body"]["actor_id"],
                task_id=control["body"]["task_id"], control=control,
            )
            read_turns = tuple(
                ref(item) for item in host.store.records("model_response_receipt")
                if item["body"].get("mission_id")
                == host._run(run.run_id)["body"]["mission_id"]
                and item["body"].get("status") == "tool_requested"
                and host.store.get("context_tool_result", f"tool-result:{item['id']}")
                is not None
            )
            feedback = host.context._source_fit_feedback(
                control, read_turns, "exact_source_text_exceeds_allowance",
            )
            assert feedback["copy_ref"] == selected["copy_ref"]
            assert feedback["saved_read_succeeded"] is True
            assert feedback["source_text_in_this_call"] is False

        child_control = host._control(child.run_id, "agent-synthesis")
        source_free_call = next(
            item for item in host.store.records("agent_call")
            if item["body"].get("mission_id") == child_control["body"]["mission_id"]
            and not item["body"].get("input_refs")
        )
        trace_context = TraceContext(
            mission_id=child_control["body"]["mission_id"],
            run_id=child.run_id, task_id=child_control["body"]["task_id"],
            attempt_id=source_free_call["body"]["attempt_id"],
            instance_id=child_control["body"]["actor_id"],
            operation_id="forged-source-consumption",
            call_id=source_free_call["id"],
            call_version=source_free_call["version"],
        )
        with pytest.raises(StateError, match="another mission"):
            host.store.transact(
                "reject-unpinned-trace-input", {},
                lambda tx: TraceJournal(host.store).append_tx(
                    tx, "reject-unpinned-trace-input", context=trace_context,
                    category="model", status="completed", action="Read source",
                    summary="Synthetic negative trace", inputs=[selected["copy_ref"]],
                ),
                mission_id=child_control["body"]["mission_id"],
            )

        child_run = host._run(child.run_id)
        mission_id = child_run["body"]["mission_id"]
        parent_snapshot = host.store.get(
            "research_snapshot", child_run["body"]["parent_snapshot_id"], 1
        )
        altered = deepcopy(parent_snapshot["body"])
        altered["inspection"]["sources"][0]["encounterReferences"] = []
        wrong_hash_view = read_snapshot(host.store, parent_snapshot)
        wrong_hash_view["sources"][0]["contentHash"] = "0" * 64
        wrong_hash_blob = host.store.put_blob(canonical(wrong_hash_view).encode())
        original_copy = host.store.get(**selected["copy_ref"])

        def save_forged(tx):
            fake_snapshot = tx.put("research_snapshot", "forged-source-view", altered)
            bad_hash_snapshot = tx.put("research_snapshot", "forged-source-hash", {
                **parent_snapshot["body"], "blob": wrong_hash_blob,
                "storage_format": None,
            })
            foreign_copy = tx.put("source_copy", "unaccepted-parent-copy", original_copy["body"])
            cases = {
                "wrong-target": {"target_ref": {"kind": "report", "id": "unaccepted", "version": 1}},
                "wrong-snapshot": {"parent_snapshot_id": fake_snapshot["id"]},
                "wrong-source-hash": {"parent_snapshot_id": bad_hash_snapshot["id"]},
                "source-not-inherited": {"source_refs": []},
                "wrong-parent": {"parent_run_id": child.run_id},
            }
            runs = {
                label: ref(tx.put("mission_run", f"forged-{label}", {
                    **child_run["body"], **changes, "run_id": f"forged-{label}",
                }))
                for label, changes in cases.items()
            }
            return {"runs": runs, "foreign_copy": ref(foreign_copy)}

        forged = host.store.transact(
            "forged-inheritance-cases", {}, save_forged, mission_id=mission_id,
        )
        for pointer in forged["runs"].values():
            assert not inherited_public_source(
                host.store, mission_id=mission_id, run_id=pointer["id"],
                copy_ref=selected["copy_ref"],
            )
        assert not inherited_public_source(
            host.store, mission_id=mission_id, run_id=child.run_id,
            copy_ref=forged["foreign_copy"],
        )

        use = host.store.resolve(
            "source_use.copy",
            f"{selected['copy_ref']['id']}:{selected['copy_ref']['version']}",
        )
        assert use["body"]["policy_status"] == "granted"

        def revoke(tx):
            return ref(tx.put("source_use", use["id"], {
                **use["body"], "policy_status": "revoked", "allowed_uses": [],
            }, expected_version=use["version"]))

        host.store.transact(
            "revoke-inherited-copy-after-preview", {}, revoke,
            mission_id=host._run(parent.run_id)["body"]["mission_id"],
        )
        with pytest.raises(ContextError, match="restricts this model input"):
            host.context.require_current_source_use([selected["copy_ref"]])
        grandchild_control = host._control(grandchild.run_id, "agent-synthesis")
        inventory = next(
            item for item in host.store.records("mission_source_inventory")
            if item["body"]["run_id"] == grandchild.run_id
        )
        page = source_inventory_page(
            host.store, inventory_ref=ref(inventory),
            actor_id=grandchild_control["body"]["actor_id"],
            task_id=grandchild_control["body"]["task_id"],
        )
        assert page["items"][0]["model_input_status"] == "restricted"
        assert page["items"][0]["fetch"] is None
        assert "source_url" not in page["items"][0]
        revoked_feedback = host.context._source_fit_feedback(
            grandchild_control,
            tuple(
                ref(item) for item in host.store.records("model_response_receipt")
                if item["body"].get("mission_id")
                == host._run(grandchild.run_id)["body"]["mission_id"]
                and item["body"].get("status") == "tool_requested"
                and host.store.get("context_tool_result", f"tool-result:{item['id']}")
                is not None
            ),
            "exact_source_text_exceeds_allowance",
        )
        assert revoked_feedback["status"] == "saved_text_unavailable_under_current_source_policy"

        child_control = host._control(child.run_id, "agent-synthesis")

        def add_unaccepted_copy(tx):
            return ref(tx.put("mission_task_control", child_control["id"], {
                **child_control["body"],
                "source_refs": [selected["copy_ref"], forged["foreign_copy"]],
            }, expected_version=child_control["version"]))

        altered_control = host.store.transact(
            "add-unaccepted-parent-copy", {}, add_unaccepted_copy, mission_id=mission_id,
        )
        with pytest.raises(BoardError, match="another mission"):
            save_source_inventory(
                host.store, "reject-unaccepted-parent-copy",
                control_ref=altered_control,
            )
    finally:
        host.close()
