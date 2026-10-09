"""Peer update statements and proposal review keep meaning and scope separate."""

from pathlib import Path

import pytest

from ora.agents import AgentRegistry
from ora.coordination.board import ProposalBoard, ref
from ora.coordination.contributions import ContributionError, ContributionReview
from ora.coordination.mailbox import PeerMailbox
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[2]


def setup(tmp_path):
    store = StateStore(tmp_path)
    registry = AgentRegistry(store, ROOT)
    peers = [registry.register(
        f"peer-{number}", mission_id="mission", run_id="run",
        role_id="evidence_preparation_synthesis", display_name=f"Peer {number}",
    )["id"] for number in range(3)]
    outsider = registry.register(
        "outside", mission_id="other-mission", run_id="other-run",
        role_id="evidence_preparation_synthesis", display_name="Outside peer",
    )["id"]
    board = ProposalBoard(store)
    proposal = board.propose(
        "create-proposal", mission_id="mission", proposal_id="question",
        actor_id=peers[0], objective="Compare explanations",
        expected_artifact="Source-backed answer", next_action="Read evidence",
    )
    assert proposal["status"] == "accepted"
    return store, peers, outsider, proposal["proposal_ref"]


def send_shared(mail, peers, proposal_ref, message_id="message", operation_id="send"):
    return mail.send_message(
        operation_id, mission_id="mission", message_id=message_id,
        sender_id=peers[0], recipient_id=peers[1], correlation_id="discussion",
        task_ref=proposal_ref, payload={"private": "raw message material"},
    )


def flatten(page):
    return [record for entry in page["entries"] for record in entry["records"]]


def test_acknowledged_update_has_unknown_uptake_until_peer_states_it(tmp_path):
    store, peers, _, proposal_ref = setup(tmp_path)
    mail, review = PeerMailbox(store), ContributionReview(store)
    message = send_shared(mail, peers, proposal_ref)
    assert message["status"] == "accepted"
    assert mail.mark_delivered("deliver", mission_id="mission",
                               message_id="message")["status"] == "accepted"
    assert mail.acknowledge("ack", mission_id="mission", message_id="message",
                            recipient_id=peers[1])["status"] == "accepted"

    before = review.proposal_context(mission_id="mission", proposal_id="question",
                                     viewer_id=peers[2])
    messages = [item for item in flatten(before) if item["ref"]["kind"] == "mail_message"]
    assert [item["ref"]["version"] for item in messages] == [1, 2, 3]
    assert all(item["data"]["scientific_use"] == "unknown_from_delivery"
               for item in messages)
    assert {"peer_message_pending", "peer_message_delivered",
            "peer_message_acknowledged"} <= {entry["type"] for entry in before["entries"]}
    assert not any(entry["type"] == "peer_update_uptake_recorded"
                   for entry in before["entries"])

    # A registered late peer may review a message explicitly attached to the
    # shared proposal, but cannot write an uptake statement for another actor.
    used = review.record_uptake(
        "uptake-op", mission_id="mission", uptake_id="uptake-one",
        actor_id=peers[2], update_ref=message["message_ref"],
        disposition="considered", explanation="I read the alternative account.",
    )
    assert used["status"] == "accepted"
    saved = store.get(**used["uptake_ref"])
    assert saved["body"]["actor_ref"]["id"] == peers[2]
    assert saved["body"]["scientific_status"] == "peer_statement"
    assert review.record_uptake(
        "uptake-op", mission_id="mission", uptake_id="uptake-one",
        actor_id=peers[2], update_ref=message["message_ref"],
        disposition="considered", explanation="I read the alternative account.",
    ) == used
    assert len([event for event in store.events("mission")
                if event["type"] == "peer_update_uptake_recorded"]) == 1


def test_challenge_links_exact_revision_and_late_peer_sees_critique(tmp_path):
    store, peers, _, proposal_ref = setup(tmp_path)
    board, mail, review = ProposalBoard(store), PeerMailbox(store), ContributionReview(store)
    source = ref(store.transact("save-evidence", {}, lambda tx: tx.put(
        "stage3_fixture_input", "evidence-one", {"mission_id": "mission",
                                                "question": "Inspect source methods",
                                                "detail": "restricted passage"}),
        mission_id="mission"))
    contrary = ref(store.transact("save-contrary", {}, lambda tx: tx.put(
        "stage3_fixture_input", "evidence-two", {"mission_id": "mission",
                                                "question": "Inspect contrary methods",
                                                "detail": "restricted contrary passage"}),
        mission_id="mission"))
    dissent = board.dissent(
        "dissent", mission_id="mission", dissent_id="critique-one",
        proposal_ref=proposal_ref, actor_id=peers[1],
        critique="The cited result does not establish an effect.",
        evidence_refs=[source],
    )
    message = send_shared(mail, peers, proposal_ref)
    revised = board.revise(
        "revise", mission_id="mission", proposal_id="question", actor_id=peers[0],
        expected_version=1, reason="Preserve the alternative interpretation",
        evidence_refs=[source, contrary],
    )
    uptake = review.record_uptake(
        "challenge", mission_id="mission", uptake_id="challenge-one",
        actor_id=peers[2], update_ref=message["message_ref"],
        disposition="challenged",
        explanation="Contact analyst@example.test and https://private.example.test; token=hidden",
        evidence_refs=[source, contrary], effect_ref=revised["proposal_ref"],
    )
    assert uptake["status"] == "accepted"
    body = store.get(**uptake["uptake_ref"])["body"]
    assert body["effect_ref"] == revised["proposal_ref"]
    assert body["evidence_refs"] == [source, contrary]
    assert "analyst@example.test" not in body["statement"]
    assert "https://private.example.test" not in body["statement"]
    assert "hidden" not in body["statement"]

    late = review.proposal_context(mission_id="mission", proposal_id="question",
                                   viewer_id=peers[2])
    items = flatten(late)
    assert any(item["ref"] == dissent["dissent_ref"] and
               "does not establish an effect" in item["data"]["critique"]
               for item in items)
    assert any(item["ref"] == uptake["uptake_ref"] and
               item["data"]["effect_ref"] == revised["proposal_ref"]
               for item in items)
    rendered = str(late)
    assert "raw message material" not in rendered
    assert "restricted passage" not in rendered
    assert "restricted contrary passage" not in rendered


def test_clarification_keeps_original_request_unanswered(tmp_path):
    store, peers, _, proposal_ref = setup(tmp_path)
    mail, review = PeerMailbox(store), ContributionReview(store)
    opened = mail.send_request(
        "ask", mission_id="mission", request_id="request-one",
        message_id="request-message", requester_id=peers[0], recipient_id=peers[1],
        question="Which population was studied?", expected_artifact="Population note",
        cause_ref=proposal_ref, task_ref=proposal_ref, payload={"raw": "question"},
    )
    assert opened["status"] == "accepted"
    clarification = review.send_clarification(
        "clarify", mission_id="mission", request_id="request-one",
        message_id="clarification-message", actor_id=peers[1],
        question="Which source version should I inspect?",
    )
    assert clarification["status"] == "accepted"
    saved = store.get(**clarification["message_ref"])
    assert saved["body"]["correlation_id"] == "request-one"
    assert saved["body"]["request_id"] is None
    assert mail.mark_delivered("clarification-delivered", mission_id="mission",
                               message_id=saved["id"])["status"] == "accepted"
    assert mail.acknowledge("clarification-ack", mission_id="mission",
                            message_id=saved["id"],
                            recipient_id=peers[0])["status"] == "accepted"
    latest = ref(store.get("mail_message", saved["id"]))
    context = review.proposal_context(mission_id="mission", proposal_id="question",
                                      viewer_id=peers[2])
    projected = [item for item in flatten(context) if item["ref"]["kind"] == "mail_message"
                 and item["ref"]["id"] == saved["id"]]
    assert [item["ref"]["version"] for item in projected] == [1, 2, 3]
    assert all(item["data"]["message_role"] == "clarification" and
               item["data"]["clarifies_request_ref"] == clarification["original_request_ref"]
               for item in projected)
    assert "Which source version should I inspect?" not in str(context)
    for number, update_ref in enumerate((clarification["message_ref"], latest)):
        rejected = review.record_uptake(
            f"clarification-uptake-{number}", mission_id="mission",
            uptake_id=f"clarification-uptake-{number}", actor_id=peers[1],
            update_ref=update_ref, disposition="used",
            explanation="The clarification answered the question.",
        )
        assert rejected["status"] == "rejected"
        assert "original request lifecycle" in rejected["reason"]
    assert store.records("peer_update_uptake") == []
    assert mail.request_view("request-one")["status"] == "unanswered"
    assert review.send_clarification(
        "wrong-peer", mission_id="mission", request_id="request-one",
        message_id="other-message", actor_id=peers[2], question="What changed?",
    )["status"] == "rejected"
    assert store.get("mail_message", "other-message") is None


def test_mission_and_unshared_message_boundaries(tmp_path):
    store, peers, outsider, proposal_ref = setup(tmp_path)
    mail, review = PeerMailbox(store), ContributionReview(store)
    shared = send_shared(mail, peers, proposal_ref)
    direct = mail.send_message(
        "direct", mission_id="mission", message_id="direct-message",
        sender_id=peers[0], recipient_id=peers[1], correlation_id="private",
        payload={"secret": "PRIVATE BODY"},
    )
    assert review.record_uptake(
        "wrong-actor", mission_id="mission", uptake_id="wrong",
        actor_id=outsider, update_ref=shared["message_ref"],
        disposition="used", explanation="I used this.",
    )["status"] == "rejected"
    assert review.record_uptake(
        "unshared", mission_id="mission", uptake_id="unshared",
        actor_id=peers[2], update_ref=direct["message_ref"],
        disposition="used", explanation="I used this.",
    )["status"] == "rejected"
    assert review.record_uptake(
        "unshared-recipient", mission_id="mission", uptake_id="unshared-recipient",
        actor_id=peers[1], update_ref=direct["message_ref"],
        disposition="used", explanation="I used this.",
    )["status"] == "rejected"
    restricted = ref(store.transact("save-reasoning", {}, lambda tx: tx.put(
        "restricted_reasoning", "reasoning", {"mission_id": "mission",
                                            "text": "PRIVATE_REASONING"}),
        mission_id="mission"))
    instructions = ref(store.transact("save-instructions", {}, lambda tx: tx.put(
        "agent_configuration", "private-config", {"mission_id": "mission",
                                                  "instructions": "PRIVATE_INSTRUCTIONS"}),
        mission_id="mission"))
    unsupported = ref(store.transact("save-unsupported", {}, lambda tx: tx.put(
        "artifact", "unsupported-artifact", {"mission_id": "mission",
                                             "detail": "PRIVATE_ARTIFACT"}),
        mission_id="mission"))
    for label, evidence_refs, effect_ref in (
        ("restricted-evidence", [restricted], None),
        ("restricted-effect", None, restricted),
        ("private-instructions", [instructions], None),
        ("unsupported-kind", [unsupported], None),
        ("private-evidence", [direct["message_ref"]], None),
        ("private-effect", None, direct["message_ref"]),
    ):
        rejected = review.record_uptake(
            f"op-{label}", mission_id="mission", uptake_id=label,
            actor_id=peers[1], update_ref=shared["message_ref"],
            disposition="used", explanation="I used this.",
            evidence_refs=evidence_refs, effect_ref=effect_ref,
        )
        assert rejected["status"] == "rejected"
        if label == "unsupported-kind":
            assert "no safe shared projection" in rejected["reason"]
        assert store.get("peer_update_uptake", label) is None
    source = ref(store.transact("save-source-ref", {}, lambda tx: tx.put(
        "source_copy", "public-source", {"synthetic": True,
                                         "access_surface": "metadata_only"}),
        mission_id="mission"))
    fixture_input = ref(store.transact("save-fixture-input", {}, lambda tx: tx.put(
        "stage3_fixture_input", "evidence-input", {"mission_id": "mission",
                                                 "question": "Compare source versions"}),
        mission_id="mission"))
    summary = ref(store.transact("save-trace-summary", {}, lambda tx: tx.put(
        "trace_summary", "summary", {"mission_id": "mission",
                                       "summary": "A related update was reviewed"}),
        mission_id="mission"))
    trace = ref(store.transact("save-trace-ref", {}, lambda tx: tx.put(
        "coordination_trace", "trace", {"mission_id": "mission",
                                         "status": "accepted"}),
        mission_id="mission"))
    accepted = review.record_uptake(
        "source-metadata", mission_id="mission", uptake_id="source-metadata",
        actor_id=peers[1], update_ref=shared["message_ref"],
        disposition="considered", explanation="I checked the source metadata.",
        evidence_refs=[source, fixture_input, summary, trace],
    )
    assert accepted["status"] == "accepted"
    assert store.get(**accepted["uptake_ref"])["body"]["evidence_refs"] == [
        source, fixture_input, summary, trace]
    assert store.get("peer_update_uptake", "wrong") is None
    assert store.get("peer_update_uptake", "unshared") is None
    assert store.get("peer_update_uptake", "unshared-recipient") is None
    with pytest.raises(ContributionError):
        review.proposal_context(mission_id="mission", proposal_id="question",
                                viewer_id=outsider)
    with pytest.raises(ContributionError):
        review.proposal_context(mission_id="other-mission", proposal_id="question",
                                viewer_id=outsider)
    assert "PRIVATE BODY" not in str(review.proposal_context(
        mission_id="mission", proposal_id="question", viewer_id=peers[2]))
    context = str(review.proposal_context(
        mission_id="mission", proposal_id="question", viewer_id=peers[2]))
    assert "reasoning" not in context
    assert "private-config" not in context
    assert "unsupported-artifact" not in context
    assert "direct-message" not in context


def test_event_cursor_preserves_snapshot_then_finds_late_insert_and_revision(tmp_path):
    store, peers, _, proposal_ref = setup(tmp_path)
    mail, review, board = PeerMailbox(store), ContributionReview(store), ProposalBoard(store)
    # This many unrelated events makes event-scan pagination observable without
    # loading an unbounded history into one result.
    store.transact("noise", {}, lambda tx: {
        "count": [tx.emit("mission", "unrelated", {"subjects": []}) for _ in range(250)]},
        mission_id="mission")
    for number in range(65):
        send_shared(mail, peers, proposal_ref, message_id=f"z-{number:03d}",
                    operation_id=f"send-{number}")
    first = review.proposal_context(mission_id="mission", proposal_id="question",
                                    viewer_id=peers[2], limit=9)
    boundary = first["through"]
    assert first["has_more"]
    # Lexically early ID arrives after the first page. Event sequence, rather
    # than record ID order, must make it visible to a fresh reader.
    late = send_shared(mail, peers, proposal_ref, message_id="a-late",
                       operation_id="send-late")
    revision = board.revise(
        "late-revision", mission_id="mission", proposal_id="question",
        actor_id=peers[0], expected_version=1, reason="New conflicting result",
    )
    cursor, found = first["next_cursor"], list(first["entries"])
    while True:
        page = review.proposal_context(
            mission_id="mission", proposal_id="question", viewer_id=peers[2],
            after=cursor, through=boundary, limit=9)
        found.extend(page["entries"])
        assert page["next_cursor"] >= cursor
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    old_refs = [item["ref"] for entry in found for item in entry["records"]]
    assert late["message_ref"] not in old_refs
    assert revision["proposal_ref"] not in old_refs
    assert len([entry for entry in found if entry["type"] == "peer_message_pending"]) == 65
    assert len({entry["sequence"] for entry in found}) == len(found)

    fresh = review.proposal_context(
        mission_id="mission", proposal_id="question", viewer_id=peers[2],
        after=boundary, limit=9)
    fresh_refs = [item["ref"] for item in flatten(fresh)]
    assert late["message_ref"] in fresh_refs
    assert revision["proposal_ref"] in fresh_refs
    assert fresh["through"] > boundary
    restarted = ContributionReview(StateStore(tmp_path, read_only=True))
    replay = restarted.proposal_context(
        mission_id="mission", proposal_id="question", viewer_id=peers[2],
        after=boundary, through=fresh["through"], limit=9)
    assert replay["entries"] == fresh["entries"]
