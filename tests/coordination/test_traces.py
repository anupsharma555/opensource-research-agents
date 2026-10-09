"""Check causal tracing, private diagnostics and independent viewer catch-up."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from ora.agents import AgentRegistry
from ora.coordination.traces import TraceContext, TraceJournal
from ora.state import StateConflict, StateError, StateStore

ROOT = Path(__file__).resolve().parents[2]


def fixture(tmp_path):
    store = StateStore(tmp_path)
    actor = AgentRegistry(store, ROOT).register("register", mission_id="mission", run_id="run",
        role_id="evidence_preparation_synthesis", display_name="Sage")
    return store, TraceJournal(store), TraceContext("mission", "run", "task", "attempt", actor["id"], "model-operation")


def test_trace_replay_out_of_order_lineage_and_no_invented_success(tmp_path):
    store, journal, context = fixture(tmp_path)
    child = replace(context, operation_id="tool-operation", parent_operation_id="model-operation")
    pointer = journal.append("child", context=child, category="tool", status="uncertain", action="Inspect source", summary="The worker stopped before its result was recorded.", capability_state="attempted")
    assert journal.export("mission")["records"][0]["data"]["lineage_status"] == "unresolved_parent"
    journal.append("parent", context=context, category="model", status="completed", action="Choose a tool", summary="The provider returned a source lookup request.")
    assert journal.export("mission")["records"][0]["data"]["lineage_status"] == "resolved"
    assert journal.append("child", context=child, category="tool", status="uncertain", action="Inspect source", summary="The worker stopped before its result was recorded.", capability_state="attempted") == pointer
    assert len(store.records("coordination_trace")) == 2
    with pytest.raises(StateConflict):
        journal.append("child", context=child, category="tool", status="completed", action="Inspect source", summary="Changed observation")
    assert journal.export("mission")["steps"][0]["outcome"] == "uncertain"


def test_returned_reasoning_is_separate_and_critic_cannot_read_it(tmp_path):
    store, journal, context = fixture(tmp_path)
    pointer = journal.save_reasoning("provider", context=context, status="returned", text="Provider-returned test material; API_KEY=fixture_secret_123456789")
    with pytest.raises(PermissionError):
        journal.read_reasoning(pointer, mission_id="mission")
    with pytest.raises(PermissionError):
        journal.read_reasoning(pointer, mission_id="mission", diagnostic_granted=True, independent_first_pass=True)
    with pytest.raises(PermissionError):
        journal.read_reasoning(pointer, mission_id="mission", diagnostic_granted="false")
    result = journal.read_reasoning(pointer, mission_id="mission", diagnostic_granted=True)
    assert "fixture_secret_123456789" not in result["text"]
    assert result["origin"] == "provider_returned"
    assert "Provider-returned test material" not in str(journal.export("mission"))
    for availability in ("absent", "unsupported", "truncated", "unknown"):
        missing = journal.save_reasoning(availability, context=context, status=availability)
        assert store.get(**missing)["body"]["blob"] is None
    with pytest.raises(ValueError):
        journal.summarize("bad-summary", mission_id="mission", summary="Cannot consume restricted material", source_refs=[pointer])


def test_usage_receipts_have_distinct_units_and_are_not_double_counted(tmp_path):
    store, journal, context = fixture(tmp_path)
    usage = [{"receipt_id": "provider-response", "scope": "fixture-model-account", "unit": "total_tokens", "amount": 42, "basis": "observed"},
             {"receipt_id": "search-response", "scope": "fixture-search", "unit": "requests", "amount": 1, "basis": "observed"}]
    first = journal.append("first", context=context, category="model", status="completed", action="Read response", summary="A provider response was saved.", usage=usage)
    journal.append("second", context=replace(context, operation_id="format-result"), category="helper", status="completed", action="Format receipt", summary="The same usage remains linked, without another charge.", usage=usage)
    assert sorted(item["amount"] for item in journal.usage_totals("mission")) == [1, 42]
    summary = journal.summarize("summary", mission_id="mission", summary="Two recorded operations share one provider usage receipt.", source_refs=[first])
    assert store.get(**summary)["body"]["kind"] == "derived_explanation"


def test_disconnected_viewer_does_not_hold_writers_and_reconnect_pages_exactly(tmp_path):
    store, journal, context = fixture(tmp_path)
    def append(index):
        return journal.append(f"event-{index}", context=replace(context, operation_id=f"operation-{index}"), category="helper", status="completed", action="Save contribution", summary=f"Contribution {index} was accepted.")
    with ThreadPoolExecutor(max_workers=4) as pool:
        pointers = list(pool.map(append, range(40)))
    assert len(store.records("coordination_trace")) == 40  # No viewer was connected.
    restarted = TraceJournal(StateStore(tmp_path, read_only=True))
    cursor, steps = 0, []
    while True:
        page = restarted.export("mission", after=cursor, limit=7)
        cursor = page["nextCursor"]
        steps.extend(page["steps"])
        if not page["hasMore"]:
            break
    assert len(steps) == len({item["id"] for item in steps}) == len(pointers)
    assert restarted.export("mission", after=cursor)["steps"] == []


def test_unknown_timing_content_is_omitted_and_lineage_is_run_scoped(tmp_path):
    store, journal, context = fixture(tmp_path)
    other = AgentRegistry(store, ROOT).register("other-actor", mission_id="mission", run_id="other", role_id="evidence_preparation_synthesis", display_name="Finch")
    journal.append("other-run", context=replace(context, run_id="other", instance_id=other["id"]), category="helper", status="completed", action="Finish work", summary="Another run's observation.")
    journal.append("child", context=replace(context, operation_id="child", parent_operation_id=context.operation_id), category="helper", status="completed", action="Inspect data", summary="A child without its local parent.", timing={"wall_seconds": 0.1, "raw_reasoning": "PRIVATE_TIMING_SENTINEL"})
    output = journal.export("mission")
    assert "PRIVATE_TIMING_SENTINEL" not in str(output)
    assert output["records"][-1]["data"]["timing_fields_omitted"] == 1
    assert output["records"][-1]["data"]["lineage_status"] == "unresolved_parent"
    journal.append("cycle", context=replace(context, operation_id="cycle", parent_operation_id="cycle"), category="helper", status="recorded", action="Record malformed ancestry", summary="The incorrect ancestry remains visible for repair.")
    assert journal.export("mission")["records"][-1]["data"]["lineage_status"] == "cyclic_parentage"


def test_scope_private_references_and_provider_proof_are_not_inferred(tmp_path):
    store, journal, context = fixture(tmp_path)
    with pytest.raises(StateError, match="mission and run"):
        journal.append("bad-run", context=replace(context, run_id="invented"), category="model", status="completed", action="Read response", summary="No saved call supports this observation.")
    with pytest.raises(StateError, match="exact saved call"):
        journal.append("bad-proof", context=context, category="model", status="completed", action="Read response", summary="No response receipt exists.", proof="provider_observed")
    private = journal.save_reasoning("private", context=context, status="returned", text="Restricted test material")
    with pytest.raises(StateError, match="restricted lookup"):
        journal.append("bad-reference", context=context, category="helper", status="completed", action="Inspect reasoning", summary="Private material is not a general trace input.", inputs=[private])
    journal.append("clean-prose", context=context, category="helper", status="recorded", action="Contact example.person@example.test", summary="A diagnostic mentioned https://internal.example.test/private/report and API_KEY=fixture_secret_123456789.")
    export = journal.export("mission")
    assert "example.person@" not in str(export)
    assert "internal.example.test" not in str(export)
    assert "fixture_secret_123456789" not in str(export)
    assert store.records("coordination_trace")[0]["body"]["redacted_paths"]


def test_progress_keeps_finished_time_and_unanswered_peer_wait_separate(tmp_path):
    from ora.coordination.board import ProposalBoard
    from ora.coordination.capacity import CapacityPolicy, CapacityScheduler
    from ora.coordination.mailbox import PeerMailbox

    current = [1000.0]
    store = StateStore(tmp_path, clock=lambda: current[0])
    actors = [AgentRegistry(store, ROOT).register(f"actor-{i}", mission_id="mission", run_id="run", role_id="evidence_preparation_synthesis", display_name=f"Peer {i}") for i in range(2)]
    board = ProposalBoard(store)
    proposal = board.propose("proposal", mission_id="mission", proposal_id="work", actor_id=actors[0]["id"], objective="Inspect a supplied excerpt", expected_artifact="A supported observation", next_action="Ask for independent evidence")
    mailbox = PeerMailbox(store)
    mailbox.send_request("request", mission_id="mission", request_id="request", message_id="message", requester_id=actors[0]["id"], recipient_id=actors[1]["id"], question="What evidence supports this interpretation?", expected_artifact="Source reference", cause_ref=proposal["proposal_ref"], payload={}, expected_latency="medium")
    capacity = CapacityScheduler(store, CapacityPolicy(permits={"model": 1, "retrieval": 1, "compute": 1}))
    capacity.enqueue("operation", task_id="work", lane="one", kind="model", mission_id="mission", run_id="run")
    current[0] += 2
    admission = capacity.admit("host", "model")
    capacity.dispatched("operation", "host", admission["generation"])
    current[0] += 3
    capacity.settle("operation", "host", admission["generation"], used_tokens=0, outcome="completed")
    journal = TraceJournal(StateStore(tmp_path, read_only=True, clock=lambda: current[0]))
    first = journal.progress("mission")
    current[0] += 20
    later = journal.progress("mission")
    assert first["operations"][0]["elapsed_seconds"] == later["operations"][0]["elapsed_seconds"] == 5
    assert later["operations"][0]["recorded_durations"]["queue_wait_seconds"] == 2
    assert later["operations"][0]["recorded_durations"]["active_seconds"] == 3
    assert later["peer_requests"][0]["waiting_seconds"] == 25
    assert later["peer_requests"][0]["expected_latency"] == "medium"
    assert later["first_accepted_artifact_at"] is None
    assert later["next_actions"][0]["next_action"] == "Ask for independent evidence"
