"""Exact parent-report reader navigation survives compact context."""

import json

import pytest

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack
from tests.mission.test_context_metadata_compaction import _source_free_case
from tests.mission.test_service import answer, drain, service, tool


def test_direct_report_read_cues_prior_output_without_source_spans(tmp_path, monkeypatch):
    packets = []
    parent_body = "Synthetic comparison only; design and source validity are untested."

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        number = len(packets)
        if number == 1:
            return answer(parent_body, number)
        if number == 2:
            target = json.loads(packet["messages"][-1]["content"])["followup_target"]
            reader = target["fetch_exact_parent_target"]
            assert reader["tool"] == "read_context_record"
            return tool("read_context_record", {
                key: reader[key] for key in (
                    "inclusion_id", "kind", "id", "version", "offset", "limit"
                )
            }, number)
        assert number == 3
        result = json.loads(next(message["content"] for message in packet["messages"]
                                 if message.get("name") == "read_context_record"))
        assert result["report_text_status"] == "available"
        assert parent_body in result["text"]
        target = json.loads(packet["messages"][-1]["content"])["followup_target"]
        assert target["target_window_in_current_tool_result"] is True
        assert "prior agent output, not a source_copy" in target["report_use_boundary"]
        assert "read_source_window" in target["report_use_boundary"]
        assert "target_body_preview" not in target
        return answer("The synthetic report is prior analysis, not original evidence.", number)

    host = service(tmp_path, monkeypatch, transport)
    try:
        parent = host.start(LiveStartRequest(
            request_id="direct-report-parent", question="Summarize synthetic comparison",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, parent.run_id)
        snapshot = host.state(parent.run_id).snapshot
        artifact = snapshot.artifacts[0]
        child = host.followup(LiveFollowupRequest(
            request_id="direct-report-child", parent_run_id=parent.run_id,
            parent_snapshot_id=snapshot.snapshot_id,
            target_id=artifact.id, target_version=artifact.version,
            question="Explain limits of that synthetic comparison",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, child.run_id)
        assert host._run(child.run_id)["body"]["status"] == "completed"
        assert len(packets) == 3
    finally:
        host.close()


@pytest.mark.parametrize("variant", ["partial", "final_page", "source_linked", "complete"])
def test_report_origin_cue_needs_complete_source_free_lineage(tmp_path, variant):
    store, context, task, copy_ref, _window, refs, _result_ref, _call, _inventory, _history = (
        _source_free_case(tmp_path)
    )
    report_text = ("Synthetic report text. " * 160 if variant in {"partial", "final_page"}
                   else "Short synthetic source-linked report.")

    def save_parent(tx):
        parent = tx.put("mission_run", "cue-parent", {
            "mission_id": "cue-parent-m", "question": "Earlier synthetic question?",
            "scope": "Synthetic scope", "source_refs": [],
        })
        report = tx.put("report", "cue-report", {
            "mission_id": "cue-parent-m", "title": "Synthetic parent report",
            "body": report_text, "citations": [], "limitations": [],
        })
        snapshot = tx.put("research_snapshot", "cue-snapshot", {
            "mission_id": "cue-parent-m", "inspection": {
                "acceptedOutputReferences": [ref(report)],
                "sources": ([{"copyReference": copy_ref}]
                            if variant == "source_linked" else []),
            },
        })
        run = tx.get("mission_run", "window-run")
        tx.put("mission_run", run["id"], {
            **run["body"], "parent_run_id": parent["id"],
            "parent_snapshot_id": snapshot["id"], "target_ref": ref(report),
        }, expected_version=run["version"])
        return ref(report)

    report_ref = store.transact("cue-parent-save", {}, save_parent, mission_id="m")
    first = context.assemble(
        refs["source_free_call"], task, control_ref=refs["control"],
        allowance=30000, estimate_tokens=len,
    )
    inclusion = bind_no_source_pack(store, context, first, "cue-parent-inclusion")
    first = context.read_omitted_record(
        mission_id="m", actor_id="peer", task_id=task["id"],
        inclusion_ref=inclusion, pointer=report_ref, offset=0, limit=900,
    )
    read_offset = first["total_chars"] - 400 if variant == "final_page" else 0
    result = (context.read_omitted_record(
        mission_id="m", actor_id="peer", task_id=task["id"],
        inclusion_ref=inclusion, pointer=report_ref, offset=read_offset, limit=900,
    ) if read_offset else first)
    assert result["report_text_status"] == "available"
    assert (result["next_offset"] is not None) is (variant == "partial")
    arguments = {"inclusion_id": inclusion["id"], **report_ref,
                 "offset": read_offset, "limit": 900}
    blob = store.put_blob(canonical({"content": None, "tool_calls": [{
        "id": "cue-read", "type": "function", "function": {
            "name": "read_context_record", "arguments": canonical(arguments),
        },
    }]}).encode())
    response = store.transact("cue-response", {}, lambda tx: ref(tx.put(
        "model_response_receipt", "cue-response", {
            "mission_id": "m", "task_id": task["id"],
            "call_ref": refs["source_free_call"], "status": "tool_requested",
            "blob": blob,
        },
    )), mission_id="m")
    context.save_tool_result(
        "cue-result", response_ref=response, tool_call_id="cue-read",
        name="read_context_record", content=canonical(result),
    )
    next_call = store.transact("cue-next-call", {}, lambda tx: ref(tx.put(
        "agent_call", "cue-next-call", tx.get(**refs["source_free_call"])["body"],
    )), mission_id="m")
    next_pack = context.assemble(
        next_call, task, control_ref=refs["control"],
        tool_turn_refs=[response],
        allowance=3650 if variant == "complete" else 30000,
        estimate_tokens=len,
    )
    target = json.loads(next_pack.messages[-1]["content"])["followup_target"]
    assert target["target_ref"] == report_ref
    if variant == "complete":
        assert next_pack.estimated_tokens <= 3650
        assert target["target_window_in_current_tool_result"] is True
        assert "report_use_boundary" in target
        delivered = [json.loads(message["content"]) for message in next_pack.messages
                     if message.get("name") == "read_context_record"]
        assert len(delivered) == 1
        assert delivered[0]["text"] == result["text"]
        assert delivered[0]["offset"] == 0
        assert delivered[0]["next_offset"] is None
        return
    assert "report_use_boundary" not in target
    if variant == "source_linked":
        assert target["source_dependency_refs"] == [copy_ref]
    else:
        assert target["source_dependency_refs"] == []


def test_tight_followup_keeps_exact_report_reader(tmp_path):
    store, context, task, _copy_ref, _window, refs, _result_ref, call, _inventory, history = (
        _source_free_case(tmp_path)
    )

    def add_parent(tx):
        parent = tx.put("mission_run", "earlier-run", {
            "mission_id": "earlier-m", "question": "Earlier question?",
            "scope": "Earlier scope. " * 85, "source_refs": [],
        })
        report = tx.put("report", "earlier-report", {
            "mission_id": "earlier-m", "title": "Earlier answer",
            "body": "Earlier synthetic finding. " * 140,
            "citations": [], "limitations": [],
        })
        snapshot = tx.put("research_snapshot", "earlier-snapshot", {
            "mission_id": "earlier-m",
            "inspection": {"acceptedOutputReferences": [ref(report)]},
        })
        run = tx.get("mission_run", "window-run")
        tx.put("mission_run", run["id"], {
            **run["body"], "scope": "Review synthetic comparison",
            "parent_run_id": parent["id"], "parent_snapshot_id": snapshot["id"],
            "target_ref": ref(report),
        }, expected_version=run["version"])
        return ref(report)

    report_ref = store.transact("synthetic-parent", {}, add_parent, mission_id="m")
    pack = context.assemble(
        call, task, control_ref=refs["control"], history=history,
        archived_tool_turn_refs=[refs["response"]],
        allowance=3200, estimate_tokens=len,
    )
    index = json.loads(pack.messages[-1]["content"])
    target = index["followup_target"]
    assert pack.estimated_tokens <= 3200
    assert target["target_ref"] == report_ref
    assert target["target_text_status"] == "available"
    reader = target["fetch_exact_parent_target"]
    assert reader["tool"] == "read_context_record"
    assert {key: reader[key] for key in ("kind", "id", "version")} == report_ref
    inclusion_ref = bind_no_source_pack(store, context, pack, "synthetic-parent-reader")
    assert reader["inclusion_id"] == inclusion_ref["id"]
    result = context.read_omitted_record(
        mission_id="m", actor_id="peer", task_id=task["id"],
        inclusion_ref=inclusion_ref, pointer=report_ref,
        offset=reader["offset"], limit=reader["limit"],
    )
    assert result["report_text_status"] == "available"
    assert "Earlier synthetic finding" in result["text"]


@pytest.mark.parametrize("exact_target", [True, False])
def test_report_id_as_source_returns_exact_current_reader(
    tmp_path, monkeypatch, exact_target,
):
    packets = []
    parent_body = "Synthetic parent result. This is a saved report, not a source copy."
    target = None
    reader = None

    def transport(request, *, timeout):
        nonlocal reader
        packet = json.loads(request.data)
        packets.append(packet)
        number = len(packets)
        if number == 1:
            return answer(parent_body, number)
        if number == 2:
            return tool("request_capability", {"tool_id": "read_source_window"}, number)
        if number == 3:
            return tool("read_source_window", {
                "source_id": target.id if exact_target else "report-unrelated",
                "source_version": target.version,
                "block_offset": 0, "char_offset": 0, "char_limit": 1000,
            }, number)
        if number == 4:
            result = json.loads(next(message["content"] for message in packet["messages"]
                                     if message.get("name") == "read_source_window"))
            assert result["status"] == "rejected"
            if not exact_target:
                assert result["condition"] == "not an inspected current task input"
                assert "read_parent_report" not in result
                return answer("Unrelated report ID was not read.", number)
            assert result["condition"] == (
                "this ID names the accepted parent report, not a source copy"
            )
            reader = result["read_parent_report"]
            assert reader["tool"] == "read_context_record"
            assert {key: reader["arguments"][key] for key in ("kind", "id", "version")} == {
                "kind": "report", "id": target.id, "version": target.version,
            }
            assert "read_context_record" in {
                item["function"]["name"] for item in packet["tools"]
                if item.get("type") == "function"
            }
            return tool("read_context_record", reader["arguments"], number)
        assert number == 5
        result = json.loads(next(message["content"] for message in packet["messages"]
                                 if message.get("name") == "read_context_record"))
        assert result["report_text_status"] == "available"
        assert parent_body in result["text"]
        index = json.loads(packet["messages"][-1]["content"])
        target_view = index["followup_target"]
        assert target_view["target_window_in_current_tool_result"] is True
        assert "prior agent output, not a source_copy" in target_view["report_use_boundary"]
        assert "citations: []" in target_view["report_use_boundary"]
        assert "target_body_preview" not in target_view
        return answer("Synthetic follow-up uses the saved report only.", number)

    host = service(tmp_path, monkeypatch, transport)
    try:
        parent = host.start(LiveStartRequest(
            request_id="reader-parent", question="Summarize the synthetic result",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, parent.run_id)
        snapshot = host.state(parent.run_id).snapshot
        target = snapshot.artifacts[0]
        child = host.followup(LiveFollowupRequest(
            request_id="reader-child", parent_run_id=parent.run_id,
            parent_snapshot_id=snapshot.snapshot_id,
            target_id=target.id, target_version=target.version,
            question="Describe the uncertainty of the saved synthetic result",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, child.run_id)
        assert host._run(child.run_id)["body"]["status"] == "completed"
        assert len(packets) == (5 if exact_target else 4)
        assert (reader is not None) is exact_target
    finally:
        host.close()
