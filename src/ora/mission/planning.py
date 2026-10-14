"""Optional durable plans over task-scoped working notes.

Plans organize a peer's intended work without creating tasks or satisfying peer
obligations. The host binds ownership, records exact links and fences lifecycle
changes atomically. Linked records describe a basis; their shape proves neither
reading nor support. Producing-context source lineage remains authoritative.
"""

from __future__ import annotations

import hashlib
import json

from ora.contracts.models import ReadPlanInput, RuntimePlan, SavePlanInput
from ora.coordination.board import ref, require_actor, require_ref
from ora.state import StateConflict, StateError, StateStore, Transaction
from ora.state.store import canonical

from .memory import PLAN_NOTE_PREFIX, WorkingNotes

MAX_PLAN_BYTES = 64_000
MAX_WRITES_WITHOUT_PROGRESS = 3
SOURCE_KINDS = frozenset({"source_copy", "source_passage", "source_read_window"})
TERMINAL_STATUSES = frozenset({"completed", "cancelled", "closed", "failed"})


class PlanningError(StateError):
    """This local plan action needs repair; other research remains available."""


def planning_note_id(mission_id: str, actor_id: str, task_id: str) -> str:
    """One stable reserved note per actor/task, independent of caller plan IDs."""
    return PLAN_NOTE_PREFIX + hashlib.sha256(
        canonical([mission_id, actor_id, task_id]).encode()
    ).hexdigest()[:32]


def _unique_refs(pointers: list[dict]) -> list[dict]:
    return list({canonical(pointer): pointer for pointer in pointers}.values())


def _plan_links(plan: dict) -> list[dict]:
    return _unique_refs([
        *plan["linked_refs"],
        *(pointer for step in plan["steps"] for pointer in step["linked_refs"]),
    ])


class RuntimePlans:
    """Read and atomically revise one actor's advisory continuation plan."""

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.notes = WorkingNotes(store)

    @staticmethod
    def _scope(
        tx: Transaction, *, mission_id: str, actor_id: str, task_id: str,
        run_ref: dict, control_ref: dict, mutation: bool = False,
    ) -> tuple[dict, dict, dict]:
        actor = require_actor(tx, mission_id, actor_id)
        run = require_ref(tx, mission_id, run_ref)
        control = require_ref(tx, mission_id, control_ref)
        if run["kind"] != "mission_run" or control["kind"] != "mission_task_control":
            raise PlanningError("Planning requires exact run and task-control references")
        if actor["body"].get("run_id") != run["id"]:
            raise PlanningError("Planning actor belongs to another run")
        # A legitimate tool phase may advance the control while its operation is
        # executing. Recheck current identity and closure, not the old phase's
        # version, under this same write transaction.
        current_run = tx.get("mission_run", run["id"])
        current_control = tx.get("mission_task_control", control["id"])
        task = tx.get("board_proposal", task_id)
        for selected in (control, current_control):
            if selected is None or any(selected["body"].get(key) != value for key, value in (
                ("mission_id", mission_id), ("actor_id", actor_id),
                ("task_id", task_id), ("run_id", run["id"]),
            )):
                raise PlanningError("Planning control is outside this actor's run and task")
        if mutation and any(
            control["body"].get(key) != current_control["body"].get(key)
            for key in ("work_id", "call_ref", "response_ref", "tool_call")
        ):
            raise PlanningError("Planning control moved to another call or work item")
        if (task is None or task["body"].get("mission_id") != mission_id
                or current_run is None or current_run["body"].get("mission_id") != mission_id):
            raise PlanningError("Planning task or run is unavailable in this mission")
        return current_run, current_control, task

    @staticmethod
    def _effective_state(run: dict, control: dict, task: dict) -> dict:
        state = {
            "run_status": run["body"].get("status"),
            "task_status": task["body"].get("status"),
            "control_phase": control["body"].get("phase"),
            "scope_status": control["body"].get("scope_status"),
        }
        # Preserve the existing MissionWork exception for an already authorized
        # optional continuation after requested scope completion. This does not
        # reopen the run, completed task, or cancelled control.
        continued_optional = (
            state["run_status"] == "completed"
            and control["body"].get("scope_required") is False
            and state["scope_status"] == "continued"
        )
        state["continued_optional_scope"] = continued_optional
        state["can_save"] = (
            (state["run_status"] not in TERMINAL_STATUSES or continued_optional)
            and state["task_status"] not in TERMINAL_STATUSES
            and state["control_phase"] not in {"done", "cancelled", "closed", "failed"}
            and state["scope_status"] not in {"cancelled", "closed", "completed", "withdrawn"}
        )
        return state

    @staticmethod
    def _progress_tokens(tx: Transaction, actor_id: str, control: dict, task: dict) -> set[str]:
        """Content tokens for research progress, never response/delivery churn.

        Alias entries preserve every observed token across note revisions without
        returning a growing history in the model-facing plan. Random window IDs
        and executor wrappers are replaced by their meaningful exact outputs.
        """
        tokens = set()
        seen_outputs = set()

        def observe(kind: str, value) -> None:
            tokens.add(hashlib.sha256(canonical([kind, value]).encode()).hexdigest())

        def research_output(pointer: dict) -> None:
            key = canonical(pointer)
            if key in seen_outputs:
                return
            seen_outputs.add(key)
            record = tx.get(**pointer)
            if record is None:
                return
            kind, data = record["kind"], record["body"]
            if kind == "executor_artifact":
                for output in data.get("output_refs", []):
                    research_output(output)
            elif kind == "source_read_window":
                for passage in data.get("passage_refs", []):
                    observe("read_passage", passage)
            elif kind in {
                "source_copy", "source_passage", "report", "evidence_packet",
                "board_dissent", "mail_answer",
            }:
                observe("research_output", ref(record))
            # Model responses, reads, notes, catalog refreshes and diagnostic
            # receipts have no independent scientific-progress authority.

        body = control["body"]
        observe("task_objective", {
            "objective": task["body"].get("objective"),
            "expected_artifact": task["body"].get("expected_artifact"),
        })
        for pointer in body.get("source_refs", []):
            observe("task_source", pointer)
        for pointer in body.get("read_window_refs", []):
            research_output(pointer)
        rows = tx.db.execute(
            "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) "
            "WHERE r.kind='mail_request' AND json_extract(r.body,'$.mission_id')=? "
            "AND (json_extract(r.body,'$.requester_id')=? OR json_extract(r.body,'$.recipient_id')=?) "
            "ORDER BY r.id", (body["mission_id"], actor_id, actor_id),
        )
        for row in rows:
            peer = json.loads(row["body"])
            message_ref = peer.get("message_ref")
            message = tx.get(**message_ref) if isinstance(message_ref, dict) else None
            task_pointer = message["body"].get("task_ref") if message else None
            cause = peer.get("cause_ref") or {}
            if not (cause.get("kind") == "board_proposal" and cause.get("id") == task["id"]
                    or isinstance(task_pointer, dict) and task_pointer.get("id") == task["id"]):
                continue
            observe("peer_question", {"id": row["id"], **{key: peer.get(key) for key in (
                "question", "expected_artifact", "cause_ref", "requester_id", "recipient_id",
            )}})
            for pointer in peer.get("answer_refs", []):
                observe("peer_answer", pointer)
            if peer.get("review") is not None:
                observe("peer_review", {"id": row["id"], **{key: peer.get(key) for key in (
                    "accepted_answer_ref", "reviewer_id", "review",
                )}})
        rows = tx.db.execute(
            "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) "
            "WHERE r.kind IN ('board_outcome','board_dissent') "
            "AND json_extract(r.body,'$.mission_id')=? "
            "AND json_extract(r.body,'$.proposal_ref.id')=? ORDER BY r.kind,r.id",
            (body["mission_id"], task["id"]),
        )
        for row in rows:
            outcome = json.loads(row["body"])
            if row["kind"] == "board_dissent":
                observe("peer_critique", {"id": row["id"], "version": row["version"]})
            else:
                for pointer in outcome.get("result_refs", []):
                    research_output(pointer)
        return tokens

    @staticmethod
    def _progress_alias(note_id: str, token: str) -> str:
        return hashlib.sha256(canonical([note_id, token]).encode()).hexdigest()

    def save(
        self, operation_id: str, *, mission_id: str, actor_id: str,
        task_ref: dict, run_ref: dict, control_ref: dict,
        plan: dict | RuntimePlan, expected_version: int = 0,
        input_refs: list[dict] | None = None,
    ) -> dict:
        validated = SavePlanInput.model_validate({
            "plan": plan, "expected_version": expected_version,
        })
        document = validated.plan.model_dump(mode="json")
        text = canonical(document)
        if len(text.encode()) > MAX_PLAN_BYTES:
            raise PlanningError("Plan exceeds the 64000-byte working-memory payload bound")
        for pointer, kind in ((run_ref, "mission_run"), (control_ref, "mission_task_control")):
            if (type(pointer) is not dict or set(pointer) != {"kind", "id", "version"}
                    or pointer.get("kind") != kind or type(pointer.get("id")) is not str
                    or type(pointer.get("version")) is not int or pointer["version"] < 1):
                raise PlanningError(f"Exact {kind} reference is required")
        bound_run = self.store.get(**run_ref)
        if bound_run is None or bound_run["body"].get("mission_id") != mission_id:
            raise PlanningError("Exact planning run is unavailable in this mission")
        bound_control = self.store.get(**control_ref)
        if bound_control is None:
            raise PlanningError("Exact planning task control is unavailable")
        # Run/control versions are provenance, not volatile action identity. A
        # crash after commit may replay after a phase/status advance; preserve
        # scientific payload, task version and exact execution pins in the hash.
        request = {
            "mission_id": mission_id, "actor_id": actor_id, "task_ref": task_ref,
            "run_identity": {k: run_ref[k] for k in ("kind", "id")},
            "control_identity": {k: control_ref[k] for k in ("kind", "id")},
            "control_binding": {k: bound_control["body"].get(k) for k in (
                "mission_id", "run_id", "actor_id", "task_id",
                "work_id", "call_ref", "response_ref", "tool_call",
            )}, "plan": document,
            "expected_version": expected_version, "input_refs": input_refs or [],
        }

        def accept(tx: Transaction) -> dict:
            selected_task = require_ref(tx, mission_id, task_ref)
            if selected_task["kind"] != "board_proposal":
                raise PlanningError("Plan requires an exact board task")
            run, control, task = self._scope(
                tx, mission_id=mission_id, actor_id=actor_id, task_id=selected_task["id"],
                run_ref=run_ref, control_ref=control_ref, mutation=True,
            )
            if ref(task) != task_ref:
                raise StateConflict("Planning task version changed; reread before revising")
            if not self._effective_state(run, control, task)["can_save"]:
                raise PlanningError("Task or run is closed; no new planning mutation is allowed")
            note_id = planning_note_id(mission_id, actor_id, task["id"])
            previous = tx.get("working_note", note_id)
            if (previous["version"] if previous else 0) != expected_version:
                raise StateConflict("Saved plan version changed; read_plan before revising")
            if previous and previous["body"].get("planning", {}).get("run_id") != run["id"]:
                raise PlanningError("Saved plan belongs to another run")
            links = _plan_links(document)
            # Structural links are checked without exposing their text. Source
            # links also use the existing checked task-source note path below.
            for pointer in links:
                if pointer["kind"] not in SOURCE_KINDS:
                    require_ref(tx, mission_id, pointer)
            sources = _unique_refs([
                *(input_refs or []), *(p for p in links if p["kind"] in SOURCE_KINDS),
            ])
            # Revision prose alone is not a substantive new plan. Its original
            # receipt remains exact, including its original source dependencies.
            substantive = {k: v for k, v in document.items() if k != "revision_reason"}
            content_hash = hashlib.sha256(canonical(substantive).encode()).hexdigest()
            prior_metadata = previous["body"].get("planning", {}) if previous else {}
            if prior_metadata.get("content_hash") == content_hash:
                return {
                    "status": "unchanged", "changed": False,
                    "plan_ref": ref(previous), "note_ref": ref(previous),
                    "writes_without_progress": prior_metadata["writes_without_progress"],
                    "planning_status": "advisory_not_task_completion",
                }
            tokens = self._progress_tokens(tx, actor_id, control, task)
            signature = hashlib.sha256(canonical(sorted(tokens)).encode()).hexdigest()
            new_progress = any(
                tx.resolve("runtime_plan.progress", self._progress_alias(note_id, token)) is None
                for token in tokens
            )
            writes = 0 if new_progress else prior_metadata.get("writes_without_progress", 0)
            if writes >= MAX_WRITES_WITHOUT_PROGRESS:
                raise PlanningError(
                    "Planning write allowance exhausted after three substantive writes without "
                    "new host-observed research progress; continue research or read the saved plan"
                )
            metadata = {
                "schema": "ora.runtime_plan.v1", "run_id": run["id"],
                "run_ref": run_ref, "control_ref": control_ref,
                "content_hash": content_hash, "progress_signature": signature,
                "writes_without_progress": writes + 1,
            }
            pointer = self.notes.write_in_transaction(
                tx, mission_id=mission_id, actor_id=actor_id, task_ref=task_ref,
                note_id=note_id, category="checkpoint", text=text,
                input_refs=sources, expected_version=expected_version, planning_metadata=metadata,
            )
            saved = tx.get(**pointer)
            for token in tokens:
                tx.bind("runtime_plan.progress", self._progress_alias(note_id, token),
                        "working_note", note_id)
            for link in links:
                tx.link(saved, tx.get(**link), "plan_basis")
            tx.emit(mission_id, "runtime_plan_saved", {
                "plan_ref": pointer, "prior_ref": ref(previous) if previous else None,
                "actor_id": actor_id, "task_id": task["id"],
                "plan_status": document["status"], "writes_without_progress": writes + 1,
                "subjects": [{"kind": "task", "id": task["id"]}],
            })
            return {
                "status": "saved", "changed": True, "plan_ref": pointer, "note_ref": pointer,
                "writes_without_progress": writes + 1,
                "planning_status": "advisory_not_task_completion",
            }

        # StateStore checks accepted operation replay before executing closure
        # checks. An exact accepted retry remains valid after completion.
        return self.store.transact(operation_id, request, accept, mission_id=mission_id)

    def read(
        self, *, mission_id: str, actor_id: str, task_id: str,
        run_ref: dict, control_ref: dict, version: int | None = None,
    ) -> dict:
        ReadPlanInput.model_validate({"version": version})
        db = self.store._connect()
        try:
            run, control, task = self._scope(
                Transaction(self.store, db), mission_id=mission_id, actor_id=actor_id,
                task_id=task_id, run_ref=run_ref, control_ref=control_ref,
            )
        finally:
            db.close()
        result = {
            "status": "absent", "note": None, "plan_ref": None, "note_ref": None,
            "effective_state": self._effective_state(run, control, task),
            "stale_linked_refs": [], "planning_status": "advisory_not_task_completion",
        }
        note_id = planning_note_id(mission_id, actor_id, task_id)
        stored = self.store.get("working_note", note_id, version)
        if stored is None:
            if version is not None:
                raise PlanningError("Exact plan version is unavailable")
            return result
        if stored["body"].get("planning", {}).get("run_id") != run["id"]:
            raise PlanningError("Saved plan belongs to another run")
        projected = self.notes.read(
            mission_id=mission_id, actor_id=actor_id, task_id=task_id,
            note_id=note_id, version=stored["version"],
        )
        result.update(
            status="available" if "text" in projected["body"]
            else "data_unavailable_due_source_use",
            note=projected, plan_ref=ref(projected), note_ref=ref(projected),
        )
        # No parsed or stored plan prose escapes the source-use projection.
        if "text" in projected["body"]:
            document = json.loads(projected["body"]["text"])
            for pointer in _plan_links(document):
                current = self.store.get(pointer["kind"], pointer["id"])
                if current is None or ref(current) != pointer:
                    result["stale_linked_refs"].append({
                        "reference": pointer,
                        "current_reference": ref(current) if current else None,
                    })
        return result
