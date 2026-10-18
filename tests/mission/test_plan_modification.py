"""Targeted plan edits preserve untouched state, history, permissions and replay."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from ora.mission.planning import PlanningError, RuntimePlans
from ora.state import StateConflict, StateStore
from tests.mission.test_planning import document, read, seed, update


def scope(selected):
    return dict(mission_id="m", actor_id="peer", task_ref=selected["task"],
                run_ref=selected["run"], control_ref=selected["control"])


def create(plans, selected, operation="create", **kwargs):
    return plans.create(operation, **scope(selected), plan=document(), **kwargs)


def modify(plans, selected, operation="modify", version=1, **changes):
    return plans.modify(operation, **scope(selected), expected_version=version,
                        revision_reason="New synthetic evidence changes the next action", changes=changes)


def test_create_and_modify_are_distinct_and_targeted_edits_preserve_history(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    with pytest.raises(PlanningError, match="absent"):
        modify(plans, selected, status="paused")
    original = create(plans, selected)
    assert create(plans, selected) == original
    with pytest.raises(PlanningError, match="already exists"):
        create(plans, selected, operation="overwrite")
    with pytest.raises(ValidationError):
        create(plans, selected, operation="replacement", expected_version=1)
    result = modify(plans, selected, update_steps=[{"id": "read", "status": "done"}],
                    add_steps=[{"id": "check", "description": "Check counterevidence",
                                "status": "todo", "depends_on": ["review"]}], uncertainties=[])
    saved = json.loads(read(plans, selected)["note"]["body"]["text"])
    assert result["plan_ref"]["version"] == 2
    assert saved["goal"] == document()["goal"]
    assert saved["completion_criteria"] == document()["completion_criteria"]
    assert saved["steps"][0]["description"] == document()["steps"][0]["description"]
    assert saved["steps"][0]["status"] == "done" and saved["uncertainties"] == []
    assert saved["steps"][1]["depends_on"] == ["read"]
    assert saved["steps"][2]["id"] == "check"
    reopened = RuntimePlans(StateStore(tmp_path))
    assert json.loads(read(reopened, selected, version=1)["note"]["body"]["text"])["steps"][0]["status"] == "todo"
    assert modify(reopened, selected, update_steps=[{"id": "read", "status": "done"}],
                  add_steps=[{"id": "check", "description": "Check counterevidence",
                              "status": "todo", "depends_on": ["review"]}], uncertainties=[]) == result
    assert read(reopened, selected)["plan_ref"]["version"] == 2


@pytest.mark.parametrize("changes", [
    {}, {"status": None}, {"update_steps": []},
    {"update_steps": [{"id": "read"}]},
    {"update_steps": [{"id": "read", "status": None}]},
    {"update_steps": [{"id": "missing", "status": "done"}]},
    {"remove_step_ids": ["missing"]}, {"remove_step_ids": ["read"]},
    {"update_steps": [{"id": "read", "depends_on": ["review"]}]},
    {"add_steps": [{"id": "read", "description": "Duplicate", "status": "todo"}]},
    {"update_steps": [{"id": "read", "status": "done"}], "remove_step_ids": ["read"]},
    {"add_steps": [{"id": f"s{i}", "description": "Too many steps", "status": "todo"}
                   for i in range(23)]},
])
def test_invalid_edit_is_atomic_without_advancing_plan_or_progress(tmp_path, changes):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    create(plans, selected)
    before = read(plans, selected)["note"]
    with pytest.raises((PlanningError, ValidationError)):
        modify(plans, selected, **changes)
    assert read(plans, selected)["note"] == before


def test_remove_and_repair_dependency_together_and_stale_edit_rejected(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    create(plans, selected)
    modify(plans, selected, remove_step_ids=["read"], update_steps=[{"id": "review", "depends_on": []}])
    saved = json.loads(read(plans, selected)["note"]["body"]["text"])
    assert [s["id"] for s in saved["steps"]] == ["review"]
    assert saved["steps"][0]["depends_on"] == []
    with pytest.raises(StateConflict, match="version"):
        modify(plans, selected, operation="stale", status="paused")


def test_concurrent_same_version_edits_accept_one_without_lost_update(tmp_path):
    selected = seed(StateStore(tmp_path))
    create(RuntimePlans(StateStore(tmp_path)), selected)

    def edit(n):
        try:
            return modify(RuntimePlans(StateStore(tmp_path)), selected,
                          operation=f"edit-{n}", goal=f"Candidate {n}")
        except StateConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, [1, 2]))
    assert sum(r is not None for r in results) == 1
    assert read(RuntimePlans(StateStore(tmp_path)), selected)["note"]["version"] == 2


def test_modification_cannot_launder_revoked_prior_source_or_overwrite_another_actor(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    plans.create("create", **scope(selected), plan=document(), input_refs=[selected["copy"]])
    modify(plans, selected, status="paused")
    note = read(plans, selected)["note"]
    assert note["body"]["input_refs"] == [selected["copy"]]
    update(store, "source_use", "use", "revoke", allowed_uses=[])
    with pytest.raises(PlanningError, match="source-use"):
        modify(plans, selected, operation="launder", version=2, linked_refs=[], goal="Public looking prose")
    assert store.get("working_note", note["id"])["version"] == 2
    assert read(plans, selected)["status"] == "data_unavailable_due_source_use"
    with pytest.raises(PlanningError, match="outside"):
        plans.modify("other", **{**scope(selected), "actor_id": "other"}, expected_version=2,
                     revision_reason="Not the owner", changes={"status": "active"})


def test_noop_and_modifications_share_churn_guard_and_replay_survives_closure(tmp_path):
    store = StateStore(tmp_path)
    selected = seed(store)
    plans = RuntimePlans(store)
    create(plans, selected)
    unchanged = modify(plans, selected, goal=document()["goal"])
    assert unchanged["changed"] is False and unchanged["plan_ref"]["version"] == 1
    modify(plans, selected, operation="second", status="paused")
    third = modify(plans, selected, operation="third", version=2, status="active")
    with pytest.raises(PlanningError, match="allowance exhausted"):
        modify(plans, selected, operation="fourth", version=3, goal="Repeated editing")
    update(store, "mission_run", "run", "close", status="completed")
    assert modify(plans, selected, operation="third", version=2, status="active") == third
    with pytest.raises(PlanningError, match="closed"):
        modify(plans, selected, operation="after-close", version=3, status="paused")


@pytest.mark.parametrize('changes', [
    {'goal': None}, {'status': None}, {'uncertainties': None},
    {'update_steps': [{'id': 'read', 'description': None}]},
    {'update_steps': [{'id': 'read', 'linked_refs': None}]},
])
def test_attached_edit_schema_rejects_null_fields_but_allows_omission_and_clearing(changes):
    from ora.agents.capabilities import CapabilityError, validate_arguments
    from ora.mission.tool_catalog import TOOL_SCHEMAS

    base = {'expected_version': 1, 'revision_reason': 'Update the relevant constraint'}
    validate_arguments(TOOL_SCHEMAS['modify_plan'], {**base, 'changes': {'uncertainties': []}})
    with pytest.raises(CapabilityError):
        validate_arguments(TOOL_SCHEMAS['modify_plan'], {**base, 'changes': changes})
