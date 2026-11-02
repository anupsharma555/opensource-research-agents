"""Tool activity separates model selection, host execution, and later delivery."""

from __future__ import annotations

import hashlib

import pytest

from ora.observability.evaluation import record_evaluation_observation
from ora.observability.tool_activity import build_tool_activity
from ora.state import StateStore
from ora.state.store import canonical


def _ref(row):
    return {key: row[key] for key in ("kind", "id", "version")}


def _put(store, kind, identifier, body, *, version=None):
    return store.transact(
        f"seed:{kind}:{identifier}:{version or 0}", {},
        lambda tx: tx.put(kind, identifier, body, expected_version=version or 0),
    )


def _blob(store, value):
    return store.put_blob(canonical(value).encode())


def _seed(store, *, run_id="run-a", mission_id="mission-a", status="active"):
    _put(store, "mission_run", run_id, {"mission_id": mission_id, "status": status})
    _put(store, "agent_instance", "actor-a", {
        "mission_id": mission_id, "run_id": run_id,
    })


def _turn(store, suffix, name, *, messages=None, tool_id=None,
          response_status="tool_requested", run_id="run-a", mission_id="mission-a",
          save_response=True, tool_args=None):
    tool_id = tool_id or f"fc_{suffix}"
    call = _put(store, "agent_call", f"call-{suffix}", {
        "mission_id": mission_id, "instance_id": "actor-a", "task_id": "task-a",
        "attempt_id": f"attempt-{suffix}",
    })
    request = {"messages": messages or [], "tools": [{"function": {"name": name}}]}
    request_blob = _blob(store, request)
    intent = _put(store, "model_request_intent", f"intent-{suffix}", {
        "mission_id": mission_id, "instance_id": "actor-a", "task_id": "task-a",
        "attempt_id": f"attempt-{suffix}", "call_ref": _ref(call),
        "blob": request_blob, "request_sha256": request_blob["sha256"],
    })
    _put(store, "model_dispatch_attempt", f"model-dispatch:{intent['id']}", {
        "mission_id": mission_id, "intent_ref": _ref(intent), "call_ref": _ref(call),
        "request_sha256": request_blob["sha256"],
        "status": "attempted_external_effect",
    })
    raw = {"tool_calls": [{"id": tool_id, "function": {
        "name": name, "arguments": canonical(tool_args if tool_args is not None else {}),
    }}]}
    response = (_put(store, "model_response_receipt", f"model-response:{intent['id']}", {
        "mission_id": mission_id, "task_id": "task-a", "attempt_id": f"attempt-{suffix}",
        "intent_ref": _ref(intent), "call_ref": _ref(call),
        "request_sha256": request_blob["sha256"], "status": response_status,
        "blob": _blob(store, raw),
    }) if save_response else None)
    return call, response, raw["tool_calls"][0]


def _result(store, response, tool, content, *, mission_id="mission-a",
            response_ref=None):
    message = {"role": "tool", "tool_call_id": tool["id"],
               "name": tool["function"]["name"], "content": canonical(content)}
    return _put(store, "context_tool_result", f"tool-result:{response['id']}", {
        "mission_id": mission_id,
        "response_ref": response_ref or _ref(response),
        "tool_call_id": tool["id"],
        "message_sha256": hashlib.sha256(canonical(message).encode()).hexdigest(),
        "blob": _blob(store, message),
    }), message


def _work(store, suffix, call, tool, *, report_ref=None, corrupt=False, payload=None):
    work_id = f"tool-work-{suffix}"
    payload = payload if payload is not None else (
        {"report_ref": report_ref} if report_ref else
        {"tool_call_id": "wrong" if corrupt else tool["id"],
         "name": tool["function"]["name"]})
    artifact = _put(store, "executor_artifact", f"artifact-{suffix}", {
        "mission_id": "mission-a", "actor_id": "actor-a", "work_id": work_id,
        "payload": payload,
    })
    capacity = _put(store, "capacity_operation", f"capacity-{suffix}", {
        "mission_id": "mission-a", "run_id": "run-a", "task_id": "task-a",
        "attempt_id": f"attempt-{suffix}", "call_id": call["id"],
        "phase": "completed", "attempts": [{"status": "completed", "at": 1000.0,
                                            "completed_at": 1000.25}],
    })
    receipt = _put(store, "executor_receipt", f"executor-receipt-{suffix}", {
        "mission_id": "mission-a", "work_id": work_id,
        "artifact_ref": _ref(artifact), "capacity_operation_id": capacity["id"],
        "timing": {"active_seconds": 0.2, "timing_basis": "store_clock"},
    })
    return _put(store, "executor_work", f"executor-work-{suffix}", {
        "mission_id": "mission-a", "phase": "completed", "result_ref": _ref(artifact),
        "receipt_ref": _ref(receipt),
        "spec": {"handler_id": "mission-tool", "kind": "compute",
                 "mission_id": "mission-a", "run_id": "run-a", "actor_id": "actor-a",
                 "proposal_ref": {"kind": "board_proposal", "id": "task-a", "version": 1},
                 "attempt_id": f"attempt-{suffix}", "call_id": call["id"],
                 "call_version": call["version"], "work_id": work_id},
    })


def _followup_messages(tool, message, *, projected=False):
    return [{"role": "assistant", "tool_calls": [tool]},
            {**message, "content": canonical({"status": "saved_result_compacted_for_model_input"})}
            if projected else message]


def test_selection_execution_exact_delivery_and_single_attempt_timing(tmp_path):
    store = StateStore(tmp_path)
    _seed(store)
    first_call, first_response, first_tool = _turn(store, "first", "request_capability")
    work = _work(store, "first", first_call, first_tool)
    _put(store, "mission_capability_selection", "selection-first", {
        "mission_id": "mission-a", "run_id": "run-a", "actor_id": "actor-a",
        "task_id": "task-a", "source_call_ref": _ref(first_call),
        "source_work_ref": _ref(work),
        "action": "request_capability", "tool_id": "save_plan",
    })
    _, message = _result(store, first_response, first_tool,
                         {"status": "selected_for_next_turn", "tool_id": "save_plan",
                          "already_attached": True})
    _turn(store, "second", "save_plan", messages=_followup_messages(first_tool, message))
    first = build_tool_activity(StateStore(tmp_path, read_only=True), "run-a")
    again = build_tool_activity(StateStore(tmp_path, read_only=True), "run-a")
    assert first == again
    assert first["counts"]["captured_model_loops"] == 2
    assert first["counts"]["selected_tools"] == 2
    step = first["steps"][0]
    assert step["tool_name"] == "request_capability"
    assert step["selected_capability_name"] == "save_plan"
    assert step["execution_status"] == "executed"
    assert step["outcome"] == "completed"
    assert step["reuse_status"] == "reused"
    assert step["reuse_basis"] == "capability_attachment_flag"
    assert step["duration_seconds"] == 0.2
    assert step["duration_basis"] == "executor_receipt_active_seconds_store_clock"
    assert step["interval_basis"] == "single_capacity_attempt_wall_interval"
    assert step["delivery_status"] == "captured_later"
    assert step["delivery_form"] == "exact_saved_message"
    assert step["next_model_loop_index"] == 2
    assert step["result_in_next_prepared_request"] is True
    assert step["result_in_next_captured_request"] is True
    assert step["next_delivery_status"] == "captured_with_exact_result"
    assert step["result_in_later_dispatched_request"] is True
    assert step["result_in_later_captured_request"] is True
    assert first["steps"][1]["execution_status"] == "not_executed"
    assert first["steps"][1]["duration_seconds"] is None
    assert "arguments" not in str(first) and "selected_for_next_turn" not in str(first)


@pytest.mark.parametrize(("name", "content", "expected", "basis"), [
    ("fetch_source", {"status": "inspected", "fetch_feedback": {
        "retained_document_reused": True, "new_http_request": False,
    }}, "reused", "public_document_http_fetch"),
    ("fetch_source", {"status": "inspected", "fetch_feedback": {
        "retained_document_reused": False, "new_http_request": True,
    }}, "new", "public_document_http_fetch"),
    ("fetch_source", {"status": "inspected", "fetch_feedback": {
        "retained_document_reused": True, "new_http_request": True,
    }}, "unknown", None),
    ("read_source_window", {"progress_feedback": {
        "scope": "this_task_prior_checked_read_windows",
        "exact_window_repeated": True, "new_checked_location_chars": 0,
    }}, "reused", "same_task_exact_read_window"),
    ("read_source_window", {"progress_feedback": {
        "scope": "this_task_prior_checked_read_windows",
        "exact_window_repeated": False, "new_checked_location_chars": 124,
    }}, "new", "same_task_new_checked_location"),
    ("read_source_window", {"progress_feedback": {
        "scope": "this_task_prior_checked_read_windows",
        "exact_window_repeated": False, "new_checked_location_chars": 0,
    }}, "unknown", None),
])
def test_reuse_classifies_only_explicit_supported_tool_feedback(
    tmp_path, name, content, expected, basis,
):
    store = StateStore(tmp_path)
    _seed(store)
    call, response, tool = _turn(store, "first", name)
    _work(store, "first", call, tool)
    _result(store, response, tool, content)
    step = build_tool_activity(store, "run-a")["steps"][0]
    assert step["reuse_status"] == expected
    assert step["reuse_basis"] == basis


def test_projected_result_pending_reuse_and_observation_stop(tmp_path):
    store = StateStore(tmp_path)
    _seed(store)
    call, response, tool = _turn(store, "first", "search_web")
    _work(store, "first", call, tool)
    _, message = _result(store, response, tool, {"status": "pending", "reuse_status": "reused"})
    _turn(store, "second", "read_search_results",
          messages=_followup_messages(tool, message, projected=True))
    report = build_tool_activity(store, "run-a")
    step = report["steps"][0]
    assert (step["execution_status"], step["outcome"], step["reuse_status"]) == (
        "executed", "pending", "reused",
    )
    assert step["delivery_status"] == "captured_later"
    assert step["delivery_form"] == "host_projected_or_restricted"
    assert step["next_delivery_status"] == "captured_with_projected_or_restricted_result"
    assert report["counts"]["reused_tools"] == 1

    # Another run with no returned model receipt is only prepared; its marker
    # does not prove HTTP or result delivery.
    pending_store = StateStore(tmp_path / "pending")
    _seed(pending_store)
    pending_call, pending_response, pending_tool = _turn(
        pending_store, "first", "search_web",
    )
    assert pending_response is not None
    _result(pending_store, pending_response, pending_tool,
            {"status": "pending", "reuse_status": "reused"})
    _turn(pending_store, "second", "read_search_results",
          messages=_followup_messages(pending_tool, message, projected=True),
          save_response=False)
    report = build_tool_activity(pending_store, "run-a")
    step = report["steps"][0]
    assert step["result_in_later_prepared_request"] is True
    assert step["result_in_later_dispatched_request"] is False
    assert step["delivery_status"] == "not_observed_yet"
    assert step["next_model_loop_index"] is None
    assert step["result_in_later_prepared_request"] is True
    ledger = {"schema": "ora.agent_validation_budget.v1",
              "model_attempts": [], "retrieval_attempts": []}
    record_evaluation_observation(pending_store, "run-a", ledger, "model_attempt_cap", 1.0)
    step = build_tool_activity(pending_store, "run-a")["steps"][0]
    assert step["delivery_status"] == "not_delivered_after_observation"
    assert step["delivery_final"] is True


def test_result_skipped_by_next_captured_request_can_arrive_later(tmp_path):
    store = StateStore(tmp_path)
    _seed(store)
    _, response, tool = _turn(store, "first", "save_plan")
    _, message = _result(store, response, tool, {"status": "saved"})
    _turn(store, "second", "calculate", messages=[{"role": "user", "content": "continue"}],
          response_status="complete")
    _turn(store, "third", "calculate", messages=_followup_messages(tool, message),
          response_status="complete")
    first = build_tool_activity(store, "run-a")["steps"][0]
    assert first["next_model_loop_index"] == 2
    assert first["result_in_next_captured_request"] is False
    assert first["next_delivery_status"] == "captured_without_result"
    assert first["result_in_later_captured_request"] is True
    assert first["delivery_status"] == "captured_later"
    assert first["delivery_form"] == "exact_saved_message"


def test_error_receipt_does_not_prove_model_saw_serialized_tool_result(tmp_path):
    store = StateStore(tmp_path)
    _seed(store)
    _, response, tool = _turn(store, "first", "save_plan")
    _, message = _result(store, response, tool, {"status": "saved"})
    _turn(store, "second", "calculate", messages=_followup_messages(tool, message),
          response_status="failed")
    first = build_tool_activity(store, "run-a")["steps"][0]
    assert first["next_model_loop_index"] == 2
    assert first["result_in_next_prepared_request"] is True
    assert first["next_delivery_status"] == "captured_request_not_confirmed_model_input"
    assert first["result_in_next_captured_request"] is False
    assert first["result_in_later_prepared_request"] is True
    assert first["result_in_later_captured_request"] is False
    assert first["delivery_status"] == "not_observed_yet"

    _turn(store, "third", "calculate", messages=_followup_messages(tool, message),
          response_status="complete")
    recovered = build_tool_activity(store, "run-a")["steps"][0]
    assert recovered["next_model_loop_index"] == 2
    assert recovered["result_in_next_captured_request"] is False
    assert recovered["result_in_later_captured_request"] is True
    assert recovered["delivery_status"] == "captured_later"


def test_direct_publish_accepted_without_tool_message_or_duration_inference(tmp_path):
    store = StateStore(tmp_path)
    _seed(store, status="completed")
    call, response, tool = _turn(store, "publish", "publish_result")
    attempt = _put(store, "agent_attempt", "attempt-publish", {
        "mission_id": "mission-a", "run_id": "run-a", "call_ref": _ref(call),
        "status": "running",
    })
    report = _put(store, "report", "report-a", {
        "mission_id": "mission-a", "run_id": "run-a",
        "producer_attempt_ref": _ref(attempt),
    })
    _put(store, "agent_attempt", attempt["id"], {
        **attempt["body"], "status": "completed", "report_ref": _ref(report),
    }, version=attempt["version"])
    _work(store, "publish", call, tool, report_ref=_ref(report))
    step = build_tool_activity(store, "run-a")["steps"][0]
    assert step["tool_name"] == "publish_result"
    assert step["execution_status"] == "accepted_report"
    assert step["outcome"] == "completed"
    assert step["duration_seconds"] == 0.2
    assert step["delivery_status"] == "no_result_message"
    assert step["next_delivery_status"] == "no_next_captured_request"
    assert step["delivery_final"] is True
    assert step["result_in_later_captured_request"] is False
    assert response["id"] == step["response_ref"]["id"]


def test_untrusted_name_and_foreign_result_do_not_leak_or_claim_execution(tmp_path):
    store = StateStore(tmp_path)
    _seed(store)
    _turn(store, "foreign-name", "PRIVATE_PROMPT_PAYLOAD")
    _, response, tool = _turn(store, "good", "save_plan")
    _result(store, response, tool, {"status": "rejected"}, mission_id="other-mission")
    report = build_tool_activity(store, "run-a")
    assert report["counts"]["untrusted_or_unmatched_tool_calls"] == 1
    assert len(report["steps"]) == 1
    step = report["steps"][0]
    assert step["tool_name"] == "save_plan"
    assert step["execution_status"] == "not_executed"
    assert step["outcome"] == "unknown"
    assert "PRIVATE_PROMPT_PAYLOAD" not in str(report)


@pytest.mark.parametrize("valid_request", [True, False])
def test_peer_request_saved_outcome_requires_matching_durable_request(
    tmp_path, valid_request,
):
    store = StateStore(tmp_path)
    _seed(store)
    call, response, tool = _turn(store, "peer", "peer_request")
    request = _put(store, "mail_request", "request-peer", {
        "mission_id": "mission-a", "requester_id": "actor-a" if valid_request else "actor-other",
        "recipient_id": "actor-peer", "cause_ref": {
            "kind": "board_proposal", "id": "task-a", "version": 1,
        }, "status": "unanswered", "question": "Check a synthetic claim",
    })
    _result(store, response, tool, {
        "status": "request_saved", "request_id": request["id"],
        "request_ref": _ref(request), "recipient_id": "actor-peer",
    })
    _work(store, "peer", call, tool)
    prepared = _put(store, "agent_call", "call-prepared-peer", {
        "mission_id": "mission-a", "instance_id": "actor-a", "task_id": "task-a",
        "attempt_id": "attempt-prepared-peer",
    })
    request_blob = _blob(store, {"messages": [], "tools": [{"function": {"name": "peer_request"}}]})
    _put(store, "model_request_intent", "intent-prepared-peer", {
        "mission_id": "mission-a", "instance_id": "actor-a", "task_id": "task-a",
        "attempt_id": "attempt-prepared-peer", "call_ref": _ref(prepared),
        "blob": request_blob, "request_sha256": request_blob["sha256"],
    })
    steps = build_tool_activity(store, "run-a")["steps"]
    assert len(steps) == 1  # Prepared input did not select or execute another peer tool.
    step = steps[0]
    assert step["execution_status"] == "executed"
    assert step["outcome"] == ("completed" if valid_request else "unknown")
    assert step["reuse_status"] == ("new" if valid_request else "unknown")
    assert step["reuse_basis"] == ("saved_peer_request_record" if valid_request else None)
    if valid_request:
        from ora.observability.export import project_tool_activity

        run = store.get("mission_run", "run-a")
        store.transact("close-synthetic-peer-observation", {}, lambda tx: tx.put(
            "mission_run", "run-a", {**run["body"], "status": "completed"},
            expected_version=run["version"],
        ))
        projected = project_tool_activity(store, "mission-a", "run-a")
        assert projected["created"] >= 1
        tool_spans = [row for row in store.records("observability_export")
                      if row["body"]["attributes"].get("gen_ai.tool.name") == "peer_request"]
        assert len(tool_spans) == 1
        assert tool_spans[0]["body"]["attributes"]["osra.tool.outcome"] == "completed"
        assert tool_spans[0]["body"]["attributes"]["osra.tool.reuse_basis"] == (
            "saved_peer_request_record"
        )


@pytest.mark.parametrize("corruption", ["foreign_mission", "stale_response_ref", "wrong_message_hash"])
def test_corrupt_result_lineage_does_not_claim_tool_execution(tmp_path, corruption):
    store = StateStore(tmp_path)
    _seed(store)
    _, response, tool = _turn(store, "first", "save_plan")
    result, _ = _result(store, response, tool, {"status": "saved", "private": "PRIVATE"})
    body = dict(result["body"])
    if corruption == "foreign_mission":
        body["mission_id"] = "mission-foreign"
    elif corruption == "stale_response_ref":
        body["response_ref"] = {**_ref(response), "version": 99}
    else:
        body["message_sha256"] = "0" * 64
    _put(store, result["kind"], result["id"], body, version=result["version"])
    step = build_tool_activity(store, "run-a")["steps"][0]
    assert step["execution_status"] == "not_executed"
    assert step["outcome"] == "unknown"
    assert step["duration_seconds"] is None
    assert step["delivery_form"] == "none"
    assert "PRIVATE" not in str(step)


def test_rejected_tool_result_has_no_inferred_duration_and_pinned_actor_survives_head_change(tmp_path):
    store = StateStore(tmp_path)
    _seed(store)
    _, response, tool = _turn(store, "first", "save_plan")
    _result(store, response, tool, {"status": "rejected"})
    # A foreign run may advance the actor head; exact version-one run scope
    # preserves the original activity without admitting the new run.
    _put(store, "mission_run", "run-foreign", {"mission_id": "mission-a", "status": "completed"})
    _put(store, "agent_instance", "actor-a", {
        "mission_id": "mission-a", "run_id": "run-foreign",
    }, version=1)
    report = build_tool_activity(store, "run-a")
    assert len(report["steps"]) == 1
    step = report["steps"][0]
    assert step["outcome"] == "rejected"
    assert step["execution_status"] == "uncertain"
    assert step["duration_seconds"] is None
    assert step["delivery_status"] == "not_observed_yet"


def _reviewed_peer_request(store, *, suffix="peer"):
    answer = _put(store, "mail_answer", f"answer-{suffix}", {
        "mission_id": "mission-a", "request_id": f"request-{suffix}",
        "responder_id": "actor-peer", "artifact_ref": {
            "kind": "report", "id": "other-peer-report", "version": 1,
        },
    })
    _put(store, "mail_request", f"request-{suffix}", {
        "mission_id": "mission-a", "requester_id": "actor-a",
        "recipient_id": "actor-peer", "status": "awaiting_review",
        "answer_refs": [_ref(answer)],
    })
    reviewed = _put(store, "mail_request", f"request-{suffix}", {
        "mission_id": "mission-a", "requester_id": "actor-a",
        "recipient_id": "actor-peer", "status": "answered",
        "answer_refs": [_ref(answer)], "accepted_answer_ref": _ref(answer),
        "reviewer_id": "actor-a", "review": {"reason": "Synthetic review"},
    }, version=1)
    return answer, reviewed


def test_peer_turn_mixed_result_keeps_one_step_and_verified_slot_statuses(tmp_path):
    store = StateStore(tmp_path)
    _seed(store)
    answer, reviewed = _reviewed_peer_request(store)
    args = {"review": {
        "request_id": "request-peer", "request_version": 1,
        "answer_id": answer["id"], "answer_version": 1,
        "disposition": "answered", "reason": "Synthetic review",
    }, "followup_request": ["malformed private argument"]}
    call, response, tool = _turn(store, "peer", "peer_turn", tool_args=args)
    store.transact("mission-peer-review:tool-work-peer:peer-turn:review", {},
                   lambda tx: {"request_ref": _ref(reviewed)})
    operations = {"review": {"status": "review_saved", "request_ref": _ref(reviewed)},
                  "followup_request": {"status": "rejected",
                                       "reason": "private malformed argument"}}
    _work(store, "peer", call, tool, payload={
        "tool_call_id": tool["id"], "name": "peer_turn",
        "result": {"status": "peer_turn_recorded", "operations": operations},
    })
    _result(store, response, tool, {"status": "peer_turn_recorded",
                                    "operations": operations})
    report = build_tool_activity(store, "run-a")
    assert report["counts"]["selected_tools"] == 1
    assert report["counts"]["executed_tools"] == 1
    step = report["steps"][0]
    assert step["execution_status"] == "executed"
    assert step["outcome"] == "unknown"
    assert step["peer_operation_statuses"] == {
        "review": "review_saved", "followup_request": "rejected",
    }
    assert "private" not in str(report)


@pytest.mark.parametrize("accepted_board,mixed_followup", [
    (True, False), (False, False), (True, True),
])
def test_peer_turn_revision_is_published_only_with_exact_board_and_review(
    tmp_path, accepted_board, mixed_followup,
):
    store = StateStore(tmp_path)
    _seed(store, status="completed" if accepted_board else "active")
    answer, reviewed = _reviewed_peer_request(store)
    args = {"review": {
        "request_id": "request-peer", "request_version": 1,
        "answer_id": answer["id"], "answer_version": 1,
        "disposition": "answered", "reason": "Synthetic review",
    }, "report": {"kind": "revision", "title": "Private title",
                  "text": "Private report text", "citations": [], "limitations": []}}
    if mixed_followup:
        args["followup_request"] = ["private malformed follow-up"]
    call, _response, tool = _turn(store, "peer", "peer_turn", tool_args=args)
    store.transact("mission-peer-review:tool-work-peer:peer-turn:review", {},
                   lambda tx: {"request_ref": _ref(reviewed)})
    attempt = _put(store, "agent_attempt", "attempt-peer", {
        "mission_id": "mission-a", "run_id": "run-a", "call_ref": _ref(call),
        "status": "running",
    })
    report_id = "report-" + hashlib.sha256(canonical(
        ("run-a", "actor-a", "tool-work-peer")).encode()).hexdigest()[:32]
    report_ref = _ref(_put(store, "report", report_id, {
        "mission_id": "mission-a", "run_id": "run-a",
        "producer_attempt_ref": _ref(attempt),
    }))
    _put(store, "agent_attempt", attempt["id"], {
        **attempt["body"], "status": "completed", "report_ref": report_ref,
    }, version=attempt["version"])
    outcome = _put(store, "board_outcome", "outcome-peer", {
        "mission_id": "mission-a", "result_refs": [report_ref],
        "proposal_ref": {"kind": "board_proposal", "id": "task-a", "version": 1},
        "actor_ref": {"kind": "agent_instance", "id": "actor-a", "version": 1},
        "completes_task": True,
    })
    _put(store, "board_proposal", "task-a", {
        "mission_id": "mission-a", "status": "completed" if accepted_board else "open",
        "outcome_ref": _ref(outcome),
    })
    operations = {
        "review": {"status": "review_saved", "request_ref": _ref(reviewed)},
        "report": {"status": "report_saved", "report_ref": report_ref},
    }
    if mixed_followup:
        operations["followup_request"] = {"status": "rejected"}
    _work(store, "peer", call, tool, payload={
        "tool_call_id": tool["id"], "name": "peer_turn",
        "result": {"status": "peer_turn_recorded", "operations": operations},
    })
    result = build_tool_activity(store, "run-a")
    assert result["counts"]["selected_tools"] == 1
    step = result["steps"][0]
    assert step["execution_status"] == (
        "accepted_report" if accepted_board else "executed"
    )
    assert step["outcome"] == (
        "completed" if accepted_board and not mixed_followup else "unknown"
    )
    if accepted_board:
        assert step["peer_operation_statuses"] == {
            "review": "review_saved", "report": "accepted_report",
            **({"followup_request": "rejected"} if mixed_followup else {}),
        }
    else:
        assert "peer_operation_statuses" not in step
    assert "Private title" not in str(result)
    assert "Private report text" not in str(result)
