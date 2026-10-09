"""Saved local messages keep delivery, requests and result review distinct."""

from pathlib import Path

import pytest

from ora.agents import AgentRegistry
from ora.coordination.board import ProposalBoard, ref
from ora.coordination.mailbox import PeerMailbox
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[2]


def setup(tmp_path, clock=None):
    store = StateStore(tmp_path, clock=clock) if clock else StateStore(tmp_path)
    registry = AgentRegistry(store, ROOT)
    peers = [registry.register(
        f"peer-{index}", mission_id="mission", run_id="run",
        role_id="evidence_preparation_synthesis", display_name=f"Peer {index}",
    )["id"] for index in range(3)]
    board = ProposalBoard(store)
    proposal = board.propose(
        "proposal", mission_id="mission", proposal_id="question", actor_id=peers[0],
        objective="Inspect source methods", expected_artifact="Methods note",
        next_action="Ask another peer",
    )
    return store, peers, proposal["proposal_ref"]


def ask(mail, peers, cause_ref, **overrides):
    operation_id = overrides.pop("operation_id", "ask")
    args = {"mission_id": "mission", "request_id": "request", "message_id": "message",
            "requester_id": peers[0], "recipient_id": peers[1],
            "question": "Does the outcome have a comparator?",
            "expected_artifact": "Source-backed methods note",
            "cause_ref": cause_ref, "task_ref": cause_ref,
            "payload": {"observations": [{"measure": [1, 2], "unknown": None}],
                        "alternatives": ["A", "B"]},
            "expected_latency": "long"}
    args.update(overrides)
    return mail.send_request(operation_id, **args)


def save_artifact(store, artifact_id="answer"):
    return ref(store.transact(f"save-{artifact_id}", {}, lambda tx: tx.put(
        "artifact", artifact_id,
        {"mission_id": "mission", "synthetic": True, "tables": [[1, 2], [3, None]]}),
        mission_id="mission"))


def test_delivery_ack_and_answer_are_distinct_with_nested_payload(tmp_path):
    clock = [100.0]
    store, peers, cause = setup(tmp_path, clock=lambda: clock[0])
    mail = PeerMailbox(store)
    opened = ask(mail, peers, cause)
    assert opened["status"] == "accepted"
    assert len(mail.pending_outbox("mission")) == 1
    assert mail.inbox("mission", peers[1])[0]["body"]["payload"]["observations"][0]["unknown"] is None
    clock[0] = 103.0
    assert mail.acknowledge("early-ack", mission_id="mission", message_id="message",
                            recipient_id=peers[1])["status"] == "rejected"
    delivered = mail.mark_delivered("delivered", mission_id="mission", message_id="message")
    assert delivered["status"] == "accepted"
    assert mail.mark_delivered("delivery-retry", mission_id="mission", message_id="message")["status"] == "accepted"
    assert mail.acknowledge("ack", mission_id="mission", message_id="message",
                            recipient_id=peers[1])["status"] == "accepted"
    view = mail.request_view("request")
    assert view["status"] == "unanswered"
    assert view["delivery_seconds"] == 3
    assert view["answered_at"] is None
    assert mail.pending_outbox("mission") == []
    assert mail.send_message("duplicate-id", mission_id="mission", message_id="message",
                             sender_id=peers[0], recipient_id=peers[1],
                             correlation_id="request", payload={"changed": True})["code"] == "conflict"
    answer_ref = save_artifact(store)
    clock[0] = 108.0
    answer = mail.record_answer("answer-op", mission_id="mission", request_id="request",
                                answer_id="answer-one", responder_id=peers[1],
                                artifact_ref=answer_ref)
    assert answer["status"] == "accepted"
    assert mail.request_view("request")["status"] == "awaiting_review"
    clock[0] = 110.0
    reviewed = mail.review_answer("review", mission_id="mission", request_id="request",
                                  reviewer_id=peers[0], answer_id="answer-one",
                                  disposition="answered", reason="Methods note addresses question")
    assert reviewed["status"] == "accepted"
    view = mail.request_view("request")
    assert view["status"] == "answered"
    assert view["accepted_answer_ref"] == answer["answer_ref"]
    assert (view["answer_seconds"], view["resolution_seconds"], view["waiting_seconds"]) == \
        (8, 10, 10)
    assert mail.record_answer("late-failure", mission_id="mission", request_id="request",
                              answer_id="late", responder_id=peers[1],
                              artifact_ref=answer_ref)["status"] == "rejected"
    assert len(store.get("mail_request", "request")["body"]["answer_refs"]) == 1


def test_sender_replays_delivery_and_ack_without_task_subscription(tmp_path):
    store, peers, cause = setup(tmp_path)
    mail = PeerMailbox(store)
    assert mail.subscribe(
        "watch-unrelated", mission_id="mission", actor_id=peers[0],
        subjects=[{"kind": "task", "id": "unrelated"}],
    )["status"] == "accepted"
    ask(mail, peers, cause)
    assert mail.mark_delivered(
        "deliver", mission_id="mission", message_id="message",
    )["status"] == "accepted"
    assert mail.mark_delivered(
        "deliver-again", mission_id="mission", message_id="message",
    )["status"] == "accepted"
    assert mail.acknowledge(
        "ack", mission_id="mission", message_id="message",
        recipient_id=peers[1],
    )["status"] == "accepted"
    assert mail.acknowledge(
        "ack-again", mission_id="mission", message_id="message",
        recipient_id=peers[1],
    )["status"] == "accepted"

    restarted = PeerMailbox(StateStore(tmp_path))
    events = restarted.catch_up("mission", peers[0])["events"]
    transitions = [event for event in events if event["type"] in {
        "peer_message_delivered", "peer_message_acknowledged"}]
    assert [event["type"] for event in transitions] == [
        "peer_message_delivered", "peer_message_acknowledged"]
    assert all(event["body"]["recipient_ids"] == peers[:2]
               for event in transitions)
    assert [event["body"]["message_ref"]["version"] for event in transitions] == [2, 3]
    assert restarted.request_view("request")["status"] == "unanswered"


def test_late_provider_failure_does_not_replace_newer_answer(tmp_path):
    store, peers, cause = setup(tmp_path)
    mail = PeerMailbox(store)
    ask(mail, peers, cause)
    early = store.transact("source-abstract", {}, lambda tx: tx.put(
        "source_copy", "copy", {"synthetic": True, "access_surface": "abstract"}),
        mission_id="mission")
    newer = store.transact("source-upgrade", {}, lambda tx: tx.put(
        "source_copy", "copy", {"synthetic": True, "access_surface": "full_text"},
        expected_version=1), mission_id="mission")
    answer = mail.record_answer("newer-answer", mission_id="mission", request_id="request",
                                answer_id="newer", responder_id=peers[1],
                                artifact_ref=ref(newer))
    mail.review_answer("accept-newer", mission_id="mission", request_id="request",
                       reviewer_id=peers[0], answer_id="newer",
                       disposition="answered", reason="Full text addresses the question")
    failure = mail.send_message(
        "older-provider-error", mission_id="mission", message_id="older-error",
        sender_id=peers[1], recipient_id=peers[0], correlation_id="request",
        payload={"provider_attempt": "older", "status": "failed"},
        evidence_refs=[ref(early)])
    assert failure["status"] == "accepted"
    assert mail.mark_delivered("late-delivery", mission_id="mission",
                               message_id="older-error")["status"] == "accepted"
    assert mail.request_view("request")["accepted_answer_ref"] == answer["answer_ref"]
    assert store.get("mail_answer", "newer")["body"]["artifact_ref"] == ref(newer)
    assert store.get("source_copy", "copy")["version"] == 2


def test_offline_requester_replacement_checks_role_and_lineage(tmp_path):
    store, peers, cause = setup(tmp_path)
    mail = PeerMailbox(store)
    ask(mail, peers, cause)
    answer_ref = save_artifact(store)
    mail.record_answer("answer-op", mission_id="mission", request_id="request",
                       answer_id="answer-one", responder_id=peers[1],
                       artifact_ref=answer_ref)
    bad = mail.review_answer("unavailable-not-attested", mission_id="mission",
                             request_id="request", reviewer_id=peers[2],
                             answer_id="answer-one", disposition="answered",
                             reason="Independent review")
    assert bad["status"] == "rejected"
    availability = ref(store.transact("host-offline", {}, lambda tx: tx.put(
        "agent_availability", peers[0], {"mission_id": "mission",
                                         "instance_id": peers[0], "status": "offline",
                                         "recorded_by": "host"}), mission_id="mission"))
    assert mail.review_answer("string-offline", mission_id="mission",
                              request_id="request", reviewer_id=peers[2],
                              answer_id="answer-one", disposition="answered",
                              reason="Wrong type", requester_offline="false",
                              availability_ref=availability)["status"] == "rejected"
    reviewed = mail.review_answer("replacement-review", mission_id="mission",
                                  request_id="request", reviewer_id=peers[2],
                                  answer_id="answer-one", disposition="answered",
                                  reason="Independent source comparison",
                                  requester_offline=True, availability_ref=availability)
    assert reviewed["status"] == "accepted"
    assert mail.request_view("request")["review"]["independence_checked"] is True

    registry = AgentRegistry(store, ROOT)
    successor = registry.register(
        "successor", mission_id="mission", run_id="run-two",
        role_id="evidence_preparation_synthesis", display_name="Successor",
        predecessor_id=peers[0],
    )["id"]
    ask(mail, peers, cause, operation_id="ask-second", request_id="second",
        message_id="second-message")
    mail.record_answer("second-answer", mission_id="mission", request_id="second",
                       answer_id="second-answer", responder_id=peers[1],
                       artifact_ref=answer_ref)
    denied = mail.review_answer("lineage-review", mission_id="mission", request_id="second",
                                reviewer_id=successor, answer_id="second-answer",
                                disposition="answered", reason="I agree",
                                requester_offline=True, availability_ref=availability)
    assert denied["status"] == "rejected"
    assert mail.request_view("second")["status"] == "awaiting_review"


def test_partial_declined_redirect_and_replay_after_restart(tmp_path):
    store, peers, cause = setup(tmp_path)
    mail = PeerMailbox(store)
    ask(mail, peers, cause)
    assert mail.subscribe("subscribe", mission_id="mission", actor_id=peers[2],
                          subjects=[{"kind": "task", "id": "question"}])["status"] == "accepted"
    latest = store.events("mission")[-1]["sequence"]
    assert mail.ack_cursor("skip-unseen", mission_id="mission", actor_id=peers[2],
                           sequence=latest)["status"] == "rejected"
    seen = mail.catch_up("mission", peers[2])
    assert any(event["type"] == "peer_request_opened" for event in seen["events"])
    assert mail.ack_cursor("cursor", mission_id="mission", actor_id=peers[2],
                           sequence=seen["scanned_through"])["status"] == "accepted"
    assert mail.subscribe("unrelated-sub", mission_id="mission", actor_id=peers[0],
                          subjects=[{"kind": "task", "id": "other"}])["status"] == "accepted"
    assert mail.catch_up("mission", peers[0])["scanned_through"] == seen["scanned_through"]
    assert mail.ack_cursor("unrelated-cursor", mission_id="mission", actor_id=peers[0],
                           sequence=seen["scanned_through"])["status"] == "accepted"
    contradiction = store.transact("late-contradiction", {}, lambda tx: tx.put(
        "source_copy", "late", {"synthetic": True}), mission_id="mission")
    assert ProposalBoard(store).reopen(
        "late-reopen", mission_id="mission", affected_ref=cause,
        contradiction_ref=ref(contradiction), actor_id=peers[0],
        reason="Late contradiction", next_action="Check source methods",
    )["status"] == "accepted"
    answer_ref = save_artifact(store)
    mail.record_answer("partial-answer", mission_id="mission", request_id="request",
                       answer_id="partial", responder_id=peers[1],
                       artifact_ref=answer_ref, partial=True)
    mail.review_answer("partial-review", mission_id="mission", request_id="request",
                       reviewer_id=peers[0], answer_id="partial",
                       disposition="partly_answered", reason="Comparator still missing")
    assert mail.request_view("request")["status"] == "partly_answered"
    requester_events = mail.catch_up("mission", peers[0])["events"]
    assert {"peer_answer_submitted", "peer_request_reviewed"} <= {
        event["type"] for event in requester_events}
    assert mail.redirect("unauthorized-redirect", mission_id="mission",
                         old_request_id="request", new_request_id="hijacked",
                         new_message_id="hijacked-message", actor_id=peers[2],
                         recipient_id=peers[1], reason="Unrelated peer choice")["status"] == "rejected"
    assert store.get("mail_request", "hijacked") is None
    redirected = mail.redirect("redirect", mission_id="mission", old_request_id="request",
                               new_request_id="replacement", new_message_id="replacement-message",
                               actor_id=peers[0], recipient_id=peers[2],
                               reason="Ask a second reviewer")
    assert redirected["status"] == "accepted"
    assert mail.request_view("request")["status"] == "replaced"
    assert mail.request_view("replacement")["status"] == "unanswered"
    assert mail.decline("decline", mission_id="mission", request_id="replacement",
                        recipient_id=peers[2], reason="No accessible full text")["status"] == "accepted"
    restarted = PeerMailbox(StateStore(tmp_path))
    catch_up = restarted.catch_up("mission", peers[2])
    assert any(event["type"] == "proposals_reopened" for event in catch_up["events"])
    assert any(event["type"] == "peer_message_pending" and
               event["body"]["recipient_id"] == peers[2] for event in catch_up["events"])
    assert not any(event["type"] == "proposals_reopened"
                   for event in restarted.catch_up("mission", peers[0])["events"])
    assert {"peer_request_redirected", "peer_request_declined"} <= {
        event["type"] for event in restarted.catch_up("mission", peers[0])["events"]}
    assert restarted.ack_cursor("old-cursor", mission_id="mission", actor_id=peers[2],
                                sequence=0)["cursor"] == seen["scanned_through"]
    assert restarted.request_view("replacement")["status"] == "declined"


def test_redirect_retains_exact_older_context_and_partial_lineage(tmp_path):
    store, peers, task_ref = setup(tmp_path)
    mail = PeerMailbox(store)
    source_v1 = store.transact("source-v1", {}, lambda tx: tx.put(
        "source_copy", "source", {"synthetic": True, "access_surface": "abstract"}),
        mission_id="mission")
    original_payload = {
        "redirect": {"reason": "scientific hypothesis, not routing metadata"},
        "original_payload": [None, {"estimate": [1.2, 3.4]}],
        "constraints": {"excluded": ["no comparator"], "uncertain": True},
    }
    opened = ask(mail, peers, task_ref, payload=original_payload,
                 evidence_refs=[ref(source_v1)])
    assert opened["status"] == "accepted"
    original_message_ref = opened["message_ref"]
    source_v2 = store.transact("source-v2", {}, lambda tx: tx.put(
        "source_copy", "source", {"synthetic": True, "access_surface": "full_text"},
        expected_version=1), mission_id="mission")
    assert ProposalBoard(store).revise(
        "revised-task", mission_id="mission", proposal_id="question",
        actor_id=peers[0], expected_version=1,
        reason="Methods interpretation changed",
    )["status"] == "accepted"
    answer_ref = save_artifact(store, "partial-note")
    assert mail.record_answer(
        "partial-answer", mission_id="mission", request_id="request",
        answer_id="partial", responder_id=peers[1], artifact_ref=answer_ref,
        partial=True,
    )["status"] == "accepted"
    assert mail.review_answer(
        "partial-review", mission_id="mission", request_id="request",
        reviewer_id=peers[0], answer_id="partial",
        disposition="partly_answered", reason="Comparator remains unresolved",
    )["status"] == "accepted"
    before_redirect = store.get("mail_request", "request")
    assert before_redirect["body"]["status"] == "partly_answered"
    redirected = mail.redirect(
        "redirect-with-context", mission_id="mission", old_request_id="request",
        new_request_id="replacement", new_message_id="replacement-message",
        actor_id=peers[0], recipient_id=peers[2], reason="Seek independent methods note",
    )
    assert redirected["status"] == "accepted"

    restarted_store = StateStore(tmp_path)
    restarted = PeerMailbox(restarted_store)
    replacement = restarted_store.get("mail_request", "replacement")
    message = restarted_store.get(**replacement["body"]["message_ref"])
    assert message["body"]["task_ref"] == task_ref
    assert message["body"]["evidence_refs"] == [ref(source_v1)]
    assert message["body"]["payload"] == {
        "redirect": {"from_request_ref": ref(before_redirect),
                     "from_message_ref": original_message_ref,
                     "reason": "Seek independent methods note"},
        "original_payload": original_payload,
    }
    assert {"source": ref(message), "target": original_message_ref,
            "relation": "derived_from"} in restarted_store.links(message)
    assert replacement["body"]["cause_ref"] == task_ref
    assert replacement["body"]["status"] == "unanswered"
    assert restarted_store.get("source_copy", "source") == source_v2
    assert restarted_store.get("board_proposal", "question")["version"] == 2
    assert restarted_store.get("mail_request", "request", before_redirect["version"])["body"]["status"] == "partly_answered"
    old = restarted_store.get("mail_request", "request")
    assert old["body"]["status"] == "replaced"
    assert old["body"]["answer_refs"] == before_redirect["body"]["answer_refs"]
    assert old["body"]["replacement_request_ref"] == redirected["request_ref"]
    assert restarted_store.get(**original_message_ref)["body"]["payload"] == original_payload
    assert restarted.decline(
        "replacement-declined", mission_id="mission", request_id="replacement",
        recipient_id=peers[2], reason="Unable to review now",
    )["status"] == "accepted"
    assert restarted.request_view("replacement")["status"] == "declined"
    assert restarted.request_view("request")["status"] == "replaced"


def test_malformed_exchange_is_local_and_compound_commit_rolls_back(tmp_path):
    store, peers, cause = setup(tmp_path)
    mail = PeerMailbox(store)
    malformed = mail.send_message("bad", mission_id="mission", message_id="bad",
                                  sender_id=peers[0], recipient_id=peers[1],
                                  correlation_id="bad", payload={"value": float("nan")})
    assert malformed["status"] == "rejected"
    assert store.get("mail_message", "bad") is None
    assert store.get("mail_rejection", "bad") is not None
    good = mail.send_message("good", mission_id="mission", message_id="good",
                             sender_id=peers[0], recipient_id=peers[1],
                             correlation_id="good", payload={"nested": [1, {"x": None}]})
    assert good["status"] == "accepted"

    def compound(tx):
        artifact_ref = ref(tx.put("artifact", "compound", {"mission_id": "mission"}))
        mail.send_request_tx(tx, mission_id="mission", request_id="compound-request",
                             message_id="compound-message", requester_id=peers[0],
                             recipient_id=peers[1], question="Check artifact",
                             expected_artifact="Critique", cause_ref=cause,
                             payload={"artifact_ref": artifact_ref})
        raise RuntimeError("Interrupted before commit")

    with pytest.raises(RuntimeError, match="Interrupted"):
        store.transact("compound", {}, compound, mission_id="mission")
    assert store.get("artifact", "compound") is None
    assert store.get("mail_message", "compound-message") is None
    assert store.get("mail_request", "compound-request") is None
