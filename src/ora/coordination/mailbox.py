"""Durable local peer exchange, request outcomes and targeted event replay.

Transport delivery, acknowledgment, answer submission and scientific disposition
are separate facts. A later remote adapter can map its IDs to these local IDs;
no local peer requires a network service.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Callable

from ora.agents.json_shapes import JsonShapeError, finite_json
from ora.state import StateConflict, StateError, StateStore, Transaction
from ora.state.store import identifier

from .board import BoardError, ProposalBoard, ref, require_actor, require_ref


class MailError(StateError):
    """One message or request needs a scoped repair."""


class PeerMailbox:
    """StateStore-backed messages and request outcomes for registered peers."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def _request_subjects(tx: Transaction, request: dict) -> list[dict]:
        message = tx.get(**request["body"]["message_ref"])
        task = message["body"].get("task_ref") if message else None
        return [{"kind": "task", "id": task["id"]}] if task else []

    def _run(self, operation_id: str, mission_id: str, request: dict, action: Callable) -> dict:
        try:
            finite_json(request)
            value = self.store.transact(operation_id, request, action, mission_id=mission_id)
            return {"status": "accepted", **value}
        except (MailError, BoardError, StateConflict, StateError, JsonShapeError) as exc:
            code = "conflict" if isinstance(exc, StateConflict) else "invalid_message"
            rejection = {"mission_id": mission_id, "operation_id": operation_id,
                         "code": code, "reason": str(exc), "retry": "repair_or_redirect"}
            try:
                self.store.transact(f"mail-reject:{operation_id}", rejection,
                                    lambda tx: tx.put("mail_rejection", operation_id, rejection),
                                    mission_id=mission_id)
            except StateError:
                pass
            return {"status": "rejected", **rejection}

    def send_message_tx(
        self, tx: Transaction, *, mission_id: str, message_id: str,
        sender_id: str, recipient_id: str, correlation_id: str,
        payload: Any, task_ref: dict | None = None,
        evidence_refs: list[dict] | None = None,
        request_id: str | None = None,
    ) -> dict:
        """Save message plus pending delivery intent inside the caller's transaction."""
        for value in (mission_id, message_id, correlation_id):
            identifier(value)
        finite_json(payload)
        sender = require_actor(tx, mission_id, sender_id)
        recipient = require_actor(tx, mission_id, recipient_id)
        task = require_ref(tx, mission_id, task_ref) if task_ref else None
        evidence = [require_ref(tx, mission_id, p) for p in evidence_refs or []]
        if tx.get("mail_message", message_id):
            raise StateConflict("Message ID already exists")
        body = {"schema": "ora.peer_message.v1", "mission_id": mission_id,
                "duplicate_detection_id": message_id,
                "sender_ref": ref(sender), "recipient_ref": ref(recipient),
                "correlation_id": correlation_id, "request_id": request_id,
                "task_ref": ref(task) if task else None,
                "evidence_refs": [ref(item) for item in evidence],
                "payload": payload, "created_at": self.store.now(),
                "delivered_at": None, "acknowledged_at": None}
        saved = tx.put("mail_message", message_id, body)
        outbox = tx.put("mail_outbox", message_id, {
            "mission_id": mission_id, "message_ref": ref(saved),
            "recipient_ref": ref(recipient), "status": "pending", "updated_at": self.store.now()})
        tx.link(outbox, saved, "message")
        if task:
            tx.link(saved, task, "about_task")
        for item in evidence:
            tx.link(saved, item, "context")  # A reference does not grant source access.
        tx.emit(mission_id, "peer_message_pending", {"message_ref": ref(saved),
                "recipient_id": recipient_id, "correlation_id": correlation_id,
                "subjects": ([{"kind": "task", "id": task["id"]}] if task else [])})
        return {"message_ref": ref(saved), "outbox_ref": ref(outbox)}

    def send_message(
        self, operation_id: str, *, mission_id: str, message_id: str,
        sender_id: str, recipient_id: str, correlation_id: str, payload: Any,
        task_ref: dict | None = None, evidence_refs: list[dict] | None = None,
    ) -> dict:
        request = {key: value for key, value in locals().items() if key != "self"}
        return self._run(operation_id, mission_id, request,
                         lambda tx: self.send_message_tx(
                             tx, mission_id=mission_id, message_id=message_id,
                             sender_id=sender_id, recipient_id=recipient_id,
                             correlation_id=correlation_id, payload=payload,
                             task_ref=task_ref, evidence_refs=evidence_refs))

    def send_request_tx(
        self, tx: Transaction, *, mission_id: str, request_id: str,
        message_id: str, requester_id: str, recipient_id: str,
        question: str, expected_artifact: str, cause_ref: dict,
        payload: Any, task_ref: dict | None = None,
        evidence_refs: list[dict] | None = None,
        expected_latency: str = "unknown", last_useful_progress_at: str | None = None,
    ) -> dict:
        """Save the question, its message and outbox intent as one local commit."""
        identifier(request_id)
        for label, value in (("Question", question), ("Expected artifact", expected_artifact)):
            if not isinstance(value, str) or not value.strip():
                raise MailError(f"{label} needs nonempty text")
        if expected_latency not in {"short", "medium", "long", "unknown"}:
            raise MailError("Expected latency needs a known category")
        if last_useful_progress_at is not None:
            try:
                datetime.fromisoformat(last_useful_progress_at)
            except (TypeError, ValueError) as exc:
                raise MailError("Last progress needs an ISO timestamp") from exc
        cause = require_ref(tx, mission_id, cause_ref)
        if cause["kind"] == ProposalBoard.KIND:
            current_cause = tx.get(cause["kind"], cause["id"])
            if current_cause is None or current_cause["body"].get("status") == "cancelled":
                raise MailError("Source task was cancelled before peer request")
        if tx.get("mail_request", request_id):
            raise StateConflict("Request ID already exists")
        requester = require_actor(tx, mission_id, requester_id)
        run_id = requester["body"].get("run_id")
        recipient_controls = [
            json.loads(row["body"])
            for row in tx.db.execute(
                "SELECT r.body FROM records r JOIN heads h USING(kind,id,version) "
                "WHERE r.kind='mission_task_control' "
                "AND json_extract(r.body,'$.actor_id')=?",
                (recipient_id,),
            ).fetchall()
        ]
        eligible_recipient = any(
            item.get("run_id") == run_id
            and item.get("phase") not in {"cancelled", "deferred"}
            for item in recipient_controls
        )
        cancelled_recipient = next((
            item for item in recipient_controls
            if item.get("run_id") == run_id and item.get("phase") == "cancelled"
            and item.get("cancellation_request_id")
        ), None)
        cancellation = (
            tx.get("mission_branch_cancellation", cancelled_recipient["cancellation_request_id"])
            if cancelled_recipient and not eligible_recipient else None
        )
        unavailable_ref = ref(cancellation) if cancellation else None
        message = self.send_message_tx(
            tx, mission_id=mission_id, message_id=message_id,
            sender_id=requester_id, recipient_id=recipient_id,
            correlation_id=request_id, payload=payload, task_ref=task_ref,
            evidence_refs=evidence_refs, request_id=request_id)
        if unavailable_ref:
            outbox = tx.get("mail_outbox", message_id)
            tx.put(
                "mail_outbox", message_id,
                {**outbox["body"], "status": "cancelled", "updated_at": self.store.now()},
                expected_version=outbox["version"],
            )
        saved = tx.put("mail_request", request_id, {
            "mission_id": mission_id, "question": question,
            "expected_artifact": expected_artifact, "cause_ref": ref(cause),
            "requester_id": requester_id, "recipient_id": recipient_id,
            "message_ref": message["message_ref"],
            "status": "recipient_unavailable_due_branch_cancel" if unavailable_ref else "unanswered",
            "answer_refs": [], "accepted_answer_ref": None,
            "replacement_request_ref": None, "reviewer_id": None,
            "expected_latency": expected_latency,
            "last_useful_progress_at": last_useful_progress_at,
            "created_at": self.store.now(), "delivered_at": None,
            "answered_at": None, "resolved_at": self.store.now() if unavailable_ref else None,
            **({"reason": "recipient_task_cancelled", "cancellation_ref": unavailable_ref}
               if unavailable_ref else {}),
        })
        tx.link(saved, cause, "caused_by")
        tx.link(saved, tx.get(**message["message_ref"]), "sent_as")
        tx.emit(mission_id, "peer_request_opened", {"request_ref": ref(saved),
                "recipient_id": recipient_id,
                "subjects": ([{"kind": "task", "id": task_ref["id"]}] if task_ref else [])})
        if unavailable_ref:
            tx.emit(mission_id, "peer_request_recipient_unavailable", {
                "request_ref": ref(saved), "cancellation_ref": unavailable_ref,
                "recipient_ids": [requester_id],
                "subjects": ([{"kind": "task", "id": task_ref["id"]}] if task_ref else []),
            })
        return {**message, "request_ref": ref(saved)}

    def send_request(
        self, operation_id: str, *, mission_id: str, request_id: str,
        message_id: str, requester_id: str, recipient_id: str,
        question: str, expected_artifact: str, cause_ref: dict, payload: Any,
        task_ref: dict | None = None, evidence_refs: list[dict] | None = None,
        expected_latency: str = "unknown", last_useful_progress_at: str | None = None,
    ) -> dict:
        request = {key: value for key, value in locals().items() if key != "self"}
        return self._run(operation_id, mission_id, request,
                         lambda tx: self.send_request_tx(
                             tx, mission_id=mission_id, request_id=request_id,
                             message_id=message_id, requester_id=requester_id,
                             recipient_id=recipient_id, question=question,
                             expected_artifact=expected_artifact, cause_ref=cause_ref,
                             payload=payload, task_ref=task_ref,
                             evidence_refs=evidence_refs,
                             expected_latency=expected_latency,
                             last_useful_progress_at=last_useful_progress_at))

    def mark_delivered(self, operation_id: str, *, mission_id: str,
                       message_id: str) -> dict:
        request = {"mission_id": mission_id, "message_id": message_id}

        def action(tx: Transaction) -> dict:
            message = tx.get("mail_message", message_id)
            outbox = tx.get("mail_outbox", message_id)
            if message is None or outbox is None or message["body"]["mission_id"] != mission_id:
                raise MailError("Message is unavailable")
            if outbox["body"]["status"] == "delivered":
                return {"message_ref": ref(message), "outbox_ref": ref(outbox)}
            delivered_at = self.store.now()
            updated = tx.put("mail_message", message_id,
                             {**message["body"], "delivered_at": delivered_at},
                             expected_version=message["version"])
            saved_outbox = tx.put("mail_outbox", message_id,
                                 {**outbox["body"], "message_ref": ref(updated),
                                  "status": "delivered", "updated_at": delivered_at},
                                 expected_version=outbox["version"])
            tx.link(updated, message, "derived_from")
            tx.link(saved_outbox, updated, "message")
            request_id = message["body"].get("request_id")
            if request_id:
                pending = tx.get("mail_request", request_id)
                if pending and pending["body"]["delivered_at"] is None:
                    tx.put("mail_request", request_id,
                           {**pending["body"], "delivered_at": delivered_at},
                           expected_version=pending["version"])
            tx.emit(mission_id, "peer_message_delivered", {"message_ref": ref(updated),
                    "recipient_id": message["body"]["recipient_ref"]["id"],
                    "recipient_ids": [message["body"]["sender_ref"]["id"],
                                      message["body"]["recipient_ref"]["id"]],
                    "subjects": []})
            return {"message_ref": ref(updated), "outbox_ref": ref(saved_outbox)}

        return self._run(operation_id, mission_id, request, action)

    def acknowledge(self, operation_id: str, *, mission_id: str,
                    message_id: str, recipient_id: str) -> dict:
        request = {"mission_id": mission_id, "message_id": message_id,
                   "recipient_id": recipient_id}

        def action(tx: Transaction) -> dict:
            require_actor(tx, mission_id, recipient_id)
            old = tx.get("mail_message", message_id)
            if old is None or old["body"]["mission_id"] != mission_id:
                raise MailError("Message is unavailable")
            if old["body"]["recipient_ref"]["id"] != recipient_id:
                raise MailError("Only recipient can acknowledge")
            if old["body"]["delivered_at"] is None:
                raise MailError("Message has not been delivered")
            if old["body"]["acknowledged_at"]:
                return {"message_ref": ref(old)}
            saved = tx.put("mail_message", message_id,
                           {**old["body"], "acknowledged_at": self.store.now()},
                           expected_version=old["version"])
            tx.link(saved, old, "derived_from")
            tx.emit(mission_id, "peer_message_acknowledged", {"message_ref": ref(saved),
                    "recipient_id": recipient_id,
                    "recipient_ids": [old["body"]["sender_ref"]["id"], recipient_id],
                    "subjects": []})
            return {"message_ref": ref(saved)}

        return self._run(operation_id, mission_id, request, action)

    def record_answer_tx(
        self, tx: Transaction, *, mission_id: str, request_id: str,
        answer_id: str, responder_id: str, artifact_ref: dict,
        partial: bool = False,
    ) -> dict:
        """Append an answer reference; review still decides request disposition."""
        require_actor(tx, mission_id, responder_id)
        request = tx.get("mail_request", request_id)
        if request is None or request["body"]["mission_id"] != mission_id:
            raise MailError("Request is unavailable")
        if request["body"]["recipient_id"] != responder_id:
            raise MailError("Only the current recipient can answer")
        if request["body"]["status"] in {"answered", "declined", "replaced", "cancelled", "recipient_unavailable_due_branch_cancel"}:
            raise MailError("Request already has a final disposition")
        artifact = require_ref(tx, mission_id, artifact_ref)
        if tx.get("mail_answer", answer_id):
            raise StateConflict("Answer ID already exists")
        answer = tx.put("mail_answer", answer_id, {
            "mission_id": mission_id, "request_id": request_id,
            "responder_id": responder_id, "artifact_ref": ref(artifact),
            "partial": partial, "created_at": self.store.now()})
        tx.link(answer, artifact, "depends_on")
        tx.link(answer, request, "answers")
        status = "partial_awaiting_review" if partial else "awaiting_review"
        updated = tx.put("mail_request", request_id,
                         {**request["body"], "status": status,
                          "answer_refs": [*request["body"]["answer_refs"], ref(answer)],
                          "answered_at": self.store.now()},
                         expected_version=request["version"])
        tx.link(updated, request, "derived_from")
        tx.emit(mission_id, "peer_answer_submitted", {"request_ref": ref(updated),
                "answer_ref": ref(answer),
                "recipient_ids": [request["body"]["requester_id"]],
                "subjects": self._request_subjects(tx, request)})
        return {"answer_ref": ref(answer), "request_ref": ref(updated)}

    def record_answer(
        self, operation_id: str, *, mission_id: str, request_id: str,
        answer_id: str, responder_id: str, artifact_ref: dict,
        partial: bool = False,
    ) -> dict:
        request = {key: value for key, value in locals().items() if key != "self"}
        return self._run(operation_id, mission_id, request,
                         lambda tx: self.record_answer_tx(
                             tx, mission_id=mission_id, request_id=request_id,
                             answer_id=answer_id, responder_id=responder_id,
                             artifact_ref=artifact_ref, partial=partial))

    @staticmethod
    def _lineage(tx: Transaction, actor_id: str) -> set[str]:
        chain = set()
        cursor = actor_id
        while cursor and cursor not in chain:
            chain.add(cursor)
            actor = tx.get("agent_instance", cursor, 1)
            cursor = actor["body"].get("predecessor_id") if actor else None
        return chain

    def review_answer(
        self, operation_id: str, *, mission_id: str, request_id: str,
        reviewer_id: str, answer_id: str, disposition: str,
        reason: str, requester_offline: bool = False,
        availability_ref: dict | None = None,
        expected_request_version: int | None = None,
        expected_answer_version: int | None = None,
    ) -> dict:
        request = {key: value for key, value in locals().items()
                   if key != "self" and (key not in {
                       "expected_request_version", "expected_answer_version"
                   } or value is not None)}

        def action(tx: Transaction) -> dict:
            reviewer = require_actor(tx, mission_id, reviewer_id)
            pending = tx.get("mail_request", request_id)
            answer = tx.get("mail_answer", answer_id)
            if pending is None or answer is None or pending["body"]["mission_id"] != mission_id:
                raise MailError("Request or answer is unavailable")
            if (expected_request_version is not None
                    and (type(expected_request_version) is not int
                         or pending["version"] != expected_request_version)):
                raise StateConflict("Request version changed before peer review")
            if (expected_answer_version is not None
                    and (type(expected_answer_version) is not int
                         or answer["version"] != expected_answer_version)):
                raise StateConflict("Answer version changed before peer review")
            if answer["body"]["request_id"] != request_id:
                raise MailError("Answer belongs to another request")
            if ref(answer) not in pending["body"]["answer_refs"]:
                raise MailError("Answer version is not linked to the request")
            if pending["body"]["status"] in {"answered", "declined", "replaced", "cancelled", "recipient_unavailable_due_branch_cancel"}:
                raise MailError("A final disposition already exists")
            if disposition not in {"answered", "partly_answered"}:
                raise MailError("Review disposition must be answered or partly answered")
            if type(requester_offline) is not bool:
                raise MailError("Requester availability flag must be Boolean")
            identifier(reason)
            requester_id = pending["body"]["requester_id"]
            if reviewer_id != requester_id:
                requester = require_actor(tx, mission_id, requester_id)
                if not requester_offline:
                    raise MailError("Replacement review needs an offline requester")
                if availability_ref is None:
                    raise MailError("Replacement review needs a saved host availability fact")
                availability = require_ref(tx, mission_id, availability_ref)
                current_availability = tx.get("agent_availability", availability["id"])
                if (availability["kind"] != "agent_availability"
                    or current_availability is None
                    or ref(current_availability) != ref(availability)
                    or availability["body"].get("instance_id") != requester_id
                    or availability["body"].get("status") != "offline"
                    or availability["body"].get("recorded_by") != "host"):
                    raise MailError("Saved requester availability is not current or host recorded")
                if reviewer["body"]["role_id"] != requester["body"]["role_id"]:
                    raise MailError("Replacement reviewer needs the requester's role")
                responder_id = answer["body"]["responder_id"]
                if (self._lineage(tx, reviewer_id) & self._lineage(tx, requester_id)
                    or self._lineage(tx, reviewer_id) & self._lineage(tx, responder_id)):
                    raise MailError("Replacement review lacks independent lineage")
            if reviewer_id == answer["body"]["responder_id"]:
                raise MailError("Responder cannot assess its own answer")
            body = {**pending["body"], "status": disposition,
                    "accepted_answer_ref": ref(answer) if disposition == "answered" else None,
                    "reviewer_id": reviewer_id,
                    "review": {"reason": reason, "requester_offline": requester_offline,
                               "availability_ref": ref(availability) if reviewer_id != requester_id
                               else None,
                               "independence_checked": reviewer_id != requester_id},
                    "resolved_at": self.store.now() if disposition == "answered" else None,
                    "last_useful_progress_at": self.store.now()}
            saved = tx.put("mail_request", request_id, body,
                           expected_version=pending["version"])
            tx.link(saved, pending, "derived_from")
            tx.link(saved, answer, "reviewed_answer")
            tx.emit(mission_id, "peer_request_reviewed", {"request_ref": ref(saved),
                    "disposition": disposition,
                    "recipient_ids": [requester_id, answer["body"]["responder_id"]],
                    "subjects": self._request_subjects(tx, pending)})
            return {"request_ref": ref(saved)}

        return self._run(operation_id, mission_id, request, action)

    def decline(self, operation_id: str, *, mission_id: str, request_id: str,
                recipient_id: str, reason: str, expected_version: int | None = None,
                producer_response_ref: dict | None = None) -> dict:
        request = {key: value for key, value in locals().items()
                   if key != "self" and (key not in {
                       "expected_version", "producer_response_ref"
                   } or value is not None)}

        def action(tx: Transaction) -> dict:
            require_actor(tx, mission_id, recipient_id)
            old = tx.get("mail_request", request_id)
            if old is None or old["body"]["mission_id"] != mission_id:
                raise MailError("Request is unavailable")
            if expected_version is not None:
                if type(expected_version) is not int or expected_version < 1:
                    raise MailError("Expected request version must be positive")
                if old["version"] != expected_version:
                    raise StateConflict("Request changed before recipient decline")
                if old["body"]["status"] not in {"unanswered", "partly_answered"}:
                    raise MailError("Request has an answer awaiting review or a final disposition")
            if old["body"]["recipient_id"] != recipient_id or old["body"]["status"] in {"answered", "declined", "replaced", "cancelled", "recipient_unavailable_due_branch_cancel"}:
                raise MailError("Recipient or request state is invalid")
            producer = None
            if producer_response_ref is not None:
                producer = require_ref(tx, mission_id, producer_response_ref)
                call_ref = producer["body"].get("call_ref")
                call = require_ref(tx, mission_id, call_ref) if isinstance(call_ref, dict) else None
                if (producer["kind"] != "model_response_receipt"
                        or producer["body"].get("status") != "tool_requested"
                        or call is None or call["kind"] != "agent_call"
                        or call["body"].get("instance_id") != recipient_id
                        or call["body"].get("task_id") != producer["body"].get("task_id")
                        or call["body"].get("attempt_id") != producer["body"].get("attempt_id")
                        or call["body"].get("mission_id") != mission_id):
                    raise MailError("Decline reason lacks this recipient's model response")
                try:
                    payload = json.loads(self.store.read_blob(producer["body"]["blob"]["sha256"]))
                    calls = payload["tool_calls"]
                    selected = calls[0].get("function") if (
                        type(calls) is list and len(calls) == 1
                        and isinstance(calls[0], dict)
                    ) else None
                    arguments = json.loads(selected["arguments"]) if (
                        isinstance(selected, dict)
                    ) else None
                except (KeyError, IndexError, TypeError, ValueError, StateError) as exc:
                    raise MailError("Decline reason lacks an exact saved tool call") from exc
                if (not isinstance(selected, dict)
                        or selected.get("name") != "decline_peer_request"
                        or arguments != {
                            "request_id": request_id,
                            "request_version": expected_version,
                            "reason": reason,
                        }):
                    raise MailError("Decline reason differs from the saved model tool call")
            identifier(reason)
            saved = tx.put("mail_request", request_id,
                           {**old["body"], "status": "declined", "reason": reason,
                            "resolved_at": self.store.now(),
                            **({"reason_producer_response_ref": ref(producer)}
                               if producer is not None else {})},
                           expected_version=old["version"])
            tx.link(saved, old, "derived_from")
            if producer is not None:
                tx.link(saved, producer, "input")
            tx.emit(mission_id, "peer_request_declined", {"request_ref": ref(saved),
                    "recipient_ids": [old["body"]["requester_id"]],
                    "subjects": self._request_subjects(tx, old)})
            return {"request_ref": ref(saved)}

        return self._run(operation_id, mission_id, request, action)

    def redirect(
        self, operation_id: str, *, mission_id: str, old_request_id: str,
        new_request_id: str, new_message_id: str, actor_id: str,
        recipient_id: str, reason: str,
    ) -> dict:
        """Keep the old setback visible and open a correlated replacement."""
        request = {key: value for key, value in locals().items() if key != "self"}

        def action(tx: Transaction) -> dict:
            require_actor(tx, mission_id, actor_id)
            old = tx.get("mail_request", old_request_id)
            if old is None or old["body"]["mission_id"] != mission_id:
                raise MailError("Original request is unavailable")
            if old["body"]["status"] in {"answered", "replaced", "cancelled", "recipient_unavailable_due_branch_cancel"}:
                raise MailError("Original request cannot be redirected")
            delegated = (
                old["body"].get("reviewer_id") == actor_id
                and old["body"].get("review", {}).get("requester_offline") is True
                and old["body"].get("review", {}).get("independence_checked") is True
            )
            if actor_id != old["body"]["requester_id"] and not delegated:
                raise MailError("Only the requester or recorded replacement reviewer can redirect")
            identifier(reason)
            original_message_ref = old["body"]["message_ref"]
            original_message = tx.get(**original_message_ref)
            if (original_message is None
                or original_message["body"].get("mission_id") != mission_id
                or original_message["body"].get("request_id") != old_request_id):
                raise MailError("Original request message is unavailable")
            # Keep the original scientific content in its own field: arbitrary
            # nested payload keys cannot collide with redirect metadata.
            redirected_payload = {
                "redirect": {"from_request_ref": ref(old),
                             "from_message_ref": original_message_ref,
                             "reason": reason},
                "original_payload": original_message["body"]["payload"],
            }
            created = self.send_request_tx(
                tx, mission_id=mission_id, request_id=new_request_id,
                message_id=new_message_id, requester_id=actor_id,
                recipient_id=recipient_id, question=old["body"]["question"],
                expected_artifact=old["body"]["expected_artifact"],
                cause_ref=old["body"]["cause_ref"],
                payload=redirected_payload,
                task_ref=original_message["body"].get("task_ref"),
                evidence_refs=original_message["body"].get("evidence_refs", []),
                expected_latency=old["body"]["expected_latency"])
            tx.link(tx.get(**created["message_ref"]), original_message, "derived_from")
            saved = tx.put("mail_request", old_request_id,
                           {**old["body"], "status": "replaced", "reason": reason,
                            "replacement_request_ref": created["request_ref"],
                            "resolved_at": self.store.now()},
                           expected_version=old["version"])
            tx.link(tx.get(**created["request_ref"]), old, "replaces")
            tx.link(saved, old, "derived_from")
            tx.emit(mission_id, "peer_request_redirected", {"old_request_ref": ref(saved),
                    "new_request_ref": created["request_ref"],
                    "recipient_ids": [old["body"]["requester_id"],
                                      old["body"]["recipient_id"], recipient_id],
                    "subjects": self._request_subjects(tx, old)})
            return {"old_request_ref": ref(saved), **created}

        return self._run(operation_id, mission_id, request, action)

    def request_view(self, request_id: str) -> dict | None:
        record = self.store.get("mail_request", request_id)
        if record is None:
            return None
        body = record["body"]
        def elapsed(end: str | None) -> float | None:
            if end is None:
                return None
            return (datetime.fromisoformat(end) - datetime.fromisoformat(body["created_at"])).total_seconds()
        now = self.store.now()
        return {"request_ref": ref(record), **body,
                "delivery_seconds": elapsed(body["delivered_at"]),
                "answer_seconds": elapsed(body["answered_at"]),
                "resolution_seconds": elapsed(body["resolved_at"]),
                "waiting_seconds": elapsed(body["resolved_at"] or now)}

    def pending_outbox(self, mission_id: str) -> list[dict]:
        return [item for item in self._records("mail_outbox")
                if item["body"]["mission_id"] == mission_id
                and item["body"]["status"] == "pending"]

    def inbox(self, mission_id: str, recipient_id: str) -> list[dict]:
        return [item for item in self._records("mail_message")
                if item["body"]["mission_id"] == mission_id
                and item["body"]["recipient_ref"]["id"] == recipient_id]

    def _records(self, kind: str) -> list[dict]:
        items = []
        after = ""
        while True:
            page = self.store.records(kind, after_id=after)
            items.extend(page)
            if len(page) < 1000:
                return items
            after = page[-1]["id"]

    def subscribe(self, operation_id: str, *, mission_id: str, actor_id: str,
                  subjects: list[dict]) -> dict:
        request = {key: value for key, value in locals().items() if key != "self"}

        def action(tx: Transaction) -> dict:
            require_actor(tx, mission_id, actor_id)
            if not subjects or any(type(item) is not dict or set(item) != {"kind", "id"}
                                   or item["kind"] not in {"task", "claim", "artifact"}
                                   or type(item["id"]) is not str or not item["id"]
                                   for item in subjects):
                raise MailError("Subscription needs task, claim or artifact subjects")
            sub_id = f"{mission_id}:{actor_id}"
            old = tx.get("mail_subscription", sub_id)
            body = {"mission_id": mission_id, "actor_id": actor_id,
                    "subjects": subjects, "cursor": old["body"]["cursor"] if old else 0,
                    "offered_through": old["body"]["cursor"] if old else 0}
            saved = tx.put("mail_subscription", sub_id, body,
                           expected_version=old["version"] if old else 0)
            return {"subscription_ref": ref(saved)}

        return self._run(operation_id, mission_id, request, action)

    def catch_up(self, mission_id: str, actor_id: str, *, limit: int = 100) -> dict:
        if type(limit) is not int or limit <= 0:
            raise MailError("Catch-up limit must be positive")
        sub = self.store.get("mail_subscription", f"{mission_id}:{actor_id}")
        if sub is None or sub["body"]["mission_id"] != mission_id:
            raise MailError("Subscription is unavailable")
        watched = {(item["kind"], item["id"]) for item in sub["body"]["subjects"]}
        cursor = sub["body"]["cursor"]
        found = []
        scanned = cursor
        while len(found) < limit:
            batch = self.store.events(mission_id, after=scanned, limit=1000)
            if not batch:
                break
            for event in batch:
                scanned = event["sequence"]
                subjects = event["body"].get("subjects", [])
                recipient = event["body"].get("recipient_id")
                recipients = event["body"].get("recipient_ids", [])
                if recipient == actor_id or actor_id in recipients or any((item.get("kind"), item.get("id")) in watched
                                                or (item.get("kind"), "*") in watched
                                                for item in subjects if type(item) is dict):
                    found.append(event)
                    if len(found) >= limit:
                        break
            if len(batch) < 1000:
                break
        if scanned > sub["body"].get("offered_through", cursor):
            def offer(tx: Transaction) -> dict:
                current = tx.get("mail_subscription", sub["id"])
                if current is None or current["version"] != sub["version"]:
                    raise StateConflict("Subscription changed during catch-up; retry")
                saved = tx.put("mail_subscription", sub["id"],
                               {**current["body"], "offered_through": scanned},
                               expected_version=current["version"])
                return {"subscription_ref": ref(saved)}

            self.store.transact(f"mail-offer:{uuid.uuid4().hex}",
                                {"mission_id": mission_id, "actor_id": actor_id,
                                 "from": cursor, "through": scanned},
                                offer, mission_id=mission_id)
        return {"events": found, "cursor": cursor, "scanned_through": scanned}

    def ack_cursor(self, operation_id: str, *, mission_id: str,
                   actor_id: str, sequence: int) -> dict:
        request = {key: value for key, value in locals().items() if key != "self"}

        def action(tx: Transaction) -> dict:
            require_actor(tx, mission_id, actor_id)
            sub_id = f"{mission_id}:{actor_id}"
            old = tx.get("mail_subscription", sub_id)
            if old is None:
                raise MailError("Subscription is unavailable")
            if type(sequence) is not int or sequence < 0:
                raise MailError("Cursor must be nonnegative")
            if sequence > old["body"].get("offered_through", old["body"]["cursor"]):
                raise MailError("Cursor cannot pass the last offered catch-up event")
            if sequence <= old["body"]["cursor"]:
                return {"subscription_ref": ref(old), "cursor": old["body"]["cursor"]}
            saved = tx.put("mail_subscription", sub_id,
                           {**old["body"], "cursor": sequence},
                           expected_version=old["version"])
            return {"subscription_ref": ref(saved), "cursor": sequence}

        return self._run(operation_id, mission_id, request, action)
