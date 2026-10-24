"""Board prose retains ordinary formatting without weakening record IDs."""

from __future__ import annotations

import pytest

from ora.coordination.board import ProposalBoard, ref
from ora.state import StateStore
from tests.coordination.test_board import actors


def test_multiline_proposal_revision_and_reopen_preserve_exact_text(tmp_path):
    store = StateStore(tmp_path)
    first, second, *_ = actors(store)
    board = ProposalBoard(store)
    objective = "Compare two observations:\n\tfirst result\r\nsecond result"
    artifact = "A note with:\n\tfindings and limits"
    action = "Read page one\nthen page two"
    kwargs = {
        "mission_id": "mission", "proposal_id": "multiline",
        "actor_id": first, "objective": objective,
        "expected_artifact": artifact, "next_action": action,
    }
    created = board.propose("multiline-create", **kwargs)
    assert created["status"] == "accepted"
    assert board.propose("multiline-create", **kwargs) == created
    assert board.propose("multiline-conflict", **kwargs)["code"] == "conflict"
    saved = StateStore(tmp_path).get("board_proposal", "multiline")
    assert (saved["body"]["objective"], saved["body"]["expected_artifact"],
            saved["body"]["next_action"]) == (objective, artifact, action)
    invitation = board.invite(
        "multiline-invite", mission_id="mission", invitation_id="multiline-invite",
        proposal_id="multiline", sender_id=first, recipient_id=second,
        objective=objective, expected_artifact=artifact,
    )
    assert invitation["status"] == "accepted"
    invitation_body = store.get("board_invitation", "multiline-invite")["body"]
    assert invitation_body["objective"] == objective
    assert invitation_body["expected_artifact"] == artifact

    revised_objective = "Reassess:\n\tone\n\ttwo"
    revised_action = "Check the contradiction\n\tthen report"
    reopened = ProposalBoard(StateStore(tmp_path))
    revision = reopened.revise(
        "multiline-revise", mission_id="mission", proposal_id="multiline",
        actor_id=second, expected_version=1, reason="Scope changed",
        objective=revised_objective, next_action=revised_action,
    )
    assert revision["status"] == "accepted"
    assert reopened.revise(
        "multiline-revise", mission_id="mission", proposal_id="multiline",
        actor_id=second, expected_version=1, reason="Scope changed",
        objective=revised_objective, next_action=revised_action,
    ) == revision
    assert reopened.revise(
        "multiline-stale", mission_id="mission", proposal_id="multiline",
        actor_id=second, expected_version=1, reason="Scope changed",
        objective="Different text",
    )["code"] == "conflict"
    current = StateStore(tmp_path).get("board_proposal", "multiline")
    assert current["body"]["objective"] == revised_objective
    assert current["body"]["next_action"] == revised_action

    contradiction = store.transact(
        "multiline-contradiction", {},
        lambda tx: tx.put("source_copy", "multiline-contradiction", {"synthetic": True}),
        mission_id="mission",
    )
    final_action = "Reopen the note:\n\tcheck both observations"
    result = reopened.reopen(
        "multiline-reopen", mission_id="mission", affected_ref=ref(current),
        contradiction_ref=ref(contradiction), actor_id=second,
        reason="Contradiction found", next_action=final_action,
    )
    assert result["status"] == "accepted"
    assert StateStore(tmp_path).get("board_proposal", "multiline")[
        "body"
    ]["next_action"] == final_action


@pytest.mark.parametrize("field", ["objective", "expected_artifact", "next_action"])
@pytest.mark.parametrize("bad", ["   \n\t", "unsafe\x00text", "unsafe\x1btext", 4])
def test_prose_rejects_empty_unsafe_or_nontext_values(tmp_path, field, bad):
    store = StateStore(tmp_path)
    actor, *_ = actors(store)
    board = ProposalBoard(store)
    request = {
        "mission_id": "mission", "proposal_id": "valid-id",
        "actor_id": actor, "objective": "A task",
        "expected_artifact": "A note", "next_action": "Read source",
    }
    request[field] = bad
    rejected = board.propose(f"bad-{field}-{repr(bad)}", **request)
    assert rejected["status"] == "rejected"
    assert store.get("board_proposal", "valid-id") is None


def test_proposal_id_still_rejects_newline(tmp_path):
    store = StateStore(tmp_path)
    actor, *_ = actors(store)
    rejected = ProposalBoard(store).propose(
        "bad-id", mission_id="mission", proposal_id="bad\nid",
        actor_id=actor, objective="Readable\nquestion",
        expected_artifact="Note", next_action="Read source",
    )
    assert rejected["status"] == "rejected"
    assert store.records("board_proposal") == []


@pytest.mark.parametrize("field", ["objective", "next_action"])
@pytest.mark.parametrize("bad", ["\n\t  ", "unsafe\x00text", 4])
def test_revision_rejects_unsafe_prose_without_changing_version(tmp_path, field, bad):
    store = StateStore(tmp_path)
    actor, *_ = actors(store)
    board = ProposalBoard(store)
    assert board.propose(
        "safe-create", mission_id="mission", proposal_id="safe",
        actor_id=actor, objective="Original", expected_artifact="Note",
        next_action="Read",
    )["status"] == "accepted"
    rejected = board.revise(
        f"bad-revision-{field}-{repr(bad)}", mission_id="mission",
        proposal_id="safe", actor_id=actor, expected_version=1,
        reason="Check correction", **{field: bad},
    )
    assert rejected["status"] == "rejected"
    assert store.get("board_proposal", "safe")["version"] == 1
