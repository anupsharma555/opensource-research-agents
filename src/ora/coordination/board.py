"""Peer-authored research proposals with transactional ownership and history.

The board checks identities, exact references and lease fences. It does not rank
scientific ideas or authorize access to a referenced source, tool or model.
"""

from __future__ import annotations

import json
import math
from typing import Callable

from ora.agents.json_shapes import JsonShapeError, finite_json
from ora.state import StateConflict, StateError, StateStore, Transaction
from ora.state.store import identifier


class BoardError(StateError):
    """One board action needs repair; other branches remain usable."""


def ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def require_actor(tx: Transaction, mission_id: str, actor_id: str) -> dict:
    identifier(actor_id)
    actor = tx.get("agent_instance", actor_id, 1)
    if actor is None or actor["body"].get("mission_id") != mission_id:
        raise BoardError("Agent instance is unavailable in this mission")
    return actor


def require_ref(tx: Transaction, mission_id: str, pointer: dict) -> dict:
    if (
        type(pointer) is not dict or set(pointer) != {"kind", "id", "version"}
        or type(pointer.get("kind")) is not str or not pointer["kind"]
        or type(pointer.get("id")) is not str or not pointer["id"]
        or type(pointer.get("version")) is not int or pointer["version"] < 1
    ):
        raise BoardError("An exact kind, ID and positive version are required")
    record = tx.get(**pointer)
    if record is None:
        raise BoardError("Referenced record version is unavailable")
    owner = record["body"].get("mission_id")
    global_kinds = {"publication", "study_family", "source_encounter", "source_copy", "source_passage"}
    if owner != mission_id and not (owner is None and pointer["kind"] in global_kinds):
        raise BoardError("Reference belongs to another mission")
    return record


class ProposalBoard:
    """Short board commands over one shared StateStore."""

    KIND = "board_proposal"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def _revoke_claim_tx(tx: Transaction, mission_id: str, proposal_id: str) -> None:
        # Bump the generation as well as expiring the lease. A wall-clock
        # rollback must never make an older fence valid again.
        tx.db.execute(
            "UPDATE leases SET owner='__revoked__',generation=generation+1,expires_at=0 "
            "WHERE mission_id=? AND task_id=?", (mission_id, proposal_id),
        )

    def _reject(self, operation_id: str, mission_id: str, code: str,
                reason: str, retry: str = "reread_and_revise") -> dict:
        rejection = {"mission_id": mission_id, "operation_id": operation_id,
                     "code": code, "reason": reason, "retry": retry}
        try:
            self.store.transact(
                f"board-reject:{operation_id}", rejection,
                lambda tx: tx.put("board_rejection", operation_id, rejection),
                mission_id=mission_id,
            )
        except StateError:
            pass
        return {"status": "rejected", **rejection}

    def _run(self, operation_id: str, mission_id: str, request: dict, action: Callable) -> dict:
        try:
            finite_json(request)
            value = self.store.transact(operation_id, request, action, mission_id=mission_id)
            return {"status": "accepted", **value}
        except (BoardError, StateConflict, StateError, JsonShapeError) as exc:
            code = "conflict" if isinstance(exc, StateConflict) else "invalid_board_action"
            return self._reject(operation_id, mission_id, code, str(exc))

    def propose(
        self, operation_id: str, *, mission_id: str, proposal_id: str, actor_id: str,
        objective: str, expected_artifact: str, next_action: str,
        prerequisite_refs: list[dict] | None = None,
        depends_on: list[str] | None = None, branch_of: str | None = None,
        status: str = "open",
    ) -> dict:
        request = locals().copy()
        request.pop("self")

        def action(tx: Transaction) -> dict:
            actor = require_actor(tx, mission_id, actor_id)
            for value in (proposal_id, objective, expected_artifact, next_action):
                identifier(value)
            if status not in {"open", "deferred"}:
                raise BoardError("New work must be open or deferred")
            if tx.get(self.KIND, proposal_id):
                raise StateConflict("Proposal ID already exists")
            prerequisites = [require_ref(tx, mission_id, p) for p in prerequisite_refs or []]
            parents = []
            for parent_id in depends_on or []:
                parent = tx.get(self.KIND, parent_id)
                if parent is None or parent["body"]["mission_id"] != mission_id or parent_id == proposal_id:
                    raise BoardError("Dependent proposal is unavailable")
                parents.append(parent)
            branch = tx.get(self.KIND, branch_of) if branch_of else None
            if branch_of and (branch is None or branch["body"]["mission_id"] != mission_id):
                raise BoardError("Alternative branch target is unavailable")
            body = {"mission_id": mission_id, "actor_ref": ref(actor), "objective": objective,
                    "expected_artifact": expected_artifact, "next_action": next_action,
                    "status": status, "prerequisite_refs": [ref(p) for p in prerequisites],
                    "depends_on": [ref(p) for p in parents],
                    "branch_of": ref(branch) if branch else None, "outcome_ref": None}
            saved = tx.put(self.KIND, proposal_id, body)
            tx.link(saved, actor, "proposed_by")
            for target in [*prerequisites, *parents]:
                tx.link(saved, target, "depends_on")
            if branch:
                tx.link(saved, branch, "alternative_to")
            tx.emit(mission_id, "proposal_created", {"proposal_ref": ref(saved),
                    "subjects": [{"kind": "task", "id": proposal_id}]})
            return {"proposal_ref": ref(saved)}

        return self._run(operation_id, mission_id, request, action)

    def revise(
        self, operation_id: str, *, mission_id: str, proposal_id: str, actor_id: str,
        expected_version: int, reason: str, objective: str | None = None,
        next_action: str | None = None, status: str | None = None,
        evidence_refs: list[dict] | None = None,
    ) -> dict:
        request = {key: value for key, value in locals().items() if key != "self"}

        def action(tx: Transaction) -> dict:
            actor = require_actor(tx, mission_id, actor_id)
            old = tx.get(self.KIND, proposal_id)
            if old is None or old["body"]["mission_id"] != mission_id:
                raise BoardError("Proposal is unavailable")
            identifier(reason)
            if status is not None and status not in {"open", "deferred"}:
                raise BoardError("Revision state must be open or deferred")
            if objective is not None:
                identifier(objective)
            if next_action is not None:
                identifier(next_action)
            evidence = [require_ref(tx, mission_id, p) for p in evidence_refs or []]
            body = {**old["body"], "objective": objective or old["body"]["objective"],
                    "next_action": next_action or old["body"]["next_action"],
                    "status": status or old["body"]["status"],
                    "revision": {"actor_ref": ref(actor), "reason": reason,
                                 "evidence_refs": [ref(p) for p in evidence]}}
            saved = tx.put(self.KIND, proposal_id, body, expected_version=expected_version)
            tx.link(saved, old, "derived_from")
            self._revoke_claim_tx(tx, mission_id, proposal_id)
            for item in evidence:
                tx.link(saved, item, "depends_on")
            tx.emit(mission_id, "proposal_revised", {"proposal_ref": ref(saved),
                    "subjects": [{"kind": "task", "id": proposal_id}]})
            return {"proposal_ref": ref(saved)}

        return self._run(operation_id, mission_id, request, action)

    def dissent(
        self, operation_id: str, *, mission_id: str, dissent_id: str,
        proposal_ref: dict, actor_id: str, critique: str,
        evidence_refs: list[dict] | None = None,
    ) -> dict:
        request = {key: value for key, value in locals().items() if key != "self"}

        def action(tx: Transaction) -> dict:
            actor = require_actor(tx, mission_id, actor_id)
            proposal = require_ref(tx, mission_id, proposal_ref)
            if proposal["kind"] != self.KIND:
                raise BoardError("Dissent needs a proposal version")
            identifier(critique)
            evidence = [require_ref(tx, mission_id, p) for p in evidence_refs or []]
            saved = tx.put("board_dissent", dissent_id, {
                "mission_id": mission_id, "proposal_ref": ref(proposal),
                "actor_ref": ref(actor), "critique": critique,
                "evidence_refs": [ref(p) for p in evidence]})
            tx.link(saved, proposal, "challenges")
            for item in evidence:
                tx.link(saved, item, "supports")
            tx.emit(mission_id, "proposal_dissent", {"dissent_ref": ref(saved),
                    "subjects": [{"kind": "task", "id": proposal["id"]}]})
            return {"dissent_ref": ref(saved)}

        return self._run(operation_id, mission_id, request, action)

    def invite(
        self, operation_id: str, *, mission_id: str, invitation_id: str,
        proposal_id: str, sender_id: str, recipient_id: str, objective: str,
        expected_artifact: str, prerequisite_refs: list[dict] | None = None,
    ) -> dict:
        request = {key: value for key, value in locals().items() if key != "self"}

        def action(tx: Transaction) -> dict:
            sender = require_actor(tx, mission_id, sender_id)
            recipient = require_actor(tx, mission_id, recipient_id)
            proposal = tx.get(self.KIND, proposal_id)
            if proposal is None or proposal["body"]["mission_id"] != mission_id:
                raise BoardError("Invitation proposal is unavailable")
            for value in (objective, expected_artifact):
                identifier(value)
            prerequisites = [require_ref(tx, mission_id, p) for p in prerequisite_refs or []]
            saved = tx.put("board_invitation", invitation_id, {
                "mission_id": mission_id, "proposal_ref": ref(proposal),
                "sender_ref": ref(sender), "recipient_ref": ref(recipient),
                "objective": objective, "expected_artifact": expected_artifact,
                "prerequisite_refs": [ref(p) for p in prerequisites],
                "status": "offered", "counter": None, "decision_at": None})
            tx.link(saved, proposal, "about_task")
            for item in prerequisites:
                tx.link(saved, item, "depends_on")
            tx.emit(mission_id, "invitation_offered", {"invitation_ref": ref(saved),
                    "recipient_id": recipient_id,
                    "subjects": [{"kind": "task", "id": proposal_id}]})
            return {"invitation_ref": ref(saved)}

        return self._run(operation_id, mission_id, request, action)

    def respond_invitation(
        self, operation_id: str, *, mission_id: str, invitation_id: str,
        recipient_id: str, expected_version: int, decision: str,
        counter: str | None = None,
    ) -> dict:
        request = {key: value for key, value in locals().items() if key != "self"}

        def action(tx: Transaction) -> dict:
            require_actor(tx, mission_id, recipient_id)
            old = tx.get("board_invitation", invitation_id)
            if old is None or old["body"]["mission_id"] != mission_id:
                raise BoardError("Invitation is unavailable")
            if old["body"]["recipient_ref"]["id"] != recipient_id:
                raise BoardError("Only the recipient can decide this invitation")
            if old["body"]["status"] != "offered":
                raise BoardError("Invitation already has a final decision")
            if decision not in {"accepted", "declined", "countered"}:
                raise BoardError("Decision must accept, decline or counter")
            if decision == "countered":
                identifier(counter)
            if decision == "accepted":
                proposal_ref = old["body"]["proposal_ref"]
                current = tx.get(self.KIND, proposal_ref["id"])
                if current is None or ref(current) != proposal_ref:
                    raise StateConflict("Invitation names an older proposal version")
            body = {**old["body"], "status": decision,
                    "counter": counter if decision == "countered" else old["body"]["counter"],
                    "decision_at": self.store.now()}
            saved = tx.put("board_invitation", invitation_id, body,
                           expected_version=expected_version)
            tx.link(saved, old, "derived_from")
            tx.emit(mission_id, "invitation_decided", {"invitation_ref": ref(saved),
                    "decision": decision,
                    "recipient_ids": [body["sender_ref"]["id"], recipient_id],
                    "subjects": [{"kind": "task", "id": body["proposal_ref"]["id"]}]})
            return {"invitation_ref": ref(saved)}

        return self._run(operation_id, mission_id, request, action)

    def claim(
        self, operation_id: str, *, mission_id: str, proposal_id: str,
        actor_id: str, ttl: float, expected_generation: int | None = None,
        invitation_id: str | None = None, expected_version: int | None = None,
    ) -> dict:
        request = {key: value for key, value in locals().items() if key != "self"}
        if expected_version is None:
            request.pop("expected_version")  # Preserve older exact replay hashes.

        def action(tx: Transaction) -> dict:
            actor = require_actor(tx, mission_id, actor_id)
            proposal = tx.get(self.KIND, proposal_id)
            if proposal is None or proposal["body"]["mission_id"] != mission_id:
                raise BoardError("Proposal is unavailable")
            if expected_version is not None:
                if type(expected_version) is not int or expected_version < 1:
                    raise BoardError("Expected proposal version must be positive")
                if proposal["version"] != expected_version:
                    raise StateConflict("Proposal version changed before claim")
            if proposal["body"]["status"] != "open":
                raise BoardError("Only open work can be claimed")
            for pointer in proposal["body"].get("depends_on", []):
                dependency = tx.get(pointer["kind"], pointer["id"])
                if dependency is None or dependency["body"].get("status") != "completed":
                    raise BoardError("A prerequisite proposal is still open")
            if type(ttl) not in {int, float} or not math.isfinite(ttl) or ttl <= 0:
                raise BoardError("Lease lifetime must be finite and positive")
            if invitation_id is None:
                directed = tx.db.execute(
                    "SELECT 1 FROM records r JOIN heads h USING(kind,id,version) "
                    "WHERE r.kind='board_invitation' "
                    "AND json_extract(r.body,'$.mission_id')=? "
                    "AND json_extract(r.body,'$.proposal_ref.id')=? "
                    "AND json_extract(r.body,'$.recipient_ref.id')=? LIMIT 1",
                    (mission_id, proposal_id, actor_id),
                ).fetchone()
                if directed:
                    raise BoardError("Directed invitation requires a named accepted decision")
            if invitation_id:
                invitation = tx.get("board_invitation", invitation_id)
                if invitation is None or invitation["body"]["mission_id"] != mission_id:
                    raise BoardError("Invitation is unavailable")
                if (invitation["body"]["proposal_ref"] != ref(proposal)
                    or invitation["body"]["recipient_ref"]["id"] != actor_id
                    or invitation["body"]["status"] != "accepted"):
                    raise BoardError("Invitation is unaccepted or names an older proposal version")
            previous = tx.db.execute(
                "SELECT * FROM leases WHERE mission_id=? AND task_id=?",
                (mission_id, proposal_id),
            ).fetchone()
            generation = previous["generation"] if previous else 0
            if expected_generation is not None and expected_generation != generation:
                raise StateConflict("Claim generation changed; reread and replan")
            if previous and previous["expires_at"] > self.store.clock():
                raise StateConflict("Another current claim exists; retry after expiry or replan")
            lease = {"mission_id": mission_id, "task_id": proposal_id,
                     "owner": actor_id, "generation": generation + 1,
                     "expires_at": self.store.clock() + ttl}
            tx.db.execute(
                "INSERT INTO leases VALUES (:mission_id,:task_id,:owner,:generation,:expires_at) "
                "ON CONFLICT(mission_id,task_id) DO UPDATE SET "
                "owner=excluded.owner,generation=excluded.generation,expires_at=excluded.expires_at", lease)
            saved = tx.put("board_claim", f"{mission_id}:{proposal_id}:{lease['generation']}", {
                "mission_id": mission_id, "proposal_ref": ref(proposal),
                "owner_ref": ref(actor), "lease": lease,
                "invitation_id": invitation_id})
            tx.link(saved, proposal, "about_task")
            tx.emit(mission_id, "proposal_claimed", {"claim_ref": ref(saved),
                    "subjects": [{"kind": "claim", "id": proposal_id},
                                 {"kind": "task", "id": proposal_id}]})
            return {"claim_ref": ref(saved), "fence": lease}

        return self._run(operation_id, mission_id, request, action)

    def accept_result(
        self, operation_id: str, *, mission_id: str, proposal_id: str,
        actor_id: str, fence: dict, outcome_id: str,
        result_refs: list[dict] | None = None,
        save_related: Callable[[Transaction], list[dict]] | None = None,
        related_request: dict | None = None,
        complete_task: bool = True,
    ) -> dict:
        """Save related artifacts/messages in this transaction, then accept outcome.

        `save_related` must do only local transaction writes and return exact
        record references. A caller may use an existing result_refs list instead.
        The lease is checked by StateStore at entry and before commit.
        """
        if save_related is not None and related_request is None:
            return self._reject(operation_id, mission_id, "invalid_board_action",
                                "Compound result needs a stable related_request descriptor",
                                "supply_exact_inputs")
        if (type(fence) is not dict
            or set(fence) != {"mission_id", "task_id", "owner", "generation", "expires_at"}
            or any(type(fence[key]) is not str or not fence[key]
                   for key in ("mission_id", "task_id", "owner"))
            or type(fence["generation"]) is not int or fence["generation"] < 1
            or type(fence["expires_at"]) not in {int, float}
            or not math.isfinite(fence["expires_at"])):
            return self._reject(operation_id, mission_id, "invalid_board_action",
                                "Result needs an exact current claim fence")
        request = {"mission_id": mission_id, "proposal_id": proposal_id,
                   "actor_id": actor_id, "fence": fence, "outcome_id": outcome_id,
                   "result_refs": result_refs, "related_request": related_request,
                   "has_save_related": save_related is not None,
                   "complete_task": complete_task}

        def action(tx: Transaction) -> dict:
            actor = require_actor(tx, mission_id, actor_id)
            proposal = tx.get(self.KIND, proposal_id)
            if proposal is None or proposal["body"]["mission_id"] != mission_id:
                raise BoardError("Proposal is unavailable")
            if fence.get("owner") != actor_id or fence.get("task_id") != proposal_id:
                raise BoardError("Result actor does not own the claim")
            if proposal["body"]["status"] != "open":
                raise BoardError("Proposal is no longer open")
            claim = tx.get("board_claim", f"{mission_id}:{proposal_id}:{fence['generation']}")
            if claim is None or claim["body"]["proposal_ref"] != ref(proposal):
                raise StateConflict("Claim names an older proposal version")
            created = save_related(tx) if save_related else []
            pointers = [*(result_refs or []), *created]
            if not pointers:
                raise BoardError("Outcome needs at least one exact result reference")
            targets = [require_ref(tx, mission_id, p) for p in pointers]
            outcome = tx.put("board_outcome", outcome_id, {
                "mission_id": mission_id, "proposal_ref": ref(proposal),
                "actor_ref": ref(actor), "claim_generation": fence["generation"],
                "result_refs": [ref(p) for p in targets],
                "completes_task": complete_task})
            for target in targets:
                tx.link(outcome, target, "depends_on")
            tx.link(outcome, proposal, "about_task")
            if complete_task:
                updated = tx.put(self.KIND, proposal_id,
                                 {**proposal["body"], "status": "completed",
                                  "outcome_ref": ref(outcome)},
                                 expected_version=proposal["version"])
                tx.link(updated, proposal, "derived_from")
            else:
                updated = proposal
            tx.emit(mission_id, "proposal_result_accepted" if complete_task
                    else "proposal_turn_accepted", {"outcome_ref": ref(outcome),
                    "proposal_ref": ref(updated),
                    "subjects": [{"kind": "task", "id": proposal_id},
                                 {"kind": "artifact", "id": outcome_id}]})
            return {"outcome_ref": ref(outcome), "proposal_ref": ref(updated)}

        try:
            finite_json(request)
            value = self.store.transact(operation_id, request, action, mission_id=mission_id,
                                        fence=fence)
            return {"status": "accepted", **value}
        except (BoardError, StateConflict, StateError, JsonShapeError) as exc:
            return self._reject(operation_id, mission_id,
                                "stale_claim" if isinstance(exc, StateConflict)
                                else "invalid_board_action", str(exc))

    def reopen(
        self, operation_id: str, *, mission_id: str, affected_ref: dict,
        contradiction_ref: dict, actor_id: str, reason: str, next_action: str,
    ) -> dict:
        request = {key: value for key, value in locals().items() if key != "self"}

        def action(tx: Transaction) -> dict:
            actor = require_actor(tx, mission_id, actor_id)
            affected = require_ref(tx, mission_id, affected_ref)
            contradiction = require_ref(tx, mission_id, contradiction_ref)
            identifier(reason)
            identifier(next_action)
            ids = self._affected_proposal_ids(tx, mission_id, affected, reason)
            reopened = []
            for proposal_id in ids:
                old = tx.get(self.KIND, proposal_id)
                if old is None or old["body"]["mission_id"] != mission_id:
                    continue
                saved = tx.put(self.KIND, proposal_id, {
                    **old["body"], "status": "open", "outcome_ref": None,
                    "next_action": next_action,
                    "reopen": {"actor_ref": ref(actor), "reason": reason,
                               "affected_ref": ref(affected),
                               "contradiction_ref": ref(contradiction)}},
                    expected_version=old["version"])
                tx.link(saved, old, "derived_from")
                tx.link(saved, contradiction, "challenges")
                self._revoke_claim_tx(tx, mission_id, proposal_id)
                reopened.append(ref(saved))
            tx.emit(mission_id, "proposals_reopened", {"proposal_refs": reopened,
                    "subjects": [{"kind": "task", "id": p["id"]} for p in reopened]})
            return {"proposal_refs": reopened}

        return self._run(operation_id, mission_id, request, action)

    def _affected_proposal_ids(
        self, tx: Transaction, mission_id: str, affected: dict, reason: str,
    ) -> list[str]:
        """Follow exact dependency links within one mission, including global sources."""
        origin = (affected["kind"], affected["id"], affected["version"])
        pending = [origin]
        seen = {origin}
        ids = {affected["id"]} if affected["kind"] == self.KIND else set()
        relations = "'depends_on','supports','challenges','input','cites','derived_from'"
        global_kinds = {"publication", "study_family", "source_encounter",
                        "source_copy", "source_passage"}
        while pending:
            pointer = pending.pop()
            rows = tx.db.execute(
                "SELECT source_kind,source_id,source_version FROM links "
                "WHERE target_kind=? AND target_id=? AND target_version=? "
                f"AND relation IN ({relations})", pointer,
            ).fetchall()
            for row in rows:
                dependent = tuple(row)
                if dependent in seen:
                    continue
                seen.add(dependent)
                record = tx.get(*dependent)
                if record is None:
                    continue
                owner = record["body"].get("mission_id")
                if owner != mission_id and not (owner is None and record["kind"] in global_kinds):
                    continue
                pending.append(dependent)
                if record["kind"] == self.KIND:
                    ids.add(record["id"])
                    tx.db.execute(
                        "INSERT OR IGNORE INTO review_requirements VALUES (?,?,?,?,?,?,?)",
                        (*dependent, reason, *origin),
                    )
        return sorted(ids)

    def open_work(self, mission_id: str) -> list[dict]:
        """Show open/deferred work, including expired and unclaimed proposals."""
        db = self.store._connect()
        try:
            rows = db.execute(
                "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) "
                "WHERE r.kind=? ORDER BY r.id", (self.KIND,),
            ).fetchall()
            result = []
            for row in rows:
                body = json.loads(row["body"])
                if body.get("mission_id") != mission_id or body.get("status") not in {"open", "deferred"}:
                    continue
                lease = db.execute("SELECT * FROM leases WHERE mission_id=? AND task_id=?",
                                   (mission_id, row["id"])).fetchone()
                current = dict(lease) if lease and lease["expires_at"] > self.store.clock() else None
                result.append({"proposal_ref": {"kind": self.KIND, "id": row["id"],
                                                "version": row["version"]},
                               "status": body["status"], "next_action": body["next_action"],
                               "current_claim": current})
            return result
        finally:
            db.close()

    def ready_work(self, mission_id: str) -> list[dict]:
        """Mechanical dependency/claim view; peers still choose what to pursue."""
        return [item for item in self.open_work(mission_id)
                if item["status"] == "open" and item["current_claim"] is None
                and self._dependencies_complete(item["proposal_ref"])]

    def _dependencies_complete(self, proposal_ref: dict) -> bool:
        proposal = self.store.get(**proposal_ref)
        if proposal is None:
            return False
        for pointer in proposal["body"].get("depends_on", []):
            dependency = self.store.get(pointer["kind"], pointer["id"])
            if dependency is None or dependency["body"].get("status") != "completed":
                return False
        return True
