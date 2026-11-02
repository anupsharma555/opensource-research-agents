"""Generated peer interleavings against durable, caller-visible board and mail state."""

from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import example, given, settings
from hypothesis import strategies as st

from ora.agents import AgentRegistry
from ora.coordination.board import ProposalBoard, ref
from ora.coordination.mailbox import PeerMailbox
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[2]
MISSION = "synthetic-mission"


def _peers(store):
    registry = AgentRegistry(store, ROOT)
    return [
        registry.register(
            f"register-{number}",
            mission_id=MISSION,
            run_id="synthetic-run",
            role_id="evidence_preparation_synthesis",
            display_name=f"Synthetic peer {number}",
        )["id"]
        for number in range(3)
    ]


def _proposal(board, number, actor):
    return board.propose(
        f"propose-{number}",
        mission_id=MISSION,
        proposal_id=f"task-{number}",
        actor_id=actor,
        objective=f"Inspect synthetic question {number}",
        expected_artifact="Synthetic methods note",
        next_action="Ask a peer",
    )["proposal_ref"]


def _artifact(store, number):
    return ref(
        store.transact(
            f"artifact-{number}",
            {"number": number},
            lambda tx: tx.put(
                "artifact",
                f"artifact-{number}",
                {
                    "mission_id": MISSION,
                    "synthetic": True,
                    "number": number,
                },
            ),
            mission_id=MISSION,
        )
    )


@settings(max_examples=18, deadline=None)
@example(
    actions=[
        (0, "answer"),
        (0, "review"),
        (0, "deliver"),
        (1, "decline"),
        (1, "answer"),
        (0, "reopen"),
    ],
    marker=11,
)
@example(
    actions=[
        (0, "review"),
        (0, "bad_review"),
        (0, "answer"),
        (0, "bad_review"),
        (0, "review"),
        (0, "answer"),
    ],
    marker=12,
)
@example(
    actions=[
        (1, "answer"),
        (1, "decline"),
        (1, "review"),
        (0, "replay_request"),
        (0, "deliver"),
        (0, "answer"),
    ],
    marker=13,
)
@given(
    st.lists(
        st.tuples(
            st.integers(0, 1),
            st.sampled_from(
                ("deliver", "answer", "review", "bad_review", "decline", "replay_request", "reopen")
            ),
        ),
        min_size=5,
        max_size=16,
    ),
    st.integers(1, 9999),
)
def test_generated_peer_delivery_review_and_replay_survive_reopen(actions, marker):
    """A review is an explicit decision; delivery and answer alone cannot supply it."""
    with TemporaryDirectory(prefix="ora-peer-recovery-") as directory:
        tick = [1000.0]

        def clock():
            return tick[0]

        store = StateStore(directory, clock=clock)
        peers = _peers(store)
        board = ProposalBoard(store)
        tasks = [_proposal(board, number, peers[0]) for number in range(2)]
        artifacts = [_artifact(store, number) for number in range(2)]
        mail = PeerMailbox(store)
        expected = [
            {"status": "unanswered", "delivered": False, "answer_ref": None} for _ in range(2)
        ]
        questions = [
            f"Which synthetic comparison {marker}-{number} is supported?" for number in range(2)
        ]
        payloads = [
            {"marker": marker, "branch": number, "values": [number, None]} for number in range(2)
        ]
        opened = []
        for number in range(2):
            result = mail.send_request(
                f"open-{number}",
                mission_id=MISSION,
                request_id=f"request-{number}",
                message_id=f"message-{number}",
                requester_id=peers[0],
                recipient_id=peers[number + 1],
                question=questions[number],
                expected_artifact="Synthetic methods note",
                cause_ref=tasks[number],
                task_ref=tasks[number],
                payload=payloads[number],
            )
            assert result["status"] == "accepted"
            opened.append(result)

        def inspect_saved():
            # A new reader is deliberately used even when the command writer stayed open.
            reopened = StateStore(directory, clock=clock)
            reader = PeerMailbox(reopened)
            for number, model in enumerate(expected):
                request = reader.request_view(f"request-{number}")
                message = reopened.get("mail_message", f"message-{number}")
                outbox = reopened.get("mail_outbox", f"message-{number}")
                assert request["question"] == questions[number]
                assert request["expected_artifact"] == "Synthetic methods note"
                assert request["status"] == model["status"]
                assert request["accepted_answer_ref"] == (
                    model["answer_ref"] if model["status"] == "answered" else None
                )
                assert request["answer_refs"] == (
                    [model["answer_ref"]] if model["answer_ref"] else []
                )
                assert message["body"]["payload"] == payloads[number]
                assert message["body"]["correlation_id"] == f"request-{number}"
                assert len(reader.inbox(MISSION, peers[number + 1])) == 1
                assert bool(request["delivered_at"]) == model["delivered"]
                assert outbox["body"]["status"] == (
                    "delivered" if model["delivered"] else "pending"
                )
            assert len(reader.pending_outbox(MISSION)) == sum(
                not model["delivered"] for model in expected
            )
            assert len(reopened.records("mail_answer")) == sum(
                model["answer_ref"] is not None for model in expected
            )
            counts = Counter(event["type"] for event in reopened.events(MISSION))
            assert counts["peer_request_opened"] == 2
            assert counts["peer_message_pending"] == 2
            assert counts["peer_message_delivered"] == sum(model["delivered"] for model in expected)
            assert counts["peer_answer_submitted"] == sum(
                model["answer_ref"] is not None for model in expected
            )
            assert counts["peer_request_reviewed"] == sum(
                model["status"] == "answered" for model in expected
            )
            assert counts["peer_request_declined"] == sum(
                model["status"] == "declined" for model in expected
            )

        def act(number, action, operation):
            nonlocal mail
            model = expected[number]
            if action == "reopen":
                mail = PeerMailbox(StateStore(directory, clock=clock))
                return
            if action == "replay_request":
                result = mail.send_request(
                    f"open-{number}",
                    mission_id=MISSION,
                    request_id=f"request-{number}",
                    message_id=f"message-{number}",
                    requester_id=peers[0],
                    recipient_id=peers[number + 1],
                    question=questions[number],
                    expected_artifact="Synthetic methods note",
                    cause_ref=tasks[number],
                    task_ref=tasks[number],
                    payload=payloads[number],
                )
                assert result == opened[number]
                return
            if action == "changed_request":
                result = mail.send_request(
                    f"open-{number}",
                    mission_id=MISSION,
                    request_id=f"request-{number}",
                    message_id=f"message-{number}",
                    requester_id=peers[0],
                    recipient_id=peers[number + 1],
                    question="Different question",
                    expected_artifact="Synthetic methods note",
                    cause_ref=tasks[number],
                    task_ref=tasks[number],
                    payload=payloads[number],
                )
                assert result["status"] == "rejected" and result["code"] == "conflict"
                return
            if action == "deliver":
                result = mail.mark_delivered(
                    operation, mission_id=MISSION, message_id=f"message-{number}"
                )
                assert result["status"] == "accepted"
                assert (
                    mail.mark_delivered(
                        operation, mission_id=MISSION, message_id=f"message-{number}"
                    )
                    == result
                )
                model["delivered"] = True
            elif action == "answer":
                result = mail.record_answer(
                    f"answer-op-{number}",
                    mission_id=MISSION,
                    request_id=f"request-{number}",
                    answer_id=f"answer-{number}",
                    responder_id=peers[number + 1],
                    artifact_ref=artifacts[number],
                )
                if model["answer_ref"] is not None:
                    assert result["status"] == "accepted"  # Exact operation replay.
                    assert result["answer_ref"] == model["answer_ref"]
                elif model["status"] == "declined":
                    assert result["status"] == "rejected"
                else:
                    assert result["status"] == "accepted"
                    model["answer_ref"] = result["answer_ref"]
                    model["status"] = "awaiting_review"
            elif action in {"review", "bad_review"}:
                result = mail.review_answer(
                    operation,
                    mission_id=MISSION,
                    request_id=f"request-{number}",
                    reviewer_id=peers[number + 1] if action == "bad_review" else peers[0],
                    answer_id=f"answer-{number}",
                    disposition="answered",
                    reason="Synthetic comparison checked",
                )
                permitted = (
                    action == "review"
                    and model["answer_ref"] is not None
                    and model["status"] == "awaiting_review"
                )
                assert result["status"] == ("accepted" if permitted else "rejected")
                if permitted:
                    model["status"] = "answered"
            elif action == "decline":
                if number == 0:
                    return  # Keep one branch available for a complete reviewed answer.
                result = mail.decline(
                    operation,
                    mission_id=MISSION,
                    request_id=f"request-{number}",
                    recipient_id=peers[number + 1],
                    reason="Cannot verify synthetic claim",
                )
                permitted = model["status"] not in {"answered", "declined"}
                assert result["status"] == ("accepted" if permitted else "rejected")
                if permitted:
                    model["status"] = "declined"

        inspect_saved()
        for step, (number, action) in enumerate(actions):
            tick[0] += 1
            act(number, action, f"generated-{step}-{number}-{action}")
            inspect_saved()

        # Every generated example reaches a reviewed answer on one branch;
        # refusal or unresolved work on the other cannot block that branch.
        if not expected[0]["delivered"]:
            act(0, "deliver", "finish-delivery")
        if expected[0]["answer_ref"] is None:
            act(0, "answer", "finish-answer")
        if expected[0]["status"] != "answered":
            act(0, "review", "finish-review")
        assert expected[0]["status"] == "answered"
        if expected[1]["status"] not in {"answered", "declined"}:
            act(1, "decline", "finish-decline")
        act(0, "changed_request", "changed-request")
        inspect_saved()


@settings(max_examples=12, deadline=None)
@example(swap=False, ttl=2)
@example(swap=True, ttl=12)
@given(st.booleans(), st.integers(min_value=2, max_value=12))
def test_generated_expired_claim_cannot_duplicate_accepted_peer_work(swap, ttl):
    """A losing lease leaves another proposal and the replacement owner usable."""
    with TemporaryDirectory(prefix="ora-peer-claims-") as directory:
        tick = [2000.0]

        def clock():
            return tick[0]

        store = StateStore(directory, clock=clock)
        peers = _peers(store)
        board = ProposalBoard(store)
        for number in range(2):
            _proposal(board, number, peers[0])
        artifacts = [_artifact(store, number) for number in range(2)]
        stale, independent = (1, 0) if swap else (0, 1)

        original = board.claim(
            "original-claim",
            mission_id=MISSION,
            proposal_id=f"task-{stale}",
            actor_id=peers[0],
            ttl=ttl,
        )
        assert original["status"] == "accepted"
        denied = board.claim(
            "conflicting-claim",
            mission_id=MISSION,
            proposal_id=f"task-{stale}",
            actor_id=peers[2],
            ttl=ttl,
        )
        assert denied["status"] == "rejected" and denied["code"] == "conflict"

        tick[0] += ttl + 1
        store = StateStore(directory, clock=clock)
        board = ProposalBoard(store)
        assert (
            board.claim(
                "original-claim",
                mission_id=MISSION,
                proposal_id=f"task-{stale}",
                actor_id=peers[0],
                ttl=ttl,
            )
            == original
        )
        replacement = board.claim(
            "replacement-claim",
            mission_id=MISSION,
            proposal_id=f"task-{stale}",
            actor_id=peers[2],
            ttl=ttl,
            expected_generation=1,
        )
        assert replacement["status"] == "accepted"
        old = board.accept_result(
            "stale-result",
            mission_id=MISSION,
            proposal_id=f"task-{stale}",
            actor_id=peers[0],
            fence=original["fence"],
            outcome_id="stale-outcome",
            save_related=lambda tx: [
                ref(
                    tx.put(
                        "artifact",
                        "stale-artifact",
                        {
                            "mission_id": MISSION,
                            "synthetic": True,
                        },
                    )
                )
            ],
            related_request={"artifact_id": "stale-artifact"},
        )
        assert old["status"] == "rejected" and old["code"] == "stale_claim"
        assert store.get("artifact", "stale-artifact") is None
        assert store.get("board_outcome", "stale-outcome") is None

        accepted = board.accept_result(
            "current-result",
            mission_id=MISSION,
            proposal_id=f"task-{stale}",
            actor_id=peers[2],
            fence=replacement["fence"],
            outcome_id=f"outcome-{stale}",
            result_refs=[artifacts[stale]],
        )
        assert accepted["status"] == "accepted"
        other_claim = board.claim(
            "independent-claim",
            mission_id=MISSION,
            proposal_id=f"task-{independent}",
            actor_id=peers[0],
            ttl=ttl,
        )
        assert other_claim["status"] == "accepted"
        other = board.accept_result(
            "independent-result",
            mission_id=MISSION,
            proposal_id=f"task-{independent}",
            actor_id=peers[0],
            fence=other_claim["fence"],
            outcome_id=f"outcome-{independent}",
            result_refs=[artifacts[independent]],
        )
        assert other["status"] == "accepted"

        reopened = StateStore(directory, clock=clock)
        replay = ProposalBoard(reopened).accept_result(
            "current-result",
            mission_id=MISSION,
            proposal_id=f"task-{stale}",
            actor_id=peers[2],
            fence=replacement["fence"],
            outcome_id=f"outcome-{stale}",
            result_refs=[artifacts[stale]],
        )
        assert replay == accepted
        assert {item["id"] for item in reopened.records("board_outcome")} == {
            "outcome-0",
            "outcome-1",
        }
        assert all(
            reopened.get("board_proposal", f"task-{number}")["body"]["status"] == "completed"
            for number in range(2)
        )
        assert (
            sum(event["type"] == "proposal_result_accepted" for event in reopened.events(MISSION))
            == 2
        )
