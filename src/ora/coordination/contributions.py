"""Record peer-stated update use and read proposal discussion without dispatch.

An uptake record reports what one peer says they did. It is not a scientific
verdict, delivery acknowledgment, or prerequisite for other work.
"""

from __future__ import annotations

from ora.agents.json_shapes import JsonShapeError, finite_json
from ora.state import StateConflict, StateError, StateStore, Transaction
from ora.state.store import identifier
from ora.state.view_policy import FIELDS, project_record, visible_in_mission

from .board import BoardError, ref, require_actor, require_ref
from .mailbox import MailError, PeerMailbox
from .traces import public_text


class ContributionError(StateError):
    """One contribution statement or read needs scoped repair."""


_UPDATE_KINDS = {"mail_message", "board_proposal", "board_dissent", "board_outcome"}
_DISPOSITIONS = {"considered", "used", "challenged", "deferred", "inaccessible"}
# Uptake is visible to other mission peers. The diagnostic projection defines
# inspectable record kinds; a few records with a narrow diagnostic projection
# are nevertheless private inputs rather than shareable scientific references.
_PRIVATE_REFS = {"restricted_reasoning", "restricted_reasoning_raw", "agent_configuration", "agent_call",
                 "model_request_intent", "model_response_receipt",
                 "model_dispatch_attempt", "agent_handoff"}
_EVENT_REFS = ("proposal_ref", "dissent_ref", "claim_ref", "outcome_ref",
               "invitation_ref", "message_ref", "request_ref", "answer_ref",
               "old_request_ref", "new_request_ref", "uptake_ref")
_PUBLIC_PROSE = {"objective", "expected_artifact", "next_action", "critique",
                 "question", "counter", "reason", "statement"}
_SUMMARIES = {
    "proposal_created": "A peer proposed research work.",
    "proposal_revised": "A peer revised the proposal.",
    "proposal_dissent": "A peer recorded a critique.",
    "proposal_claimed": "A peer claimed this proposal's work.",
    "proposal_result_accepted": "A peer saved a result for this proposal.",
    "proposals_reopened": "New evidence reopened affected work.",
    "invitation_offered": "A peer invited another peer to contribute.",
    "invitation_decided": "A peer responded to an invitation.",
    "peer_message_pending": "A peer sent a message about this work.",
    "peer_message_delivered": "A peer message was delivered; its scientific use is unknown.",
    "peer_message_acknowledged": "A peer acknowledged a message; its scientific use is unknown.",
    "peer_request_opened": "A peer asked for evidence or review.",
    "peer_answer_submitted": "A peer submitted an answer for review.",
    "peer_request_reviewed": "A peer reviewed an answer.",
    "peer_request_declined": "A peer declined a request.",
    "peer_request_redirected": "A peer redirected a request.",
    "peer_update_uptake_recorded": "A peer stated how they handled an update.",
}


def _shared_message(tx: Transaction, mission_id: str, message: dict) -> bool:
    task = message["body"].get("task_ref")
    return (isinstance(task, dict) and task.get("kind") == "board_proposal"
            and require_ref(tx, mission_id, task)["kind"] == "board_proposal")


def _shareable_ref(tx: Transaction, mission_id: str, pointer: dict) -> dict:
    record = require_ref(tx, mission_id, pointer)
    if not visible_in_mission(record, mission_id):
        raise ContributionError("Uptake reference is not visible in this mission")
    if record["kind"] in _PRIVATE_REFS:
        raise ContributionError("Uptake cannot publish a restricted record reference")
    if record["kind"] == "mail_message" and not _shared_message(tx, mission_id, record):
        raise ContributionError("Uptake cannot publish an unshared direct message reference")
    # Check policy membership without rendering potentially large source data
    # while holding the update's write transaction.
    if record["kind"] not in FIELDS:
        raise ContributionError("Uptake reference has no safe shared projection")
    return record


def _clarifies_request(db, mission_id: str, message: dict) -> dict | None:
    """Find a request linked to this message, without reading its private payload.

    The link is made on message v1; delivery and acknowledgment create later
    versions of the same message. Correlation and mission checks keep the marker
    tied to the original request rather than an unrelated link.
    """
    row = db.execute(
        "SELECT l.target_kind, l.target_id, l.target_version FROM links l "
        "JOIN records r ON r.kind=l.target_kind AND r.id=l.target_id "
        "AND r.version=l.target_version "
        "WHERE l.source_kind='mail_message' AND l.source_id=? "
        "AND l.relation='clarifies' AND l.target_kind='mail_request' "
        "AND l.target_id=? AND json_extract(r.body,'$.mission_id')=? "
        "ORDER BY l.source_version LIMIT 1",
        (message["id"], message["body"].get("correlation_id"), mission_id),
    ).fetchone()
    return ({"kind": row["target_kind"], "id": row["target_id"],
             "version": row["target_version"]} if row else None)


def _about_proposal(event: dict, proposal_id: str, store: StateStore) -> bool:
    body = event["body"]
    if any(item.get("kind") == "task" and item.get("id") == proposal_id
           for item in body.get("subjects", []) if isinstance(item, dict)):
        return True
    pointer = body.get("proposal_ref")
    if isinstance(pointer, dict) and pointer.get("id") == proposal_id:
        return True
    if any(isinstance(item, dict) and item.get("id") == proposal_id
           for item in body.get("proposal_refs", [])):
        return True
    # Delivery and acknowledgment events intentionally have no task subjects.
    # Resolve their exact saved message version to restore the proposal link.
    if event["type"] in {"peer_message_delivered", "peer_message_acknowledged"}:
        message_ref = body.get("message_ref")
        if isinstance(message_ref, dict):
            message = store.get(**message_ref)
            if message is not None and message["body"].get("mission_id") == event["mission_id"]:
                task = message["body"].get("task_ref")
                return (isinstance(task, dict) and task.get("kind") == "board_proposal"
                        and task.get("id") == proposal_id)
    return False


def _pointers(event: dict) -> list[dict]:
    body = event["body"]
    pointers = [body[key] for key in _EVENT_REFS if isinstance(body.get(key), dict)]
    pointers.extend(item for item in body.get("proposal_refs", []) if isinstance(item, dict))
    return pointers


def _project(record: dict | None, pointer: dict, mission_id: str, db) -> dict:
    if record is None or record["body"].get("mission_id") != mission_id:
        return {"ref": pointer, "availability": "record_unavailable", "data": {}}
    if record["kind"] == "peer_update_uptake":
        body = record["body"]
        data = {key: body[key] for key in ("actor_ref", "update_ref", "disposition",
                                           "statement", "evidence_refs", "effect_ref",
                                           "scientific_status")}
    else:
        data = project_record(record)
    if record["kind"] == "mail_request":
        # The broad diagnostic policy contains nested reviewer notes. Keep the
        # proposal view to its public request state and readable question.
        data = {key: value for key, value in data.items()
                if key in {"mission_id", "requester_id", "recipient_id", "question",
                           "cause_ref", "message_ref", "status", "accepted_answer_ref"}}
    data = {key: public_text(value) if key in _PUBLIC_PROSE and isinstance(value, str)
            else value for key, value in data.items()}
    if record["kind"] == "mail_message":
        # The existing diagnostic policy excludes the arbitrary message payload.
        data["scientific_use"] = "unknown_from_delivery"
        clarified = _clarifies_request(db, mission_id, record)
        if clarified is not None:
            data["message_role"] = "clarification"
            data["clarifies_request_ref"] = clarified
    return {"ref": ref(record), "availability": "projected", "data": data}


class ContributionReview:
    """Additive peer statements and event-bounded proposal-context pages."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def _run(self, operation_id: str, mission_id: str, request: dict, action) -> dict:
        try:
            finite_json(request)
            result = self.store.transact(operation_id, request, action, mission_id=mission_id)
            return {"status": "accepted", **result}
        except (ContributionError, BoardError, MailError, StateConflict,
                StateError, JsonShapeError) as exc:
            return {"status": "rejected", "code": "conflict" if isinstance(exc, StateConflict)
                    else "invalid_contribution", "reason": str(exc)}

    def record_uptake(
        self, operation_id: str, *, mission_id: str, uptake_id: str,
        actor_id: str, update_ref: dict, disposition: str, explanation: str,
        evidence_refs: list[dict] | None = None, effect_ref: dict | None = None,
    ) -> dict:
        """Save one peer's optional statement about an exact ordinary update."""
        request = {key: value for key, value in locals().items() if key != "self"}

        def action(tx: Transaction) -> dict:
            actor = require_actor(tx, mission_id, actor_id)
            identifier(uptake_id)
            if disposition not in _DISPOSITIONS:
                raise ContributionError("Choose a known update disposition")
            identifier(explanation)
            update = require_ref(tx, mission_id, update_ref)
            if update["kind"] not in _UPDATE_KINDS:
                raise ContributionError("Uptake needs an ordinary message or board update")
            if update["kind"] == "mail_message":
                body = update["body"]
                if body.get("request_id") is not None:
                    raise ContributionError("Tracked requests use their answer-review lifecycle")
                if _clarifies_request(tx.db, mission_id, update) is not None:
                    raise ContributionError("Clarifications use the original request lifecycle")
                if not _shared_message(tx, mission_id, update):
                    raise ContributionError("Unshared direct message has no shared uptake record")
            evidence = [_shareable_ref(tx, mission_id, p) for p in evidence_refs or []]
            effect = _shareable_ref(tx, mission_id, effect_ref) if effect_ref else None
            if tx.get("peer_update_uptake", uptake_id):
                raise StateConflict("Uptake ID already exists")
            statement = public_text(explanation)
            body = {"schema": "ora.peer_update_uptake.v1", "mission_id": mission_id,
                    "actor_ref": ref(actor), "update_ref": ref(update),
                    "disposition": disposition, "statement": statement,
                    "evidence_refs": [ref(item) for item in evidence],
                    "effect_ref": ref(effect) if effect else None,
                    "created_at": self.store.now(), "scientific_status": "peer_statement"}
            saved = tx.put("peer_update_uptake", uptake_id, body)
            tx.link(saved, update, "about_update")
            for item in evidence:
                tx.link(saved, item, "context")
            if effect:
                tx.link(saved, effect, "reported_effect")
            task_id = (update["body"].get("task_ref") or {}).get("id") if update["kind"] == "mail_message" else None
            if update["kind"] == "board_proposal":
                task_id = update["id"]
            elif update["kind"] in {"board_dissent", "board_outcome"}:
                task_id = update["body"]["proposal_ref"]["id"]
            tx.emit(mission_id, "peer_update_uptake_recorded", {
                "uptake_ref": ref(saved),
                "subjects": [{"kind": "task", "id": task_id}] if task_id else []})
            return {"uptake_ref": ref(saved)}

        return self._run(operation_id, mission_id, request, action)

    def send_clarification(
        self, operation_id: str, *, mission_id: str, request_id: str,
        message_id: str, actor_id: str, question: str,
    ) -> dict:
        """Ask about an open request without recording an answer or closing it."""
        request = {key: value for key, value in locals().items() if key != "self"}

        def action(tx: Transaction) -> dict:
            require_actor(tx, mission_id, actor_id)
            identifier(question)
            original = tx.get("mail_request", request_id)
            if original is None or original["body"].get("mission_id") != mission_id:
                raise ContributionError("Original request is unavailable")
            body = original["body"]
            if actor_id != body["recipient_id"]:
                raise ContributionError("Only the requested peer can ask this clarification")
            if body["status"] not in {"unanswered", "partly_answered", "awaiting_review", "partial_awaiting_review"}:
                raise ContributionError("Original request is no longer open for clarification")
            original_message = tx.get(**body["message_ref"])
            if original_message is None:
                raise ContributionError("Original message version is unavailable")
            created = PeerMailbox(self.store).send_message_tx(
                tx, mission_id=mission_id, message_id=message_id,
                sender_id=actor_id, recipient_id=body["requester_id"],
                correlation_id=request_id,
                payload={"kind": "clarification", "question": question,
                         "request_ref": ref(original)},
                task_ref=original_message["body"].get("task_ref"))
            tx.link(tx.get(**created["message_ref"]), original, "clarifies")
            return {**created, "original_request_ref": ref(original)}

        return self._run(operation_id, mission_id, request, action)

    def proposal_context(
        self, *, mission_id: str, proposal_id: str, viewer_id: str,
        after: int = 0, through: int | None = None, limit: int = 100,
    ) -> dict:
        """Read an event-bounded page; a fresh call uses a new high-water mark.

        ``has_more`` means unscanned mission events within ``through`` remain.
        An empty page can therefore still have more events to scan.
        """
        if (type(after) is not int or after < 0 or type(limit) is not int
                or not 1 <= limit <= 200 or
                (through is not None and (type(through) is not int or through < after))):
            raise ContributionError("Context cursor or page size is invalid")
        identifier(mission_id)
        identifier(proposal_id)
        identifier(viewer_id)
        actor = self.store.get("agent_instance", viewer_id, 1)
        proposal = self.store.get("board_proposal", proposal_id)
        if actor is None or actor["body"].get("mission_id") != mission_id:
            raise ContributionError("Viewer is unavailable in this mission")
        if proposal is None or proposal["body"].get("mission_id") != mission_id:
            raise ContributionError("Proposal is unavailable in this mission")
        db = self.store._connect()
        try:
            latest = db.execute("SELECT COALESCE(MAX(sequence),0) FROM events WHERE mission_id=?",
                                (mission_id,)).fetchone()[0]
        finally:
            db.close()
        if through is None:
            through = latest
        elif through > latest:
            raise ContributionError("Context high-water mark is unavailable")
        if after > through:
            raise ContributionError("Context cursor exceeds its high-water mark")

        entries, scanned = [], after
        scan_budget = max(200, limit * 10)
        projection_db = self.store._connect()
        try:
            while len(entries) < limit and scan_budget > 0 and scanned < through:
                requested = min(100, scan_budget)
                batch = self.store.events(mission_id, after=scanned,
                                          limit=requested)
                if not batch:
                    scanned = through
                    break
                for event in batch:
                    if event["sequence"] > through:
                        scanned = through
                        break
                    scanned = event["sequence"]
                    scan_budget -= 1
                    if event["type"] in _SUMMARIES and _about_proposal(event, proposal_id,
                                                                        self.store):
                        records = [_project(self.store.get(**pointer), pointer, mission_id,
                                            projection_db) for pointer in _pointers(event)]
                        entries.append({"sequence": scanned, "type": event["type"],
                                        "summary": _SUMMARIES[event["type"]],
                                        "records": records})
                    if len(entries) >= limit or scan_budget == 0:
                        break
                if len(batch) < requested and len(entries) < limit and scan_budget > 0:
                    # No additional matching mission events existed when this page was read.
                    scanned = through if scanned < through else scanned
                    break
        finally:
            projection_db.close()
        return {"schema": "ora.proposal_context.v1", "mission_id": mission_id,
                "proposal_id": proposal_id, "viewer_id": viewer_id,
                "visibility": "mission_peer_projected", "through": through,
                "entries": entries, "next_cursor": scanned,
                "has_more": scanned < through}
