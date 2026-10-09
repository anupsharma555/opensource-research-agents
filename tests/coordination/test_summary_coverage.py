"""Summary read boundaries and typed trace links remain honest after later work."""

from dataclasses import replace
from pathlib import Path

import pytest

from ora.agents import AgentRegistry
from ora.coordination.traces import TraceContext, TraceJournal
from ora.state import StateConflict, StateError, StateStore

ROOT = Path(__file__).resolve().parents[2]


def fixture(tmp_path):
    store = StateStore(tmp_path)
    actor = AgentRegistry(store, ROOT).register(
        "register-peer", mission_id="mission", run_id="run",
        role_id="evidence_preparation_synthesis", display_name="Research peer",
    )
    context = TraceContext("mission", "run", "task", "attempt", actor["id"], "decision")
    return store, TraceJournal(store), context


def saved(store, kind, id, mission_id="mission", **body):
    return store.transact(
        f"save-{kind}-{id}", {"kind": kind, "id": id, "mission_id": mission_id, **body},
        lambda tx: tx.put(kind, id, {"mission_id": mission_id, **body}),
        mission_id=mission_id,
    )


def pointer(record):
    return {key: record[key] for key in ("kind", "id", "version")}


def trace(journal, context, key, *, inputs=None, relationships=None):
    return journal.append(
        key, context=context, category="agent", status="completed",
        action="Review evidence", summary="A peer considered saved evidence.",
        inputs=inputs, relationships=relationships,
    )


def coverage(context, cursor, *, event_refs=None, gaps=None):
    return {"scope": {"run_id": context.run_id, "instance_ids": [context.instance_id]},
            "through_event_sequence": cursor, "event_refs": event_refs or [],
            "generation_state": "complete", "known_gaps": gaps or []}


def test_exact_multi_input_relationships_survive_duplicate_and_out_of_order_events(tmp_path):
    store, journal, context = fixture(tmp_path)
    first = pointer(saved(store, "source_copy", "source-one", version_label="one"))
    second = pointer(saved(store, "source_copy", "source-two", version_label="two"))
    reply = pointer(saved(store, "mail_message", "peer-reply", response_to="earlier-request"))
    old = pointer(saved(store, "research_claim", "claim", wording="Earlier version"))
    links = [{"relation": "input_consumed", "reference": first},
             {"relation": "input_consumed", "reference": second},
             {"relation": "reply_to", "reference": reply},
             {"relation": "revision_of", "reference": old}]
    child = replace(context, operation_id="revised-decision", parent_operation_id="prior-operation")
    observed = trace(journal, child, "revised", inputs=[first, second, reply], relationships=links)
    before = journal.export("mission")["records"][-1]["data"]
    assert before["lineage_status"] == "unresolved_parent"
    assert before["input_refs"] == [first, second, reply]
    assert {(item["relation"], item["reference"]["id"]) for item in before["relationships"]} == {
        ("input_consumed", "source-one"), ("input_consumed", "source-two"),
        ("reply_to", "peer-reply"), ("revision_of", "claim")}
    assert {(item["relation"], item["target"]["id"]) for item in store.links(observed)
            if item["relation"] in {"input_consumed", "reply_to", "revision_of"}} == {
        ("input_consumed", "source-one"), ("input_consumed", "source-two"),
        ("reply_to", "peer-reply"), ("revision_of", "claim")}
    trace(journal, replace(context, operation_id="prior-operation"), "parent")
    assert journal.export("mission")["records"][0]["data"]["lineage_status"] == "resolved"
    assert trace(journal, child, "revised", inputs=[first, second, reply], relationships=links) == observed
    assert len(store.records("coordination_trace")) == 2
    with pytest.raises(StateConflict):
        trace(journal, child, "revised", inputs=[first, second, reply],
              relationships=links[:-1])


def test_retry_link_keeps_the_exact_prior_trace_version(tmp_path):
    store, journal, context = fixture(tmp_path)
    previous = trace(journal, context, "prior-attempt")
    retried = trace(
        journal, replace(context, operation_id="retry"), "retried-attempt",
        relationships=[{"relation": "retry_of", "reference": previous}],
    )
    assert store.get(**retried)["body"]["relationships"] == [
        {"relation": "retry_of", "reference": previous}]
    assert [(item["relation"], item["target"]) for item in store.links(retried)
            if item["relation"] == "retry_of"] == [("retry_of", previous)]


def test_older_trace_shape_replays_inside_a_new_enclosing_transaction(tmp_path):
    store, journal, context = fixture(tmp_path)
    original = trace(journal, context, "legacy-event")
    original_body = store.get(**original)["body"]
    assert "relationships" not in original_body
    events_before = [event for event in store.events("mission")
                     if event["type"] == "coordination_trace_recorded"]
    replayed = store.transact(
        "different-enclosing-operation", {"event_key": "legacy-event"},
        lambda tx: journal.append_tx(
            tx, "legacy-event", context=context, category="agent", status="completed",
            action="Review evidence", summary="A peer considered saved evidence.",
        ), mission_id="mission",
    )
    assert replayed == original
    assert store.get(**original)["body"] == original_body
    assert [event for event in store.events("mission")
            if event["type"] == "coordination_trace_recorded"] == events_before


def test_declared_cursor_survives_late_reply_and_correction_without_tip_promotion(tmp_path):
    store, journal, context = fixture(tmp_path)
    source = pointer(saved(store, "source_copy", "source", revision=1))
    first = trace(journal, context, "initial", inputs=[source])
    read_cursor = journal.export("mission")["nextCursor"]
    # New work arrives between the caller's read and the summary transaction.
    corrected = pointer(saved(store, "source_copy", "corrected", revision=2))
    later = trace(journal, replace(context, operation_id="later"), "correction", inputs=[corrected])
    old = journal.summarize(
        "first-summary", mission_id="mission", summary="The initial source supports the draft.",
        source_refs=[first, source], level="agent",
        coverage=coverage(context, read_cursor, event_refs=[first]),
    )
    first_view = journal.summary_coverage(old, mission_id="mission")
    assert first_view["through_event_sequence"] == read_cursor
    assert first_view["currentness"] == "newer_scoped_events"
    assert first_view["newer_scoped_event_count"] == 1
    assert first_view["missing_source_refs"] == []
    with pytest.raises(StateError, match="newer than its read boundary"):
        journal.summarize(
            "false-summary", mission_id="mission", summary="Pretend the later correction was read.",
            source_refs=[later], level="agent", coverage=coverage(context, read_cursor),
        )
    latest_cursor = journal.export("mission")["nextCursor"]
    updated = journal.summarize(
        "updated-summary", mission_id="mission", summary="The correction changed the draft.",
        source_refs=[first, later, corrected], level="agent",
        coverage=coverage(context, latest_cursor, event_refs=[first, later]),
    )
    latest_view = journal.summary_coverage(updated, mission_id="mission")
    assert latest_view["through_event_sequence"] == latest_cursor
    assert latest_view["currentness"] == "as_of_boundary"
    assert latest_view["newer_scoped_event_count"] == 0
    assert journal.summary_coverage(old, mission_id="mission")["currentness"] == "newer_scoped_events"
    disconnected_reader = TraceJournal(StateStore(tmp_path, read_only=True))
    assert disconnected_reader.summary_coverage(old, mission_id="mission") == first_view
    assert disconnected_reader.summary_coverage(updated, mission_id="mission") == latest_view
    assert journal.summarize(
        "first-summary", mission_id="mission", summary="The initial source supports the draft.",
        source_refs=[first, source], level="agent",
        coverage=coverage(context, read_cursor, event_refs=[first]),
    ) == old


def test_omitted_delayed_and_unavailable_are_distinct_from_newer_events(tmp_path):
    store, journal, context = fixture(tmp_path)
    source = pointer(saved(store, "source_copy", "source"))
    first = trace(journal, context, "first", inputs=[source])
    cursor = journal.export("mission")["nextCursor"]
    gaps = [{"kind": "omitted", "reason_code": "private_payloads_not_in_overview", "reference": source},
            {"kind": "delayed", "reason_code": "study_passage_not_yet_supplied"},
            {"kind": "unavailable", "reason_code": "missing_record"}]
    summary = journal.summarize(
        "partial", mission_id="mission", summary="A bounded review of the initial event.",
        source_refs=[first], coverage={**coverage(context, cursor, event_refs=[first], gaps=gaps),
                                       "generation_state": "partial"},
    )
    view = journal.summary_coverage(summary, mission_id="mission")
    assert view["currentness"] == "known_gaps"
    assert view["generation_state"] == "partial"
    assert [item["kind"] for item in view["known_gaps"]] == ["omitted", "delayed", "unavailable"]
    assert [item["reason_code"] for item in view["known_gaps"]] == [
        "private_payloads_not_in_overview", "study_passage_not_yet_supplied", "missing_record"]
    assert view["newer_scoped_event_count"] == 0
    # A peer event without a trace has unknown relevance to this scoped summary.
    store.transact("late-peer", {}, lambda tx: {"event_id": tx.emit(
        "mission", "peer_reply_recorded", {"message_id": "later"})}, mission_id="mission")
    view = journal.summary_coverage(summary, mission_id="mission")
    assert view["currentness"] == "unclassified_newer_events"
    assert view["newer_scoped_event_count"] == 0
    assert view["newer_unclassified_event_count"] == 1
    # A damaged newer trace payload is an unavailable record, not a valid new contribution.
    missing = {"kind": "coordination_trace", "id": "unavailable-trace", "version": 1}
    store.transact("missing-trace-event", {}, lambda tx: {"event_id": tx.emit(
        "mission", "coordination_trace_recorded", {"trace_ref": missing})}, mission_id="mission")
    view = journal.summary_coverage(summary, mission_id="mission")
    assert view["currentness"] == "missing_records"
    assert view["unresolved_newer_trace_count"] == 1
    assert view["newer_scoped_event_count"] == 0
    foreign_actor = AgentRegistry(store, ROOT).register(
        "register-foreign-peer", mission_id="other-mission", run_id="run",
        role_id="evidence_preparation_synthesis", display_name="Other peer",
    )
    foreign_context = replace(context, mission_id="other-mission", instance_id=foreign_actor["id"])
    foreign_trace = trace(journal, foreign_context, "foreign-trace")
    store.transact("foreign-payload-event", {}, lambda tx: {"event_id": tx.emit(
        "mission", "coordination_trace_recorded", {"trace_ref": foreign_trace})}, mission_id="mission")
    view = journal.summary_coverage(summary, mission_id="mission")
    assert view["unresolved_newer_trace_count"] == 2
    assert view["newer_scoped_event_count"] == 0


@pytest.mark.parametrize("reason_code", [
    "", "Private note: person@example.test", "https://private.example.test/detail",
    "raw free text", "Capitalized_code", "a" * 97, 7,
])
def test_gap_reason_rejects_free_text_and_malformed_codes(tmp_path, reason_code):
    store, journal, context = fixture(tmp_path)
    first = trace(journal, context, "first")
    cursor = journal.export("mission")["nextCursor"]
    with pytest.raises(StateError, match="reason code"):
        journal.summarize(
            "bad-gap", mission_id="mission", summary="The source needs a later check.",
            source_refs=[first], coverage=coverage(context, cursor, gaps=[
                {"kind": "delayed", "reason_code": reason_code}]),
        )
    assert store.records("trace_summary") == []


def test_zero_event_cursor_does_not_certify_nontrace_source_completeness(tmp_path):
    store = StateStore(tmp_path)
    store.transact("register-actor", {}, lambda tx: tx.put(
        "agent_instance", "actor", {"mission_id": "mission", "run_id": "run"}),
        mission_id="mission")
    source = pointer(saved(store, "source_copy", "after-zero-cursor"))
    assert store.events("mission") == []
    journal = TraceJournal(store)
    summary = journal.summarize(
        "zero-boundary", mission_id="mission", summary="The named source was considered.",
        source_refs=[source], coverage={"scope": {"run_id": "run"},
                                        "through_event_sequence": 0,
                                        "generation_state": "complete"},
    )
    view = journal.summary_coverage(summary, mission_id="mission")
    assert view["coverage_status"] == "declared"
    assert view["coverage_basis"] == "caller_declared_read_boundary"
    assert view["source_refs"] == [source]
    assert view["source_completeness"] == "not_established"
    assert view["event_refs"] == []
    assert view["observed_event_sequence"] == 0
    assert view["currentness"] == "as_of_boundary"


def test_explicit_set_without_cursor_and_legacy_summary_never_claim_currentness(tmp_path):
    store, journal, context = fixture(tmp_path)
    first = trace(journal, context, "first")
    legacy = journal.summarize("legacy", mission_id="mission", summary="Older explanation.", source_refs=[first])
    assert "coverage" not in store.get(**legacy)["body"]
    assert journal.summary_coverage(legacy, mission_id="mission")["currentness"] == "unknown_without_boundary"
    selected = journal.summarize(
        "selected", mission_id="mission", summary="Only the named event was considered.",
        source_refs=[first], coverage={"scope": {"run_id": "run"}, "event_refs": [first],
                                       "generation_state": "complete"},
    )
    assert journal.summary_coverage(selected, mission_id="mission")["currentness"] == "unknown_without_boundary"
    trace(journal, replace(context, operation_id="later"), "later")
    assert journal.summary_coverage(selected, mission_id="mission")["newer_scoped_event_count"] is None


def test_reject_cross_mission_restricted_and_false_scope(tmp_path):
    store, journal, context = fixture(tmp_path)
    outside = pointer(saved(store, "source_copy", "outside", mission_id="other-mission"))
    restricted = journal.save_reasoning("private", context=context, status="returned",
                                        text="Private provider reasoning")
    with pytest.raises(StateError, match="another mission"):
        trace(journal, context, "cross", relationships=[
            {"relation": "input_consumed", "reference": outside}])
    with pytest.raises(StateError, match="restricted"):
        trace(journal, context, "private", relationships=[
            {"relation": "input_consumed", "reference": restricted}])
    first = trace(journal, context, "first")
    cursor = journal.export("mission")["nextCursor"]
    with pytest.raises(StateError, match="unavailable or restricted"):
        journal.summarize("cross", mission_id="mission", summary="Wrong source.",
                          source_refs=[outside], coverage=coverage(context, cursor))
    with pytest.raises(StateError, match="restricted"):
        journal.summarize("private", mission_id="mission", summary="Wrong source.",
                          source_refs=[first], coverage=coverage(context, cursor, gaps=[
                              {"kind": "omitted", "reason_code": "restricted", "reference": restricted}]))
    other_run = replace(context, run_id="other-run", operation_id="other")
    with pytest.raises(StateError, match="scope"):
        journal.summarize("wrong-run", mission_id="mission", summary="Wrong run.",
                          source_refs=[first], coverage=coverage(other_run, cursor))
    shared_source = pointer(saved(store, "source_copy", "shared-source"))
    with pytest.raises(StateError, match="no registered actor"):
        journal.summarize(
            "unregistered-run", mission_id="mission", summary="Invented run scope.",
            source_refs=[shared_source],
            coverage={"scope": {"run_id": "unregistered"},
                      "through_event_sequence": cursor, "generation_state": "complete"},
        )
    with pytest.raises(StateError, match="boundary"):
        journal.summarize("future", mission_id="mission", summary="Invented future.",
                          source_refs=[first], coverage=coverage(context, cursor + 100))
    with pytest.raises(StateError, match="duplicates"):
        journal.summarize("duplicate-set", mission_id="mission", summary="Repeated event.",
                          source_refs=[first], coverage=coverage(context, cursor, event_refs=[first, first]))
    visible = journal.summarize(
        "visible-summary", mission_id="mission", summary="Visible metadata only.",
        source_refs=[first], coverage=coverage(context, cursor),
    )
    assert "Private provider reasoning" not in str(journal.summary_coverage(visible, mission_id="mission"))
    with pytest.raises(StateError, match="unavailable"):
        journal.summary_coverage(visible, mission_id="other-mission")
