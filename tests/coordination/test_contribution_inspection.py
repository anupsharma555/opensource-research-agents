"""Exercise one stored contribution review through backend, CLI and Atlas data."""

import json
import subprocess
import sys

from ora.coordination.contribution_demo import SNAPSHOT_ID, build_review_fixture
from ora.coordination.contribution_inspect import inspect_contributions
from ora.coordination.contributions import ContributionReview
from ora.coordination.demo import MISSION, PRIVATE_MARKER, ROOT
from ora.coordination.inspection import read_stage3
from ora.state import StateStore


def test_saved_review_preserves_unknown_uptake_versions_and_explanation_boundaries(tmp_path):
    state_dir = tmp_path / "state"
    snapshot = build_review_fixture(ROOT, state_dir)
    store = StateStore(state_dir, read_only=True)
    review = next(item["data"] for item in snapshot["inspection"]["records"]
                  if item["reference"]["kind"] == "contribution_review_view")
    early, recent = review["summary_views"]
    assert early["through_event_sequence"] < recent["through_event_sequence"]
    assert early["newer_scoped_event_count"] > 0
    assert recent["newer_scoped_event_count"] == 0
    assert early["known_gaps"][0]["kind"] == "omitted"
    assert recent["known_gaps"][0]["kind"] == "delayed"
    assert store.get("mail_request", "stage3-question")["body"]["status"] == "unanswered"
    unknown = store.get("mail_message", "contribution-unassessed")
    assert unknown["body"]["acknowledged_at"] is not None
    uptake = store.records("peer_update_uptake")
    assert len(uptake) == 1 and uptake[0]["body"]["disposition"] == "challenged"
    assert uptake[0]["body"]["scientific_status"] == "peer_statement"
    assert uptake[0]["body"]["update_ref"]["id"] != unknown["id"]
    assert uptake[0]["body"]["effect_ref"]["version"] == 2
    trace = next(r for r in store.records("coordination_trace")
                 if r["body"]["operation_id"] == "contribution-revision")
    assert len(trace["body"]["input_refs"]) == 3
    assert {p["relation"] for p in trace["body"]["relationships"]} == {
        "input_consumed", "reply_to", "revision_of"}
    assert PRIVATE_MARKER not in json.dumps(snapshot)
    assert snapshot["artifacts"] == [] and snapshot["mission"]["reportArtifactId"] is None
    assert len(store.records("executor_artifact")) == 3
    assert store.get("research_snapshot", "stage3-offline-concurrent-snapshot-v1") is not None
    writable = StateStore(state_dir)
    pending = writable.get("mail_request", "stage3-partial-review")
    clarification = ContributionReview(writable).send_clarification(
        "clarify-partial", mission_id=MISSION, request_id=pending["id"],
        message_id="clarify-partial", actor_id=pending["body"]["recipient_id"],
        question="Can you supply the missing study methods while reviewing this partial answer?",
    )
    assert clarification["status"] == "accepted"
    assert writable.get("mail_request", pending["id"])["body"]["status"] == "partial_awaiting_review"


def test_codex_and_atlas_share_frozen_projection_without_dispatch_or_writes(tmp_path):
    state_dir = tmp_path / "state"
    snapshot = build_review_fixture(ROOT, state_dir)
    store = StateStore(state_dir, read_only=True)
    database = state_dir / "state.sqlite3"
    before = database.read_bytes()
    events_before = store.events(MISSION)
    assert read_stage3(state_dir, SNAPSHOT_ID) == snapshot
    saved_view = next(item["data"] for item in snapshot["inspection"]["records"]
                      if item["reference"]["kind"] == "contribution_review_view")
    context = saved_view["proposal_context"]
    view = inspect_contributions(state_dir, mission_id=MISSION,
                                 proposal_id=context["proposal_id"],
                                 viewer_id=context["viewer_id"], through=context["through"])
    assert view == context
    command = [sys.executable, "-m", "ora.coordination.contribution_inspect",
               "--state-dir", str(state_dir), "--mission-id", MISSION,
               "--proposal-id", context["proposal_id"], "--viewer-id", context["viewer_id"],
               "--through", str(context["through"])]
    assert json.loads(subprocess.check_output(command, cwd=ROOT, text=True)) == context
    cursor, pages = 0, []
    while True:
        page = inspect_contributions(state_dir, mission_id=MISSION,
                                     proposal_id=context["proposal_id"], viewer_id=context["viewer_id"],
                                     after=cursor, through=context["through"], limit=4)
        pages.extend(page["entries"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert pages == context["entries"]
    summary = saved_view["summary_views"][0]
    latest = inspect_contributions(state_dir, mission_id=MISSION, summary_ref=summary["reference"])
    assert latest["through_event_sequence"] == summary["through_event_sequence"]
    assert latest["summary"] == summary["summary"]
    assert latest["observed_event_sequence"] >= summary["observed_event_sequence"]
    assert database.read_bytes() == before
    assert store.events(MISSION) == events_before
    assert not store.records("agent_call")


def test_contribution_cli_reports_invalid_read_request(tmp_path):
    state_dir = tmp_path / "state"
    StateStore(state_dir)
    result = subprocess.run([sys.executable, "-m", "ora.coordination.contribution_inspect",
                             "--state-dir", str(state_dir), "--mission-id", MISSION],
                            cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 2
    assert "viewing peer" in result.stderr and "Traceback" not in result.stderr
