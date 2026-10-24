"""Read-only, exact-run lifecycle of model-selected mission tools.

This projection exposes trusted tool names and receipt-derived states only. It
never returns model arguments, source text, tool contents, or private reasoning.
An executor duration measures the tool callback, not model generation or queue
wait. Persistence alone does not prove delivery to a later model request.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator, Set
from datetime import datetime, timezone
from functools import cache

from ora.state import StateError, StateStore
from ora.state.store import canonical, unpack

from .evaluation import read_evaluation_observation

SCHEMA = "ora.tool_activity.v1"

class _TrustedToolNames(Set[str]):
    """Read the host's real registry lazily to avoid the service/export import cycle."""

    @staticmethod
    @cache
    def _names() -> frozenset[str]:
        from ora.mission.tool_catalog import TOOL_PURPOSES, TOOL_SCHEMAS

        return frozenset(TOOL_SCHEMAS) | frozenset(TOOL_PURPOSES)

    def __contains__(self, value: object) -> bool:
        return isinstance(value, str) and value in self._names()

    def __iter__(self) -> Iterator[str]:
        return iter(self._names())

    def __len__(self) -> int:
        return len(self._names())


TRUSTED_TOOL_NAMES: Set[str] = _TrustedToolNames()
_TOOL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MAX_BLOB_BYTES = 2_000_000
_MODEL_SEEN_STATUSES = frozenset({"complete", "tool_requested"})


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _rows(store: StateStore, kind: str, mission_id: str):
    """Read only current heads in one mission before decoding record bodies."""
    after = ""
    location = f"{store.db_path.as_uri()}?mode=ro"
    with sqlite3.connect(location, uri=True) as db:
        db.row_factory = sqlite3.Row
        while True:
            page = db.execute(
                "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) "
                "WHERE r.kind=? AND r.id>? AND json_extract(r.body,'$.mission_id')=? "
                "ORDER BY r.id LIMIT 500", (kind, after, mission_id),
            ).fetchall()
            if not page:
                return
            yield from (unpack(row) for row in page)
            after = page[-1]["id"]


def _blob_json(store: StateStore, body: dict) -> dict | None:
    blob = body.get("blob")
    if (not isinstance(blob, dict) or type(blob.get("size")) is not int
            or not 0 <= blob["size"] <= _MAX_BLOB_BYTES):
        return None
    try:
        value = json.loads(store.read_blob(blob["sha256"]))
    except (KeyError, OSError, StateError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _exact(store: StateStore, pointer: object, kind: str) -> dict | None:
    if (not isinstance(pointer, dict) or set(pointer) != {"kind", "id", "version"}
            or pointer.get("kind") != kind or not isinstance(pointer.get("id"), str)
            or type(pointer.get("version")) is not int):
        return None
    try:
        record = store.get(**pointer)
    except StateError:
        return None
    return record if record is not None and _ref(record) == pointer else None


def _scope(store: StateStore, run: dict, intent: dict) -> tuple[dict, dict] | None:
    body = intent["body"]
    mission_id = run["body"].get("mission_id")
    call = _exact(store, body.get("call_ref"), "agent_call")
    if call is None or body.get("mission_id") != mission_id:
        return None
    actor_id = call["body"].get("instance_id")
    actor = store.get("agent_instance", actor_id, 1) if isinstance(actor_id, str) else None
    if (actor is None or actor["body"].get("run_id") != run["id"]
            or actor["body"].get("mission_id") != mission_id
            or call["body"].get("mission_id") != mission_id
            or body.get("instance_id") != actor_id
            or body.get("task_id") != call["body"].get("task_id")
            or body.get("attempt_id") != call["body"].get("attempt_id")):
        return None
    return call, actor


def _valid_turn(store: StateStore, run: dict, intent: dict) -> dict | None:
    scoped = _scope(store, run, intent)
    if scoped is None:
        return None
    call, actor = scoped
    intent_ref = _ref(intent)
    marker = store.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}", 1)
    response = store.get("model_response_receipt", f"model-response:{intent['id']}")
    if (marker is None or marker["body"].get("status") != "attempted_external_effect"
            or marker["body"].get("intent_ref") != intent_ref
            or marker["body"].get("call_ref") != _ref(call)
            or marker["body"].get("request_sha256") != intent["body"].get("request_sha256")
            or response is not None and (
                response["body"].get("intent_ref") != intent_ref
                or response["body"].get("call_ref") != _ref(call)
                or response["body"].get("mission_id") != run["body"]["mission_id"]
                or response["body"].get("task_id") != intent["body"].get("task_id")
                or response["body"].get("attempt_id") != intent["body"].get("attempt_id")
                or response["body"].get("request_sha256") != intent["body"].get("request_sha256")
            )):
        return None
    request = _blob_json(store, intent["body"])
    if (request is None or not isinstance(request.get("messages"), list)
            or intent["body"].get("blob", {}).get("sha256")
            != intent["body"].get("request_sha256")):
        return None
    return {"intent": intent, "call": call, "actor": actor, "marker": marker,
            "response": response, "request": request}


def _result(store: StateStore, response: dict, tool_id: str,
            tool_name: str) -> tuple[dict, dict] | None:
    result = store.get("context_tool_result", f"tool-result:{response['id']}")
    if (result is None or result["body"].get("mission_id") != response["body"].get("mission_id")
            or result["body"].get("response_ref") != _ref(response)
            or result["body"].get("tool_call_id") != tool_id):
        return None
    message = _blob_json(store, result["body"])
    if (message is None or message.get("role") != "tool"
            or message.get("name") != tool_name
            or message.get("tool_call_id") != tool_id
            or not isinstance(message.get("content"), str)
            or hashlib.sha256(canonical(message).encode()).hexdigest()
            != result["body"].get("message_sha256")):
        return None
    return result, message


def _outcome(message: dict, tool_name: str,
             selected_capability_name: str | None) -> tuple[str, str, str | None]:
    try:
        content = json.loads(message["content"])
    except (ValueError, TypeError):
        return "unknown", "unknown", None
    if not isinstance(content, dict):
        return "unknown", "unknown", None
    reuse = content.get("reuse_status")
    reuse_status = reuse if isinstance(reuse, str) and reuse in {"new", "reused"} else "unknown"
    reuse_basis = "explicit_operation_reuse_status" if reuse_status != "unknown" else None
    status = content.get("status") or content.get("outcome")
    if status is not None and not isinstance(status, str):
        return "unknown", reuse_status, reuse_basis
    if reuse_status == "unknown" and tool_name == "request_capability" and (
        selected_capability_name is not None and status == "selected_for_next_turn"
        and type(content.get("already_attached")) is bool
    ):
        reuse_status = "reused" if content["already_attached"] else "new"
        reuse_basis = "capability_attachment_flag"
    if reuse_status == "unknown" and tool_name == "fetch_source" and status == "inspected":
        feedback = content.get("fetch_feedback")
        if isinstance(feedback, dict):
            retained = feedback.get("retained_document_reused")
            new_http = feedback.get("new_http_request")
            if type(retained) is bool and type(new_http) is bool and retained != new_http:
                reuse_status = "reused" if retained else "new"
                reuse_basis = "public_document_http_fetch"
    if reuse_status == "unknown" and tool_name == "read_source_window":
        progress = content.get("progress_feedback")
        if (isinstance(progress, dict)
                and progress.get("scope") == "this_task_prior_checked_read_windows"
                and type(progress.get("exact_window_repeated")) is bool
                and type(progress.get("new_checked_location_chars")) is int
                and progress["new_checked_location_chars"] >= 0):
            if progress["exact_window_repeated"] and progress["new_checked_location_chars"] == 0:
                reuse_status = "reused"
                reuse_basis = "same_task_exact_read_window"
            elif (not progress["exact_window_repeated"]
                  and progress["new_checked_location_chars"] > 0):
                reuse_status = "new"
                reuse_basis = "same_task_new_checked_location"
    if status in {"pending", "running", "queued", "wait_requested", "waiting"}:
        return "pending", reuse_status, reuse_basis
    if status in {"rejected", "invalid", "denied", "unavailable"}:
        return "rejected", reuse_status, reuse_basis
    if status in {"failed", "error", "partial_failure"} or content.get("failed") is True:
        return "failed", reuse_status, reuse_basis
    if status is None or status in {"completed", "complete", "saved", "inspected",
                                    "selected_for_next_turn", "accepted", "success"}:
        return "completed", reuse_status, reuse_basis
    return "unknown", reuse_status, reuse_basis


def _saved_peer_request(store: StateStore, run: dict, turn: dict,
                        message: dict | None) -> bool:
    """Require the exact saved request before calling a peer tool successful."""
    if message is None:
        return False
    try:
        content = json.loads(message["content"])
    except (KeyError, TypeError, ValueError):
        return False
    if not isinstance(content, dict) or content.get("status") != "request_saved":
        return False
    request = _exact(store, content.get("request_ref"), "mail_request")
    if request is None or content.get("request_id") != request["id"]:
        return False
    body = request["body"]
    cause = body.get("cause_ref")
    return (body.get("mission_id") == run["body"].get("mission_id")
            and body.get("requester_id") == turn["actor"]["id"]
            and body.get("recipient_id") == content.get("recipient_id")
            and isinstance(cause, dict)
            and cause.get("kind") == "board_proposal"
            and cause.get("id") == turn["call"]["body"].get("task_id"))


def _accepted_report(store: StateStore, run: dict, turn: dict) -> bool:
    attempt_id = turn["intent"]["body"].get("attempt_id")
    attempt = store.get("agent_attempt", attempt_id) if isinstance(attempt_id, str) else None
    if (attempt is None or attempt["body"].get("call_ref") != _ref(turn["call"])
            or attempt["body"].get("run_id") != run["id"]
            or attempt["body"].get("mission_id") != run["body"]["mission_id"]):
        return False
    report = _exact(store, attempt["body"].get("report_ref"), "report")
    producer = (_exact(store, report["body"].get("producer_attempt_ref"), "agent_attempt")
                if report else None)
    return bool(report and producer and attempt["body"].get("status") == "completed"
                and producer["body"].get("call_ref") == _ref(turn["call"])
                and producer["body"].get("run_id") == run["id"]
                and report["body"].get("mission_id") == run["body"]["mission_id"]
                and report["body"].get("run_id") == run["id"])


def _selection(store: StateStore, run: dict, turn: dict,
               selections: list[dict]) -> str | None:
    for record in selections:
        body = record["body"]
        name = body.get("tool_id")
        work = _exact(store, body.get("source_work_ref"), "executor_work")
        spec = work["body"].get("spec") if work else None
        if (body.get("run_id") == run["id"]
                and body.get("mission_id") == run["body"]["mission_id"]
                and body.get("actor_id") == turn["actor"]["id"]
                and body.get("task_id") == turn["call"]["body"].get("task_id")
                and body.get("source_call_ref") == _ref(turn["call"])
                and body.get("action") == "request_capability"
                and isinstance(spec, dict)
                and spec.get("handler_id") == "mission-tool"
                and spec.get("mission_id") == run["body"]["mission_id"]
                and spec.get("run_id") == run["id"]
                and spec.get("actor_id") == turn["actor"]["id"]
                and isinstance(spec.get("proposal_ref"), dict)
                and spec["proposal_ref"].get("id") == turn["call"]["body"].get("task_id")
                and spec.get("attempt_id") == turn["intent"]["body"].get("attempt_id")
                and spec.get("call_id") == turn["call"]["id"]
                and spec.get("call_version") == turn["call"]["version"]
                and name in TRUSTED_TOOL_NAMES):
            return name
    return None


def _work_timing(store: StateStore, run: dict, turn: dict,
                 tool_id: str, tool_name: str, works: list[dict],
                 *, accepted_report: bool) -> tuple:
    matches = []
    for work in works:
        spec = work["body"].get("spec")
        if not isinstance(spec, dict):
            continue
        proposal = spec.get("proposal_ref")
        if (spec.get("handler_id") == "mission-tool"
                and spec.get("mission_id") == run["body"]["mission_id"]
                and spec.get("run_id") == run["id"]
                and spec.get("actor_id") == turn["actor"]["id"]
                and isinstance(proposal, dict)
                and proposal.get("id") == turn["call"]["body"].get("task_id")
                and spec.get("attempt_id") == turn["intent"]["body"].get("attempt_id")
                and spec.get("call_id") == turn["call"]["id"]
                and spec.get("call_version") == turn["call"]["version"]):
            matches.append(work)
    if len(matches) != 1:
        return None, None, None, "not_executed", None, None, None
    work = matches[0]
    phase = work["body"].get("phase")
    receipt = _exact(store, work["body"].get("receipt_ref"), "executor_receipt")
    artifact = _exact(store, work["body"].get("result_ref"), "executor_artifact")
    payload = artifact["body"].get("payload") if artifact is not None else None
    attempt = store.get("agent_attempt", turn["intent"]["body"].get("attempt_id"))
    payload_matches = bool(
        isinstance(payload, dict) and (
            (accepted_report and tool_name == "publish_result"
             and attempt is not None
             and payload.get("report_ref") == attempt["body"].get("report_ref"))
            or (payload.get("tool_call_id") == tool_id and payload.get("name") == tool_name)
        )
    )
    if (receipt is None or artifact is None or phase != "completed"
            or receipt["body"].get("work_id") != work["body"]["spec"].get("work_id")
            or receipt["body"].get("mission_id") != run["body"]["mission_id"]
            or receipt["body"].get("artifact_ref") != _ref(artifact)
            or artifact["body"].get("work_id") != work["body"]["spec"].get("work_id")
            or artifact["body"].get("mission_id") != run["body"]["mission_id"]
            or artifact["body"].get("actor_id") != turn["actor"]["id"]
            or not payload_matches):
        return (None, None, None,
                "uncertain" if phase == "result_unaccepted" else "not_executed",
                None, None, None)
    timing = receipt["body"].get("timing")
    seconds = timing.get("active_seconds") if isinstance(timing, dict) else None
    duration = (float(seconds) if isinstance(timing, dict)
                and timing.get("timing_basis") == "store_clock"
                and type(seconds) in {float, int} and 0 <= seconds <= 1e9
                and math.isfinite(seconds) else None)
    capacity_id = receipt["body"].get("capacity_operation_id")
    capacity = (store.get("capacity_operation", capacity_id)
                if isinstance(capacity_id, str) else None)
    interval_start = interval_end = interval_basis = None
    if capacity is not None:
        body = capacity["body"]
        attempts = body.get("attempts")
        if (body.get("mission_id") == run["body"]["mission_id"]
                and body.get("run_id") == run["id"]
                and body.get("task_id") == turn["call"]["body"].get("task_id")
                and body.get("attempt_id") == turn["intent"]["body"].get("attempt_id")
                and body.get("call_id") == turn["call"]["id"]
                and body.get("phase") == "completed"
                and isinstance(attempts, list) and len(attempts) == 1
                and isinstance(attempts[0], dict)
                and attempts[0].get("status") == "completed"):
            start = attempts[0].get("at")
            end = attempts[0].get("completed_at")
            if (type(start) in {float, int} and type(end) in {float, int}
                    and 0 <= start <= end <= 253402300799
                    and math.isfinite(start) and math.isfinite(end)):
                interval_start = datetime.fromtimestamp(start, timezone.utc).isoformat()
                interval_end = datetime.fromtimestamp(end, timezone.utc).isoformat()
                interval_basis = "single_capacity_attempt_wall_interval"
    return (duration,
            "executor_receipt_active_seconds_store_clock" if duration is not None else None,
            receipt["created_at"], "executed",
            interval_start, interval_end, interval_basis)


def _delivery_index(turns: list[dict]) -> dict[tuple[str, str, str, str], list[tuple]]:
    """Index actual serialized tool messages once, including host projections."""
    index: dict[tuple[str, str, str, str], list[tuple]] = {}
    for turn in turns:
        messages = turn["request"]["messages"]
        selected = set()
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            calls = message.get("tool_calls")
            for item in calls if isinstance(calls, list) else ():
                if (isinstance(item, dict) and isinstance(item.get("function"), dict)
                        and isinstance(item.get("id"), str)
                        and isinstance(item["function"].get("name"), str)):
                    selected.add((item["id"], item["function"]["name"]))
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            tool_id, name = message.get("tool_call_id"), message.get("name")
            if (not isinstance(tool_id, str) or not isinstance(name, str)
                    or (tool_id, name) not in selected
                    or name not in TRUSTED_TOOL_NAMES
                    or not isinstance(message.get("content"), str)):
                continue
            key = (turn["actor"]["id"], turn["call"]["body"]["task_id"], tool_id, name)
            index.setdefault(key, []).append((
                turn["marker"]["created_at"],
                turn["response"]["body"].get("status") if turn["response"] else None,
                message["content"],
            ))
    return index


def _next_delivery(next_turn: tuple[int, dict] | None, message: dict | None,
                   result_at: str | None, tool_id: str, tool_name: str,
                   *, published: bool) -> tuple[int | None, bool, bool, str, str]:
    if next_turn is None:
        return None, False, False, "no_next_captured_request", "none"
    loop_index, turn = next_turn
    if message is None:
        return (loop_index, False, False,
                "no_result_message" if published else "result_not_saved", "none")
    if turn["marker"]["created_at"] <= (result_at or ""):
        return loop_index, False, False, "captured_without_result", "none"
    calls = {
        (item.get("id"), item.get("function", {}).get("name"))
        for candidate in turn["request"]["messages"]
        if isinstance(candidate, dict) and candidate.get("role") == "assistant"
        for item in (candidate.get("tool_calls")
                     if isinstance(candidate.get("tool_calls"), list) else ())
        if isinstance(item, dict) and isinstance(item.get("id"), str)
        and isinstance(item.get("function"), dict)
        and isinstance(item["function"].get("name"), str)
    }
    if (tool_id, tool_name) not in calls:
        return loop_index, False, False, "captured_without_result", "none"
    for candidate in turn["request"]["messages"]:
        if (isinstance(candidate, dict) and candidate.get("role") == "tool"
                and candidate.get("tool_call_id") == tool_id
                and candidate.get("name") == tool_name
                and isinstance(candidate.get("content"), str)):
            if turn["response"]["body"].get("status") not in _MODEL_SEEN_STATUSES:
                return (loop_index, True, False,
                        "captured_request_not_confirmed_model_input", "none")
            if candidate["content"] == message["content"]:
                return loop_index, True, True, "captured_with_exact_result", "exact_saved_message"
            return (loop_index, True, True, "captured_with_projected_or_restricted_result",
                    "host_projected_or_restricted")
    return loop_index, False, False, "captured_without_result", "none"


def _delivery(result_message: dict | None, result_at: str | None, turn: dict,
              deliveries: dict[tuple[str, str, str, str], list[tuple]],
              tool_id: str, tool_name: str, *, run_closed: bool,
              observation_closed: bool) -> tuple[str, bool, bool, bool, bool, str]:
    if result_message is None:
        return "no_result_message", False, False, False, True, "none"
    key = (turn["actor"]["id"], turn["call"]["body"]["task_id"], tool_id, tool_name)
    later = [row for row in deliveries.get(key, ()) if row[0] > (result_at or "")]
    captured = [row for row in later if row[1] in _MODEL_SEEN_STATUSES]
    if captured:
        form = ("exact_saved_message" if any(row[2] == result_message["content"] for row in captured)
                else "host_projected_or_restricted")
        return "captured_later", bool(later), True, True, True, form
    if run_closed:
        return "not_delivered_after_terminal", bool(later), False, False, True, "none"
    if observation_closed:
        return "not_delivered_after_observation", bool(later), False, False, True, "none"
    return "not_observed_yet", bool(later), False, False, False, "none"


def build_tool_activity(store: StateStore, run_id: str) -> dict:
    """Inspect saved tools for one run without mutating state or reading reasoning."""
    run = store.get("mission_run", run_id)
    if run is None or not isinstance(run["body"].get("mission_id"), str):
        raise StateError("Tool activity needs an exact mission run")
    dispatches = []
    mission_id = run["body"]["mission_id"]
    for intent in _rows(store, "model_request_intent", mission_id):
        turn = _valid_turn(store, run, intent)
        if turn is not None:
            dispatches.append(turn)
    dispatches.sort(key=lambda item: (item["marker"]["created_at"], item["intent"]["id"]))
    turns = [turn for turn in dispatches if turn["response"] is not None]
    turns.sort(key=lambda item: (item["marker"]["created_at"], item["intent"]["id"]))
    deliveries = _delivery_index(dispatches)
    next_by_response: dict[str, tuple[int, dict]] = {}
    by_peer_task: dict[tuple[str, str], list[tuple[int, dict]]] = {}
    for index, turn in enumerate(turns, 1):
        key = (turn["actor"]["id"], turn["call"]["body"]["task_id"])
        by_peer_task.setdefault(key, []).append((index, turn))
    for group in by_peer_task.values():
        for current, next_turn in zip(group, group[1:]):
            next_by_response[current[1]["response"]["id"]] = next_turn
    works_by_call: dict[tuple[str, int], list[dict]] = {}
    for work in _rows(store, "executor_work", mission_id):
        spec = work["body"].get("spec")
        if (isinstance(spec, dict) and spec.get("handler_id") == "mission-tool"
                and isinstance(spec.get("call_id"), str)
                and type(spec.get("call_version")) is int):
            works_by_call.setdefault((spec["call_id"], spec["call_version"]), []).append(work)
    selections_by_call: dict[tuple[str, int], list[dict]] = {}
    for selected in _rows(store, "mission_capability_selection", mission_id):
        pointer = selected["body"].get("source_call_ref")
        if (isinstance(pointer, dict) and pointer.get("kind") == "agent_call"
                and isinstance(pointer.get("id"), str)
                and type(pointer.get("version")) is int):
            selections_by_call.setdefault((pointer["id"], pointer["version"]), []).append(selected)
    run_closed = run["body"].get("status") in {"completed", "failed", "cancelled"}
    observation = read_evaluation_observation(store, run_id)
    observation_closed = bool(observation and observation.get("observation_stopped") is True)
    steps = []
    untrusted = 0
    for loop_index, turn in enumerate(turns, 1):
        response = turn["response"]
        if response["body"].get("status") != "tool_requested":
            continue
        raw = _blob_json(store, response["body"])
        tools = raw.get("tool_calls") if raw else None
        offered = turn["request"].get("tools")
        offered_names = {
            tool.get("function", {}).get("name") for tool in offered
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
            and isinstance(tool["function"].get("name"), str)
        } if isinstance(offered, list) else set()
        if not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], dict):
            untrusted += 1
            continue
        tool = tools[0]
        tool_id = tool.get("id")
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if (not isinstance(tool_id, str) or not _TOOL_ID.fullmatch(tool_id)
                or name not in TRUSTED_TOOL_NAMES or name not in offered_names):
            untrusted += 1
            continue
        exact_result = _result(store, response, tool_id, name)
        result, message = exact_result if exact_result else (None, None)
        published = name == "publish_result" and _accepted_report(store, run, turn)
        call_key = (turn["call"]["id"], turn["call"]["version"])
        selected_capability = (
            _selection(store, run, turn, selections_by_call.get(call_key, ()))
            if name == "request_capability" else None
        )
        (duration, duration_basis, ended_at, work_status,
         interval_start, interval_end, interval_basis) = _work_timing(
            store, run, turn, tool_id, name, works_by_call.get(call_key, ()),
            accepted_report=published,
        )
        execution = ("accepted_report" if published else "executed"
                     if work_status == "executed" else "uncertain"
                     if message is not None or work_status == "uncertain" else "not_executed")
        outcome, reuse, reuse_basis = (
            _outcome(message, name, selected_capability) if message else ("unknown", "unknown", None)
        )
        if execution not in {"executed", "accepted_report"}:
            reuse, reuse_basis = "unknown", None
        if published:
            outcome = "completed"
        elif message is None:
            outcome = "unknown"
        elif name == "peer_request" and execution == "executed" and _saved_peer_request(
            store, run, turn, message,
        ):
            outcome, reuse, reuse_basis = "completed", "new", "saved_peer_request_record"
        (delivery, prepared, dispatched, captured,
         delivery_final, delivery_form) = _delivery(
            message, result["created_at"] if result else None,
            turn, deliveries, tool_id, name,
            run_closed=run_closed, observation_closed=observation_closed,
        )
        if message is None and not published:
            delivery = ("not_delivered_after_terminal" if run_closed else
                        "not_delivered_after_observation" if observation_closed else
                        "not_observed_yet")
            delivery_final = run_closed or observation_closed
        (next_loop_index, prepared_next, in_next, next_status,
         next_form) = _next_delivery(
            next_by_response.get(response["id"]), message,
            result["created_at"] if result else None, tool_id, name,
            published=published,
        )
        response_ref = _ref(response)
        steps.append({
            "step_id": hashlib.sha256(canonical([response_ref, tool_id]).encode()).hexdigest()[:24],
            "loop_index": loop_index,
            "run_id": run_id, "mission_id": run["body"]["mission_id"],
            "actor_id": turn["actor"]["id"],
            "task_id": turn["call"]["body"]["task_id"],
            "call_ref": _ref(turn["call"]), "response_ref": response_ref,
            "tool_call_id": tool_id, "tool_name": name,
            "selected_capability_name": selected_capability,
            "selection_status": "verified", "execution_status": execution,
            "outcome": outcome, "reuse_status": reuse, "reuse_basis": reuse_basis,
            "selected_at_utc": response["created_at"],
            "ended_at_utc": ended_at,
            "duration_seconds": duration, "duration_basis": duration_basis,
            "interval_start_utc": interval_start,
            "interval_end_utc": interval_end,
            "interval_basis": interval_basis,
            "delivery_status": delivery, "delivery_final": delivery_final,
            "delivery_form": delivery_form,
            "next_model_loop_index": next_loop_index,
            "result_in_next_prepared_request": prepared_next,
            "result_in_next_captured_request": in_next,
            "next_delivery_status": next_status,
            "next_delivery_form": next_form,
            "result_in_later_prepared_request": prepared,
            "result_in_later_dispatched_request": dispatched,
            "result_in_later_captured_request": captured,
        })
    outcomes = Counter(step["outcome"] for step in steps)
    executions = Counter(step["execution_status"] for step in steps)
    return {
        "schema": SCHEMA,
        "basis": "exact_run_dispatched_model_receipts_and_tool_result_lineage",
        "delivery_basis": "serialized_tool_message_pair_not_other_source_context",
        "run_id": run_id, "mission_id": run["body"]["mission_id"],
        "counts": {
            "captured_model_loops": len(turns), "selected_tools": len(steps),
            "untrusted_or_unmatched_tool_calls": untrusted,
            "executed_tools": executions["executed"] + executions["accepted_report"],
            "outcomes": dict(sorted(outcomes.items())),
            "reused_tools": sum(step["reuse_status"] == "reused" for step in steps),
            "delivered_to_captured_model": sum(
                step["result_in_later_captured_request"] for step in steps
            ),
        },
        "steps": steps,
    }
