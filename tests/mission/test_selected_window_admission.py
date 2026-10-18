"""Offline admission of an exact checked source read under a crowded context."""

from __future__ import annotations

import hashlib
import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.coordination.mailbox import PeerMailbox
from ora.evidence import read_source_window
from ora.mission.source_context import plan_source_turn
from ora.state.store import canonical
from tests.mission.test_service import MODEL, service


def _saved_tool_request(host, control, *, response_id, tool_id, name, arguments, result,
                        completion_tokens=6):
    private = host.store.put_blob(canonical({
        "content": None,
        "tool_calls": [{"id": tool_id, "type": "function", "function": {
            "name": name, "arguments": canonical(arguments),
        }}],
    }).encode())
    call_ref = control["body"]["call_ref"]
    mission_id = control["body"]["mission_id"]

    def save(tx):
        return ref(tx.put("model_response_receipt", response_id, {
            "mission_id": mission_id, "task_id": control["body"]["task_id"],
            "call_ref": call_ref, "status": "tool_requested", "blob": private,
            "usage": {"prompt_tokens": 100, "completion_tokens": completion_tokens,
                      "total_tokens": 100 + completion_tokens},
        }))

    response = host.store.transact(f"fixture-response:{response_id}", {}, save,
                                   mission_id=mission_id)
    result_ref = host.context.save_tool_result(
        f"fixture-result:{response_id}", response_ref=response,
        tool_call_id=tool_id, name=name, content=canonical(result),
    )
    return response, result_ref


def _bind_source_free_pack(host, pack):
    config = host.store.get(**pack.configuration_ref)
    request = {"messages": [
        {"role": "developer", "content": config["body"]["instructions"]},
        *pack.messages,
    ]}
    encoded = canonical(request).encode()
    blob = host.store.put_blob(encoded)
    mission_id = config["body"]["mission_id"]
    intent_id = f"model-intent:{pack.call_ref['id']}:{pack.call_ref['version']}"

    def save(tx):
        return ref(tx.put("model_request_intent", intent_id, {
            "mission_id": mission_id, "call_ref": pack.call_ref,
            "configuration_ref": pack.configuration_ref, "source_refs": [],
            "context_source_dependency_refs": list(
                host.context.context_source_dependency_refs(pack)
            ),
            "request_sha256": hashlib.sha256(encoded).hexdigest(), "blob": blob,
        }))

    intent_ref = host.store.transact("synthetic-report-intent", {}, save,
                                     mission_id=mission_id)
    return host.context.save_inclusion("synthetic-report-inclusion", pack, intent_ref)


def _ready_selected_window(
    tmp_path, monkeypatch, *, window_chars=2000, completion_tokens=6, crowded=True,
):
    def unexpected_provider_call(_request, *, timeout):
        raise AssertionError("Offline preparation must not call the model provider")

    host = service(tmp_path, monkeypatch, unexpected_provider_call)
    run = host.start(LiveStartRequest(
        request_id="crowded-selected-window", question="Review the supplied abstract",
        scope=("Evaluate the synthetic result within this exact question. " * 10
               if crowded else ""),
        evidence_text="DOI: 10.1234/synthetic. SELECTED_MARKER " + "A" * 4000,
        agent_ids=["agent-synthesis", "agent-critique"] if crowded else ["agent-synthesis"],
    ))
    actor = "agent-synthesis"
    control = host._control(run.run_id, actor)
    assert host._schedule_model(control)
    control = host._control(run.run_id, actor)
    copy_ref = control["body"]["source_refs"][0]
    original_call = control["body"]["call_ref"]
    mission_id = control["body"]["mission_id"]

    def add_parent(tx):
        parent = tx.put("mission_run", "synthetic-parent-with-long-identity", {
            "mission_id": "synthetic-parent-mission", "question": "Earlier question. " * 45,
            "scope": "Earlier scope. " * 55, "source_refs": [],
        })
        report = tx.put("report", "synthetic-parent-report-with-long-identity", {
            "mission_id": "synthetic-parent-mission", "title": "Earlier synthesis",
            "body": "Earlier bounded finding. " * 90,
            "citations": [], "limitations": [],
        })
        snapshot = tx.put("research_snapshot", "synthetic-parent-snapshot", {
            "mission_id": "synthetic-parent-mission",
            "inspection": {"acceptedOutputReferences": [ref(report)]},
        })
        current = tx.get("mission_run", run.run_id)
        changed = tx.put("mission_run", current["id"], {
            **current["body"], "parent_run_id": parent["id"],
            "parent_snapshot_id": snapshot["id"], "target_ref": ref(report),
        }, expected_version=current["version"])
        return ref(changed)

    host.store.transact("synthetic-parent-for-window", {}, add_parent,
                        mission_id=mission_id)
    if crowded:
        recipient = host._control(run.run_id, "agent-critique")["body"]["actor_id"]
        task = host.store.get("board_proposal", control["body"]["task_id"])
        for number in range(2):
            PeerMailbox(host.store).send_request(
                f"synthetic-peer-request-{number}", mission_id=mission_id,
                request_id=f"synthetic-request-with-long-identity-{number}",
                message_id=f"synthetic-message-with-long-identity-{number}",
                requester_id=control["body"]["actor_id"], recipient_id=recipient,
                question=f"Independently assess synthetic point {number}",
                expected_artifact="A scoped review", cause_ref=ref(task),
                payload={}, task_ref=ref(task),
            )

    history = []
    for number in range(12 if crowded else 0):
        response, _ = _saved_tool_request(
            host, control, response_id=f"synthetic-archived-response-{number}",
            tool_id=f"synthetic-archive-tool-{number}", name="read_source_inventory",
            arguments={"cursor": number}, result={"items": [], "next_cursor": None},
        )
        history.append(response)

    window = read_source_window(
        host.evidence, "synthetic-selected-read", copy_ref,
        block_offset=0, block_limit=1, char_offset=0, char_limit=window_chars,
    )
    window.update(
        requested_char_limit=window_chars, effective_char_limit=window_chars,
        returned_chars=sum(len(item["text_preview"]) for item in window["passages"]),
        remaining_in_block_chars=next(
            (item["characters"] for item in window["omissions"]
             if item["reason"] == "remaining_characters_in_block"), 0,
        ),
        next_request={"source_id": copy_ref["id"], "source_version": copy_ref["version"],
                      **window["next_cursor"], "char_limit": window_chars,
                      "selection_mode": "replace"},
    )
    window["passages"] = [
        {**item, "span_id": host.store.get(**item["passage_ref"])["body"]["span_id"]}
        for item in window["passages"]
    ]
    window["model_selection"] = {
        "mode": "replace", "passage_refs": window["passage_refs"],
        "prior_passage_refs": control["body"]["selected_passage_refs"].get(copy_ref["id"], []),
    }
    response, result_ref = _saved_tool_request(
        host, control,
        response_id=f"model-response:model-intent:{original_call['id']}:{original_call['version']}",
        tool_id="synthetic-selected-window-tool", name="read_source_window",
        arguments={"source_id": copy_ref["id"], "source_version": copy_ref["version"],
                   "block_offset": 0, "char_offset": 0, "char_limit": window_chars,
                   "selection_mode": "replace"}, result=window,
        completion_tokens=completion_tokens,
    )
    host._update_control(
        control, phase="ready_model", tool_turn_refs=[*history, response],
        selected_passage_refs={copy_ref["id"]: window["passage_refs"]},
        read_window_refs=[window["window_ref"]],
    )
    return host, run, actor, copy_ref, window, response, result_ref


def test_selected_window_archives_exact_duplicate_when_full_request_needs_space(
    tmp_path, monkeypatch
):
    host, run, actor, copy_ref, window, response, result_ref = _ready_selected_window(
        tmp_path, monkeypatch
    )
    try:
        control = host._control(run.run_id, actor)
        proposal = host.store.get("board_proposal", control["body"]["task_id"])
        candidates = []
        # Catalog descriptions can shift the exact fit; retain an 8k search bound.
        for target in range(6200, 8001, 50):
            plan = plan_source_turn(host.store, ref(control),
                                    source_input_allowance_tokens=target)
            if plan["mode"] == "source_free_recovery":
                continue
            fit = host._preview_soft_source_plan(
                control, proposal, plan, total_target=target,
                sequence=control["body"]["sequence"] + 1,
            )
            if fit and fit["archive_selected_window"]:
                candidates.append((target, fit))
                break
        assert candidates, "Crowded synthetic context must exercise the archived-pair fallback"
        target, preview = candidates[0]
        assert preview["estimated_request_tokens"] <= target
        prior_intent = host.store.get(
            "model_request_intent",
            f"model-intent:{control['body']['call_ref']['id']}:1",
        )
        prior_hash = prior_intent["body"]["request_sha256"]
        host._update_control(
            control, input_budget={"observed_limit_tokens": target + 6,
                                   "target_fraction": .70},
        )
        assert host._schedule_model(host._control(run.run_id, actor))
        prepared = host._control(run.run_id, actor)
        call = host.store.get(**prepared["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        )
        request = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        assert budget["source_turn_mode"] == "all_allowed_sources_fit"
        assert budget["preview_admission_target_tokens"] == target
        assert budget["exact_complete_request_estimate_tokens"] <= target
        assert budget["observed_account_limit"] == target + 6
        assert budget["exact_complete_request_estimate_tokens"] + 6 <= (
            budget["observed_account_limit"]
        )
        assert budget["output_reserve"] == call["body"]["settings"]["max_completion_tokens"]
        assert budget["current_tool_turn_ref"] is None
        assert response in budget["compacted_tool_turn_refs"]
        expected_tools = {
            "activate_skill", "board_read", "discover_capabilities", "peer_request",
            "publish_result", "read_context_page", "read_context_record",
            "read_source_inventory", "read_source_window", "release_skill",
            "request_capability", "review_peer_answer", "search_scholarly",
            "select_working_sources", "search_saved_passages",
        }
        assert len(call["body"]["exposed_tools"]) == len(expected_tools)
        assert set(call["body"]["exposed_tools"]) == expected_tools
        assert request["model"] == MODEL
        assert request["reasoning_effort"] == "high"
        assert len(request["tools"]) == len(expected_tools)
        assert {item["function"]["name"] for item in request["tools"]} == expected_tools
        index = json.loads(request["messages"][-1]["content"])
        source_packet = next(
            json.loads(message["content"])["source_packets"][0]
            for message in request["messages"] if message["role"] == "user"
            and '"source_packets"' in message["content"]
        )
        selected_text = host.store.get(**window["passage_refs"][0])["body"]["text"]
        assert source_packet["text"].endswith(selected_text)
        assert source_packet["text"].count(selected_text) == 1
        assert "SELECTED_MARKER" in source_packet["text"]
        assert intent["body"]["source_decisions"][0]["passage_refs"] == (
            window["passage_refs"]
        )
        assert index["run_scope"]["scope_preview"] == host._run(run.run_id)["body"]["scope"]
        assert len(index["request_dispositions"]) == 2
        assert all(item["status"] == "unanswered" for item in index["request_dispositions"])
        assert index["followup_target"]["target_body_preview"]
        inclusion = host.store.get("context_inclusion", f"context:{intent['id']}")
        assert inclusion["body"]["intent_ref"] == ref(intent)
        assert inclusion["body"]["source_refs"] == [copy_ref]
        assert response in [item["ref"] for item in inclusion["body"]["omitted"]]
        cursor = 0
        seen = []
        while True:
            page = host.context.read_omitted_page(
                mission_id=control["body"]["mission_id"],
                actor_id=control["body"]["actor_id"],
                task_id=control["body"]["task_id"],
                inclusion_ref=ref(inclusion), cursor=cursor, limit=100,
            )
            seen.extend(item["ref"] for item in page["items"])
            if page["next_cursor"] is None:
                break
            cursor = page["next_cursor"]
        assert response in seen
        assert host.store.get("model_request_intent", prior_intent["id"])["body"][
            "request_sha256"
        ] == prior_hash
    finally:
        host.close()


@pytest.mark.parametrize("boundary", ["unselected", "revoked"])
def test_selected_window_fallback_rejects_unselected_or_restricted_text(
    tmp_path, monkeypatch, boundary
):
    host, run, actor, copy_ref, window, response, _result_ref = _ready_selected_window(
        tmp_path, monkeypatch
    )
    try:
        control = host._control(run.run_id, actor)
        if boundary == "unselected":
            host._update_control(control, selected_passage_refs={copy_ref["id"]: []})
        else:
            use_ref = host.context._source_status(host.store.get(**copy_ref))[
                "current_source_use_ref"
            ]

            def revoke(tx):
                prior = tx.get(**use_ref)
                return ref(tx.put(prior["kind"], prior["id"], {
                    **prior["body"], "allowed_uses": [],
                    "policy_status": "requires_source_policy_resolution",
                }, expected_version=prior["version"]))

            host.store.transact("synthetic-window-revoke", {}, revoke,
                                mission_id=control["body"]["mission_id"])
        control = host._control(run.run_id, actor)
        assert not host.context.checked_window_is_selected_input(
            response, call=host.store.get(**control["body"]["call_ref"]),
            control=control, selected_source_refs=(copy_ref,),
        )
        host._update_control(
            control, input_budget={"observed_limit_tokens": (
                10006 if boundary == "revoked" else 7506
            ),
                                   "target_fraction": .70},
        )
        assert host._schedule_model(host._control(run.run_id, actor))
        prepared = host._control(run.run_id, actor)
        call = host.store.get(**prepared["body"]["call_ref"])
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        )
        if boundary == "unselected":
            assert budget["source_turn_mode"] == "source_free_recovery"
        else:
            assert budget["source_exclusion_refs"]
        assert budget["current_tool_turn_ref"] != response
        assert intent["body"]["source_decisions"] == []
        assert response in budget["compacted_tool_turn_refs"]
        assert window["passage_refs"][0] not in [
            pointer for decision in intent["body"]["source_decisions"]
            for pointer in decision.get("passage_refs", [])
        ]
    finally:
        host.close()


def test_current_report_read_is_not_archived_as_a_selected_window(
    tmp_path, monkeypatch
):
    host, run, actor, copy_ref, _window, source_response, _result_ref = (
        _ready_selected_window(tmp_path, monkeypatch)
    )
    try:
        control = host._control(run.run_id, actor)
        current_call = host.store.get(**control["body"]["call_ref"])

        def save_source_free_call(tx):
            return ref(tx.put("agent_call", "synthetic-report-window-call", {
                **current_call["body"], "input_refs": [],
            }))

        report_call = host.store.transact(
            "synthetic-report-window-call", {}, save_source_free_call,
            mission_id=control["body"]["mission_id"],
        )
        task = host.store.get("board_proposal", control["body"]["task_id"])
        pack = host.context.assemble(report_call, ref(task), control_ref=ref(control))
        inclusion = _bind_source_free_pack(host, pack)
        report_ref = host._run(run.run_id)["body"]["target_ref"]
        report_window = host.context.read_omitted_record(
            mission_id=control["body"]["mission_id"], actor_id=control["body"]["actor_id"],
            task_id=task["id"], inclusion_ref=inclusion, pointer=report_ref,
            offset=0, limit=2000,
        )
        assert report_window["report_text_status"] == "available"
        report_response, _ = _saved_tool_request(
            host, control, response_id="synthetic-current-report-response",
            tool_id="synthetic-current-report-tool", name="read_context_record",
            arguments={"inclusion_id": inclusion["id"], **report_ref,
                       "offset": 0, "limit": 2000}, result=report_window,
        )
        host._update_control(control, tool_turn_refs=[
            *control["body"]["tool_turn_refs"], report_response,
        ])
        control = host._control(run.run_id, actor)
        plan = plan_source_turn(host.store, ref(control),
                                source_input_allowance_tokens=10000)
        preview = host._preview_soft_source_plan(
            control, task, plan, total_target=10000,
            sequence=control["body"]["sequence"] + 1,
        )
        assert preview is None or preview["archive_selected_window"] is False
        host._update_control(control, input_budget={
            "observed_limit_tokens": 10006, "target_fraction": .70,
        })
        assert host._schedule_model(host._control(run.run_id, actor))
        prepared = host._control(run.run_id, actor)
        call = host.store.get(**prepared["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        )
        request = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        assert any(message.get("role") == "tool" and message.get("content") == canonical(
            report_window
        ) for message in request["messages"])
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        assert budget["current_tool_turn_ref"] == report_response
        assert source_response in budget["compacted_tool_turn_refs"]
    finally:
        host.close()
