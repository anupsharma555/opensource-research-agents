"""Agent-authenticated optional work and invitations over the existing board.

Board proposals remain the source of truth for research work. Controls make a
chosen branch or invitation review runnable by the mission host; they do not
choose an interpretation, require consensus, or enlarge the user's requested
participant list.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from ora.agents import AgentRegistry
from ora.coordination.board import ProposalBoard, ref
from ora.state import StateConflict, StateStore
from ora.state.store import canonical


class WorkError(ValueError):
    """Only this agent action needs repair; other work remains usable."""


def work_control_id(
    run_id: str,
    proposal_id: str,
    actor_id: str,
    control_type: str,
    invitation_id: str | None = None,
) -> str:
    """Stable ID independent of role alias, so one peer can hold several tasks."""
    digest = hashlib.sha256(
        canonical([run_id, proposal_id, actor_id, control_type, invitation_id]).encode()
    ).hexdigest()
    return "mission-work-control-" + digest[:32]


def _exact(store: StateStore, pointer: dict, kind: str) -> dict:
    if (
        type(pointer) is not dict
        or set(pointer) != {"kind", "id", "version"}
        or pointer.get("kind") != kind
        or type(pointer.get("id")) is not str
        or type(pointer.get("version")) is not int
        or pointer["version"] < 1
    ):
        raise WorkError(f"Exact {kind} reference is required")
    record = store.get(**pointer)
    if record is None:
        raise WorkError(f"Exact {kind} version is unavailable")
    return record


def _all(store: StateStore, kind: str) -> list[dict]:
    output: list[dict] = []
    after = ""
    while True:
        page = store.records(kind, after_id=after)
        output.extend(page)
        if len(page) < 1000:
            return output
        after = page[-1]["id"]


def _operation_exists(store: StateStore, operation_id: str) -> bool:
    """Allow an exact durable board replay past a now-historical version."""
    db = store._connect()
    try:
        return (
            db.execute("SELECT 1 FROM operations WHERE id=?", (operation_id,)).fetchone()
            is not None
        )
    finally:
        db.close()


class MissionWork:
    """Thin mission authority and control layer around ProposalBoard."""

    def __init__(
        self,
        store: StateStore,
        board: ProposalBoard,
        registry: AgentRegistry,
        role_choices: Mapping[str, tuple[str, str]],
    ) -> None:
        self.store = store
        self.board = board
        self.registry = registry
        self.role_choices = dict(role_choices)

    def _authorize(
        self,
        run_ref: dict,
        actor_id: str,
        source_control_ref: dict,
        *,
        allow_historical_control: bool = False,
        replay: bool = False,
    ) -> tuple[dict, dict, dict]:
        run = _exact(self.store, run_ref, "mission_run")
        control = _exact(self.store, source_control_ref, "mission_task_control")
        actor = self.store.get("agent_instance", actor_id, 1)
        if actor is None:
            raise WorkError("Actor is unavailable")
        mission_id, run_id = run["body"].get("mission_id"), run["id"]
        if (
            actor["body"].get("mission_id") != mission_id
            or actor["body"].get("run_id") != run_id
            or control["body"].get("mission_id") != mission_id
            or control["body"].get("run_id") != run_id
            or control["body"].get("actor_id") != actor_id
        ):
            raise WorkError("Actor, run and exact task control must belong together")
        current = self.store.get("mission_task_control", control["id"])
        if not allow_historical_control and ref(current) != source_control_ref:
            raise WorkError("Task control changed; reread its current version")
        if not replay and (
            control["body"].get("phase") == "done"
            and control["body"].get("scope_status") != "continued"
        ):
            raise WorkError("Completed task control cannot author a new action")
        current_run = self.store.get("mission_run", run_id)
        if (
            not replay
            and current_run["body"].get("status") == "completed"
            and not (
                control["body"].get("scope_required") is False
                and control["body"].get("scope_status") == "continued"
            )
        ):
            raise WorkError("This scoped run has completed; continue in a new scope")
        return run, actor, control

    def _current_proposal(self, run: dict, pointer: dict) -> dict:
        proposal = _exact(self.store, pointer, "board_proposal")
        if proposal["body"].get("mission_id") != run["body"]["mission_id"]:
            raise WorkError("Proposal belongs to another mission")
        current = self.store.get("board_proposal", proposal["id"])
        if ref(current) != pointer:
            raise WorkError("Proposal version changed; reread before acting")
        return proposal

    def _control(
        self,
        *,
        run: dict,
        actor: dict,
        alias: str,
        proposal_ref: dict,
        source_control: dict,
        control_type: str,
        invitation_ref: dict | None = None,
        offered_proposal_ref: dict | None = None,
        invitation_offer: dict | None = None,
        requested_participant: bool,
    ) -> dict:
        if alias not in self.role_choices:
            raise WorkError("Control needs a supported role alias")
        control_id = work_control_id(
            run["id"],
            proposal_ref["id"],
            actor["id"],
            control_type,
            invitation_ref["id"] if invitation_ref else None,
        )
        existing = self.store.get("mission_task_control", control_id)
        if existing:
            body = existing["body"]
            if (
                body.get("actor_id") != actor["id"]
                or body.get("task_id") != proposal_ref["id"]
                or body.get("control_type") != control_type
                or (body.get("invitation_ref") or {}).get("id") != (invitation_ref or {}).get("id")
            ):
                raise WorkError("Optional control identity collision")
            return ref(existing)
        scope_open = self.store.get("mission_run", run["id"])["body"]["status"] != "completed"
        body = {
            "schema": "ora.mission_task.v1",
            "mission_id": run["body"]["mission_id"],
            "run_id": run["id"],
            "alias": alias,
            "actor_id": actor["id"],
            "task_id": proposal_ref["id"],
            "proposal_ref": proposal_ref,
            "phase": "ready_model" if scope_open else "waiting",
            "sequence": 0,
            "work_id": None,
            "call_ref": None,
            "response_ref": None,
            "tool_call": None,
            "tool_turn_refs": [],
            "source_refs": list(source_control["body"].get("source_refs", [])),
            "selected_passage_refs": {
                key: list(value)
                for key, value in source_control["body"].get("selected_passage_refs", {}).items()
            },
            "read_window_refs": list(source_control["body"].get("read_window_refs", [])),
            "feedback": {
                "status": "invitation_offered"
                if control_type == "invitation_review"
                else "new_peer_work",
                "proposal_ref": proposal_ref,
                "offered_proposal_ref": offered_proposal_ref,
                "invitation_ref": invitation_ref,
                "invitation_offer": invitation_offer,
                "next_action": "Consider and respond to the invitation"
                if control_type == "invitation_review"
                else "Choose how to pursue, revise or defer this proposed work",
            },
            "report_ref": None,
            "last_progress_at": self.store.now(),
            "waiting_reason": None
            if scope_open
            else "Scope completed before optional control activation",
            "scope_required": False,
            "scope_status": (
                "pending_next_scope"
                if not scope_open
                else "offered"
                if control_type == "invitation_review"
                else "open"
            ),
            "control_type": control_type,
            "origin_proposal_ref": offered_proposal_ref
            or source_control["body"].get("proposal_ref"),
            "invitation_ref": invitation_ref,
            "requested_participant": requested_participant,
        }

        def create(tx):
            saved = tx.put("mission_task_control", control_id, body)
            tx.link(saved, tx.get(**proposal_ref), "about_task")
            if invitation_ref:
                tx.link(saved, tx.get(**invitation_ref), "about_invitation")
            return {"control_ref": ref(saved)}

        result = self.store.transact(
            "mission-work-control:" + control_id, body, create, mission_id=run["body"]["mission_id"]
        )
        return result["control_ref"]

    def propose(
        self,
        operation_id: str,
        *,
        run_ref: dict,
        actor_id: str,
        source_control_ref: dict,
        proposal_id: str,
        objective: str,
        expected_artifact: str,
        next_action: str,
        branch_of_ref: dict | None = None,
        depends_on: list[str] | None = None,
        deferred: bool = False,
    ) -> dict:
        replay = _operation_exists(self.store, operation_id)
        run, actor, control = self._authorize(
            run_ref,
            actor_id,
            source_control_ref,
            allow_historical_control=replay,
            replay=replay,
        )
        if branch_of_ref:
            (
                _exact(self.store, branch_of_ref, "board_proposal")
                if replay
                else self._current_proposal(run, branch_of_ref)
            )
        response = self.board.propose(
            operation_id,
            mission_id=run["body"]["mission_id"],
            proposal_id=proposal_id,
            actor_id=actor_id,
            objective=objective,
            expected_artifact=expected_artifact,
            next_action=next_action,
            depends_on=depends_on,
            branch_of=branch_of_ref["id"] if branch_of_ref else None,
            prerequisite_refs=[source_control_ref],
            status="deferred" if deferred else "open",
        )
        if response["status"] != "accepted":
            return {**response, "next_action": "Reread the board and revise this branch only"}
        if deferred:
            return {
                **response,
                "scope_required": False,
                "scope_status": "deferred",
                "next_action": "The proposal remains visible for later pursuit",
            }
        control_ref = self._control(
            run=run,
            actor=actor,
            alias=control["body"]["alias"],
            proposal_ref=response["proposal_ref"],
            source_control=control,
            control_type="research_work",
            requested_participant=control["body"].get("requested_participant", True),
        )
        return {
            **response,
            "control_ref": control_ref,
            "scope_required": False,
            "scope_status": self.store.get(**control_ref)["body"]["scope_status"],
            "next_action": "Optional work is ready; it does not block selected scope completion",
        }

    def revise(
        self,
        operation_id: str,
        *,
        run_ref: dict,
        actor_id: str,
        source_control_ref: dict,
        proposal_ref: dict,
        reason: str,
        objective: str | None = None,
        next_action: str | None = None,
    ) -> dict:
        replay = _operation_exists(self.store, operation_id)
        run, _actor, _control = self._authorize(
            run_ref,
            actor_id,
            source_control_ref,
            allow_historical_control=replay,
            replay=replay,
        )
        proposal = (
            _exact(self.store, proposal_ref, "board_proposal")
            if replay
            else self._current_proposal(run, proposal_ref)
        )
        if proposal["body"].get("mission_id") != run["body"]["mission_id"]:
            raise WorkError("Proposal belongs to another mission")
        response = self.board.revise(
            operation_id,
            mission_id=run["body"]["mission_id"],
            proposal_id=proposal["id"],
            actor_id=actor_id,
            expected_version=proposal["version"],
            reason=reason,
            objective=objective,
            next_action=next_action,
        )
        return {
            **response,
            "next_action": "Use the revised exact proposal version"
            if response["status"] == "accepted"
            else "Reread the current proposal",
        }

    def defer(
        self,
        operation_id: str,
        *,
        run_ref: dict,
        actor_id: str,
        source_control_ref: dict,
        proposal_ref: dict,
        reason: str,
    ) -> dict:
        replay = _operation_exists(self.store, operation_id)
        run, _actor, _control = self._authorize(
            run_ref,
            actor_id,
            source_control_ref,
            allow_historical_control=replay,
            replay=replay,
        )
        proposal = (
            _exact(self.store, proposal_ref, "board_proposal")
            if replay
            else self._current_proposal(run, proposal_ref)
        )
        if proposal["body"].get("mission_id") != run["body"]["mission_id"]:
            raise WorkError("Proposal belongs to another mission")
        response = self.board.revise(
            operation_id,
            mission_id=run["body"]["mission_id"],
            proposal_id=proposal["id"],
            actor_id=actor_id,
            expected_version=proposal["version"],
            reason=reason,
            status="deferred",
        )
        if response["status"] != "accepted":
            return {**response, "next_action": "Reread the current proposal"}
        for control in _all(self.store, "mission_task_control"):
            body = control["body"]
            if (
                body.get("run_id") == run["id"]
                and body.get("task_id") == proposal["id"]
                and body.get("scope_required") is False
                and body.get("phase") not in {"done", "deferred"}
            ):
                self._set_control(control, phase="deferred", scope_status="deferred")
        return {
            **response,
            "scope_status": "deferred",
            "next_action": "Other branches remain usable; reopen with an exact revision",
        }

    def _set_control(self, control: dict, **changes: Any) -> dict:
        body = {**control["body"], **changes}

        def save(tx):
            current = tx.get("mission_task_control", control["id"])
            if ref(current) != ref(control):
                raise StateConflict("Optional control changed before state update")
            return {
                "control_ref": ref(
                    tx.put(
                        "mission_task_control",
                        control["id"],
                        body,
                        expected_version=control["version"],
                    )
                )
            }

        return self.store.transact(
            "mission-work-status-"
            + hashlib.sha256(canonical([control["id"], body]).encode()).hexdigest()[:32],
            body,
            save,
            mission_id=body["mission_id"],
        )

    def _recipient(self, run: dict, recipient_alias: str, invitation_id: str) -> tuple[dict, bool]:
        if recipient_alias not in self.role_choices:
            raise WorkError("Invitation needs a supported role alias")
        name, role_id = self.role_choices[recipient_alias]
        requested = recipient_alias in run["body"]["agent_ids"]
        if requested:
            actor_ids = {
                item["body"]["actor_id"]
                for item in _all(self.store, "mission_task_control")
                if item["body"].get("run_id") == run["id"]
                and item["body"].get("alias") == recipient_alias
                and item["body"].get("requested_participant") is True
            }
            actor = (
                self.store.get("agent_instance", next(iter(actor_ids)), 1)
                if len(actor_ids) == 1 else None
            )
            if (
                actor is None
                or actor["body"].get("mission_id") != run["body"]["mission_id"]
                or actor["body"].get("run_id") != run["id"]
                or actor["body"].get("role_id") != role_id
            ):
                raise WorkError("Requested recipient has no registered actor")
            return actor, True
        suffix = hashlib.sha256(invitation_id.encode()).hexdigest()[:8]
        actor = self.registry.register(
            f"mission-invited-actor:{invitation_id}",
            mission_id=run["body"]["mission_id"],
            run_id=run["id"],
            role_id=role_id,
            display_name=f"{name[:60]} (invited {suffix})",
        )
        return actor, False

    def invite(
        self,
        operation_id: str,
        *,
        run_ref: dict,
        actor_id: str,
        source_control_ref: dict,
        proposal_ref: dict,
        recipient_alias: str,
        objective: str,
        expected_artifact: str,
        invitation_id: str,
    ) -> dict:
        replay = _operation_exists(self.store, operation_id)
        run, _actor, control = self._authorize(
            run_ref,
            actor_id,
            source_control_ref,
            allow_historical_control=replay,
            replay=replay,
        )
        proposal = (
            _exact(self.store, proposal_ref, "board_proposal")
            if replay
            else self._current_proposal(run, proposal_ref)
        )
        if proposal["body"].get("mission_id") != run["body"]["mission_id"]:
            raise WorkError("Invitation proposal belongs to another mission")
        if not replay and proposal["body"].get("status") != "open":
            raise WorkError("Only open work can receive a new invitation")
        recipient, requested = self._recipient(run, recipient_alias, invitation_id)
        if recipient["id"] == actor_id:
            raise WorkError("An agent cannot invite itself")
        response = self.board.invite(
            operation_id,
            mission_id=run["body"]["mission_id"],
            invitation_id=invitation_id,
            proposal_id=proposal["id"],
            sender_id=actor_id,
            recipient_id=recipient["id"],
            objective=objective,
            expected_artifact=expected_artifact,
            prerequisite_refs=[source_control_ref],
        )
        if response["status"] != "accepted":
            return {**response, "next_action": "Reread the proposal and recipient before inviting"}
        invitation = self.store.get(**response["invitation_ref"])
        if invitation["body"]["proposal_ref"] != proposal_ref:
            return {
                "status": "conflict",
                "invitation_ref": response["invitation_ref"],
                "next_action": "Proposal changed while invitation was offered; do not activate stale work",
            }
        review_proposal_id = (
            "invitation-review-" + hashlib.sha256(invitation_id.encode()).hexdigest()[:32]
        )
        review_proposal = self.board.propose(
            "mission-invitation-review-propose:" + invitation_id,
            mission_id=run["body"]["mission_id"],
            proposal_id=review_proposal_id,
            actor_id=recipient["id"],
            objective="Review invitation: " + objective,
            expected_artifact="Invitation decision for " + expected_artifact,
            next_action="Consider and respond to the offered invitation",
            prerequisite_refs=[response["invitation_ref"]],
        )
        if review_proposal["status"] != "accepted":
            return {
                **review_proposal,
                "invitation_ref": response["invitation_ref"],
                "next_action": "Retry invitation activation; the offer is durably saved",
            }
        review_ref = self._control(
            run=run,
            actor=recipient,
            alias=recipient_alias,
            proposal_ref=review_proposal["proposal_ref"],
            source_control=control,
            control_type="invitation_review",
            invitation_ref=response["invitation_ref"],
            offered_proposal_ref=proposal_ref,
            invitation_offer={
                "objective": invitation["body"]["objective"],
                "expected_artifact": invitation["body"]["expected_artifact"],
            },
            requested_participant=requested,
        )
        return {
            **response,
            "recipient_ref": ref(recipient),
            "review_proposal_ref": review_proposal["proposal_ref"],
            "review_control_ref": review_ref,
            "requested_participant": requested,
            "scope_required": False,
            "next_action": "Recipient review turn is ready; only recipient can accept or decline",
        }

    def respond_invitation(
        self,
        operation_id: str,
        *,
        run_ref: dict,
        recipient_id: str,
        review_control_ref: dict,
        invitation_ref: dict,
        decision: str,
        counter: str | None = None,
    ) -> dict:
        replay = _operation_exists(self.store, operation_id)
        run, actor, review = self._authorize(
            run_ref,
            recipient_id,
            review_control_ref,
            allow_historical_control=replay,
            replay=replay,
        )
        if (
            review["body"].get("control_type") != "invitation_review"
            or review["body"].get("invitation_ref") != invitation_ref
        ):
            raise WorkError("Actor control does not name this invitation")
        invitation = _exact(self.store, invitation_ref, "board_invitation")
        if (
            invitation["body"].get("mission_id") != run["body"]["mission_id"]
            or invitation["body"]["recipient_ref"]["id"] != recipient_id
            or (
                not replay
                and ref(self.store.get("board_invitation", invitation["id"])) != invitation_ref
            )
        ):
            raise WorkError("Invitation changed or belongs to another recipient")
        response = self.board.respond_invitation(
            operation_id,
            mission_id=run["body"]["mission_id"],
            invitation_id=invitation["id"],
            recipient_id=recipient_id,
            expected_version=invitation["version"],
            decision=decision,
            counter=counter,
        )
        if response["status"] != "accepted":
            return {**response, "next_action": "Reread the invitation and decide again"}
        review_completion = self._complete_review(run, actor, review, response["invitation_ref"])
        if review_completion["status"] != "accepted":
            return {
                **response,
                "review_completion": review_completion,
                "next_action": "Invitation decision saved; retry this operation after review claim releases",
            }
        current_review = self.store.get("mission_task_control", review["id"])
        if current_review["body"].get("phase") != "done":
            self._set_control(current_review, phase="done", scope_status=decision)
        if decision != "accepted":
            return {**response, "next_action": "Invitation review closed; other work can continue"}
        proposal_ref = invitation["body"]["proposal_ref"]
        work_ref = self._control(
            run=run,
            actor=actor,
            alias=review["body"]["alias"],
            proposal_ref=proposal_ref,
            source_control=review,
            control_type="research_work",
            invitation_ref=response["invitation_ref"],
            requested_participant=review["body"]["requested_participant"],
        )
        return {
            **response,
            "review_completion": review_completion,
            "work_control_ref": work_ref,
            "scope_required": False,
            "next_action": "Recipient may claim this proposal using the accepted invitation",
        }

    def _complete_review(self, run: dict, actor: dict, review: dict, decision_ref: dict) -> dict:
        proposal_id = review["body"]["task_id"]
        current = self.store.get("board_proposal", proposal_id)
        if current is None or current["body"].get("mission_id") != run["body"]["mission_id"]:
            raise WorkError("Invitation review proposal is unavailable")
        if current["body"]["status"] == "completed":
            return {"status": "accepted", "proposal_ref": ref(current)}
        db = self.store._connect()
        try:
            row = db.execute(
                "SELECT owner,generation,expires_at FROM leases WHERE mission_id=? AND task_id=?",
                (run["body"]["mission_id"], proposal_id),
            ).fetchone()
            generation = row["generation"] if row else 0
        finally:
            db.close()
        prior_operation = f"mission-invitation-review-claim:{decision_ref['id']}:{generation}"
        reuse_claim = bool(
            row
            and row["owner"] == actor["id"]
            and row["expires_at"] > self.store.clock()
            and _operation_exists(self.store, prior_operation)
        )
        claim_generation = generation if reuse_claim else generation + 1
        claim = self.board.claim(
            f"mission-invitation-review-claim:{decision_ref['id']}:{claim_generation}",
            mission_id=run["body"]["mission_id"],
            proposal_id=proposal_id,
            actor_id=actor["id"],
            ttl=60,
            expected_generation=claim_generation - 1,
        )
        if claim["status"] != "accepted":
            return claim
        return self.board.accept_result(
            f"mission-invitation-review-complete:{decision_ref['id']}:{claim['fence']['generation']}",
            mission_id=run["body"]["mission_id"],
            proposal_id=proposal_id,
            actor_id=actor["id"],
            fence=claim["fence"],
            outcome_id="invitation-review-outcome-"
            + hashlib.sha256(decision_ref["id"].encode()).hexdigest()[:32],
            result_refs=[decision_ref],
        )

    def claim(
        self,
        operation_id: str,
        *,
        run_ref: dict,
        actor_id: str,
        source_control_ref: dict,
        proposal_ref: dict,
        ttl: float = 60,
        expected_generation: int | None = None,
    ) -> dict:
        run, _actor, control = self._authorize(run_ref, actor_id, source_control_ref)
        proposal = self._current_proposal(run, proposal_ref)
        if control["body"].get("task_id") != proposal["id"]:
            raise WorkError("Task control does not name this proposal")
        invitation = control["body"].get("invitation_ref")
        if invitation and control["body"].get("control_type") != "invitation_review":
            current = self.store.get("board_invitation", invitation["id"])
            if current is None or current["body"]["status"] != "accepted":
                raise WorkError("Directed work needs an accepted invitation")
        response = self.board.claim(
            operation_id,
            mission_id=run["body"]["mission_id"],
            proposal_id=proposal["id"],
            actor_id=actor_id,
            ttl=ttl,
            expected_generation=expected_generation,
            invitation_id=invitation["id"]
            if invitation and control["body"].get("control_type") != "invitation_review"
            else None,
        )
        return {
            **response,
            "next_action": "Use the exact claim fence for any result"
            if response["status"] == "accepted"
            else "Reread claim and proposal state",
        }

    def open_optional_work(self, run_id: str) -> list[dict]:
        """Read optional controls without implying they are completion requirements."""
        return [
            {
                "control_ref": ref(record),
                "proposal_ref": body["proposal_ref"],
                "actor_id": body["actor_id"],
                "control_type": body["control_type"],
                "scope_status": body["scope_status"],
                "phase": body["phase"],
            }
            for record in _all(self.store, "mission_task_control")
            if (body := record["body"]).get("run_id") == run_id
            and body.get("scope_required") is False
            and body.get("phase") not in {"done", "deferred"}
        ]

    def record_scope_disposition(
        self,
        operation_id: str,
        *,
        run_ref: dict,
        actor_id: str,
        source_control_ref: dict,
        decisions: list[dict],
        reason: str,
    ) -> dict:
        """One peer proposes how open optional work relates to scoped completion.

        Each decision is {control_ref, disposition: continue|defer}. This is a
        saved, reviewable proposal, not automatic cancellation or consensus.
        The completion owner checks that it still covers current optional work.
        """
        run, actor, _control = self._authorize(run_ref, actor_id, source_control_ref)
        policy_v2 = run["body"].get("participation_policy_version") == 2
        if not isinstance(reason, str) or not reason.strip() or not isinstance(decisions, list):
            raise WorkError("Scoped completion needs a reason and optional-work decisions")
        parsed = []
        for item in decisions:
            if not isinstance(item, dict) or set(item) != {"control_ref", "disposition"}:
                raise WorkError(
                    "Each optional-work decision needs an exact control and disposition"
                )
            if item["disposition"] not in {"continue", "defer"}:
                raise WorkError("Optional work can continue or defer from this scope")
            parsed.append(item)
        body = {
            "schema": "ora.mission_scope_disposition.v1",
            "mission_id": run["body"]["mission_id"],
            "run_ref": run_ref,
            "actor_ref": ref(actor),
            "source_control_ref": source_control_ref,
            "decisions": parsed,
            "reason": reason,
            "created_at": self.store.now(),
        }
        record_id = (
            "mission-scope-disposition-" + hashlib.sha256(operation_id.encode()).hexdigest()[:32]
        )

        def save(tx):
            current_run = tx.get("mission_run", run["id"])
            if ref(current_run) != run_ref:
                raise StateConflict("Run changed before scoped completion proposal")
            rows = tx.db.execute(
                "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) "
                "WHERE r.kind='mission_task_control'"
            ).fetchall()
            open_controls = set()
            for row in rows:
                item = json.loads(row["body"])
                if (
                    item.get("run_id") == run["id"]
                    and item.get("scope_required") is False
                    and item.get("phase") not in {"done", "deferred"}
                ):
                    open_controls.add(("mission_task_control", row["id"], row["version"]))
            selected = set()
            for item in parsed:
                pointer = item["control_ref"]
                if (
                    type(pointer) is not dict
                    or set(pointer) != {"kind", "id", "version"}
                    or pointer.get("kind") != "mission_task_control"
                    or type(pointer.get("version")) is not int
                ):
                    raise WorkError("Exact optional control reference is required")
                record = tx.get(**pointer)
                if record is None:
                    raise WorkError("Optional control version is unavailable")
                if record["body"].get("run_id") != run["id"]:
                    raise WorkError("Optional control belongs to another run")
                selected.add((pointer["kind"], pointer["id"], pointer["version"]))
            if len(parsed) != len(selected) or (
                selected - open_controls if policy_v2 else selected != open_controls
            ):
                raise StateConflict("Open optional work changed; reread before scoped completion")
            saved = tx.put("mission_scope_disposition", record_id, body)
            tx.link(saved, current_run, "about_run")
            for item in parsed:
                tx.link(saved, tx.get(**item["control_ref"]), "covers_optional_work")
            return {"disposition_ref": ref(saved)}

        result = self.store.transact(operation_id, body, save, mission_id=run["body"]["mission_id"])
        return {
            **result,
            "status": "recorded",
            "next_action": "Selected scope can close if this proposal still covers open optional work",
        }

    def disposition_is_current(self, disposition_ref: dict) -> bool:
        """Phase-only progress does not invalidate an agent's scope decision."""
        record = _exact(self.store, disposition_ref, "mission_scope_disposition")
        body = record["body"]
        run_ref = body["run_ref"]
        run = self.store.get("mission_run", run_ref["id"])
        if run is None or ref(run) != run_ref:
            return False
        policy_v2 = run["body"].get("participation_policy_version") == 2
        selected = {item["control_ref"]["id"]: item for item in body["decisions"]}
        if len(selected) != len(body["decisions"]):
            return False
        current = {
            item["id"]: item
            for item in _all(self.store, "mission_task_control")
            if item["body"].get("run_id") == run["id"]
            and item["body"].get("scope_required") is False
        }
        open_ids = {
            control_id
            for control_id, item in current.items()
            if item["body"].get("phase") not in {"done", "deferred"}
        }
        if not policy_v2 and open_ids - selected.keys():
            return False
        for control_id, decision in selected.items():
            saved = self.store.get(**decision["control_ref"])
            head = current.get(control_id)
            if saved is None or head is None:
                return False
            identity = (
                "mission_id",
                "run_id",
                "actor_id",
                "task_id",
                "control_type",
                "invitation_ref",
                "proposal_ref",
            )
            if any(saved["body"].get(key) != head["body"].get(key) for key in identity):
                return False
            if control_id in open_ids:
                proposal = self.store.get("board_proposal", head["body"]["task_id"])
                if proposal is None or ref(proposal) != head["body"]["proposal_ref"]:
                    return False
                if decision["disposition"] == "defer" and head["body"]["phase"] in {
                    "pending_model",
                    "pending_tool",
                }:
                    return False
        return True
