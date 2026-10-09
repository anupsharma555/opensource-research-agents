"""Create an offline example of shared critique, optional uptake and summary age.

The generator extends a fresh Stage 3 callback fixture with actual saved peer and
trace records. Its Atlas export and Codex queries share the same read projections;
it never opens historical state, invokes a model or claims a scientific result.
"""

from __future__ import annotations

import argparse
import copy
import json
import uuid
from pathlib import Path

from ora.agents import AgentRegistry
from ora.state import StateStore
from ora.state.inspection import load_snapshot, save_snapshot

from .board import ProposalBoard, ref
from .contributions import ContributionReview
from .demo import MISSION, ROOT, RUN, build_fixture
from .mailbox import PeerMailbox
from .traces import TraceContext, TraceJournal

SNAPSHOT_ID = "stage3-contribution-review-snapshot-v1"


def _tip(store: StateStore) -> int:
    db = store._connect()
    try:
        return db.execute("SELECT COALESCE(MAX(sequence),0) FROM events WHERE mission_id=?",
                          (MISSION,)).fetchone()[0]
    finally:
        db.close()


def _accepted(result: dict) -> dict:
    if result.get("status") != "accepted":
        raise RuntimeError(f"Contribution fixture action failed: {result}")
    return result


def build_review_fixture(root: Path, state_dir: Path) -> dict:
    """Save a fresh peer-review example while retaining the original base snapshot."""
    base = build_fixture(root, state_dir)
    store = StateStore(state_dir)
    board, mail = ProposalBoard(store), PeerMailbox(store)
    contributions, journal = ContributionReview(store), TraceJournal(store)
    actors = store.records("agent_instance")
    author = next(a for a in actors if a["body"]["role_id"] == "literature_acquisition")
    recipient = next(a for a in actors if a["body"]["role_id"] == "evidence_preparation_synthesis")
    critic = next(a for a in actors if a["body"]["role_id"] == "independent_methods_reviewer_critic")
    proposal = store.get("board_proposal", "d-evidence-gap")
    evidence = store.get("stage3_fixture_input", "stage3-synthetic-input", 1)
    dissent = store.get("board_dissent", "stage3-limited-critique")
    message = _accepted(mail.send_message(
        "contribution-update", mission_id=MISSION, message_id="contribution-update",
        sender_id=author["id"], recipient_id=recipient["id"],
        correlation_id="shared-input-limits", task_ref=ref(proposal),
        evidence_refs=[ref(evidence), ref(dissent)],
        payload={"discussion": "The callback proves local execution; a study appraisal needs source passages."},
    ))
    _accepted(mail.mark_delivered("contribution-delivered", mission_id=MISSION,
                                  message_id=message["message_ref"]["id"]))
    _accepted(mail.acknowledge("contribution-ack", mission_id=MISSION,
                               message_id=message["message_ref"]["id"],
                               recipient_id=recipient["id"]))
    update = ref(store.get("mail_message", message["message_ref"]["id"]))
    # Save the reader's actual boundary before another peer joins or replies.
    early = journal.summarize(
        "contribution-early-summary", mission_id=MISSION,
        summary="A critique identifies the missing study passage. The update was acknowledged; no use of it is recorded yet.",
        source_refs=[ref(dissent), update],
        coverage={"scope": {"run_id": RUN}, "through_event_sequence": _tip(store),
                  "generation_state": "partial", "known_gaps": [
                      {"kind": "omitted", "reason_code": "private_payloads_not_in_overview"}]},
    )
    late = AgentRegistry(store, root).register(
        "contribution-late-peer", mission_id=MISSION, run_id=RUN,
        role_id="independent_methods_reviewer_critic", display_name="Follow-up review peer",
    )
    late_context = contributions.proposal_context(
        mission_id=MISSION, proposal_id=proposal["id"], viewer_id=late["id"], limit=100,
    )
    if not any(item["ref"] == ref(dissent) for event in late_context["entries"]
               for item in event["records"]):
        raise RuntimeError("The late peer could not discover the saved critique")
    corrected = store.transact("contribution-corrected-input", {}, lambda tx: tx.put(
        "stage3_fixture_input", evidence["id"],
        {**evidence["body"], "uncertainty": "Execution observations remain insufficient for a scientific appraisal."},
        expected_version=1), mission_id=MISSION)
    revision = _accepted(board.revise(
        "contribution-revision", mission_id=MISSION, proposal_id=proposal["id"],
        actor_id=late["id"], expected_version=proposal["version"],
        reason="The shared critique distinguishes execution proof from scientific evidence.",
        next_action="Seek a permitted study passage before attempting the appraisal.",
        evidence_refs=[ref(evidence), ref(corrected), ref(dissent)],
    ))
    uptake = _accepted(contributions.record_uptake(
        "contribution-challenged", mission_id=MISSION, uptake_id="contribution-challenged",
        actor_id=late["id"], update_ref=update, disposition="challenged",
        explanation="I used the prior critique to revise the next action; execution success does not establish study validity.",
        evidence_refs=[ref(evidence), ref(corrected), ref(dissent)],
        effect_ref=revision["proposal_ref"],
    ))
    _accepted(mail.send_message(
        "contribution-unassessed", mission_id=MISSION,
        message_id="contribution-unassessed", sender_id=author["id"],
        recipient_id=recipient["id"], correlation_id="source-update-not-yet-reviewed",
        task_ref=revision["proposal_ref"], evidence_refs=[ref(corrected)],
        payload={"update": "A corrected input is available for optional review."},
    ))
    _accepted(mail.mark_delivered("contribution-unassessed-delivered", mission_id=MISSION,
                                  message_id="contribution-unassessed"))
    _accepted(mail.acknowledge("contribution-unassessed-ack", mission_id=MISSION,
                               message_id="contribution-unassessed", recipient_id=recipient["id"]))
    _accepted(contributions.send_clarification(
        "contribution-clarification", mission_id=MISSION, request_id="stage3-question",
        message_id="contribution-clarification", actor_id=critic["id"],
        question="Which exact study passage should the appraisal examine?",
    ))
    context = TraceContext(MISSION, RUN, proposal["id"], "review-follow-up",
                           late["id"], "contribution-revision")
    revision_event = journal.append(
        "contribution-revision", context=context, category="peer_review", status="recorded",
        action="Revise the next research action", summary="A late peer linked a critique to a revised evidence request.",
        inputs=[ref(evidence), ref(corrected), update], outputs=[revision["proposal_ref"], uptake["uptake_ref"]],
        relationships=[{"relation": "input_consumed", "reference": ref(evidence)},
                       {"relation": "input_consumed", "reference": ref(corrected)},
                       {"relation": "reply_to", "reference": update},
                       {"relation": "revision_of", "reference": ref(proposal)}],
    )
    recent = journal.summarize(
        "contribution-current-summary", mission_id=MISSION,
        summary="The late peer found the critique and revised the next action. The original question remains open pending a study passage.",
        source_refs=[revision_event, uptake["uptake_ref"], revision["proposal_ref"]],
        coverage={"scope": {"run_id": RUN}, "through_event_sequence": _tip(store),
                  "generation_state": "partial", "known_gaps": [
                      {"kind": "delayed", "reason_code": "study_passage_not_yet_supplied"}]},
    )
    context_view = contributions.proposal_context(
        mission_id=MISSION, proposal_id=proposal["id"], viewer_id=late["id"], limit=100,
    )
    summary_views = [journal.summary_coverage(p, mission_id=MISSION) for p in (early, recent)]
    review = store.transact("contribution-review-view", {}, lambda tx: tx.put(
        "contribution_review_view", "shared-review", {
            "mission_id": MISSION, "run_id": RUN,
            "purpose": "Saved peer statements and their linked effects; no scientific correctness is inferred.",
            "proposal_context": context_view,
            "summary_views": summary_views,
            "late_peer_ref": ref(late),
            "source_refs": [ref(dissent), update, uptake["uptake_ref"], revision_event, early, recent],
            "proof": "offline_test",
        }), mission_id=MISSION)
    snapshot = copy.deepcopy(base)
    snapshot.update(snapshotId=SNAPSHOT_ID, sampleVersion="stage3-contribution-review-1",
                    capturedAt=store.now())
    snapshot["agents"].append({"id": late["id"], "name": late["body"]["display_name"],
                               "role": "Review peer", "status": "waiting"})
    refs = [item["reference"] for item in base["inspection"]["records"]]
    refs += [ref(review), ref(late), ref(corrected), update, uptake["uptake_ref"],
             revision["proposal_ref"], revision_event, early, recent]
    refs += [p["ref"] for entry in context_view["entries"] for p in entry["records"]
             if store.get(**p["ref"]) is not None]
    save_snapshot(store, "contribution-save-snapshot", snapshot,
                  source_provenance=[], attempt_provenance=base["inspection"]["attempts"],
                  detail_refs=refs, accepted_outputs=[], timing_basis="observed_local")
    return load_snapshot(store, SNAPSHOT_ID)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default=f"contribution-review-{uuid.uuid4().hex[:12]}")
    args = parser.parse_args()
    if Path(args.name).name != args.name or not args.name.startswith("contribution-review-"):
        parser.error("Name must be one contribution-review-* directory name")
    destination = ROOT / ".local/stage3/extensions" / args.name
    snapshot = build_review_fixture(ROOT, destination / "state")
    with (destination / "snapshot.json").open("x") as output:
        json.dump(snapshot, output, ensure_ascii=False, indent=2)
        output.write("\n")
    view = next(item["data"] for item in snapshot["inspection"]["records"]
                if item["reference"]["kind"] == "contribution_review_view")
    context = view["proposal_context"]
    print(json.dumps({"state_dir": str(destination / "state"),
                      "snapshot_id": SNAPSHOT_ID, "snapshot": str(destination / "snapshot.json"),
                      "review_query": {key: context[key] for key in (
                          "mission_id", "proposal_id", "viewer_id", "through")},
                      "summary_refs": [item["reference"] for item in view["summary_views"]],
                      "provider_calls": 0, "proof": "offline_test"}))


if __name__ == "__main__":
    main()
