"""Check readable traces against actual saved event and reference boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ora.agents import (
    COMMON_TOOL_IDS,
    AgentRegistry,
    Capability,
    CapabilityCatalog,
    ConfigurationRegistry,
    run_local_tool,
)
from ora.retrieval import record_delivery
from ora.state import StateStore
from ora.state.demo import build_recorded_workflow
from ora.state.steps import explain_event

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("response, outcome", [
    ({"status": "error"}, "provider_failure"),
    ({"status": "quota_wait"}, "provider_failure"),
    ({"status": "ok", "count": 1, "offset": 0, "items": ["malformed item"]}, "item_failure"),
    (None, "malformed_payload"),
    ({"status": "ok", "count": 0, "offset": 0, "items": []}, "empty"),
])
def test_retrieval_steps_keep_actual_failure_and_empty_states(tmp_path, response, outcome):
    store = StateStore(tmp_path)
    request = {"mission_id": "mission", "task_id": "task", "request_id": "request",
               "attempt_id": "attempt", "provider": "reference", "route": "abstracts",
               "operation": "search", "query": "depression trials", "filters": {},
               "requested_at": "2026-10-08T14:00:00Z", "requested_window": {"offset": 0, "limit": 1}}
    saved = record_delivery(store, request, response, delivery_id="delivery", adapter="count_offset", retrieved_at="2026-10-08T14:01:00Z")
    step = explain_event(store, store.events("mission")[0])
    assert saved["outcome"] == step["outcome"] == outcome
    assert outcome.replace("_", " ") in step["summary"]
    assert saved["delivery_ref"] in step["outputReferences"]


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def test_recorded_workflow_explains_saved_steps_without_inventing_calls(tmp_path: Path) -> None:
    child = build_recorded_workflow(ROOT, tmp_path)["child"]
    store = StateStore(tmp_path, read_only=True)
    steps = child["inspection"]["steps"]
    saved_snapshot = store.get(**child["inspection"]["snapshotReference"])
    # Snapshot acceptance is storage bookkeeping, not agent work. Match the
    # saved event boundary exactly rather than including later store activity.
    visible_events = [
        event for event in store.events(child["mission"]["id"])
        if event["sequence"] <= saved_snapshot["body"]["event_cursor"]
        and event["type"] != "snapshot_accepted"
    ]
    assert [step["id"] for step in steps] == [event["id"] for event in visible_events]
    assert all(
        step["action"] and step["summary"] and step["outcome"] and step["proof"] for step in steps
    )
    for step in steps:
        for pointer in [*step["inputReferences"], *step["outputReferences"]]:
            assert store.get(**pointer) is not None

    delivery = next(step for step in steps if step["action"] == "Record a retrieval delivery")
    assert delivery["actorName"] == "Finch"
    assert delivery["outcome"] == "success"
    assert {ref["kind"] for ref in delivery["outputReferences"]} == {
        "retrieval_delivery",
        "retrieval_attempt",
        "retrieval_hit",
    }
    assert "1 new hit" in delivery["summary"]
    assert "reference-adapter/recorded-abstracts" in delivery["summary"]

    prepared = next(step for step in steps if step["action"] == "Prepare an agent call")
    assert prepared["actorName"] in {"Finch", "Sage", "Mira", "Vale"}
    assert prepared["outcome"] == "prepared"
    assert {ref["kind"] for ref in prepared["inputReferences"]} >= {
        "agent_configuration",
        "source_copy",
    }
    assert "no model invocation" in prepared["summary"]

    report = next(step for step in steps if step["action"] == "Save a research report")
    assert report["actorName"] == "Sage"
    report_event = next(
        event for event in store.events(child["mission"]["id"]) if event["id"] == report["id"]
    )
    expected = report_event["body"].get("input_refs", [])
    assert all(pointer in report["inputReferences"] for pointer in expected)
    assert {pointer["kind"] for pointer in report["inputReferences"]} >= {
        "attempt",
        "claim",
        "source_passage",
    }
    assert report["outputReferences"][0]["kind"] == "report"
    assert any(step["action"] == "Ask a peer for help" for step in steps)
    assert any(step["action"] == "Record peer delivery" for step in steps)
    assert any(step["action"] == "Record a peer reply" for step in steps)

    visible = json.dumps(steps)
    assert "Synthetic publication abstract." not in visible
    assert "cognitive behavioural therapy depression six weeks" not in visible
    assert "reasoning text" not in visible
    assert all(
        "offline_test" in step["proof"] or "no model" in step["proof"] or "saved" in step["proof"]
        for step in steps
    )


def test_capability_steps_distinguish_preparation_exposure_and_result(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    actor = AgentRegistry(store, ROOT).register(
        "actor",
        mission_id="mission",
        run_id="run",
        role_id="literature_acquisition",
        display_name="Finch",
    )
    specialist = Capability(
        "lookup_fixture",
        "1",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "specialist:lookup",
        lambda args: {"found": bool(args["query"])},
        implementation_ref="build:step-fixture-v1",
    )
    catalog = CapabilityCatalog.with_common([specialist])
    configs = ConfigurationRegistry(store, ROOT, catalog)
    config = configs.save_configuration(
        "config",
        instance_id=actor["id"],
        mission_id="mission",
        task_scope="synthetic lookup",
        provider="example",
        endpoint="https://models.example.test/v1",
        model_id="example-model",
        settings={},
        reasoning_settings={},
        instructions="Use checked inputs.",
        skill_choices={},
        grants={f"common:{tool_id}" for tool_id in COMMON_TOOL_IDS} | {"specialist:lookup"},
        input_refs=[],
    )
    configs.start_call(
        "call-op",
        call_id="call",
        configuration_id=config["id"],
        mission_id="mission",
        task_id="task",
        attempt_id="attempt",
        loaded_skill_ids=[],
        exposed_tool_ids=sorted(COMMON_TOOL_IDS),
    )
    run_local_tool(
        store,
        catalog,
        "discover",
        mission_id="mission",
        call_id="call",
        manifest_version=1,
        tool_id="discover_capabilities",
        arguments={},
    )
    run_local_tool(
        store,
        catalog,
        "expose",
        mission_id="mission",
        call_id="call",
        manifest_version=1,
        tool_id="request_capability",
        arguments={"tool_id": "lookup_fixture"},
    )
    run_local_tool(
        store,
        catalog,
        "invoke",
        mission_id="mission",
        call_id="call",
        manifest_version=2,
        tool_id="lookup_fixture",
        arguments={"query": "fixture"},
    )
    steps = [explain_event(store, event) for event in store.events("mission")]
    prepared = next(step for step in steps if step["action"] == "Prepare an agent call")
    exposed = next(step for step in steps if step["action"] == "Expose a permitted tool")
    results = [step for step in steps if step["action"] == "Record a tool result"]
    assert prepared["actorName"] == exposed["actorName"] == "Finch"
    assert prepared["outputReferences"] == [{"kind": "agent_call", "id": "call", "version": 1}]
    assert exposed["inputReferences"] == prepared["outputReferences"]
    assert exposed["outputReferences"] == [{"kind": "agent_call", "id": "call", "version": 2}]
    assert len(results) == 3 and all(item["outcome"] == "completed" for item in results)
    assert all(item["actorName"] == "Finch" for item in results)
    assert [item["inputReferences"][0]["version"] for item in results] == [1, 1, 2]
    assert all(item["outputReferences"][0]["kind"] == "capability_invocation" for item in results)
    assert "has not run" in exposed["summary"]
    assert "saved local invocation receipt" in results[-1]["proof"]


def test_peer_status_and_review_steps_redact_private_text(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    actor = AgentRegistry(store, ROOT).register(
        "actor",
        mission_id="mission",
        run_id="run",
        role_id="analysis",
        display_name="Analyst",
    )
    statuses = ("requested", "delivered", "acknowledged", "fulfilled")
    for status in statuses:

        def save(tx, state=status):
            message = tx.put(
                "message",
                state,
                {
                    "mission_id": "mission",
                    "fromAgentId": actor["id"],
                    "status": state,
                    "summary": "Contact alice@example.org using Bearer secretvalue at https://example.test/data",
                    "nextAction": "Review the saved request.",
                },
            )
            tx.emit(
                "mission",
                "message_saved",
                {"record": _ref(message), "raw_response": "must stay hidden"},
            )
            return message

        store.transact(f"message-{status}", {"status": status}, save, mission_id="mission")

    def review(tx):
        source = tx.put("source_copy", "source", {"synthetic": True})
        notice = tx.put("source_copy", "notice", {"synthetic": True})
        claim = tx.put("claim", "claim", {"synthetic": True})
        tx.link(claim, source, "depends_on")
        affected = tx.invalidate(source, "correction notice")
        tx.emit(
            "mission",
            "source_review_required",
            {
                "kind": "correction",
                "affected_copy_ref": _ref(source),
                "notice_copy_ref": _ref(notice),
                "review_refs": affected,
                "raw_notice": "private notice text must stay hidden",
            },
        )
        return claim

    store.transact("review", {}, review, mission_id="mission")
    steps = [explain_event(store, event) for event in store.events("mission")]
    message_steps = [
        step
        for step in steps
        if step["action"]
        in {
            "Ask a peer for help",
            "Record peer delivery",
            "Acknowledge peer work",
            "Record a peer reply",
        }
    ]
    assert len(message_steps) == 4
    assert all(step["actorName"] == "Analyst" for step in message_steps)
    assert message_steps[0]["outcome"] == "requested"
    assert message_steps[-1]["outcome"] == "fulfilled"
    review_step = next(
        step for step in steps if step["action"] == "Flag source-dependent work for review"
    )
    assert review_step["outcome"] == "review_required"
    assert review_step["outputReferences"] == [{"kind": "claim", "id": "claim", "version": 1}]
    assert review_step["nextAction"]
    rendered = json.dumps(steps)
    assert "alice@example.org" not in rendered
    assert "secretvalue" not in rendered
    assert "https://example.test/data" not in rendered
    assert "must stay hidden" not in rendered


def test_later_link_does_not_rewrite_an_older_event_step(tmp_path: Path) -> None:
    store = StateStore(tmp_path)

    def first(tx):
        source = tx.put("source_passage", "passage", {"synthetic": True})
        claim = tx.put("claim", "claim", {"text": "Synthetic finding"})
        tx.emit("mission", "claim_saved", {"record": _ref(claim)})
        return {"source": _ref(source), "claim": _ref(claim)}

    accepted = store.transact("first", {}, first, mission_id="mission")
    original_event = store.events("mission")[0]

    def later(tx):
        tx.link(tx.get(**accepted["claim"]), tx.get(**accepted["source"]), "cites")
        tx.emit(
            "mission",
            "claim_saved",
            {"record": accepted["claim"], "input_refs": [accepted["source"]]},
        )
        return {"linked": True}

    store.transact("later", {}, later, mission_id="mission")
    assert explain_event(store, original_event)["inputReferences"] == []
    second_event = store.events("mission")[1]
    assert explain_event(store, second_event)["inputReferences"] == [accepted["source"]]
