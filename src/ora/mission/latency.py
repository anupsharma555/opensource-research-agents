"""Model-authored latency expectations and observed Stage 4 timing.

An expectation is a revisable peer statement, not a host complexity score or
deadline. Measurements read accepted outcomes and capacity records; operation
durations may overlap and must not be added to mission wall time.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import get_args

from ora.contracts.models import Latency
from ora.coordination.board import ref, require_ref
from ora.state import StateConflict, StateError, StateStore, Transaction
from ora.state.store import canonical, identifier


class LatencyError(StateError):
    """An expectation or saved timing reference needs local repair."""


_CATEGORIES = frozenset(get_args(Latency))
_WAITS = ("queue_wait_seconds", "quota_wait_seconds", "provider_wait_seconds", "retry_wait_seconds")


def _expectation_id(mission_id: str, actor_id: str, task_id: str) -> str:
    digest = hashlib.sha256(canonical([mission_id, actor_id, task_id]).encode()).hexdigest()
    return f"mission-latency:{digest}"


def save_expectation(
    store: StateStore,
    operation_id: str,
    *,
    mission_id: str,
    run_id: str,
    actor_ref: dict,
    task_ref: dict,
    response_ref: dict,
    category: str,
    basis: str,
    expected_version: int = 0,
    review_after_seconds: float | None = None,
) -> dict:
    """Save a peer's selected category with exact observed response lineage.

    The caller must map this category and basis from that model response. The
    response reference establishes provenance, but does not interpret text.
    Revisions use the same ID and optimistic expected_version, preserving old
    records. A revised board task may be named by its current exact version.
    """
    for value in (operation_id, mission_id, run_id):
        identifier(value)
    if category not in _CATEGORIES or not isinstance(basis, str) or not basis.strip():
        raise LatencyError("Model expectation needs a known category and plain-language basis")
    if type(expected_version) is not int or expected_version < 0:
        raise LatencyError("Expected latency version must be nonnegative")
    if review_after_seconds is not None:
        try:
            valid_review = (
                type(review_after_seconds) in {int, float}
                and math.isfinite(float(review_after_seconds))
                and review_after_seconds > 0
            )
        except OverflowError:
            valid_review = False
        if not valid_review:
            raise LatencyError("Review interval must be a positive finite number of seconds")
    request = {
        "mission_id": mission_id,
        "run_id": run_id,
        "actor_ref": actor_ref,
        "task_ref": task_ref,
        "response_ref": response_ref,
        "category": category,
        "basis": basis.strip(),
        "expected_version": expected_version,
    }
    if review_after_seconds is not None:
        request["review_after_seconds"] = review_after_seconds

    def save(tx: Transaction) -> dict:
        run = tx.get("mission_run", run_id)
        actor = require_ref(tx, mission_id, actor_ref)
        task = require_ref(tx, mission_id, task_ref)
        response = require_ref(tx, mission_id, response_ref)
        if run is None or run["body"].get("mission_id") != mission_id:
            raise LatencyError("Mission run is unavailable")
        if (
            actor["kind"] != "agent_instance"
            or actor["body"].get("run_id") != run_id
            or task["kind"] != "board_proposal"
            or tx.get("board_proposal", task["id"])["version"] != task["version"]
            or response["kind"] != "model_response_receipt"
            or response["body"].get("status") not in {"complete", "tool_requested"}
            or response["body"].get("task_id") != task["id"]
        ):
            raise LatencyError("Expectation needs a current task and matching model response")
        call_ref = response["body"].get("call_ref")
        call = require_ref(tx, mission_id, call_ref)
        if (
            call["kind"] != "agent_call"
            or call["body"].get("instance_id") != actor["id"]
            or call["body"].get("task_id") != task["id"]
        ):
            raise LatencyError("Response call belongs to another peer or task")
        record_id = _expectation_id(mission_id, actor["id"], task["id"])
        previous = tx.get("mission_latency_expectation", record_id)
        if previous and previous["body"].get("run_id") != run_id:
            raise LatencyError("Expectation ID belongs to another run")
        if (previous["version"] if previous else 0) != expected_version:
            raise StateConflict("Saved latency expectation version changed")
        if previous and previous["body"].get("response_ref") == response_ref:
            raise LatencyError("A revised expectation needs a new model response")
        body = {
            "schema": "ora.mission_latency_expectation.v1",
            "mission_id": mission_id,
            "run_id": run_id,
            "actor_ref": ref(actor),
            "task_ref": ref(task),
            "response_ref": ref(response),
            "category": category,
            "basis": basis.strip(),
            "authorship": "model_response_selected",
            "recorded_at": store.now(),
        }
        if review_after_seconds is not None:
            body["review_after_seconds"] = review_after_seconds
        saved = tx.put(
            "mission_latency_expectation", record_id, body, expected_version=expected_version
        )
        for target, relation in ((actor, "about_actor"), (task, "about_task"), (response, "input")):
            tx.link(saved, target, relation)
        if previous:
            tx.link(saved, previous, "derived_from")
        return {"expectation_ref": ref(saved)}

    return store.transact(operation_id, request, save, mission_id=mission_id)["expectation_ref"]


def read_expectation(
    store: StateStore,
    mission_id: str,
    actor_id: str,
    task_id: str,
) -> dict | None:
    """Return the saved estimate plus task-version diagnostics, if any.

    Ordinary proposal progress can change versions without changing the work.
    Mismatch and changed-meaning flags inform the viewer; neither hides the
    model's estimate or gates execution.
    """
    db = store._connect()
    try:
        db.execute("BEGIN")
        tx = Transaction(store, db)
        record = tx.get(
            "mission_latency_expectation", _expectation_id(mission_id, actor_id, task_id)
        )
        if record is None or record["body"].get("mission_id") != mission_id:
            return None
        task = tx.get("board_proposal", task_id)
        authored_task = tx.get(**record["body"]["task_ref"])
        fields = ("objective", "expected_artifact", "depends_on", "branch_of")
        meaning_changed = (
            any(authored_task["body"].get(key) != task["body"].get(key) for key in fields)
            if authored_task and task
            else None
        )
        return {
            **record,
            "current_task_ref": ref(task) if task else None,
            "task_version_mismatch": task is None or record["body"]["task_ref"] != ref(task),
            "task_meaning_changed": meaning_changed,
        }
    finally:
        db.close()


def _review_id(expectation_ref: dict) -> str:
    digest = hashlib.sha256(canonical(expectation_ref).encode()).hexdigest()
    return f"mission-latency-review:{digest}"


def read_due_review(
    store: StateStore, mission_id: str, actor_id: str, task_id: str
) -> dict | None:
    """Read only the current, same-task expectation's saved review."""
    expectation = read_expectation(store, mission_id, actor_id, task_id)
    if expectation is None or expectation["task_meaning_changed"] is not False:
        return None
    review = store.get("mission_host_diagnostic", _review_id(ref(expectation)))
    if review is None:
        return None
    body = review["body"]
    if (
        body.get("category") != "latency_review_due"
        or body.get("mission_id") != mission_id
        or body.get("run_id") != expectation["body"].get("run_id")
        or body.get("actor_id") != actor_id
        or body.get("task_id") != task_id
        or body.get("expectation_ref") != ref(expectation)
    ):
        return None
    run = store.get("mission_run", body["run_id"])
    task = store.get("board_proposal", task_id)
    control_ref = body.get("control_ref")
    control = (
        store.get("mission_task_control", control_ref["id"])
        if isinstance(control_ref, dict) and control_ref.get("kind") == "mission_task_control"
        else None
    )
    if (
        run is None or run["body"].get("status") == "cancelled"
        or (run["body"].get("status") == "completed"
            and control is not None
            and control["body"].get("scope_status") != "continued")
        or task is None or task["body"].get("status") != "open"
        or control is None
        or control["body"].get("mission_id") != mission_id
        or control["body"].get("run_id") != body["run_id"]
        or control["body"].get("actor_id") != actor_id
        or control["body"].get("task_id") != task_id
        or control["body"].get("phase") in {"done", "deferred"}
        or control["body"].get("operator_credential_hold")
    ):
        return None
    return review


def read_review_delivery_response(store: StateStore, review: dict) -> dict | None:
    """Find a successful exact model exchange that included this review.

    A prepared input, dispatch marker, omitted reference or failed response is
    not delivery. The returned receipt says nothing about scientific uptake.
    """
    if (
        not isinstance(review, dict)
        or review.get("kind") != "mission_host_diagnostic"
        or review.get("body", {}).get("category") != "latency_review_due"
    ):
        return None
    review_ref = ref(review)
    body = review["body"]

    def exact(pointer: object, kind: str) -> bool:
        return (
            type(pointer) is dict and set(pointer) == {"kind", "id", "version"}
            and pointer["kind"] == kind and isinstance(pointer["id"], str)
            and type(pointer["version"]) is int and pointer["version"] > 0
        )

    db = store._connect()
    try:
        rows = db.execute(
            "SELECT source_id,source_version FROM links "
            "WHERE target_kind=? AND target_id=? AND target_version=? "
            "AND source_kind='context_inclusion' AND relation='input' "
            "ORDER BY source_id,source_version",
            (review_ref["kind"], review_ref["id"], review_ref["version"]),
        ).fetchall()
        tx = Transaction(store, db)
        for row in rows:
            inclusion = tx.get("context_inclusion", row["source_id"], row["source_version"])
            if inclusion is None:
                continue
            selected = inclusion["body"]
            if (
                selected.get("mission_id") != body.get("mission_id")
                or not isinstance(selected.get("included_refs"), list)
                or review_ref not in selected["included_refs"]
                or not isinstance(selected.get("omitted"), list)
                or any(isinstance(item, dict) and
                       (item.get("ref") == review_ref or item.get("fetch") == review_ref)
                       for item in selected["omitted"])
                or not exact(selected.get("intent_ref"), "model_request_intent")
                or not exact(selected.get("call_ref"), "agent_call")
                or not exact(selected.get("task_ref"), "board_proposal")
                or selected["task_ref"].get("id") != body.get("task_id")
                or not isinstance(selected.get("request_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", selected["request_sha256"]) is None
            ):
                continue
            intent = tx.get(**selected["intent_ref"])
            call = tx.get(**selected["call_ref"])
            task = tx.get(**selected["task_ref"])
            if (
                intent is None or intent["kind"] != "model_request_intent"
                or call is None or call["kind"] != "agent_call"
                or task is None or task["body"].get("mission_id") != body.get("mission_id")
                or ref(task) != selected["task_ref"]
                or ref(call) != selected["call_ref"]
                or ref(intent) != selected["intent_ref"]
                or call["body"].get("mission_id") != body.get("mission_id")
                or call["body"].get("instance_id") != body.get("actor_id")
                or call["body"].get("task_id") != body.get("task_id")
                or intent["body"].get("mission_id") != body.get("mission_id")
                or intent["body"].get("task_id") != body.get("task_id")
                or intent["body"].get("call_ref") != selected["call_ref"]
                or intent["body"].get("request_sha256") != selected.get("request_sha256")
            ):
                continue
            response = tx.get("model_response_receipt", f"model-response:{intent['id']}")
            if (
                response is not None
                and response["body"].get("status") in {"complete", "tool_requested"}
                and (response["body"].get("http_status") is None or
                     (type(response["body"]["http_status"]) is int
                      and 200 <= response["body"]["http_status"] < 300))
                and response["body"].get("mission_id") == body.get("mission_id")
                and response["body"].get("task_id") == body.get("task_id")
                and response["body"].get("call_ref") == selected["call_ref"]
                and response["body"].get("intent_ref") == selected["intent_ref"]
                and response["body"].get("request_sha256") == selected["request_sha256"]
            ):
                return ref(response)
        return None
    finally:
        db.close()


def record_due_latency_reviews(
    store: StateStore, run: dict, controls: list[dict]
) -> tuple[bool, float | None]:
    """Save one neutral review per due estimate without changing work state."""
    if run["body"].get("status") == "cancelled":
        return False, None
    mission_id = run["body"]["mission_id"]
    now = store.clock()
    changed, next_due = False, None
    for control in controls:
        body = control["body"]
        if (body.get("mission_id") != mission_id or body.get("run_id") != run["id"]
                or body.get("phase") in {"done", "deferred"}
                or (run["body"].get("status") == "completed"
                    and body.get("scope_status") != "continued")
                or body.get("operator_credential_hold")):
            continue
        expectation = read_expectation(store, mission_id, body["actor_id"], body["task_id"])
        if (expectation is None or expectation["task_meaning_changed"] is not False
                or "review_after_seconds" not in expectation["body"]):
            continue
        expectation_ref = ref(expectation)
        review_id = _review_id(expectation_ref)
        if store.get("mission_host_diagnostic", review_id):
            continue
        start = _stamp(expectation["body"].get("recorded_at"))
        if start is None:
            continue
        due = start + expectation["body"]["review_after_seconds"]
        if now < due:
            next_due = due if next_due is None else min(next_due, due)
            continue

        def save(tx: Transaction) -> dict:
            existing = tx.get("mission_host_diagnostic", review_id)
            if existing:
                return {"created": False}
            current_run = tx.get("mission_run", run["id"])
            current_control = tx.get("mission_task_control", control["id"])
            current_expectation = tx.get("mission_latency_expectation", expectation["id"])
            task = tx.get("board_proposal", body["task_id"])
            authored_task = tx.get(**expectation["body"]["task_ref"])
            if (current_run is None or current_run["body"].get("status") == "cancelled"
                    or current_control is None or current_expectation is None
                    or ref(current_expectation) != expectation_ref
                    or current_expectation["body"] != expectation["body"]
                    or current_control["body"].get("mission_id") != mission_id
                    or current_control["body"].get("run_id") != run["id"]
                    or current_control["body"].get("actor_id") != body["actor_id"]
                    or current_control["body"].get("task_id") != body["task_id"]
                    or current_control["body"].get("phase") in {"done", "deferred"}
                    or (current_run["body"].get("status") == "completed"
                        and current_control["body"].get("scope_status") != "continued")
                    or current_control["body"].get("operator_credential_hold")
                    or task is None or task["body"].get("status") != "open"
                    or authored_task is None
                    or any(task["body"].get(key) != authored_task["body"].get(key)
                           for key in ("objective", "expected_artifact", "depends_on", "branch_of"))):
                raise StateConflict("Latency review inputs changed before recording")
            current = current_control["body"]
            cap_ref = current.get("capacity_wait_ref")
            cap = tx.get(**cap_ref) if isinstance(cap_ref, dict) else None
            cap_reason = (
                cap["body"].get("wait_reason")
                if cap and cap["body"].get("mission_id") == mission_id
                and cap["body"].get("task_id") == current["task_id"] else None
            )
            wait_kind = (
                "provider_or_quota_wait" if cap_reason in {"quota", "provider", "retry"}
                or current.get("phase") == "retry_at" else
                "other_wait" if current.get("phase") == "waiting" else None
            )
            observed = store.clock()
            message = (
                "The peer-selected review interval elapsed during a recorded provider or quota wait. "
                "Inspect the wait before choosing a continuation."
                if wait_kind == "provider_or_quota_wait" else
                "The peer-selected review interval elapsed. Inspect current work and waits "
                "before choosing whether to revise the estimate or continue."
            )
            saved = tx.put("mission_host_diagnostic", review_id, {
                "schema": "ora.mission_host_diagnostic.v1",
                "category": "latency_review_due",
                "error_class": "LatencyReviewDue",
                "mission_id": mission_id,
                "run_id": run["id"],
                "actor_id": current["actor_id"],
                "task_id": current["task_id"],
                "expectation_ref": expectation_ref,
                "control_ref": ref(current_control),
                "recorded_at": store.now(),
                "review_after_seconds": expectation["body"]["review_after_seconds"],
                "elapsed_since_expectation_seconds": max(0, observed - start),
                "phase": current.get("phase"),
                "waiting_reason": current.get("waiting_reason"),
                "wait_kind": wait_kind,
                "capacity_wait_until": current.get("capacity_wait_until"),
                "last_progress_at": current.get("last_progress_at"),
                "message": message,
            })
            tx.link(saved, expectation, "input")
            tx.link(saved, task, "about_task")
            return {"created": True}

        try:
            result = store.transact(
                review_id, {"expectation_ref": expectation_ref}, save,
                mission_id=mission_id,
            )
        except StateConflict:
            continue
        changed = result["created"] or changed
    return changed, next_due


def _stamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


def _duration(start: float | None, end: float | None) -> float | None:
    if start is None or end is None or end < start:
        return None
    return end - start


def _union_seconds(intervals: list[tuple[float, float]]) -> float:
    total = 0.0
    end = -math.inf
    for start, stop in sorted(intervals):
        total += max(0.0, stop - max(start, end))
        end = max(end, stop)
    return total


def inspect_latency(
    store: StateStore, run_id: str, *, task_id: str | None = None, now: float | None = None
) -> dict:
    """Read mission or task timing from one saved snapshot.

    The first board outcome is an accepted contribution, not proof of scientific
    usefulness. Completed model-attempt intervals support wall-union and summed
    activity separately. Capacity wait counters are operation sums and may
    overlap across peers. Unrecorded peer wait remains None.
    """
    identifier(run_id)
    if task_id is not None:
        identifier(task_id)
    observed = store.clock() if now is None else now
    if type(observed) not in {int, float} or not math.isfinite(observed):
        raise LatencyError("Observation time must be finite")
    db = store._connect()
    try:
        db.execute("BEGIN")
        row = db.execute(
            "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) "
            "WHERE r.kind='mission_run' AND r.id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise LatencyError("Mission run is unavailable")
        run = json.loads(row["body"])
        mission_id = run["mission_id"]
        task = task_head = None
        if task_id is not None:
            task = db.execute(
                "SELECT * FROM records WHERE kind='board_proposal' AND id=? AND version=1",
                (task_id,),
            ).fetchone()
            if task is None or json.loads(task["body"]).get("mission_id") != mission_id:
                raise LatencyError("Task is unavailable in this mission")
            task_head = db.execute(
                "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) "
                "WHERE r.kind='board_proposal' AND r.id=?",
                (task_id,),
            ).fetchone()
        heads = {}
        for kind in ("board_outcome", "capacity_operation"):
            query = (
                "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) "
                "WHERE r.kind=? AND json_extract(r.body,'$.mission_id')=?"
            )
            params: tuple = (kind, mission_id)
            if task_id is not None:
                column = "task_id" if kind == "capacity_operation" else "proposal_ref.id"
                query += f" AND json_extract(r.body,'$.{column}')=?"
                params += (task_id,)
            heads[kind] = list(db.execute(query + " ORDER BY r.id", params))
        db.rollback()
    finally:
        db.close()

    start_at = task["created_at"] if task is not None else run.get("created_at")
    start = _stamp(start_at)
    accepted = min(heads["board_outcome"], key=lambda item: item["created_at"], default=None)
    first_at = accepted["created_at"] if accepted else None
    first = _stamp(first_at)
    through_at = datetime.fromtimestamp(observed, timezone.utc).isoformat()
    if task_head is not None and json.loads(task_head["body"]).get("status") == "completed":
        outcome_ref = json.loads(task_head["body"]).get("outcome_ref")
        completed_outcome = next(
            (
                item
                for item in heads["board_outcome"]
                if outcome_ref == {key: item[key] for key in ("kind", "id", "version")}
            ),
            None,
        )
        through_at = completed_outcome["created_at"] if completed_outcome else None
    elif task_id is None and run.get("status") == "completed":
        through_at = run.get("completed_at")
    through = _stamp(through_at)
    intervals: list[tuple[float, float]] = []
    incomplete_attempts = 0
    waits: dict[str, float | None] = {key: None for key in _WAITS}
    wait_incomplete: set[str] = set()
    operations = []
    for item in heads["capacity_operation"]:
        body = json.loads(item["body"])
        pointer = {key: item[key] for key in ("kind", "id", "version")}
        operations.append(pointer)
        if body.get("kind") == "model":
            for attempt in body.get("attempts", []):
                began, ended = attempt.get("at"), attempt.get("completed_at")
                if (
                    type(began) in {int, float}
                    and type(ended) in {int, float}
                    and math.isfinite(began)
                    and math.isfinite(ended)
                    and ended >= began
                ):
                    intervals.append((began, ended))
                else:
                    incomplete_attempts += 1
        for key in _WAITS:
            value = body.get(key)
            if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                wait_incomplete.add(key)
                continue
            waits[key] = (waits[key] or 0) + value
        if body.get("phase") == "queued":
            began = body.get("queued_at")
            if type(began) in {int, float} and math.isfinite(began) and began <= observed:
                waits["queue_wait_seconds"] = (waits["queue_wait_seconds"] or 0) + observed - began
            else:
                wait_incomplete.add("queue_wait_seconds")
        elif body.get("phase") == "waiting":
            reason = body.get("wait_reason")
            key = {
                "quota": "quota_wait_seconds",
                "provider": "provider_wait_seconds",
                "retry": "retry_wait_seconds",
            }.get(reason)
            began = body.get("wait_started_at")
            if key and type(began) in {int, float} and math.isfinite(began) and began <= observed:
                waits[key] = (waits[key] or 0) + observed - began
            elif key:
                wait_incomplete.add(key)
    waits = {key: None if key in wait_incomplete else value for key, value in waits.items()}
    elapsed = _duration(start, through)
    to_first = _duration(start, first)
    return {
        "schema": "ora.mission_latency_observation.v1",
        "mission_id": mission_id,
        "run_ref": {"kind": "mission_run", "id": run_id, "version": row["version"]},
        "task_id": task_id,
        "observed_at": datetime.fromtimestamp(observed, timezone.utc).isoformat(),
        "started_at": start_at if start is not None else None,
        "elapsed_through_at": through_at if through is not None else None,
        "first_accepted_contribution_at": first_at if first is not None else None,
        "first_accepted_outcome_ref": {key: accepted[key] for key in ("kind", "id", "version")}
        if accepted
        else None,
        "elapsed_seconds": elapsed,
        "seconds_to_first_accepted": to_first,
        "model_active_wall_seconds": _union_seconds(intervals) if intervals else None,
        "model_active_operation_sum_seconds": sum(stop - begin for begin, stop in intervals)
        if intervals
        else None,
        "model_attempts_without_closed_interval": incomplete_attempts,
        "capacity_wait_operation_sums_seconds": waits,
        "peer_wait_seconds": None,
        "capacity_refs": operations,
        "interpretation": "Accepted outcomes are recorded contributions, not scientific usefulness. Model activity uses closed intervals; open attempts remain unknown. Capacity wait sums can overlap across peers and are not mission elapsed time. Peer request lifetime is not peer waiting time.",
    }
