"""Keep partial peer work and its exact critique context visible to reviewers."""

import asyncio
from pathlib import Path

from ora.agents import AgentRegistry
from ora.coordination.board import ProposalBoard, ref
from ora.coordination.capacity import CapacityPolicy, CapacityScheduler
from ora.coordination.executor import ConcurrentExecutor
from ora.coordination.mailbox import PeerMailbox
from ora.coordination.traces import TraceJournal
from ora.state import StateStore
from ora.state.view_policy import project_record

ROOT = Path(__file__).resolve().parents[2]


def test_partial_answer_remains_pending_with_readable_critique_and_invitation(tmp_path):
    store = StateStore(tmp_path)
    registry = AgentRegistry(store, ROOT)
    requester, responder = [registry.register(
        f"register-{name}", mission_id="mission", run_id="run",
        role_id="evidence_preparation_synthesis", display_name=name)
        for name in ("Sage", "Finch")]
    board = ProposalBoard(store)
    mailbox = PeerMailbox(store)
    proposal = board.propose(
        "propose", mission_id="mission", proposal_id="question",
        actor_id=requester["id"], objective="Check a supplied abstract's comparison",
        expected_artifact="A supported comparison", next_action="Seek methods detail")
    invitation = board.invite(
        "invite", mission_id="mission", invitation_id="invitation", proposal_id="question",
        sender_id=requester["id"], recipient_id=responder["id"],
        objective="Check the reported comparator", expected_artifact="A methods note")
    dissent = board.dissent(
        "dissent", mission_id="mission", dissent_id="critique",
        proposal_ref=proposal["proposal_ref"], actor_id=responder["id"],
        critique="The abstract does not identify the control condition.")
    request = mailbox.send_request(
        "request", mission_id="mission", request_id="request", message_id="message",
        requester_id=requester["id"], recipient_id=responder["id"],
        question="What comparison is explicitly reported?", expected_artifact="A methods note",
        cause_ref=dissent["dissent_ref"], task_ref=proposal["proposal_ref"], payload={})
    assert request["status"] == "accepted"
    answer = mailbox.record_answer(
        "answer", mission_id="mission", request_id="request", answer_id="answer",
        responder_id=responder["id"], artifact_ref=dissent["dissent_ref"], partial=True)
    assert answer["status"] == "accepted"
    executor = ConcurrentExecutor(
        store, board, mailbox,
        CapacityScheduler(store, CapacityPolicy(permits={"model": 1, "retrieval": 1, "compute": 1})),
        TraceJournal(store), {}, owner="host")
    observed = asyncio.run(executor.run_ready("mission"))
    assert observed["pending_peer_requests"] == ["request"]
    assert observed["mission_status"] == "open"
    assert store.get("mail_request", "request")["body"]["status"] == "partial_awaiting_review"

    visible = project_record(store.get(**dissent["dissent_ref"]))
    assert visible["critique"] == "The abstract does not identify the control condition."
    assert visible["actor_ref"] == ref(responder)
    assert visible["proposal_ref"] == proposal["proposal_ref"]
    visible_invitation = project_record(store.get(**invitation["invitation_ref"]))
    assert visible_invitation["sender_ref"] == ref(requester)
    assert visible_invitation["objective"] == "Check the reported comparator"
    visible_answer = project_record(store.get(**answer["answer_ref"]))
    assert visible_answer["request_id"] == "request"
    assert visible_answer["artifact_ref"] == dissent["dissent_ref"]
