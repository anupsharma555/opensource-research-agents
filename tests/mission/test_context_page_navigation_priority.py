"""Offline context-page detours must not create new peer-work cues."""

from __future__ import annotations

import json

from ora.coordination.board import ProposalBoard, ref
from ora.mission.context import WorkingContext
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack, fixture


def _page_turn(store, context, call, task, inclusion, *, operation, cursor=0,
               rejected=False, unrelated_failed_tool=False):
    tool_call_id = f"{operation}-call"
    assistant = {"content": None, "tool_calls": [{
        "id": tool_call_id, "type": "function", "function": {
            "name": "read_context_page",
            "arguments": canonical({"inclusion_id": inclusion["id"], "cursor": cursor,
                                    "limit": 2}),
        },
    }]}
    blob = store.put_blob(canonical(assistant).encode())
    response = store.transact(operation, {}, lambda tx: ref(tx.put(
        "model_response_receipt", f"{operation}-response", {
            "mission_id": "m", "task_id": task["id"], "call_ref": call,
            "status": "tool_requested", "blob": blob,
        },
    )), mission_id="m")
    if rejected:
        content = canonical({"status": "rejected",
                             "reason": "Context inclusion is unavailable in this mission"})
    else:
        content = canonical(context.read_omitted_page(
            mission_id="m", actor_id="peer", task_id=task["id"],
            inclusion_ref=inclusion, cursor=cursor, limit=2,
        ))
    context.save_tool_result(
        f"{operation}-result", response_ref=response, tool_call_id=tool_call_id,
        name="read_context_page", content=content,
    )

    def outcome(tx, suffix, handler, outputs, payload=None):
        artifact = tx.put("executor_artifact", f"executor-artifact:{suffix}", {
            "mission_id": "m", "work_id": suffix, "actor_id": "peer",
            "handler_id": handler, "output_refs": outputs,
            **({"payload": payload} if payload is not None else {}),
        })
        receipt = tx.put("executor_receipt", f"executor-receipt:{suffix}", {
            "mission_id": "m", "work_id": suffix, "artifact_ref": ref(artifact),
        })
        trace = tx.put("coordination_trace", f"executor-trace:{suffix}", {
            "mission_id": "m", "instance_id": "peer",
            "proof": "recorded_local_callback", "output_refs": [ref(artifact)],
        })
        tx.put("board_outcome", f"executor-outcome:{suffix}", {
            "mission_id": "m", "actor_ref": ref(tx.get("agent_instance", "peer")),
            "proposal_ref": task,
            "result_refs": [ref(artifact), ref(receipt), ref(trace)],
            "completes_task": False,
        })

    def save_wrappers(tx):
        outcome(tx, f"{operation}-model", "mission-model", [response])
        payload = ({"name": "read_context_page", "tool_call_id": tool_call_id,
                    "result": json.loads(content)} if rejected else None)
        outcome(tx, f"{operation}-tool", "mission-tool",
                [] if rejected else [inclusion], payload)
        if unrelated_failed_tool:
            outcome(tx, f"{operation}-unrelated", "mission-tool", [], {
                "name": "read_source_window", "tool_call_id": "other-tool-call",
                "result": {"status": "rejected", "reason": "Different tool error"},
            })
        return {"response_ref": response}

    store.transact(f"{operation}-wrappers", {}, save_wrappers, mission_id="m")
    return response


def _view(pack):
    return json.loads(pack.messages[-1]["content"])


def _next_call(store, prior, name):
    body = store.get(**prior)["body"]
    return store.transact(name, {}, lambda tx: ref(tx.put(
        "agent_call", name, body,
    )), mission_id="m")


def _current_capability_turn(store, context, call, task):
    tool_call_id = "current-capability-call"
    assistant = {"content": None, "tool_calls": [{
        "id": tool_call_id, "type": "function", "function": {
            "name": "request_capability", "arguments": canonical({"tool_id": "peer_request"}),
        },
    }]}
    blob = store.put_blob(canonical(assistant).encode())
    response = store.transact("current-capability-response", {}, lambda tx: ref(tx.put(
        "model_response_receipt", "current-capability-response", {
            "mission_id": "m", "task_id": task["id"], "call_ref": call,
            "status": "tool_requested", "blob": blob,
        },
    )), mission_id="m")
    context.save_tool_result(
        "current-capability-result", response_ref=response, tool_call_id=tool_call_id,
        name="request_capability", content=canonical({"status": "selected_for_next_turn"}),
    )
    return response


def test_empty_page_detours_do_not_advertise_more_peer_work(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    context = WorkingContext(store)
    initial = context.assemble(call, task)
    assert "older_records" not in _view(initial)
    assert _view(initial)["omitted"] is None
    first_inclusion = bind_no_source_pack(store, context, initial, "initial-page-inclusion")

    first = _page_turn(store, context, call, task, first_inclusion, operation="empty-page")
    second_call = _next_call(store, call, "second-call")
    after_first = context.assemble(second_call, task, archived_tool_turn_refs=[first])
    first_view = _view(after_first)
    assert first_view["record_counts"]["shared_updates"] == 0
    assert first_view["record_counts"]["completed_tool_turns"] == 0
    assert first_view["omitted"] is None
    assert "older_records" not in first_view
    assert len(after_first.omitted) == 3  # Exact durable outcomes and response remain pageable.
    second_inclusion = bind_no_source_pack(store, context, after_first, "second-page-inclusion")

    second = _page_turn(store, context, second_call, task, second_inclusion,
                        operation="routine-page")
    third_call = _next_call(store, call, "third-call")
    after_second = context.assemble(third_call, task, archived_tool_turn_refs=[first, second])
    second_view = _view(after_second)
    assert second_view["omitted"] is None
    assert "older_records" not in second_view
    assert second_view["record_counts"]["shared_updates"] == 0
    assert len(after_second.omitted) == 6

    dissent = ProposalBoard(store).dissent(
        "real-critique", mission_id="m", dissent_id="real-critique",
        proposal_ref=task, actor_id="sender", critique="Synthetic counterevidence",
    )
    with_peer_work = context.assemble(third_call, task, archived_tool_turn_refs=[first, second])
    assert dissent["dissent_ref"] in [item["ref"] for item in _view(with_peer_work)["details"]]


def test_rejected_page_has_current_error_without_new_page_cue(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    context = WorkingContext(store)
    initial = context.assemble(call, task)
    inclusion = bind_no_source_pack(store, context, initial, "error-page-inclusion")
    rejected = _page_turn(store, context, call, task, inclusion,
                          operation="bad-page", rejected=True)
    second_call = _next_call(store, call, "error-second-call")
    continued = context.assemble(second_call, task, archived_tool_turn_refs=[rejected])
    view = _view(continued)
    assert view["current_tool_feedback"]["status"] == "rejected"
    assert "unavailable" in view["current_tool_feedback"]["reason"]
    assert view["omitted"] is None
    assert "older_records" not in view
    assert len(continued.omitted) == 3
    second_inclusion = bind_no_source_pack(store, context, continued,
                                           "error-page-continuation")
    corrected = _page_turn(store, context, second_call, task, second_inclusion,
                           operation="corrected-page")
    third_call = _next_call(store, call, "error-third-call")
    after_correction = _view(context.assemble(
        third_call, task, archived_tool_turn_refs=[rejected, corrected],
    ))
    assert "current_tool_feedback" not in after_correction
    assert "older_records" not in after_correction


def test_unrelated_failed_tool_wrapper_is_not_hidden_by_page_history(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    context = WorkingContext(store)
    initial = context.assemble(call, task)
    inclusion = bind_no_source_pack(store, context, initial, "mixed-page-inclusion")
    page = _page_turn(store, context, call, task, inclusion,
                      operation="mixed-page", unrelated_failed_tool=True)
    next_call = _next_call(store, call, "mixed-second-call")
    continued = context.assemble(next_call, task, archived_tool_turn_refs=[page])
    view = _view(continued)
    assert view["record_counts"]["shared_updates"] == 1
    assert view["omitted"]["count"] == 1
    assert view["older_records"]["tool"] == "read_context_page"


def test_newer_current_tool_result_supersedes_old_page_error(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    context = WorkingContext(store)
    initial = context.assemble(call, task)
    inclusion = bind_no_source_pack(store, context, initial, "old-error-inclusion")
    rejected = _page_turn(store, context, call, task, inclusion,
                          operation="old-error", rejected=True)
    second_call = _next_call(store, call, "current-tool-call")
    current = _current_capability_turn(store, context, second_call, task)
    third_call = _next_call(store, call, "after-current-tool-call")
    continued = context.assemble(
        third_call, task, archived_tool_turn_refs=[rejected], tool_turn_refs=[current],
    )
    assert "current_tool_feedback" not in _view(continued)
    assert any(message.get("name") == "request_capability" and message["role"] == "tool"
               for message in continued.messages)
