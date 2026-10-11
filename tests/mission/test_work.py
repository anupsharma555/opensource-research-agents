"""Optional model-led work remains authenticated, versioned and non-blocking."""

from __future__ import annotations

from pathlib import Path

import pytest

from ora.agents import AgentRegistry
from ora.coordination.board import ProposalBoard, ref
from ora.mission.work import MissionWork, WorkError
from ora.state import StateConflict, StateStore

ROOT = Path(__file__).resolve().parents[2]
ROLES = {
    "agent-synthesis": ("Sage", "evidence_preparation_synthesis"),
    "agent-critique": ("Mira", "independent_methods_reviewer_critic"),
}


def setup(tmp_path):
    store = StateStore(tmp_path)
    mission_id, run_id = "synthetic-mission", "synthetic-run"
    run_body = {
        "schema": "ora.mission_run.v1",
        "mission_id": mission_id,
        "run_id": run_id,
        "agent_ids": ["agent-synthesis"],
        "status": "active",
    }
    run_ref = store.transact(
        "synthetic-run-init",
        run_body,
        lambda tx: ref(tx.put("mission_run", run_id, run_body)),
        mission_id=mission_id,
    )
    registry = AgentRegistry(store, ROOT)
    actor = registry.register(
        "synthetic-actor",
        mission_id=mission_id,
        run_id=run_id,
        role_id="evidence_preparation_synthesis",
        display_name="Sage",
    )
    board = ProposalBoard(store)
    root = board.propose(
        "synthetic-root",
        mission_id=mission_id,
        proposal_id="root-task",
        actor_id=actor["id"],
        objective="Compare interpretations",
        expected_artifact="Supported contribution",
        next_action="Inspect evidence",
    )
    assert root["status"] == "accepted"
    control_body = {
        "schema": "ora.mission_task.v1",
        "mission_id": mission_id,
        "run_id": run_id,
        "alias": "agent-synthesis",
        "actor_id": actor["id"],
        "task_id": "root-task",
        "proposal_ref": root["proposal_ref"],
        "phase": "pending_tool",
        "source_refs": [],
        "scope_status": "open",
    }
    control_ref = store.transact(
        "synthetic-control-init",
        control_body,
        lambda tx: ref(tx.put("mission_task_control", "root-control", control_body)),
        mission_id=mission_id,
    )
    work = MissionWork(store, board, registry, ROLES)
    return store, board, registry, work, run_ref, actor, control_ref, root["proposal_ref"]


def propose(work, run_ref, actor, control_ref, root_ref, *, label="child"):
    return work.propose(
        "propose-" + label,
        run_ref=run_ref,
        actor_id=actor["id"],
        source_control_ref=control_ref,
        proposal_id=label,
        objective="Investigate a competing explanation",
        expected_artifact="Evidence-linked appraisal",
        next_action="Search contrary evidence",
        branch_of_ref=root_ref,
    )


def test_agent_proposal_creates_distinct_optional_control_without_changing_requested_roles(
    tmp_path,
):
    store, board, _registry, work, run_ref, actor, control_ref, root_ref = setup(tmp_path)
    child = propose(work, run_ref, actor, control_ref, root_ref)
    assert child["status"] == "accepted"
    control = store.get(**child["control_ref"])
    assert control["body"]["phase"] == "ready_model"
    assert control["body"]["scope_required"] is False
    assert control["body"]["actor_id"] == actor["id"]
    assert control["id"] != control_ref["id"]
    assert store.get(**child["proposal_ref"])["body"]["branch_of"] == root_ref
    assert store.get("mission_run", run_ref["id"])["body"]["agent_ids"] == ["agent-synthesis"]
    assert [item["proposal_ref"]["id"] for item in work.open_optional_work(run_ref["id"])] == [
        "child"
    ]
    reopened = MissionWork(
        StateStore(tmp_path),
        ProposalBoard(StateStore(tmp_path)),
        AgentRegistry(StateStore(tmp_path), ROOT),
        ROLES,
    )
    assert reopened.open_optional_work(run_ref["id"])[0]["control_ref"] == child["control_ref"]
    assert board.open_work("synthetic-mission")


def test_exact_actor_and_proposal_versions_guard_revision_and_defer(tmp_path):
    store, _board, registry, work, run_ref, actor, control_ref, root_ref = setup(tmp_path)
    child = propose(work, run_ref, actor, control_ref, root_ref)
    foreign = registry.register(
        "foreign-actor",
        mission_id="other-mission",
        run_id="other-run",
        role_id="evidence_preparation_synthesis",
        display_name="Foreign Peer",
    )
    with pytest.raises(WorkError, match="belong together"):
        work.revise(
            "foreign-revise",
            run_ref=run_ref,
            actor_id=foreign["id"],
            source_control_ref=control_ref,
            proposal_ref=child["proposal_ref"],
            reason="Different view",
        )
    revised = work.revise(
        "revision",
        run_ref=run_ref,
        actor_id=actor["id"],
        source_control_ref=control_ref,
        proposal_ref=child["proposal_ref"],
        reason="Evidence changed",
        objective="Check a different mechanism",
    )
    assert revised["status"] == "accepted" and revised["proposal_ref"]["version"] == 2
    replay = work.revise(
        "revision",
        run_ref=run_ref,
        actor_id=actor["id"],
        source_control_ref=control_ref,
        proposal_ref=child["proposal_ref"],
        reason="Evidence changed",
        objective="Check a different mechanism",
    )
    assert replay["proposal_ref"] == revised["proposal_ref"]
    with pytest.raises(WorkError, match="version changed"):
        work.defer(
            "stale-defer",
            run_ref=run_ref,
            actor_id=actor["id"],
            source_control_ref=control_ref,
            proposal_ref=child["proposal_ref"],
            reason="Pause",
        )
    deferred = work.defer(
        "defer",
        run_ref=run_ref,
        actor_id=actor["id"],
        source_control_ref=control_ref,
        proposal_ref=revised["proposal_ref"],
        reason="Wait for a relevant source",
    )
    assert deferred["status"] == "accepted"
    assert (
        work.defer(
            "defer",
            run_ref=run_ref,
            actor_id=actor["id"],
            source_control_ref=control_ref,
            proposal_ref=revised["proposal_ref"],
            reason="Wait for a relevant source",
        )["proposal_ref"]
        == deferred["proposal_ref"]
    )
    assert store.get("board_proposal", "child")["body"]["status"] == "deferred"
    assert store.get("board_proposal", "root-task")["body"]["status"] == "open"
    assert work.open_optional_work(run_ref["id"]) == []


def test_invited_unrequested_peer_gets_review_turn_before_explicit_acceptance(tmp_path):
    store, board, _registry, work, run_ref, actor, control_ref, root_ref = setup(tmp_path)
    child = propose(work, run_ref, actor, control_ref, root_ref)
    offer = work.invite(
        "offer",
        run_ref=run_ref,
        actor_id=actor["id"],
        source_control_ref=control_ref,
        proposal_ref=child["proposal_ref"],
        recipient_alias="agent-critique",
        objective="Independent methods review",
        expected_artifact="Critical appraisal",
        invitation_id="invite-critique",
    )
    assert offer["status"] == "accepted"
    assert offer["requested_participant"] is False
    review = store.get(**offer["review_control_ref"])
    assert review["body"]["control_type"] == "invitation_review"
    assert review["body"]["phase"] == "ready_model"
    assert review["body"]["task_id"] == offer["review_proposal_ref"]["id"]
    assert review["body"]["task_id"] != child["proposal_ref"]["id"]
    assert review["body"]["proposal_ref"] == offer["review_proposal_ref"]
    assert review["body"]["feedback"]["invitation_offer"] == {
        "objective": "Independent methods review",
        "expected_artifact": "Critical appraisal",
    }
    review_proposal = store.get(**offer["review_proposal_ref"])
    assert review_proposal["body"]["actor_ref"]["id"] == offer["recipient_ref"]["id"]
    assert review_proposal["body"]["status"] == "open"
    assert any(
        item["proposal_ref"]["id"] == review["body"]["task_id"]
        for item in board.ready_work("synthetic-mission")
    )
    assert (
        work.claim(
            "review-claim-before-acceptance",
            run_ref=run_ref,
            actor_id=offer["recipient_ref"]["id"],
            source_control_ref=offer["review_control_ref"],
            proposal_ref=offer["review_proposal_ref"],
            ttl=1,
        )["status"]
        == "accepted"
    )
    original_clock = store.clock
    store.clock = lambda: original_clock() + 2
    assert store.get(**offer["invitation_ref"])["body"]["status"] == "offered"
    assert store.get("mission_run", run_ref["id"])["body"]["agent_ids"] == ["agent-synthesis"]
    with pytest.raises(WorkError, match="belong together"):
        work.respond_invitation(
            "forged",
            run_ref=run_ref,
            recipient_id=actor["id"],
            review_control_ref=offer["review_control_ref"],
            invitation_ref=offer["invitation_ref"],
            decision="accepted",
        )
    accepted = work.respond_invitation(
        "recipient-accept",
        run_ref=run_ref,
        recipient_id=offer["recipient_ref"]["id"],
        review_control_ref=offer["review_control_ref"],
        invitation_ref=offer["invitation_ref"],
        decision="accepted",
    )
    assert accepted["status"] == "accepted"
    assert accepted["review_completion"]["status"] == "accepted"
    assert store.get("board_proposal", review_proposal["id"])["body"]["status"] == "completed"
    assert store.get(**accepted["work_control_ref"])["body"]["phase"] == "ready_model"
    assert store.get("mission_task_control", review["id"])["body"]["phase"] == "done"
    claim = work.claim(
        "recipient-claim",
        run_ref=run_ref,
        actor_id=offer["recipient_ref"]["id"],
        source_control_ref=accepted["work_control_ref"],
        proposal_ref=child["proposal_ref"],
    )
    assert claim["status"] == "accepted"
    assert claim["fence"]["owner"] == offer["recipient_ref"]["id"]
    replay = work.respond_invitation(
        "recipient-accept",
        run_ref=run_ref,
        recipient_id=offer["recipient_ref"]["id"],
        review_control_ref=offer["review_control_ref"],
        invitation_ref=offer["invitation_ref"],
        decision="accepted",
    )
    assert replay["work_control_ref"] == accepted["work_control_ref"]


def test_declined_invitation_completes_review_without_opening_research_work(tmp_path):
    store, _board, _registry, work, run_ref, actor, control_ref, root_ref = setup(tmp_path)
    child = propose(work, run_ref, actor, control_ref, root_ref)
    offer = work.invite(
        "offer-decline",
        run_ref=run_ref,
        actor_id=actor["id"],
        source_control_ref=control_ref,
        proposal_ref=child["proposal_ref"],
        recipient_alias="agent-critique",
        objective="Independent methods review",
        expected_artifact="Critical appraisal",
        invitation_id="invite-decline",
    )
    result = work.respond_invitation(
        "recipient-decline",
        run_ref=run_ref,
        recipient_id=offer["recipient_ref"]["id"],
        review_control_ref=offer["review_control_ref"],
        invitation_ref=offer["invitation_ref"],
        decision="declined",
    )
    assert result["status"] == "accepted"
    assert (
        store.get("board_proposal", offer["review_proposal_ref"]["id"])["body"]["status"]
        == "completed"
    )
    assert all(
        item["control_type"] != "research_work" or item["actor_id"] != offer["recipient_ref"]["id"]
        for item in work.open_optional_work(run_ref["id"])
    )


def test_invitation_decision_replay_repairs_crash_after_board_claim(tmp_path):
    store, board, _registry, work, run_ref, actor, control_ref, root_ref = setup(tmp_path)
    child = propose(work, run_ref, actor, control_ref, root_ref)
    offer = work.invite(
        "offer-replay",
        run_ref=run_ref,
        actor_id=actor["id"],
        source_control_ref=control_ref,
        proposal_ref=child["proposal_ref"],
        recipient_alias="agent-critique",
        objective="Independent review",
        expected_artifact="Review decision",
        invitation_id="invite-replay",
    )
    original_accept_result = board.accept_result
    crashed = False

    def fail_once(*args, **kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("synthetic crash after review claim")
        return original_accept_result(*args, **kwargs)

    board.accept_result = fail_once
    decision = dict(
        operation_id="respond-replay",
        run_ref=run_ref,
        recipient_id=offer["recipient_ref"]["id"],
        review_control_ref=offer["review_control_ref"],
        invitation_ref=offer["invitation_ref"],
        decision="accepted",
    )
    with pytest.raises(RuntimeError, match="synthetic crash"):
        work.respond_invitation(**decision)
    assert store.get("board_invitation", "invite-replay")["body"]["status"] == "accepted"
    assert (
        store.get("board_proposal", offer["review_proposal_ref"]["id"])["body"]["status"] == "open"
    )
    resumed = work.respond_invitation(**decision)
    assert resumed["status"] == "accepted"
    assert resumed["review_completion"]["status"] == "accepted"
    assert (
        store.get("board_proposal", offer["review_proposal_ref"]["id"])["body"]["status"]
        == "completed"
    )
    assert store.get(**resumed["work_control_ref"])["body"]["phase"] == "ready_model"


def test_scope_disposition_tracks_exact_optional_set_and_detects_new_work(tmp_path):
    store, _board, _registry, work, run_ref, actor, control_ref, root_ref = setup(tmp_path)
    first = propose(work, run_ref, actor, control_ref, root_ref)
    decision = work.record_scope_disposition(
        "scope-one",
        run_ref=run_ref,
        actor_id=actor["id"],
        source_control_ref=control_ref,
        decisions=[{"control_ref": first["control_ref"], "disposition": "continue"}],
        reason="Selected contribution is supported while this branch remains useful",
    )
    assert decision["status"] == "recorded"
    assert work.disposition_is_current(decision["disposition_ref"])
    propose(work, run_ref, actor, control_ref, root_ref, label="second")
    assert not work.disposition_is_current(decision["disposition_ref"])
    with pytest.raises(StateConflict, match="Open optional work changed"):
        work.record_scope_disposition(
            "stale-scope",
            run_ref=run_ref,
            actor_id=actor["id"],
            source_control_ref=control_ref,
            decisions=[{"control_ref": first["control_ref"], "disposition": "continue"}],
            reason="This is now incomplete",
        )
    assert store.get("board_proposal", "second") is not None


def test_scope_close_race_retains_new_proposal_for_future_scope(tmp_path):
    store, board, _registry, work, run_ref, actor, control_ref, root_ref = setup(tmp_path)
    original = board.propose

    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        body = {**store.get("mission_run", run_ref["id"])["body"], "status": "completed"}
        store.transact(
            "synthetic-close",
            body,
            lambda tx: ref(
                tx.put("mission_run", run_ref["id"], body, expected_version=run_ref["version"])
            ),
            mission_id="synthetic-mission",
        )
        return result

    board.propose = race
    result = propose(work, run_ref, actor, control_ref, root_ref)
    optional = store.get(**result["control_ref"])["body"]
    assert optional["scope_status"] == "pending_next_scope"
    assert optional["phase"] == "waiting"
    assert store.get("board_proposal", "child")["body"]["status"] == "open"
