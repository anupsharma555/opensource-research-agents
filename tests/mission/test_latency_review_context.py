"""Due reviews stay task-scoped, pageable and separate from host failures."""

import hashlib
import json

import pytest

from ora.agents.capabilities import content_hash
from ora.coordination.board import ref
from ora.mission.context import ContextError, WorkingContext
from ora.mission.latency import record_due_latency_reviews, save_expectation
from ora.mission.projection import _host_diagnostic_view
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack, fixture


def _due_review(tmp_path):
    clock = [100.0]
    store = StateStore(tmp_path, clock=lambda: clock[0])
    call, task = fixture(store)

    def seed(tx):
        actor = tx.get("agent_instance", "peer")
        actor = tx.put("agent_instance", "peer", {**actor["body"], "run_id": "run"},
                       expected_version=actor["version"])
        run = tx.put("mission_run", "run", {
            "mission_id": "m", "run_id": "run", "status": "active",
            "created_at": store.now(),
        })
        control = tx.put("mission_task_control", "control", {
            "mission_id": "m", "run_id": "run", "actor_id": "peer", "task_id": "task",
            "phase": "ready_model", "proposal_ref": task,
        })
        response = tx.put("model_response_receipt", "response", {
            "mission_id": "m", "task_id": "task", "call_ref": call, "status": "complete",
        })
        return {"actor": ref(actor), "run": ref(run), "control": ref(control),
                "response": ref(response)}

    pointers = store.transact("latency-context-seed", {}, seed)
    save_expectation(store, "latency-context-expectation", mission_id="m", run_id="run",
                     actor_ref=pointers["actor"], task_ref=task,
                     response_ref=pointers["response"], category="medium",
                     basis="Review the retained work after this interval", review_after_seconds=10)
    clock[0] = 110.0
    assert record_due_latency_reviews(
        store, store.get(**pointers["run"]), [store.get(**pointers["control"])],
    ) == (True, None)
    review = store.records("mission_host_diagnostic")[0]
    return store, call, task, review


def test_current_review_enters_only_its_peers_task_and_revised_scope_hides_it(tmp_path):
    store, call, task, review = _due_review(tmp_path)
    context = WorkingContext(store)
    pack = context.assemble(call, task)
    details = json.loads(pack.messages[-1]["content"])["details"]
    assert [item["ref"] for item in details if item["type"] == "latency_review"] == [ref(review)]
    assert ref(review) in pack.included_refs

    def another_actor(tx):
        old = tx.get(**call)
        original_config = tx.get("agent_configuration", old["body"]["configuration_id"])
        config_body = {key: value for key, value in original_config["body"].items()
                       if key != "configuration_sha256"}
        config_body["instance_id"] = "sender"
        config_body["configuration_sha256"] = content_hash(config_body)
        config = tx.put("agent_configuration", "foreign-peer-config", config_body)
        return ref(tx.put("agent_call", "foreign-peer-call", {
            **old["body"], "instance_id": "sender",
            "configuration_id": config["id"],
            "configuration_sha256": config_body["configuration_sha256"],
        }))

    # A different correctly configured peer does not receive this peer's review.
    foreign = store.transact("foreign-peer-call", {}, another_actor)
    other_pack = context.assemble(foreign, task)
    assert not any(item["type"] == "latency_review"
                   for item in json.loads(other_pack.messages[-1]["content"])["details"])

    def revise(tx):
        old = tx.get(**task)
        return ref(tx.put("board_proposal", task["id"], {
            **old["body"], "objective": "Inspect a different research question",
        }, expected_version=task["version"]))

    changed_task = store.transact("new-question", {}, revise)
    revised = context.assemble(call, changed_task)
    assert not any(item["type"] == "latency_review"
                   for item in json.loads(revised.messages[-1]["content"])["details"])
    assert store.get(**ref(review)) == review
    view = _host_diagnostic_view(store, review, "m")
    assert view["status"] == "historical_review"


def test_omitted_review_has_exact_bounded_reader_and_does_not_allow_other_diagnostics(tmp_path):
    store, call, task, review = _due_review(tmp_path)
    context = WorkingContext(store)
    full = context.assemble(call, task)
    view = json.loads(full.messages[-1]["content"])
    core = {**view, "details": [], "omitted": {"count": 1}}
    allowance = len(canonical([{"role": "user", "content": canonical(core)}])) + 200
    compact = context.assemble(call, task, allowance=allowance, estimate_tokens=len)
    assert compact.omitted[0]["fetch"] == ref(review)
    assert ref(review) not in compact.included_refs
    inclusion = bind_no_source_pack(store, context, compact, "latency-omitted")
    page = context.read_omitted_page(mission_id="m", actor_id="peer", task_id="task",
                                     inclusion_ref=inclusion, cursor=0, limit=1)
    assert page["returned_count"] == 1
    assert page["items"][0]["summary"]["category"] == "latency_review_due"
    assert "peer-selected review interval" in page["items"][0]["summary"]["message"]["text"]
    window = context.read_omitted_record(
        mission_id="m", actor_id="peer", task_id="task", inclusion_ref=inclusion,
        pointer=ref(review), offset=0, limit=2000,
    )
    assert "peer-selected review interval" in window["text"]
    assert json.loads(window["text"])["expectation_ref"] == review["body"]["expectation_ref"]

    # Even a malformed inclusion cannot disclose an unrelated private host error.
    def unrelated(tx):
        error = tx.put("mission_host_diagnostic", "unrelated", {
            "mission_id": "m", "actor_id": "sender", "task_id": "task",
            "category": "host_error", "message": "PRIVATE_UNRELATED_ERROR",
        })
        old = tx.get(**inclusion)
        forged = tx.put("context_inclusion", "forged", {
            **old["body"], "omitted": [{"type": "latency_review", "ref": ref(error),
                                       "fetch": ref(error)}],
        })
        return {"error": ref(error), "inclusion": ref(forged)}

    unrelated_refs = store.transact("unrelated-error", {}, unrelated)
    with pytest.raises(ContextError, match="outside this peer's scope"):
        context.read_omitted_record(mission_id="m", actor_id="peer", task_id="task",
                                    inclusion_ref=unrelated_refs["inclusion"],
                                    pointer=unrelated_refs["error"], offset=0, limit=2000)


def test_snapshot_diagnostic_labels_a_review_without_calling_it_a_failure(tmp_path):
    store, _, _, review = _due_review(tmp_path)
    view = _host_diagnostic_view(store, review, "m")
    assert view["taskId"] == "task"
    assert view["kind"] == "latency_review"
    assert view["status"] == "review_due"
    assert view["occurredAt"] == review["body"]["recorded_at"]
    error = {"id": "actual-error", "body": {
        "error_class": "StateError", "message": "A host step needs inspection",
        "recorded_at": store.now(),
    }}
    assert _host_diagnostic_view(store, error, "m")["status"] == "host_error"


def _prepare_review_input(store, template_call, task, name):
    """Bind a real context pack to a saved input without invoking any provider."""
    def new_call(tx):
        return ref(tx.put("agent_call", name, tx.get(**template_call)["body"]))

    call = store.transact(f"call-{name}", {}, new_call)
    context = WorkingContext(store)
    pack = context.assemble(call, task)
    config = store.get(**pack.configuration_ref)
    encoded = canonical({"messages": [
        {"role": "developer", "content": config["body"]["instructions"]}, *pack.messages,
    ]}).encode()
    blob = store.put_blob(encoded)

    def input_record(tx):
        return ref(tx.put("model_request_intent", f"model-intent:{name}:1", {
            "mission_id": "m", "task_id": task["id"], "call_ref": call,
            "configuration_ref": pack.configuration_ref, "source_refs": [],
            "request_sha256": hashlib.sha256(encoded).hexdigest(), "blob": blob,
        }))

    intent = store.transact(f"input-{name}", {}, input_record)
    inclusion = context.save_inclusion(f"inclusion-{name}", pack, intent)
    return pack, store.get(**inclusion)


def test_notice_retries_after_failed_call_but_is_not_repeated_after_success(tmp_path):
    store, call, task, review = _due_review(tmp_path)
    failed_pack, failed_inclusion = _prepare_review_input(store, call, task, "failed-call")
    assert ref(review) in failed_pack.included_refs
    assert _host_diagnostic_view(store, review, "m")["status"] == "review_due"

    def response_for(inclusion, status):
        body = inclusion["body"]

        def save(tx):
            return ref(tx.put("model_response_receipt",
                              f"model-response:{body['intent_ref']['id']}", {
                "mission_id": "m", "task_id": "task", "call_ref": body["call_ref"],
                "intent_ref": body["intent_ref"], "request_sha256": body["request_sha256"],
                "status": status,
            }))

        return store.transact(f"response-{inclusion['id']}", {}, save)

    response_for(failed_inclusion, "provider_error")
    retry_pack, retry_inclusion = _prepare_review_input(store, call, task, "retry-call")
    assert ref(review) in retry_pack.included_refs
    assert _host_diagnostic_view(store, review, "m")["status"] == "review_due"
    response_for(retry_inclusion, "tool_requested")

    # Reopening retains exact delivery evidence, without inventing an agent's
    # review decision or repeating the notice on every subsequent input.
    reopened = StateStore(tmp_path)
    next_pack, _ = _prepare_review_input(reopened, call, task, "next-call")
    assert ref(review) not in next_pack.included_refs
    assert not any(item["type"] == "latency_review" for item in next_pack.omitted)
    view = _host_diagnostic_view(reopened, review, "m")
    assert view["status"] == "notice_delivered"
    assert "no review decision is inferred" in view["message"]
    assert reopened.get(**ref(review)) == review
