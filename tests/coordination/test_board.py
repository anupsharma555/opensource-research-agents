"""Offline board fixtures: peer authority, ownership, plurality and repair."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ora.agents import AgentRegistry
from ora.coordination.board import ProposalBoard, ref
from ora.coordination.mailbox import PeerMailbox
from ora.state import StateStore
from ora.state.store import canonical

ROOT = Path(__file__).resolve().parents[2]


def actors(store):
    registry = AgentRegistry(store, ROOT)
    return [registry.register(
        f"agent-{number}", mission_id="mission", run_id="run",
        role_id="evidence_preparation_synthesis", display_name=f"Peer {number}",
    )["id"] for number in range(4)]


def proposal(board, actor, proposal_id="question", **kwargs):
    return board.propose(
        f"propose-{proposal_id}", mission_id="mission", proposal_id=proposal_id,
        actor_id=actor, objective="Compare competing explanations",
        expected_artifact="Evidence summary", next_action="Search sources",
        **kwargs,
    )


def artifact(tx, artifact_id):
    return ref(tx.put("artifact", artifact_id, {
        "mission_id": "mission", "synthetic": True, "content": {"estimate": [1, 2]}}))


def test_concurrent_claim_and_expired_owner_cannot_commit(tmp_path):
    clock = [100.0]
    store = StateStore(tmp_path, clock=lambda: clock[0])
    first, second, *_ = actors(store)
    board = ProposalBoard(store)
    assert proposal(board, first)["status"] == "accepted"
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(
            lambda pair: board.claim(pair[0], mission_id="mission", proposal_id="question",
                                     actor_id=pair[1], ttl=5),
            [("claim-one", first), ("claim-two", second)],
        ))
    assert [item["status"] for item in claims].count("accepted") == 1
    winner = next(item for item in claims if item["status"] == "accepted")
    loser = second if winner["fence"]["owner"] == first else first
    assert next(item for item in claims if item["status"] == "rejected")["code"] == "conflict"
    assert board.open_work("mission")[0]["current_claim"]["owner"] == winner["fence"]["owner"]
    clock[0] = 106.0
    replacement = board.claim("replacement", mission_id="mission", proposal_id="question",
                              actor_id=loser, ttl=10, expected_generation=1)
    assert replacement["status"] == "accepted"
    rejected = board.accept_result(
        "stale-result", mission_id="mission", proposal_id="question",
        actor_id=winner["fence"]["owner"], fence=winner["fence"],
        outcome_id="stale", save_related=lambda tx: [artifact(tx, "stale")],
        related_request={"artifact_id": "stale"},
    )
    assert rejected["code"] == "stale_claim"
    assert store.get("artifact", "stale") is None
    accepted = board.accept_result(
        "current-result", mission_id="mission", proposal_id="question",
        actor_id=loser, fence=replacement["fence"], outcome_id="current",
        save_related=lambda tx: [artifact(tx, "current")],
        related_request={"artifact_id": "current"},
    )
    assert accepted["status"] == "accepted"
    assert store.get("board_outcome", "current")["body"]["result_refs"] == [
        {"kind": "artifact", "id": "current", "version": 1}]
    assert not board.open_work("mission")
    assert len([e for e in StateStore(tmp_path, clock=lambda: clock[0]).events("mission")
                if e["type"] == "proposal_result_accepted"]) == 1


def test_claim_exact_proposal_version_is_atomic_and_old_replay_hash_is_stable(tmp_path):
    store = StateStore(tmp_path)
    actor, *_ = actors(store)
    board = ProposalBoard(store)
    assert proposal(board, actor)["status"] == "accepted"
    changed = board.revise(
        "revised-task", mission_id="mission", proposal_id="question",
        actor_id=actor, expected_version=1,
        reason="The requested scope changed", next_action="Inspect new material",
    )
    assert changed["status"] == "accepted"
    obsolete = board.claim(
        "old-version-claim", mission_id="mission", proposal_id="question",
        actor_id=actor, ttl=10, expected_version=1,
    )
    assert obsolete["status"] == "rejected" and obsolete["code"] == "conflict"
    assert board.open_work("mission")[0]["current_claim"] is None
    assert board.claim(
        "invalid-version-claim", mission_id="mission", proposal_id="question",
        actor_id=actor, ttl=10, expected_version=True,
    )["status"] == "rejected"
    fresh = board.claim(
        "fresh-version-claim", mission_id="mission", proposal_id="question",
        actor_id=actor, ttl=10, expected_version=2,
    )
    assert fresh["status"] == "accepted"
    assert store.get(**fresh["claim_ref"])["body"]["proposal_ref"] == changed["proposal_ref"]

    assert proposal(board, actor, proposal_id="legacy-question")["status"] == "accepted"
    legacy_request = {
        "operation_id": "legacy-claim", "mission_id": "mission",
        "proposal_id": "legacy-question", "actor_id": actor, "ttl": 10,
        "expected_generation": None, "invitation_id": None,
    }
    legacy = board.claim(
        "legacy-claim", mission_id="mission", proposal_id="legacy-question",
        actor_id=actor, ttl=10,
    )
    assert legacy["status"] == "accepted"
    restarted = ProposalBoard(StateStore(tmp_path))
    replay = restarted.claim(
        "legacy-claim", mission_id="mission", proposal_id="legacy-question",
        actor_id=actor, ttl=10,
    )
    assert replay == legacy
    db = store._connect()
    try:
        stored = db.execute(
            "SELECT request_hash FROM operations WHERE id='legacy-claim'"
        ).fetchone()["request_hash"]
    finally:
        db.close()
    assert stored == hashlib.sha256(canonical({
        "request": legacy_request, "mission_id": "mission", "fence": None,
    }).encode()).hexdigest()
    assert restarted.claim(
        "legacy-claim", mission_id="mission", proposal_id="legacy-question",
        actor_id=actor, ttl=10,
    ) == legacy


def test_invitation_decisions_and_unclaimed_work(tmp_path):
    store = StateStore(tmp_path)
    a, b, c, *_ = actors(store)
    board = ProposalBoard(store)
    proposal(board, a)
    for label, recipient in (("decline", b), ("counter", c)):
        assert board.invite(
            f"invite-{label}", mission_id="mission", invitation_id=label,
            proposal_id="question", sender_id=a, recipient_id=recipient,
            objective="Independent appraisal", expected_artifact="Critique",
        )["status"] == "accepted"
    assert board.respond_invitation("declined", mission_id="mission", invitation_id="decline",
                                   recipient_id=b, expected_version=1,
                                   decision="declined")["status"] == "accepted"
    assert board.claim("declined-bypass", mission_id="mission", proposal_id="question",
                       actor_id=b, ttl=10)["status"] == "rejected"
    assert board.claim("unaccepted", mission_id="mission", proposal_id="question",
                       actor_id=c, ttl=10, invitation_id="counter")["status"] == "rejected"
    assert board.claim("offered-bypass", mission_id="mission", proposal_id="question",
                       actor_id=c, ttl=10)["status"] == "rejected"
    counter = board.respond_invitation("countered", mission_id="mission",
                                      invitation_id="counter", recipient_id=c,
                                      expected_version=1, decision="countered",
                                      counter="Need source methods first")
    assert counter["status"] == "accepted"
    assert board.open_work("mission")[0]["current_claim"] is None
    assert board.invite("revised-invite", mission_id="mission", invitation_id="revised",
                        proposal_id="question", sender_id=a, recipient_id=c,
                        objective="Independent appraisal after source methods arrive",
                        expected_artifact="Critique")["status"] == "accepted"
    assert board.respond_invitation("accepted", mission_id="mission", invitation_id="revised",
                                   recipient_id=c, expected_version=1,
                                   decision="accepted")["status"] == "accepted"
    claimed = board.claim("claim-accepted", mission_id="mission", proposal_id="question",
                          actor_id=c, ttl=10, invitation_id="revised")
    assert claimed["status"] == "accepted"
    assert board.open_work("mission")[0]["current_claim"]["owner"] == c


def test_inviter_replays_each_decision_without_task_subscription(tmp_path):
    store = StateStore(tmp_path)
    inviter, recipient, *_ = actors(store)
    board = ProposalBoard(store)
    mailbox = PeerMailbox(store)
    proposal(board, inviter)
    assert mailbox.subscribe(
        "watch-unrelated", mission_id="mission", actor_id=inviter,
        subjects=[{"kind": "task", "id": "unrelated"}],
    )["status"] == "accepted"
    for decision in ("accepted", "declined", "countered"):
        invitation_id = f"invitation-{decision}"
        offered = board.invite(
            f"offer-{decision}", mission_id="mission", invitation_id=invitation_id,
            proposal_id="question", sender_id=inviter, recipient_id=recipient,
            objective="Independent appraisal", expected_artifact="Critique",
        )
        assert offered["status"] == "accepted"
        decided = board.respond_invitation(
            f"decide-{decision}", mission_id="mission", invitation_id=invitation_id,
            recipient_id=recipient, expected_version=1, decision=decision,
            counter="Need methods" if decision == "countered" else None,
        )
        assert decided["status"] == "accepted"

    restarted = PeerMailbox(StateStore(tmp_path))
    events = [event for event in restarted.catch_up("mission", inviter)["events"]
              if event["type"] == "invitation_decided"]
    assert [event["body"]["decision"] for event in events] == [
        "accepted", "declined", "countered"]
    assert all(event["body"]["recipient_ids"] == [inviter, recipient]
               for event in events)
    assert [event["body"]["invitation_ref"]["version"] for event in events] == [2, 2, 2]
    assert not any(event["type"] == "invitation_offered"
                   for event in restarted.catch_up("mission", inviter)["events"])


def test_plurality_revision_targeted_reopen_and_local_rejection(tmp_path):
    store = StateStore(tmp_path)
    a, b, *_ = actors(store)
    board = ProposalBoard(store)
    source = store.transact("source", {}, lambda tx: tx.put(
        "source_copy", "source", {"synthetic": True}), mission_id="mission")
    contradiction = store.transact("contradiction", {}, lambda tx: tx.put(
        "source_copy", "contradiction", {"synthetic": True}), mission_id="mission")
    root = proposal(board, a, prerequisite_refs=[ref(source)])
    assert proposal(board, b, "corroborate", depends_on=["question"])["status"] == "accepted"
    assert proposal(board, b, "alternative", branch_of="question")["status"] == "accepted"
    assert proposal(board, a, "unrelated")["status"] == "accepted"
    assert {item["proposal_ref"]["id"] for item in board.ready_work("mission")} == {
        "question", "alternative", "unrelated"}
    assert board.claim("too-early", mission_id="mission", proposal_id="corroborate",
                       actor_id=b, ttl=10)["status"] == "rejected"
    dissent = board.dissent("dissent", mission_id="mission", dissent_id="critique",
                            proposal_ref=root["proposal_ref"], actor_id=b,
                            critique="The abstract cannot establish causation",
                            evidence_refs=[ref(source)])
    assert dissent["status"] == "accepted"
    revised = board.revise("revise", mission_id="mission", proposal_id="question", actor_id=b,
                           expected_version=1, reason="Methods unresolved",
                           next_action="Seek full text", evidence_refs=[ref(source)])
    assert revised["proposal_ref"]["version"] == 2
    bad = board.revise("stale", mission_id="mission", proposal_id="question", actor_id=a,
                       expected_version=1, reason="Old input")
    assert bad["status"] == "rejected"
    assert store.get("board_rejection", "stale")["body"]["code"] == "conflict"
    affected_fence = board.claim("affected-claim", mission_id="mission",
                                 proposal_id="question", actor_id=b, ttl=60)["fence"]
    old_result = board.accept_result(
        "affected-result", mission_id="mission", proposal_id="question",
        actor_id=b, fence=affected_fence, outcome_id="old-result",
        save_related=lambda tx: [artifact(tx, "old-evidence")],
        related_request={"artifact_id": "old-evidence"},
    )
    assert old_result["status"] == "accepted"
    unaffected_fence = board.claim("unaffected-claim", mission_id="mission",
                                   proposal_id="unrelated", actor_id=a, ttl=60)["fence"]
    assert board.accept_result(
        "unaffected-result", mission_id="mission", proposal_id="unrelated",
        actor_id=a, fence=unaffected_fence, outcome_id="unaffected-result",
        save_related=lambda tx: [artifact(tx, "unaffected-evidence")],
        related_request={"artifact_id": "unaffected-evidence"},
    )["status"] == "accepted"
    reopened = board.reopen("reopen", mission_id="mission", affected_ref=ref(source),
                            contradiction_ref=ref(contradiction), actor_id=b,
                            reason="Contradictory source", next_action="Reassess methods")
    assert {p["id"] for p in reopened["proposal_refs"]} == {"question", "corroborate"}
    assert store.get("board_proposal", "question", 1)["body"]["objective"] == \
        "Compare competing explanations"
    assert store.get("board_dissent", "critique") is not None
    assert store.get("board_outcome", "old-result") is not None
    assert store.get("board_proposal", "alternative")["version"] == 1
    assert store.get("board_proposal", "unrelated")["version"] == 2
    assert store.get("board_proposal", "unrelated")["body"]["status"] == "completed"
    assert board.accept_result(
        "obsolete-after-reopen", mission_id="mission", proposal_id="question",
        actor_id=b, fence=affected_fence, outcome_id="obsolete",
        result_refs=[{"kind": "artifact", "id": "old-evidence", "version": 1}],
    )["code"] == "stale_claim"


def test_result_artifact_and_peer_delivery_intent_commit_together(tmp_path):
    store = StateStore(tmp_path)
    a, b, *_ = actors(store)
    board = ProposalBoard(store)
    mailbox = PeerMailbox(store)
    proposal_ref = proposal(board, a)["proposal_ref"]
    fence = board.claim("claim", mission_id="mission", proposal_id="question",
                        actor_id=a, ttl=60)["fence"]

    def save_related(tx):
        evidence = artifact(tx, "bundle")
        message = mailbox.send_message_tx(
            tx, mission_id="mission", message_id="notify", sender_id=a,
            recipient_id=b, correlation_id="question", payload={"result": evidence},
            task_ref=proposal_ref, evidence_refs=[evidence])
        return [evidence, message["message_ref"]]

    accepted = board.accept_result("result", mission_id="mission", proposal_id="question",
                                   actor_id=a, fence=fence, outcome_id="outcome",
                                   save_related=save_related,
                                   related_request={"artifact_id": "bundle", "message_id": "notify"})
    assert accepted["status"] == "accepted"
    restarted = StateStore(tmp_path)
    assert restarted.get("artifact", "bundle") is not None
    assert restarted.get("mail_message", "notify") is not None
    assert len(PeerMailbox(restarted).pending_outbox("mission")) == 1
    assert len(restarted.get("board_outcome", "outcome")["body"]["result_refs"]) == 2


def test_revised_proposal_requires_fresh_invitation(tmp_path):
    store = StateStore(tmp_path)
    a, b, *_ = actors(store)
    board = ProposalBoard(store)
    proposal(board, a)
    board.invite("invite", mission_id="mission", invitation_id="old",
                 proposal_id="question", sender_id=a, recipient_id=b,
                 objective="Check source", expected_artifact="Critique")
    board.respond_invitation("accept", mission_id="mission", invitation_id="old",
                            recipient_id=b, expected_version=1, decision="accepted")
    board.invite("second-invite", mission_id="mission", invitation_id="offered-stale",
                 proposal_id="question", sender_id=a, recipient_id=b,
                 objective="Check methods", expected_artifact="Methods note")
    board.revise("revise", mission_id="mission", proposal_id="question",
                 actor_id=a, expected_version=1, reason="New source changes task")
    assert board.respond_invitation("late-accept", mission_id="mission", invitation_id="offered-stale",
                                   recipient_id=b, expected_version=1,
                                   decision="accepted")["status"] == "rejected"
    assert board.claim("old-invite-claim", mission_id="mission", proposal_id="question",
                       actor_id=b, ttl=10, invitation_id="old")["status"] == "rejected"
    assert board.open_work("mission")[0]["current_claim"] is None


def test_revision_revokes_claim_even_if_clock_moves_back(tmp_path):
    clock = [100.0]
    store = StateStore(tmp_path, clock=lambda: clock[0])
    a, b, *_ = actors(store)
    board = ProposalBoard(store)
    proposal(board, a)
    old = board.claim("old-claim", mission_id="mission", proposal_id="question",
                      actor_id=a, ttl=50)["fence"]
    clock[0] = 120.0
    assert board.revise("change-task", mission_id="mission", proposal_id="question",
                        actor_id=b, expected_version=1,
                        reason="Contradictory methods", next_action="Seek comparator")["status"] == "accepted"
    clock[0] = 110.0
    rejected = board.accept_result(
        "obsolete-result", mission_id="mission", proposal_id="question",
        actor_id=a, fence=old, outcome_id="obsolete",
        result_refs=[ref(store.transact("unrelated-artifact", {}, lambda tx: tx.put(
            "artifact", "prior", {"mission_id": "mission"}), mission_id="mission"))],
    )
    assert rejected["code"] == "stale_claim"
    assert store.get("board_outcome", "obsolete") is None
    fresh = board.claim("fresh-claim", mission_id="mission", proposal_id="question",
                        actor_id=b, ttl=10, expected_generation=2)
    assert fresh["status"] == "accepted"
    assert fresh["fence"]["generation"] == 3
    assert board.accept_result(
        "missing-fence", mission_id="mission", proposal_id="question",
        actor_id=b, fence=None, outcome_id="missing",
        result_refs=[{"kind": "artifact", "id": "prior", "version": 1}],
    )["status"] == "rejected"
    assert store.get("board_rejection", "missing-fence") is not None


def test_shared_source_reopen_stays_within_requested_mission(tmp_path):
    store = StateStore(tmp_path)
    first, *_ = actors(store)
    other = AgentRegistry(store, ROOT).register(
        "other-mission-peer", mission_id="other-mission", run_id="other-run",
        role_id="evidence_preparation_synthesis", display_name="Other Peer",
    )["id"]
    source = store.transact("shared-source", {}, lambda tx: tx.put(
        "source_copy", "shared", {"synthetic": True}), mission_id="mission")
    contradiction = store.transact("new-source", {}, lambda tx: tx.put(
        "source_copy", "new", {"synthetic": True}), mission_id="mission")
    board = ProposalBoard(store)
    proposal(board, first, prerequisite_refs=[ref(source)])
    foreign = board.propose(
        "foreign-proposal", mission_id="other-mission", proposal_id="foreign",
        actor_id=other, objective="Inspect shared copy", expected_artifact="Note",
        next_action="Read source", prerequisite_refs=[ref(source)],
    )
    assert foreign["status"] == "accepted"
    reopened = board.reopen(
        "reopen-local", mission_id="mission", affected_ref=ref(source),
        contradiction_ref=ref(contradiction), actor_id=first,
        reason="New contradiction", next_action="Reassess local portfolio",
    )
    assert [item["id"] for item in reopened["proposal_refs"]] == ["question"]
    assert store.get("board_proposal", "foreign")["version"] == 1
    assert store.review_status(store.get("board_proposal", "foreign")) == []
