"""Peer-selected review times create neutral, durable task diagnostics."""

from __future__ import annotations

import hashlib
import math

import pytest

from ora.agents.capabilities import validate_arguments
from ora.coordination.board import ref
from ora.mission.latency import (
    LatencyError,
    read_due_review,
    read_review_delivery_response,
    record_due_latency_reviews,
    save_expectation,
)
from ora.mission.observation_tools import OBSERVATION_TOOL_SCHEMAS, record_latency_choice
from ora.state import StateStore


def fixture(store: StateStore) -> tuple[dict, dict]:
    def save(tx):
        run = tx.put("mission_run", "run", {
            "mission_id": "m", "status": "active", "created_at": store.now(),
        })
        inputs = {}
        for name in ("a", "b"):
            actor = tx.put("agent_instance", f"actor-{name}", {
                "mission_id": "m", "run_id": "run",
            })
            task = tx.put("board_proposal", f"task-{name}", {
                "mission_id": "m", "status": "open", "objective": f"Review {name}",
                "expected_artifact": "A synthetic answer",
            })
            call = tx.put("agent_call", f"call-{name}", {
                "mission_id": "m", "instance_id": actor["id"], "task_id": task["id"],
            })
            response = tx.put("model_response_receipt", f"response-{name}", {
                "mission_id": "m", "status": "tool_requested", "task_id": task["id"],
                "call_ref": ref(call),
            })
            control = tx.put("mission_task_control", f"control-{name}", {
                "mission_id": "m", "run_id": "run", "actor_id": actor["id"],
                "task_id": task["id"], "phase": "ready_model",
                "proposal_ref": ref(task), "response_ref": ref(response),
                "last_progress_at": store.now(),
            })
            inputs[name] = {
                "actor": ref(actor), "task": ref(task), "response": ref(response),
                "control": ref(control),
            }
        return {"run": ref(run), "inputs": inputs}

    saved = store.transact("seed-review", {}, save, mission_id="m")
    return store.get(**saved["run"]), saved["inputs"]


def estimate(store: StateStore, inputs: dict, name: str, *, operation: str,
             seconds: float | None, version: int = 0, response: dict | None = None) -> dict:
    item = inputs[name]
    return save_expectation(
        store, operation, mission_id="m", run_id="run",
        actor_ref=item["actor"], task_ref=item["task"],
        response_ref=response or item["response"], category="medium",
        basis="Review the saved evidence", expected_version=version,
        review_after_seconds=seconds,
    )


def controls(store: StateStore) -> list[dict]:
    return [store.get("mission_task_control", f"control-{name}") for name in ("a", "b")]


def revise(store: StateStore, kind: str, record_id: str, **changes) -> dict:
    def save(tx):
        prior = tx.get(kind, record_id)
        return ref(tx.put(kind, record_id, {
            **prior["body"], **changes,
        }, expected_version=prior["version"]))

    return store.transact(
        f"revise-{kind}-{record_id}-{store.get(kind, record_id)['version']}",
        changes, save, mission_id="m",
    )


def next_response(store: StateStore, name: str) -> dict:
    def save(tx):
        call = tx.put("agent_call", f"new-call-{name}", {
            "mission_id": "m", "instance_id": f"actor-{name}",
            "task_id": f"task-{name}",
        })
        response = tx.put("model_response_receipt", f"new-response-{name}", {
            "mission_id": "m", "status": "tool_requested",
            "task_id": f"task-{name}", "call_ref": ref(call),
        })
        return ref(response)

    return store.transact(f"next-response-{name}", {}, save, mission_id="m")


def test_due_review_is_task_local_durable_and_does_not_advance_work(tmp_path) -> None:
    now = [100.0]
    store = StateStore(tmp_path, clock=lambda: now[0])
    run, inputs = fixture(store)
    estimate(store, inputs, "a", operation="estimate-a", seconds=10)
    estimate(store, inputs, "b", operation="estimate-b", seconds=None)
    before = controls(store)
    assert record_due_latency_reviews(store, run, before) == (False, 110.0)
    now[0] = 109.0
    assert record_due_latency_reviews(store, run, before) == (False, 110.0)
    assert not store.records("mission_host_diagnostic")
    now[0] = 110.0
    assert record_due_latency_reviews(store, run, before) == (True, None)
    review = read_due_review(store, "m", "actor-a", "task-a")
    assert review["body"]["category"] == "latency_review_due"
    assert review["body"]["expectation_ref"]["version"] == 1
    assert review["body"]["elapsed_since_expectation_seconds"] == 10
    assert review["body"]["phase"] == "ready_model"
    assert read_due_review(store, "m", "actor-b", "task-b") is None
    assert controls(store) == before
    assert store.records("agent_call") and len(store.records("agent_call")) == 2
    assert store.records("capacity_operation") == []

    reopened = StateStore(tmp_path, clock=lambda: now[0])
    assert record_due_latency_reviews(reopened, reopened.get("mission_run", "run"),
                                      controls(reopened)) == (False, None)
    assert read_due_review(reopened, "m", "actor-a", "task-a") == review
    assert len(reopened.records("mission_host_diagnostic")) == 1


def test_revised_expectation_can_schedule_one_new_review_without_replaying_old(tmp_path) -> None:
    now = [100.0]
    store = StateStore(tmp_path, clock=lambda: now[0])
    run, inputs = fixture(store)
    first = estimate(store, inputs, "a", operation="estimate-a", seconds=2)
    now[0] = 102.0
    assert record_due_latency_reviews(store, run, controls(store))[0]
    old_review = read_due_review(store, "m", "actor-a", "task-a")
    now[0] = 103.0
    second = estimate(store, inputs, "a", operation="estimate-a-2", seconds=5,
                      version=1, response=next_response(store, "a"))
    assert second["version"] == 2
    assert read_due_review(store, "m", "actor-a", "task-a") is None
    assert record_due_latency_reviews(store, run, controls(store)) == (False, 108.0)
    now[0] = 108.0
    assert record_due_latency_reviews(store, run, controls(store)) == (True, None)
    current = read_due_review(store, "m", "actor-a", "task-a")
    assert current["body"]["expectation_ref"] == second
    assert current != old_review
    assert store.get(**first)["body"]["review_after_seconds"] == 2
    assert len(store.records("mission_host_diagnostic")) == 2


def test_known_wait_is_named_without_claiming_model_stall(tmp_path) -> None:
    now = [100.0]
    store = StateStore(tmp_path, clock=lambda: now[0])
    run, inputs = fixture(store)
    estimate(store, inputs, "a", operation="estimate-a", seconds=3)
    cap = store.transact("quota-wait", {}, lambda tx: ref(tx.put(
        "capacity_operation", "quota-a", {
            "mission_id": "m", "task_id": "task-a", "phase": "waiting",
            "wait_reason": "quota",
        }
    )), mission_id="m")
    revise(store, "mission_task_control", "control-a", phase="waiting",
           waiting_reason="Provider daily token wait", capacity_wait_ref=cap,
           capacity_wait_until=300.0)
    now[0] = 103.0
    assert record_due_latency_reviews(store, run, controls(store))[0]
    review = read_due_review(store, "m", "actor-a", "task-a")
    assert review["body"]["wait_kind"] == "provider_or_quota_wait"
    assert review["body"]["waiting_reason"] == "Provider daily token wait"
    assert "stall" not in review["body"]["message"].lower()
    assert controls(store)[0]["body"]["phase"] == "waiting"


@pytest.mark.parametrize("change", ["done", "deferred", "operator", "cancelled", "meaning"])
def test_terminal_hold_and_changed_scope_do_not_create_active_review(tmp_path, change) -> None:
    now = [100.0]
    store = StateStore(tmp_path, clock=lambda: now[0])
    run, inputs = fixture(store)
    estimate(store, inputs, "a", operation="estimate-a", seconds=2)
    if change in {"done", "deferred"}:
        revise(store, "mission_task_control", "control-a", phase=change)
    elif change == "operator":
        revise(store, "mission_task_control", "control-a",
               operator_credential_hold={"phase": "ready_model"})
    elif change == "cancelled":
        revise(store, "mission_run", "run", status="cancelled")
    else:
        revise(store, "board_proposal", "task-a", objective="Different question")
    now[0] = 102.0
    assert record_due_latency_reviews(store, store.get("mission_run", "run"),
                                      controls(store)) == (False, None)
    assert read_due_review(store, "m", "actor-a", "task-a") is None


def test_review_interval_validation_and_legacy_replay(tmp_path) -> None:
    now = [100.0]
    store = StateStore(tmp_path, clock=lambda: now[0])
    run, inputs = fixture(store)
    for invalid in (0, -1, float("nan"), math.inf, True, "4", 10**1000):
        with pytest.raises(LatencyError, match="positive finite"):
            estimate(store, inputs, "a", operation=f"invalid-{invalid}", seconds=invalid)
    assert store.records("mission_latency_expectation") == []
    validate_arguments(OBSERVATION_TOOL_SCHEMAS["estimate_latency"], {
        "category": "medium", "basis": "Review", "expected_version": 0,
        "review_after_seconds": 10,
    })
    first = estimate(store, inputs, "a", operation="legacy-estimate", seconds=None)
    assert "review_after_seconds" not in store.get(**first)["body"]
    assert estimate(store, inputs, "a", operation="legacy-estimate", seconds=None) == first
    now[0] = 1000.0
    assert record_due_latency_reviews(store, run, controls(store)) == (False, None)


def test_model_facing_choice_passes_review_interval_without_changing_control(tmp_path) -> None:
    now = [100.0]
    store = StateStore(tmp_path, clock=lambda: now[0])
    _run, inputs = fixture(store)
    before = store.get(**inputs["a"]["control"])
    result = record_latency_choice(
        store, "model-review-choice", control_ref=inputs["a"]["control"],
        response_ref=inputs["a"]["response"], category="long",
        basis="The peer needs to compare two passages", expected_version=0,
        review_after_seconds=7,
    )
    assert result["status"] == "latency_expectation_saved"
    assert result["control_delta"] == {}
    assert store.get(**result["expectation_ref"])["body"]["review_after_seconds"] == 7
    assert store.get(**inputs["a"]["control"]) == before


def _saved_review(store: StateStore, inputs: dict, now: list[float], run: dict) -> dict:
    estimate(store, inputs, "a", operation="delivery-estimate", seconds=1)
    now[0] = 101.0
    assert record_due_latency_reviews(store, run, controls(store))[0]
    return read_due_review(store, "m", "actor-a", "task-a")


def _delivery_case(store: StateStore, inputs: dict, review: dict, *,
                   status: str | None = "complete", included: bool = True,
                   dispatch_only: bool = False, wrong_actor: bool = False,
                   wrong_task: bool = False, wrong_hash: bool = False,
                   wrong_intent: bool = False, wrong_call: bool = False,
                   missing_hash: bool = False, incomplete_call_ref: bool = False,
                   failed_http: bool = False) -> dict:
    digest = None if missing_hash else hashlib.sha256(b"synthetic exact request").hexdigest()
    review_ref = ref(review)

    def save(tx):
        call_ref = {"kind": "agent_call", "id": "call-b" if wrong_actor else "call-a",
                    "version": 1}
        if incomplete_call_ref:
            call_ref.pop("version")
        task_ref = inputs["b" if wrong_task else "a"]["task"]
        intent = tx.put("model_request_intent", "delivery-intent", {
            "mission_id": "m", "task_id": "task-a", "call_ref": call_ref,
            "request_sha256": digest,
        })
        inclusion = tx.put("context_inclusion", "delivery-inclusion", {
            "mission_id": "m", "task_ref": task_ref, "call_ref": call_ref,
            "intent_ref": ref(intent), "request_sha256": digest,
            "included_refs": [review_ref] if included else [],
            "omitted": [] if included else [{"type": "latency_review", "ref": review_ref,
                                               "fetch": review_ref}],
        })
        # A corrupt reverse link must not turn an omitted record into delivery.
        tx.link(inclusion, review, "input")
        if dispatch_only:
            tx.put("model_dispatch_attempt", "delivery-dispatch", {
                "mission_id": "m", "intent_ref": ref(intent), "status": "dispatched",
            })
        response_ref = None
        if status is not None:
            response = tx.put("model_response_receipt", f"model-response:{intent['id']}", {
                "mission_id": "m", "task_id": "task-a",
                "call_ref": {"kind": "agent_call", "id": "call-b", "version": 1}
                if wrong_call else call_ref,
                "intent_ref": {"kind": "model_request_intent", "id": "other-intent",
                               "version": 1} if wrong_intent else ref(intent),
                "request_sha256": hashlib.sha256(b"other request").hexdigest()
                if wrong_hash else digest,
                "status": status,
                "http_status": 500 if failed_http else 200,
            })
            response_ref = ref(response)
        return {"response_ref": response_ref}

    return store.transact("delivery-seed", {}, save, mission_id="m")


@pytest.mark.parametrize("status", ["complete", "tool_requested"])
def test_exact_successful_response_delivers_review_without_scientific_acknowledgment(
    tmp_path, status,
) -> None:
    now = [100.0]
    store = StateStore(tmp_path, clock=lambda: now[0])
    run, inputs = fixture(store)
    review = _saved_review(store, inputs, now, run)
    saved = _delivery_case(store, inputs, review, status=status)
    assert read_review_delivery_response(store, review) == saved["response_ref"]
    assert read_due_review(store, "m", "actor-a", "task-a") == review
    reopened = StateStore(tmp_path, clock=lambda: now[0])
    assert read_review_delivery_response(reopened, review) == saved["response_ref"]


@pytest.mark.parametrize("variation", [
    "prepared", "dispatched", "omitted", "failed", "wrong_actor", "wrong_task",
    "wrong_hash", "wrong_intent", "wrong_call", "missing_hash",
    "incomplete_call_ref", "failed_http",
])
def test_prepared_omitted_failed_or_foreign_records_do_not_deliver_review(
    tmp_path, variation,
) -> None:
    now = [100.0]
    store = StateStore(tmp_path, clock=lambda: now[0])
    run, inputs = fixture(store)
    review = _saved_review(store, inputs, now, run)
    options = {
        "status": None if variation in {"prepared", "dispatched"} else
        "failed" if variation == "failed" else "complete",
        "included": variation != "omitted",
        "dispatch_only": variation == "dispatched",
        "wrong_actor": variation == "wrong_actor",
        "wrong_task": variation == "wrong_task",
        "wrong_hash": variation == "wrong_hash",
        "wrong_intent": variation == "wrong_intent",
        "wrong_call": variation == "wrong_call",
        "missing_hash": variation == "missing_hash",
        "incomplete_call_ref": variation == "incomplete_call_ref",
        "failed_http": variation == "failed_http",
    }
    _delivery_case(store, inputs, review, **options)
    assert read_review_delivery_response(store, review) is None
