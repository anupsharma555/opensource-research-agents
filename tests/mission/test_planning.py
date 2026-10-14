"""Synthetic durable planning checks; no model planning or live research proof."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ora.contracts.models import RuntimePlan
from ora.coordination.board import ref
from ora.mission.memory import MemoryError, WorkingNotes
from ora.mission.planning import PlanningError, RuntimePlans, planning_note_id
from ora.state import StateConflict, StateError, StateStore


def seed(store):
    def save(tx):
        run = tx.put("mission_run", "run", {"mission_id": "m", "status": "active"})
        tx.put("agent_instance", "peer", {"mission_id": "m", "run_id": "run"})
        tx.put("agent_instance", "other", {"mission_id": "m", "run_id": "run"})
        task = tx.put("board_proposal", "task", {
            "mission_id": "m", "status": "open", "objective": "Investigate the synthetic question",
        })
        copy = tx.put("source_copy", "copy", {"mission_id": "m"})
        passage = tx.put("source_passage", "passage", {
            "mission_id": "m", "copy_ref": ref(copy), "locator": "synthetic:1",
        })
        use = tx.put("source_use", "use", {
            "copy_ref": ref(copy), "allowed_uses": ["model_input"], "policy_status": "granted",
        })
        tx.bind("source_use.copy", "copy:1", "source_use", use["id"])
        control = tx.put("mission_task_control", "control", {
            "mission_id": "m", "run_id": "run", "actor_id": "peer", "task_id": "task",
            "phase": "ready_model", "scope_status": "required", "source_refs": [ref(copy)],
            "read_window_refs": [],
        })
        request = tx.put("mail_request", "request", {
            "mission_id": "m", "requester_id": "peer", "recipient_id": "other",
            "cause_ref": ref(task), "question": "Inspect the synthetic counterevidence",
            "answer_refs": [], "status": "unanswered",
        })
        return {"run": ref(run), "task": ref(task), "control": ref(control),
                "copy": ref(copy), "passage": ref(passage), "request": ref(request)}
    return store.transact("seed", {}, save, mission_id="m")


def document(number=0, **changes):
    return {
        "goal": f"Investigate candidate {number}",
        "completion_criteria": ["Describe limitations and counterevidence"],
        "uncertainties": ["Synthetic observations require review"], "status": "active",
        "steps": [
            {"id": "read", "description": "Inspect selected evidence", "status": "todo"},
            {"id": "review", "description": "Compare interpretations", "status": "blocked",
             "depends_on": ["read"]},
        ], "linked_refs": [], "revision_reason": "Retain this optional continuation aid",
        **changes,
    }


def save(plans, selected, operation="plan-1", number=0, **changes):
    return plans.save(
        operation, mission_id="m", actor_id="peer", task_ref=selected["task"],
        run_ref=selected["run"], control_ref=selected["control"],
        plan=document(number), **changes,
    )


def read(plans, selected, **changes):
    return plans.read(
        mission_id="m", actor_id="peer", task_id="task", run_ref=selected["run"],
        control_ref=selected["control"], **changes,
    )


def update(store, kind, record_id, operation, **changes):
    def action(tx):
        old = tx.get(kind, record_id)
        return ref(tx.put(kind, record_id, {**old["body"], **changes}, expected_version=old["version"]))
    return store.transact(operation, {}, action, mission_id="m")


def test_persistence_history_exact_replay_and_unchanged_do_not_churn(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    assert read(plans, selected)["status"] == "absent"
    first = save(plans, selected)
    assert save(plans, selected) == first
    same = save(plans, selected, operation="plan-identical", expected_version=1)
    assert same["changed"] is False
    assert same["plan_ref"] == first["plan_ref"]
    assert same["writes_without_progress"] == 1
    second = save(plans, selected, operation="plan-2", number=1, expected_version=1)
    assert second["writes_without_progress"] == 2
    reopened = RuntimePlans(StateStore(tmp_path))
    assert read(reopened, selected)["note_ref"] == second["plan_ref"]
    historical = read(reopened, selected, version=1)
    assert historical["note_ref"] == first["plan_ref"]
    assert json.loads(historical["note"]["body"]["text"])["goal"] == document()["goal"]
    assert historical["note"]["body"]["category"] == "checkpoint"
    assert first["plan_ref"]["id"] == planning_note_id("m", "peer", "task")
    assert "goal" not in json.dumps(second)
    with pytest.raises(StateConflict, match="different inputs"):
        save(plans, selected, number=7)
    with pytest.raises(StateConflict, match="plan version"):
        save(plans, selected, operation="stale", number=2, expected_version=1)
    with pytest.raises(PlanningError, match="unavailable"):
        read(plans, selected, version=12)


def test_reserved_note_namespace_cannot_be_written_through_generic_notes(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    notes = WorkingNotes(store)
    for note_id in (planning_note_id("m", "peer", "task"), "runtime-plan-arbitrary"):
        with pytest.raises(MemoryError, match="reserved"):
            notes.write("generic-" + note_id, mission_id="m", actor_id="peer",
                        task_ref=selected["task"], note_id=note_id, category="checkpoint", text="bypass")
    assert store.records("working_note") == []


def test_actor_and_control_binding_cannot_read_or_save_another_plan(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    save(plans, selected)
    with pytest.raises(PlanningError, match="outside"):
        plans.read(mission_id="m", actor_id="other", task_id="task",
                   run_ref=selected["run"], control_ref=selected["control"])
    with pytest.raises(PlanningError, match="outside"):
        plans.save("other-save", mission_id="m", actor_id="other", task_ref=selected["task"],
                   run_ref=selected["run"], control_ref=selected["control"], plan=document())
    assert store.get("working_note", planning_note_id("m", "other", "task")) is None


@pytest.mark.parametrize("changes", [
    {"goal": "   "}, {"revision_reason": "\t"}, {"completion_criteria": []},
    {"actor_id": "forged"}, {"goal": "g" * 4001},
    {"steps": [{"id": "a", "description": "A", "status": "todo", "depends_on": ["missing"]}]},
    {"steps": [{"id": "a", "description": "A", "status": "todo", "depends_on": ["a"]}]},
    {"steps": [{"id": "a", "description": "A", "status": "todo"}] * 2},
    {"steps": [{"id": str(n), "description": "A", "status": "todo"} for n in range(25)]},
    {"linked_refs": [{"kind": "report", "id": "r", "version": True}]},
    {"linked_refs": [{"kind": "report", "id": "r", "version": "1"}]},
    {"linked_refs": [{"kind": "agent_instance", "id": "peer", "version": 1}]},
])
def test_canonical_plan_rejects_invalid_shapes(changes):
    with pytest.raises(ValidationError):
        RuntimePlan.model_validate(document(**changes))


def test_dependency_graph_permits_multiple_independent_scientific_paths():
    plan = RuntimePlan.model_validate(document(steps=[
        {"id": "one", "description": "Hypothesize", "status": "in_progress"},
        {"id": "two", "description": "Critique", "status": "done"},
        {"id": "three", "description": "Defer uncertain branch", "status": "deferred",
         "depends_on": ["one", "two"]},
    ], status="completed"))
    assert plan.status == "completed"


def test_document_payload_bound_is_separate_from_research_length(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    oversized = document(uncertainties=["u" * 2000] * 24,
                         completion_criteria=["c" * 2000] * 24)
    with pytest.raises(PlanningError, match="payload bound"):
        RuntimePlans(store).save("large", mission_id="m", actor_id="peer",
                                task_ref=selected["task"], run_ref=selected["run"],
                                control_ref=selected["control"], plan=oversized)
    assert store.records("working_note") == []


def test_links_resolve_exact_versions_without_rebinding_or_exposing_prose(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    linked = document(linked_refs=[selected["request"], selected["copy"]])
    result = plans.save("links", mission_id="m", actor_id="peer", task_ref=selected["task"],
                        run_ref=selected["run"], control_ref=selected["control"], plan=linked)
    saved = store.get(**result["plan_ref"])
    assert saved["body"]["input_refs"] == [selected["copy"]]
    assert any(link["relation"] == "plan_basis" and link["target"] == selected["request"]
               for link in store.links(saved))
    new_request = update(store, "mail_request", "request", "answer-arrived", answer_refs=[
        {"kind": "mail_answer", "id": "answer", "version": 1},
    ])
    view = read(plans, selected)
    assert view["stale_linked_refs"] == [{"reference": selected["request"],
                                         "current_reference": new_request}]
    assert json.loads(view["note"]["body"]["text"])["linked_refs"][0] == selected["request"]
    assert "Inspect the synthetic counterevidence" not in json.dumps(view)
    with pytest.raises(StateError, match="unavailable"):
        plans.save("missing-link", mission_id="m", actor_id="peer", task_ref=selected["task"],
                   run_ref=selected["run"], control_ref=selected["control"], expected_version=1,
                   plan=document(linked_refs=[{"kind": "report", "id": "missing", "version": 1}]))


def test_current_source_use_masks_plan_and_transitive_producing_note_inputs(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    note = WorkingNotes(store).write("basis", mission_id="m", actor_id="peer",
                                    task_ref=selected["task"], note_id="prior-note", category="note",
                                    text="Synthetic source-derived memory", input_refs=[selected["copy"]])
    plans = RuntimePlans(store)
    save(plans, selected, input_refs=[note])
    assert read(plans, selected)["status"] == "available"
    update(store, "source_use", "use", "revoke", allowed_uses=[], policy_status="restricted")
    masked = read(plans, selected)
    assert masked["status"] == "data_unavailable_due_source_use"
    assert "text" not in masked["note"]["body"]
    assert document()["goal"] not in json.dumps(masked)
    assert masked["note"]["body"]["source_dependency_refs"] == [selected["copy"]]
    # Immutable history is retained while current model-facing reads are masked.
    assert document()["goal"] in store.get(**masked["note_ref"])["body"]["text"]


def test_churn_is_durable_local_and_catalog_sequence_repeat_reads_do_not_reset(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    for n in range(3):
        result = save(plans, selected, operation=f"p{n}", number=n, expected_version=n)
        assert result["writes_without_progress"] == n + 1
    RuntimePlans(StateStore(tmp_path)).read(
        mission_id="m", actor_id="peer", task_id="task", run_ref=selected["run"],
        control_ref=selected["control"],
    )
    update(store, "mission_task_control", "control", "catalog", sequence=700,
           tool_catalog_ref={"kind": "catalog", "id": "refresh", "version": 1})
    update(store, "mail_request", "request", "delivery", delivered_at="later", status="delivered")
    with pytest.raises(PlanningError, match="three substantive writes"):
        save(RuntimePlans(StateStore(tmp_path)), selected, operation="exhausted", number=3,
             expected_version=3)
    same = plans.save("reason-only", mission_id="m", actor_id="peer", task_ref=selected["task"],
                      run_ref=selected["run"], control_ref=selected["control"], expected_version=3,
                      plan=document(2, revision_reason="Rephrase this explanation"))
    assert same["changed"] is False
    assert len(store.records("working_note")) == 1
    # The local planner diagnostic does not fence ordinary research memory.
    WorkingNotes(store).write("continue-research", mission_id="m", actor_id="peer",
                             task_ref=selected["task"], note_id="research", category="note",
                             text="Further research remains available")


def test_new_reading_progress_resets_allowance_but_repeat_window_ids_do_not(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    for n in range(3):
        save(plans, selected, operation=f"p{n}", number=n, expected_version=n)

    def window(tx, name):
        return ref(tx.put("source_read_window", name, {
            "copy_ref": selected["copy"], "passage_refs": [selected["passage"]],
        }))
    first_window = store.transact("read", {}, lambda tx: window(tx, "read-one"))
    update(store, "mission_task_control", "control", "read-progress", read_window_refs=[first_window])
    assert save(plans, selected, operation="p3", number=3, expected_version=3)["writes_without_progress"] == 1
    for n in range(4, 6):
        save(plans, selected, operation=f"p{n}", number=n, expected_version=n)
    repeated = store.transact("reread", {}, lambda tx: window(tx, "read-repeat"))
    update(store, "mission_task_control", "control", "repeat-progress",
           read_window_refs=[first_window, repeated], selected_passage_refs={"copy": [selected["passage"]]})
    with pytest.raises(PlanningError, match="allowance exhausted"):
        save(plans, selected, operation="p6", number=6, expected_version=6)


@pytest.mark.parametrize("kind,id,changes", [
    ("mission_run", "run", {"status": "completed"}),
    ("mission_run", "run", {"status": "cancelled"}),
    ("board_proposal", "task", {"status": "completed"}),
    ("mission_task_control", "control", {"phase": "done"}),
    ("mission_task_control", "control", {"scope_status": "cancelled"}),
])
def test_lifecycle_rechecked_but_exact_accepted_replay_survives_closure(tmp_path, kind, id, changes):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    receipt = save(plans, selected)
    update(store, kind, id, "close", **changes)
    assert save(plans, selected) == receipt
    current_selected = {**selected, "task": ref(store.get("board_proposal", "task"))}
    with pytest.raises(PlanningError, match="closed"):
        save(plans, current_selected, operation="new", number=1, expected_version=1)
    view = read(plans, selected)
    assert view["effective_state"]["can_save"] is False
    assert view["status"] == "available"


def test_completed_advisory_plan_does_not_complete_task_or_resolve_requests(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    RuntimePlans(store).save("completed-plan", mission_id="m", actor_id="peer",
                            task_ref=selected["task"], run_ref=selected["run"],
                            control_ref=selected["control"], plan=document(status="completed"))
    assert store.get("board_proposal", "task")["body"]["status"] == "open"
    assert store.get("mail_request", "request")["body"]["status"] == "unanswered"
    assert store.get("mission_run", "run")["body"]["status"] == "active"


def test_phase_only_advance_keeps_operation_replay_identity(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    first = save(plans, selected)
    current_control = update(store, "mission_task_control", "control", "phase-only",
                             phase="waiting", sequence=10)
    current_run = update(store, "mission_run", "run", "run-closed", status="completed")
    assert save(plans, {**selected, "run": current_run, "control": current_control}) == first
    with pytest.raises(StateConflict, match="different inputs"):
        save(plans, {**selected, "run": current_run, "control": current_control}, number=1)


def test_new_call_or_work_binding_rejects_stale_worker_and_changes_replay_identity(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    first = save(plans, selected)
    current = update(store, "mission_task_control", "control", "next-work", work_id="new-work",
                     call_ref={"kind": "model_call", "id": "new-call", "version": 1})
    with pytest.raises(PlanningError, match="another call or work"):
        save(plans, selected, operation="stale-worker", number=1, expected_version=1)
    with pytest.raises(StateConflict, match="different inputs"):
        save(plans, {**selected, "control": current})
    assert save(plans, selected) == first


def test_seen_objective_and_source_toggles_never_refill_allowance(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    for n in range(3):
        save(plans, selected, operation=f"p{n}", number=n, expected_version=n)
    # An unseen objective is progress; reverting to a previously observed one
    # is not. Source-set removal also cannot invent a new observed source.
    task_v2 = update(store, "board_proposal", "task", "new-objective", objective="New question")
    second_scope = {**selected, "task": task_v2}
    for n in range(3, 6):
        save(plans, second_scope, operation=f"p{n}", number=n, expected_version=n)
    task_v3 = update(store, "board_proposal", "task", "old-objective",
                     objective="Investigate the synthetic question")
    update(store, "mission_task_control", "control", "drop-source", source_refs=[])
    with pytest.raises(PlanningError, match="allowance exhausted"):
        save(plans, {**selected, "task": task_v3}, operation="p6", number=6, expected_version=6)


def test_accepted_planning_and_model_turn_wrappers_are_not_research_progress(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    saved = None
    for n in range(3):
        saved = save(plans, selected, operation=f"p{n}", number=n, expected_version=n)

    def outcomes(tx):
        plan_artifact = tx.put("executor_artifact", "plan-turn", {
            "mission_id": "m", "output_refs": [saved["plan_ref"]],
        })
        model_artifact = tx.put("executor_artifact", "model-turn", {
            "mission_id": "m", "output_refs": [{"kind": "model_response", "id": "response", "version": 1}],
        })
        tx.put("model_response", "response", {"mission_id": "m"})
        for n, artifact in enumerate((plan_artifact, model_artifact)):
            tx.put("board_outcome", f"outcome-{n}", {
                "mission_id": "m", "proposal_ref": selected["task"], "result_refs": [ref(artifact)],
            })
        return {}
    store.transact("accept-wrappers", {}, outcomes)
    with pytest.raises(PlanningError, match="allowance exhausted"):
        save(plans, selected, operation="p3", number=3, expected_version=3)


def test_new_peer_review_is_progress_but_reverting_review_is_not(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    for n in range(3):
        save(plans, selected, operation=f"p{n}", number=n, expected_version=n)
    update(store, "mail_request", "request", "review", review={"reason": "Needs counterevidence"},
           reviewer_id="peer")
    for n in range(3, 6):
        save(plans, selected, operation=f"p{n}", number=n, expected_version=n)
    update(store, "mail_request", "request", "repeat-review", delivered_at="much-later")
    with pytest.raises(PlanningError, match="allowance exhausted"):
        save(plans, selected, operation="p6", number=6, expected_version=6)


def test_existing_optional_continuation_authority_survives_requested_scope_completion(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    current = update(store, "mission_task_control", "control", "optional-continuation",
                     scope_required=False, scope_status="continued")
    update(store, "mission_run", "run", "requested-scope-complete", status="completed")
    plans = RuntimePlans(store)
    result = save(plans, {**selected, "control": current})
    assert result["changed"] is True
    view = read(plans, selected)
    assert view["effective_state"]["run_status"] == "completed"
    assert view["effective_state"]["continued_optional_scope"] is True
    assert view["effective_state"]["can_save"] is True
    assert store.get("mission_run", "run")["body"]["status"] == "completed"
    update(store, "mission_run", "run", "cancel-optional-scope", status="cancelled")
    with pytest.raises(PlanningError, match="closed"):
        save(plans, {**selected, "control": current}, operation="after-cancel", number=1,
             expected_version=1)


def test_plan_event_is_safe_atomic_and_replay_does_not_emit_again(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    receipt = save(plans, selected)
    assert save(plans, selected) == receipt
    events = [event for event in store.events("m") if event["type"] == "runtime_plan_saved"]
    assert len(events) == 1
    assert events[0]["body"]["plan_ref"] == receipt["plan_ref"]
    assert document()["goal"] not in json.dumps(events)
    assert events[0]["body"]["plan_status"] == "active"


def test_fault_rolls_back_note_progress_index_and_event_together(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)

    def interrupt(point):
        if point == "after_outbox_write":
            raise OSError("Synthetic interrupted plan transaction")
    store.fault = interrupt
    with pytest.raises(OSError, match="interrupted"):
        save(plans, selected)
    assert store.records("working_note") == []
    assert store.events("m") == []
    db = store._connect()
    try:
        assert db.execute("SELECT COUNT(*) FROM aliases WHERE namespace='runtime_plan.progress'").fetchone()[0] == 0
    finally:
        db.close()
    store.fault = lambda point: None
    assert save(plans, selected)["writes_without_progress"] == 1


def test_accepted_scientific_result_is_new_progress(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    for n in range(3):
        save(plans, selected, operation=f"p{n}", number=n, expected_version=n)

    def result(tx):
        report = tx.put("report", "finding", {"mission_id": "m", "task_id": "task"})
        artifact = tx.put("executor_artifact", "result-turn", {
            "mission_id": "m", "output_refs": [ref(report)],
        })
        return ref(tx.put("board_outcome", "research-outcome", {
            "mission_id": "m", "proposal_ref": selected["task"], "result_refs": [ref(artifact)],
        }))
    store.transact("accept-research", {}, result)
    assert save(plans, selected, operation="p3", number=3, expected_version=3)["writes_without_progress"] == 1
