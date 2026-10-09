"""Run peer-selected local work concurrently without owning scientific choices.

The registered callback is a trusted host boundary. Intent and dispatch are
durable before it runs; accepted local effects share the board result commit.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import threading
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping

from ora.agents.json_shapes import finite_json
from ora.state import StateConflict, StateError, StateStore
from ora.state.store import Transaction, canonical

from .board import ProposalBoard, ref, require_ref
from .capacity import KINDS, CapacityScheduler
from .mailbox import PeerMailbox
from .traces import TraceContext, TraceJournal

_WORK_LISTENERS: dict[tuple[str, str], set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}
_WORK_LISTENERS_LOCK = threading.Lock()


def _id(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class WorkSpec:
    """Exact, caller-selected work and handler identity; no callback is serialized."""

    work_id: str
    mission_id: str
    run_id: str
    proposal_ref: dict
    actor_id: str
    configuration_ref: dict
    input_refs: tuple[dict, ...]
    handler_id: str
    handler_version: str
    kind: str
    lane: str
    quota_scopes: tuple[str, ...] = ()
    estimated_tokens: int = 0
    attempt_id: str = ""
    call_id: str = ""
    call_version: int | None = None
    invitation_id: str | None = None
    review: bool = False

    def intent(self) -> dict:
        return json.loads(canonical(asdict(self)))


@dataclass(frozen=True)
class WorkResult:
    """A callback's observed operation result, not a scientific truth claim."""

    disposition: str = "completed"
    payload: dict | None = None
    output_refs: tuple[dict, ...] = ()
    messages: tuple[dict, ...] = ()
    used_tokens: int | None = None
    cached_tokens: int = 0
    retry_after: float = 0
    rate_scope: str | None = None


class ConcurrentExecutor:
    """Drive ready submissions while independent callbacks overlap in asyncio."""

    KIND = "executor_work"

    def __init__(self, store: StateStore, board: ProposalBoard,
                 mailbox: PeerMailbox, capacity: CapacityScheduler,
                 traces: TraceJournal, handlers: Mapping[tuple[str, str], Callable],
                 *, owner: str, lease_seconds: float = 60):
        if not owner or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise StateError("Executor needs a host owner and positive lease")
        self.store, self.board, self.mailbox = store, board, mailbox
        self.capacity, self.traces = capacity, traces
        self.handlers = dict(handlers)
        self.owner, self.lease_seconds = owner, lease_seconds

    @staticmethod
    def _capacity_id(spec: WorkSpec) -> str:
        return _id("executor-cap:", [spec.mission_id, spec.work_id])

    @staticmethod
    def _work_record_id(spec: WorkSpec) -> str:
        return _id("executor-work:", [spec.mission_id, spec.work_id])

    @staticmethod
    def _spec_from(record: dict) -> WorkSpec:
        body = record["body"]["spec"]
        return WorkSpec(**{**body, "input_refs": tuple(body["input_refs"]),
                           "quota_scopes": tuple(body["quota_scopes"])})

    def _validate_tx(self, tx: Transaction, spec: WorkSpec) -> None:
        if spec.kind not in KINDS or not all(isinstance(value, str) and value for value in (
            spec.work_id, spec.mission_id, spec.run_id, spec.actor_id,
            spec.handler_id, spec.handler_version, spec.lane, spec.attempt_id,
        )):
            raise StateError("Work needs exact mission, actor, handler and attempt identities")
        if spec.kind != "model" and spec.estimated_tokens:
            raise StateError("Only model work reserves tokens")
        actor = tx.get("agent_instance", spec.actor_id, version=1)
        if actor is None or actor["body"].get("mission_id") != spec.mission_id or actor["body"].get("run_id") != spec.run_id:
            raise StateError("Work actor is unavailable in this run")
        config = require_ref(tx, spec.mission_id, spec.configuration_ref)
        if config["kind"] != "agent_configuration" or config["body"].get("instance_id") != spec.actor_id:
            raise StateError("Work configuration belongs to another actor")
        for pointer in spec.input_refs:
            require_ref(tx, spec.mission_id, pointer)
        if spec.call_id:
            if type(spec.call_version) is not int or spec.call_version < 1:
                raise StateError("Pinned call needs an exact positive version")
            call = tx.get("agent_call", spec.call_id, spec.call_version)
            if call is None or any(call["body"].get(key) != value for key, value in (
                ("mission_id", spec.mission_id), ("instance_id", spec.actor_id),
                ("task_id", spec.proposal_ref["id"]), ("attempt_id", spec.attempt_id),
                ("configuration_id", spec.configuration_ref["id"]),
            )):
                raise StateError("Pinned call does not match work actor, task and configuration")
            pinned_sources = [{key: item[key] for key in ("kind", "id", "version")}
                              for item in call["body"].get("input_refs", [])
                              if item.get("kind") == "source_copy"]
            work_sources = [pointer for pointer in spec.input_refs
                            if pointer.get("kind") == "source_copy"]
            if sorted(canonical(pointer) for pointer in pinned_sources) != sorted(
                    canonical(pointer) for pointer in work_sources):
                raise StateConflict("Work source copies differ from the pinned call manifest")
        elif spec.call_version is not None:
            raise StateError("Call version requires a call ID")
        proposal = require_ref(tx, spec.mission_id, spec.proposal_ref)
        if proposal["kind"] != ProposalBoard.KIND or proposal["body"].get("status") != "open":
            raise StateConflict("Work proposal is not open at its pinned version")
        if ref(tx.get(proposal["kind"], proposal["id"])) != spec.proposal_ref:
            raise StateConflict("Work proposal was revised")

    def submit(self, spec: WorkSpec) -> dict:
        """Persist exact intent; replay after a later clock reading is harmless."""
        if not isinstance(spec, WorkSpec) or (spec.handler_id, spec.handler_version) not in self.handlers:
            raise StateError("Work needs a registered, version-pinned host callback")
        intent = spec.intent()
        finite_json(intent)

        def save(tx):
            self._validate_tx(tx, spec)
            record_id = self._work_record_id(spec)
            if spec.call_id:
                rows = tx.db.execute(
                    "SELECT r.id,r.body FROM records r JOIN heads h USING(kind,id,version) "
                    "WHERE r.kind=?", (self.KIND,),
                ).fetchall()
                for row in rows:
                    other = json.loads(row["body"])["spec"]
                    if (row["id"] != record_id and other["mission_id"] == spec.mission_id
                            and other["call_id"] == spec.call_id
                            and other["call_version"] == spec.call_version):
                        raise StateConflict("Exact call manifest is already assigned to other work")
            existing = tx.get(self.KIND, record_id)
            if existing:
                if existing["body"]["spec"] != intent:
                    raise StateConflict("Work ID already names different inputs")
                return ref(existing)
            saved = tx.put(self.KIND, record_id, {
                "mission_id": spec.mission_id, "spec": intent, "phase": "pending",
                "created_at": self.store.now(), "result_ref": None,
            })
            tx.emit(spec.mission_id, "executor_work_submitted", {"work_ref": ref(saved)})
            return ref(saved)

        self.store.transact(_id("executor-submit:", [spec.mission_id, spec.work_id]),
                            intent, save, mission_id=spec.mission_id)
        self.notify(spec.mission_id)
        return self.store.get(self.KIND, self._work_record_id(spec))

    def notify(self, mission_id: str) -> None:
        """Wake in-process runners after durable work or board state changes."""
        key = (str(self.store.db_path), mission_id)
        with _WORK_LISTENERS_LOCK:
            listeners = tuple(_WORK_LISTENERS.get(key, ()))
        for loop, event in listeners:
            try:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass  # The loop closed after durable submission; a later scan can recover it.

    def _records(self, kind: str) -> list[dict]:
        after, result = "", []
        while True:
            page = self.store.records(kind, limit=500, after_id=after)
            result.extend(page)
            if len(page) < 500:
                return result
            after = page[-1]["id"]

    def _work(self, mission_id: str) -> list[dict]:
        return [record for record in self._records(self.KIND)
                if record["body"]["mission_id"] == mission_id]

    def _prepare(self, mission_id: str) -> dict[str, WorkSpec]:
        ready_refs = {item["proposal_ref"]["id"]: item["proposal_ref"]
                      for item in self.board.ready_work(mission_id)}
        work = self._work(mission_id)
        uncertain_proposals = set()
        for record in work:
            spec = self._spec_from(record)
            cap = self.capacity.get(self._capacity_id(spec))
            if cap and cap["phase"] == "uncertain":
                uncertain_proposals.add(spec.proposal_ref["id"])
        eligible = {}
        for record in work:
            if record["body"]["phase"] != "pending":
                continue
            spec = self._spec_from(record)
            if (spec.handler_id, spec.handler_version) not in self.handlers:
                continue  # Pinned implementation must be restored, not silently replaced.
            if ready_refs.get(spec.proposal_ref["id"]) != spec.proposal_ref:
                continue
            if spec.proposal_ref["id"] in uncertain_proposals:
                continue
            db = self.store._connect()
            try:
                self._validate_tx(Transaction(self.store, db), spec)
            except (StateError, StateConflict):
                continue
            finally:
                db.close()
            cap_id = self._capacity_id(spec)
            capacity = self.capacity.get(cap_id)
            if capacity is None:
                self.capacity.enqueue(cap_id, task_id=spec.proposal_ref["id"], lane=spec.lane,
                                      kind=spec.kind, quota_scopes=spec.quota_scopes,
                                      estimated_tokens=spec.estimated_tokens, review=spec.review,
                                      mission_id=spec.mission_id, run_id=spec.run_id,
                                      attempt_id=spec.attempt_id, call_id=spec.call_id,
                                      budget_version=spec.configuration_ref["id"])
            elif capacity["phase"] not in {"queued", "waiting"}:
                continue
            eligible[cap_id] = spec
        return eligible

    def _renew_board(self, spec: WorkSpec, fence: dict) -> None:
        def save(tx):
            tx.db.execute("UPDATE leases SET expires_at=? WHERE mission_id=? AND task_id=? "
                          "AND owner=? AND generation=?",
                          (self.store.clock() + self.lease_seconds, spec.mission_id,
                           spec.proposal_ref["id"], spec.actor_id, fence["generation"]))
            return {"renewed": True}
        self.store.transact(_id("executor-board-heartbeat:", [spec.work_id, uuid.uuid4().hex]),
                            {"work_id": spec.work_id}, save, mission_id=spec.mission_id, fence=fence)

    def _release_board(self, spec: WorkSpec, fence: dict) -> None:
        def save(tx):
            changed = tx.db.execute(
                "UPDATE leases SET owner='__revoked__',generation=generation+1,expires_at=0 "
                "WHERE mission_id=? AND task_id=? AND owner=? AND generation=? AND expires_at>?",
                (spec.mission_id, spec.proposal_ref["id"], spec.actor_id,
                 fence["generation"], self.store.clock()),
            )
            if changed.rowcount != 1:
                raise StateConflict("Board claim changed before release")
            return {"released": True}
        self.store.transact(_id("executor-board-release:", [spec.work_id, fence["generation"]]),
                            {"work_id": spec.work_id, "generation": fence["generation"]},
                            save, mission_id=spec.mission_id)

    async def _keepalive(self, spec: WorkSpec, cap: dict, fence: dict) -> None:
        while True:
            await asyncio.sleep(max(0.05, self.lease_seconds / 3))
            self._renew_board(spec, fence)
            self.capacity.heartbeat(self._capacity_id(spec), self.owner,
                                    cap["generation"], lease_seconds=self.lease_seconds)

    async def _invoke(self, spec: WorkSpec) -> WorkResult:
        handler = self.handlers[(spec.handler_id, spec.handler_version)]
        # Nested reference dictionaries are copied so callbacks cannot alter
        # the executor's pinned acceptance inputs through a frozen dataclass.
        callback_spec = self._spec_from({"body": {"spec": spec.intent()}})
        if inspect.iscoroutinefunction(handler):
            result = await handler(callback_spec)
        else:
            result = await asyncio.to_thread(handler, callback_spec)
            if inspect.isawaitable(result):
                result = await result
        if not isinstance(result, WorkResult):
            raise StateError("Registered callback must return WorkResult")
        finite_json(json.loads(canonical(asdict(result))))
        return result

    @staticmethod
    def _model_receipt_tx(tx: Transaction, spec: WorkSpec,
                          result: WorkResult, *, check_work_lineage: bool = True) -> dict | None:
        if spec.kind != "model" or not spec.call_id:
            return None
        model_receipts = [pointer for pointer in result.output_refs
                          if isinstance(pointer, dict)
                          and pointer.get("kind") == "model_response_receipt"]
        if len(model_receipts) != 1:
            raise StateError("Pinned model call needs one exact response receipt")
        observed = require_ref(tx, spec.mission_id, model_receipts[0])
        call_ref = {"kind": "agent_call", "id": spec.call_id,
                    "version": spec.call_version}
        if (observed["body"].get("schema") != "ora.model_response_receipt.v1"
            or observed["body"].get("call_ref") != call_ref
            or observed["body"].get("configuration_ref") != spec.configuration_ref):
            raise StateConflict("Model response receipt differs from pinned call")
        intent = require_ref(tx, spec.mission_id, observed["body"].get("intent_ref"))
        if (intent["kind"] != "model_request_intent"
            or intent["body"].get("call_ref") != call_ref
            or intent["body"].get("configuration_ref") != spec.configuration_ref
            or intent["body"].get("request_sha256") != observed["body"].get("request_sha256")):
            raise StateConflict("Model response does not match its saved request intent")
        saved_sources = intent["body"].get("source_refs", [])
        call = tx.get("agent_call", spec.call_id, spec.call_version)
        if call is None:
            raise StateConflict("Pinned model call is unavailable")
        pinned_sources = [{key: item[key] for key in ("kind", "id", "version")}
                          for item in call["body"].get("input_refs", [])
                          if item.get("kind") == "source_copy"]
        work_sources = [pointer for pointer in spec.input_refs
                        if pointer.get("kind") == "source_copy"]
        if (type(saved_sources) is not list
            or sorted(canonical(pointer) for pointer in saved_sources)
               != sorted(canonical(pointer) for pointer in pinned_sources)):
            raise StateConflict("Saved model request differs from its pinned source copies")
        if (check_work_lineage
            and sorted(canonical(pointer) for pointer in saved_sources)
               != sorted(canonical(pointer) for pointer in work_sources)):
            raise StateConflict("Work source lineage differs from the saved model request")
        usage = observed["body"].get("usage")
        if type(usage) is not dict:
            raise StateConflict("Model response receipt lacks usage accounting")
        total = usage.get("total_tokens")
        if ((total is not None and (type(total) is not int or total < 0))
            or result.used_tokens != total):
            raise StateConflict("Callback usage differs from the saved provider receipt")
        return observed

    @staticmethod
    def _saved_model_receipt_tx(tx: Transaction, spec: WorkSpec) -> dict | None:
        if spec.kind != "model" or not spec.call_id:
            return None
        receipt = tx.get("model_response_receipt",
                         f"model-response:model-intent:{spec.call_id}:{spec.call_version}")
        if receipt is None:
            return None
        pointer = ref(receipt)
        usage = receipt["body"].get("usage")
        probe = WorkResult(output_refs=(pointer,),
                           used_tokens=usage.get("total_tokens") if type(usage) is dict else None)
        ConcurrentExecutor._model_receipt_tx(tx, spec, probe, check_work_lineage=False)
        return receipt

    @staticmethod
    def _model_receipt_effect(receipt: dict) -> str:
        """Classify an exact receipt without assuming every capture ended a call."""
        body = receipt["body"]
        if (body.get("status") == "failed" and body.get("error_kind") == "http_error"
            and body.get("http_status") == 429):
            return "known_rejection"
        if (body.get("status") == "failed"
            and body.get("error_kind") == "credential_unavailable"):
            return "no_request"
        digest = body.get("response_sha256")
        total = body.get("usage", {}).get("total_tokens")
        if (not isinstance(digest, str) or len(digest) != 64
            or not isinstance(body.get("returned_model"), str)
            or type(total) is not int or total < 0):
            return "unverified"
        status, finish, error = (body.get("status"), body.get("finish_reason"),
                                 body.get("error_kind"))
        if status == "complete":
            return "known" if finish == "stop" and error is None else "unverified"
        if status == "tool_requested":
            return "known" if finish == "tool_calls" and error is None else "unverified"
        if status == "partial":
            return ("known" if ((finish == "length" and error is None)
                                or (finish == "stop" and error == "invalid_final_schema"))
                    else "unverified")
        if (status == "failed" and finish in {"stop", "tool_calls", "length"}
            and error in {"invalid_tool_calls", "unsupported_parallel_tool_calls",
                          "unexpected_tool_call"}):
            return "known"
        return "unverified"

    def _mark_external_unknown(self, spec: WorkSpec, generation: int) -> None:
        try:
            self.capacity.timeout(self._capacity_id(spec), self.owner, generation)
        except (StateError, StateConflict):
            self.capacity.expire()

    def _record_unaccepted(self, spec: WorkSpec, generation: int,
                           result: WorkResult | None, reason_code: str) -> dict | None:
        """Save a known callback/receipt without claiming board acceptance.

        Only the current capacity owner may release its reservation. An old
        callback can still preserve its observation for later reconciliation.
        """
        result_data = json.loads(canonical(asdict(result))) if result else None
        observation_id = _id("executor-unaccepted:",
                             [spec.mission_id, spec.work_id, generation])
        request = {"spec": spec.intent(), "generation": generation,
                   "result": result_data, "reason_code": reason_code}

        def save(tx: Transaction) -> dict:
            work = tx.get(self.KIND, self._work_record_id(spec))
            if work is None:
                raise StateConflict("Work intent vanished before result observation")
            if work["body"]["phase"] == "completed":
                return {"status": "completed"}
            prior = tx.get("executor_unaccepted_result", observation_id)
            if prior is not None:
                return {"status": "result_unaccepted", "observation_ref": ref(prior),
                        "capacity_reconciled": prior["body"]["capacity_reconciled"]}
            receipt = None
            if spec.kind == "model" and spec.call_id:
                try:
                    if result is not None:
                        receipt = self._model_receipt_tx(tx, spec, result)
                except (StateConflict, StateError, KeyError, TypeError):
                    receipt = None
                if receipt is None:
                    try:
                        receipt = self._saved_model_receipt_tx(tx, spec)
                    except (StateConflict, StateError, KeyError, TypeError):
                        receipt = None
            if spec.kind == "model" and spec.call_id:
                external_effect = (self._model_receipt_effect(receipt) if receipt
                                   else "unverified")
                known = external_effect != "unverified"
            else:
                known = result is not None
                external_effect = "known" if known else "unverified"
            if not known and result is None:
                return {"status": "uncertain"}
            usage = receipt["body"].get("usage", {}) if receipt else {}
            used_tokens = (0 if external_effect == "no_request" else
                           usage.get("total_tokens") if receipt
                           else result.used_tokens if result is not None else None)
            if used_tokens is not None and (type(used_tokens) is not int or used_tokens < 0):
                known = False
                used_tokens = None
            cached_tokens = (result.cached_tokens if result is not None and known
                             and (receipt is None or result.used_tokens == used_tokens) else 0)
            if used_tokens is None:
                cached_tokens = 0
            if (type(cached_tokens) is not int or cached_tokens < 0
                or (used_tokens is not None and cached_tokens > used_tokens)):
                cached_tokens = 0
            cap_record = tx.get("capacity_operation", self._capacity_id(spec))
            cap_body = cap_record["body"] if cap_record else {}
            current_owner = (cap_body.get("phase") == "dispatched"
                             and cap_body.get("owner") == self.owner
                             and cap_body.get("generation") == generation
                             and cap_body.get("lease_until", 0) > self.store.clock())
            reconciled = bool(known and current_owner)
            observation = tx.put("executor_unaccepted_result", observation_id, {
                "mission_id": spec.mission_id, "work_id": spec.work_id,
                "proposal_ref": spec.proposal_ref,
                "configuration_ref": spec.configuration_ref,
                "input_refs": list(spec.input_refs),
                "handler_id": spec.handler_id, "handler_version": spec.handler_version,
                "capacity_operation_id": self._capacity_id(spec),
                "capacity_generation": generation, "reason_code": reason_code,
                "result": result_data,
                "result_sha256": hashlib.sha256(canonical(result_data).encode()).hexdigest()
                                 if result_data is not None else None,
                "observed_receipt_ref": ref(receipt) if receipt else None,
                "external_effect": external_effect,
                "used_tokens": used_tokens, "cached_tokens": cached_tokens,
                "capacity_reconciled": reconciled,
                "created_at": self.store.clock(),
            })
            observation_ref = ref(observation)
            self.traces.append_tx(
                tx, _id("executor-unaccepted-event:", [spec.mission_id, spec.work_id, generation]),
                context=TraceContext(spec.mission_id, spec.run_id, spec.proposal_ref["id"],
                                     spec.attempt_id, spec.actor_id, self._capacity_id(spec),
                                     call_id=spec.call_id or None,
                                     call_version=spec.call_version,
                                     configuration_ref=spec.configuration_ref),
                category=spec.kind, status="result_unaccepted",
                action="Record unaccepted callback observation",
                summary="A callback or saved model receipt was observed, but board acceptance did not complete.",
                inputs=[spec.proposal_ref],
                outputs=[observation_ref], proof="recorded_local_event")
            if reconciled:
                self.capacity.record_unaccepted_tx(
                    tx, self._capacity_id(spec), self.owner, generation,
                    used_tokens=used_tokens, cached_tokens=cached_tokens,
                    output_ref=observation_id)
            updated_work = tx.put(self.KIND, self._work_record_id(spec),
                                  {**work["body"], "phase": "result_unaccepted",
                                   "result_ref": observation_ref},
                                  expected_version=work["version"])
            tx.emit(spec.mission_id, "executor_result_unaccepted", {
                "work_ref": ref(updated_work), "observation_ref": observation_ref,
                "external_effect": external_effect,
                "capacity_reconciled": reconciled})
            return {"status": "result_unaccepted", "observation_ref": observation_ref,
                    "capacity_reconciled": reconciled}

        saved = self.store.transact(
            _id("executor-observe:", [spec.mission_id, spec.work_id, generation,
                                      hashlib.sha256(canonical(request).encode()).hexdigest()]),
            request, save, mission_id=spec.mission_id)
        if saved["status"] == "result_unaccepted" and not saved["capacity_reconciled"]:
            self._mark_external_unknown(spec, generation)
        return saved

    def _observe_or_unknown(self, spec: WorkSpec, generation: int, fence: dict,
                            result: WorkResult | None, reason_code: str) -> dict:
        try:
            observed = self._record_unaccepted(spec, generation, result, reason_code)
        except Exception:
            observed = None
            try:
                work = self.store.get(self.KIND, self._work_record_id(spec))
                if work is not None and work["body"]["phase"] == "completed":
                    observed = {"status": "completed"}
                elif work is not None and work["body"]["phase"] == "result_unaccepted":
                    saved = self.store.get(**work["body"]["result_ref"])
                    if saved is not None:
                        observed = {"status": "result_unaccepted",
                                    "observation_ref": ref(saved),
                                    "capacity_reconciled": saved["body"]["capacity_reconciled"]}
            except Exception:
                pass
        if observed is not None and observed["status"] == "result_unaccepted" and observed["capacity_reconciled"]:
            try:
                self._release_board(spec, fence)
            except (StateError, StateConflict):
                pass  # Another owner or revised proposal has already superseded this fence.
        if observed is not None and observed["status"] != "uncertain":
            return {"work_id": spec.work_id, **observed}
        self._mark_external_unknown(spec, generation)
        return {"work_id": spec.work_id, "status": "uncertain"}

    def _accept(self, spec: WorkSpec, cap: dict, fence: dict,
                result: WorkResult) -> dict:
        result_data = json.loads(canonical(asdict(result)))
        result_hash = hashlib.sha256(canonical(result_data).encode()).hexdigest()
        artifact_id = _id("executor-artifact:", [spec.mission_id, spec.work_id])
        receipt_id = _id("executor-receipt:", [spec.mission_id, spec.work_id])
        work_intent = spec.intent()
        related_request = {
            "work_id": spec.work_id, "proposal_ref": spec.proposal_ref,
            "configuration_ref": spec.configuration_ref, "input_refs": list(spec.input_refs),
            "handler_id": spec.handler_id, "handler_version": spec.handler_version,
            "work_sha256": hashlib.sha256(canonical(work_intent).encode()).hexdigest(),
            "result_sha256": result_hash,
        }

        def save_related(tx: Transaction) -> list[dict]:
            self._validate_tx(tx, spec)
            model_receipt = self._model_receipt_tx(tx, spec, result)
            if (model_receipt is not None
                and (model_receipt["body"].get("status") not in {"complete", "tool_requested"}
                     or self._model_receipt_effect(model_receipt) != "known")):
                raise StateConflict("Callback completion differs from saved model outcome")
            capacity_record = tx.get("capacity_operation", self._capacity_id(spec))
            capacity_body = capacity_record["body"]
            now = self.store.clock()
            timing = {
                "timing_basis": "store_clock",
                "elapsed_seconds": max(0, now - capacity_body["submitted_at"]),
                "queue_wait_seconds": capacity_body["queue_wait_seconds"],
                "quota_wait_seconds": capacity_body["quota_wait_seconds"],
                "provider_wait_seconds": capacity_body["provider_wait_seconds"],
                "retry_wait_seconds": capacity_body["retry_wait_seconds"],
                "active_seconds": capacity_body["active_seconds"]
                                  + max(0, now - capacity_body["attempts"][-1]["at"]),
                "peer_wait": "not_measured_by_executor",
            }
            artifact = tx.put("executor_artifact", artifact_id, {
                "mission_id": spec.mission_id, "work_id": spec.work_id,
                "actor_id": spec.actor_id, "configuration_ref": spec.configuration_ref,
                "input_refs": list(spec.input_refs), "output_refs": list(result.output_refs),
                "handler_id": spec.handler_id, "handler_version": spec.handler_version,
                "payload": result_data["payload"], "result_sha256": result_hash,
                "proof": "recorded_callback_result",
            })
            for pointer in result.output_refs:
                source = require_ref(tx, spec.mission_id, pointer)
                tx.link(artifact, source, "derived_from")
            receipt = tx.put("executor_receipt", receipt_id, {
                "mission_id": spec.mission_id, "work_id": spec.work_id,
                "artifact_ref": ref(artifact), "capacity_operation_id": self._capacity_id(spec),
                "used_tokens": result.used_tokens, "cached_tokens": result.cached_tokens,
                "result_sha256": result_hash, "handler_version": spec.handler_version,
                "timing": timing,
            })
            pointers = [ref(artifact), ref(receipt)]
            for index, message in enumerate(result.messages):
                if type(message) is not dict or set(message) - {"recipient_id", "payload", "correlation_id", "evidence_refs"} or not {"recipient_id", "payload"} <= set(message):
                    raise StateError("Callback message needs recipient and payload")
                sent = self.mailbox.send_message_tx(
                    tx, mission_id=spec.mission_id,
                    message_id=_id("executor-message:", [spec.mission_id, spec.work_id, index]),
                    sender_id=spec.actor_id, recipient_id=message["recipient_id"],
                    correlation_id=message.get("correlation_id", spec.proposal_ref["id"]),
                    payload=message["payload"], task_ref=spec.proposal_ref,
                    evidence_refs=message.get("evidence_refs", []))
                pointers.append(sent["message_ref"])
            trace = self.traces.append_tx(
                tx, _id("executor-event:", [spec.mission_id, spec.work_id]),
                context=TraceContext(spec.mission_id, spec.run_id, spec.proposal_ref["id"],
                                     spec.attempt_id, spec.actor_id, self._capacity_id(spec),
                                     call_id=spec.call_id or None,
                                     call_version=spec.call_version,
                                     configuration_ref=spec.configuration_ref),
                category=spec.kind, status="completed", action="Execute peer-selected work",
                summary="The registered callback returned and its local result was accepted.",
                inputs=list(spec.input_refs), outputs=[ref(artifact)],
                timing=timing,
                proof="recorded_local_callback")
            pointers.append(trace)
            self.capacity.settle_tx(tx, self._capacity_id(spec), self.owner,
                                    cap["generation"], used_tokens=result.used_tokens,
                                    cached_tokens=result.cached_tokens, outcome="completed",
                                    output_ref=artifact_id)
            work = tx.get(self.KIND, self._work_record_id(spec))
            tx.put(self.KIND, self._work_record_id(spec),
                   {**work["body"], "phase": "completed", "result_ref": ref(artifact),
                    "receipt_ref": ref(receipt)}, expected_version=work["version"])
            return pointers

        return self.board.accept_result(
            _id("executor-accept:", [spec.mission_id, spec.work_id]),
            mission_id=spec.mission_id, proposal_id=spec.proposal_ref["id"],
            actor_id=spec.actor_id, fence=fence,
            outcome_id=_id("executor-outcome:", [spec.mission_id, spec.work_id]),
            save_related=save_related, related_request=related_request)

    async def _execute(self, spec: WorkSpec, cap: dict, fence: dict) -> dict:
        cap_id = self._capacity_id(spec)
        try:
            self.capacity.dispatched(cap_id, self.owner, cap["generation"])
        except (StateError, StateConflict):
            return {"work_id": spec.work_id, "status": "not_dispatched"}
        keepalive = asyncio.create_task(self._keepalive(spec, cap, fence))
        try:
            try:
                result = await self._invoke(spec)
            except asyncio.CancelledError:
                self._observe_or_unknown(
                    spec, cap["generation"], fence, None,
                    "callback_cancelled_after_dispatch")
                raise
            except Exception as exc:
                observed = self._observe_or_unknown(
                    spec, cap["generation"], fence, None, "callback_failed_after_dispatch")
                return {**observed, "error_class": type(exc).__name__}
            if result.disposition == "completed":
                try:
                    accepted = self._accept(spec, cap, fence, result)
                except Exception as exc:
                    observed = self._observe_or_unknown(
                        spec, cap["generation"], fence, result, "acceptance_failed")
                    return {**observed, "error_class": type(exc).__name__}
                if accepted["status"] == "accepted":
                    return {"work_id": spec.work_id, "status": "completed", **accepted}
                observed = self._observe_or_unknown(
                    spec, cap["generation"], fence, result, "board_rejected")
                return {**observed, "board_rejection": accepted["code"]}
            if result.disposition == "rate_limited":
                try:
                    state = self.capacity.rate_limited(cap_id, self.owner, cap["generation"],
                                                       scope=result.rate_scope or "",
                                                       retry_after=result.retry_after,
                                                       used_tokens=result.used_tokens)
                except (StateError, StateConflict):
                    self._mark_external_unknown(spec, cap["generation"])
                    return {"work_id": spec.work_id, "status": "uncertain"}
                try:
                    self._release_board(spec, fence)
                except (StateError, StateConflict):
                    return {"work_id": spec.work_id, "status": state["phase"],
                            "board_claim_release": "pending_expiry"}
                return {"work_id": spec.work_id, "status": state["phase"]}
            if result.disposition in {"temporary_no_effect", "terminal", "uncertain"}:
                try:
                    state = self.capacity.transport_failure(
                        cap_id, self.owner, cap["generation"],
                        classification=result.disposition, retry_after=result.retry_after)
                except (StateError, StateConflict):
                    self._mark_external_unknown(spec, cap["generation"])
                    return {"work_id": spec.work_id, "status": "uncertain"}
                if result.disposition != "uncertain":
                    try:
                        self._release_board(spec, fence)
                    except (StateError, StateConflict):
                        return {"work_id": spec.work_id, "status": state["phase"],
                                "board_claim_release": "pending_expiry"}
                return {"work_id": spec.work_id, "status": state["phase"]}
            self._mark_external_unknown(spec, cap["generation"])
            return {"work_id": spec.work_id, "status": "uncertain", "error_class": "InvalidDisposition"}
        finally:
            keepalive.cancel()
            try:
                await keepalive
            except asyncio.CancelledError:
                pass
            except Exception:
                pass  # The result fence still determines whether acceptance is safe.

    async def run_ready(self, mission_id: str) -> dict:
        """Drain eligible callbacks; return durable waits without mission completion."""
        self.capacity.expire()
        active: set[asyncio.Task] = set()
        observed = []
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        key = (str(self.store.db_path), mission_id)
        with _WORK_LISTENERS_LOCK:
            _WORK_LISTENERS.setdefault(key, set()).add((loop, wake))
        try:
            while True:
                wake.clear()
                eligible = self._prepare(mission_id)
                for kind in KINDS:
                    ids = frozenset(cap_id for cap_id, spec in eligible.items() if spec.kind == kind)
                    while ids:
                        admitted = self.capacity.admit(self.owner, kind, mission_id=mission_id,
                                                       eligible_operation_ids=ids,
                                                       lease_seconds=self.lease_seconds)
                        if admitted is None:
                            break
                        cap_id = admitted["operation_id"]
                        spec = eligible.pop(cap_id)
                        ids = ids - {cap_id}
                        claim = self.board.claim(
                            _id("executor-claim:", [spec.mission_id, spec.work_id,
                                                    admitted["generation"]]),
                            mission_id=mission_id, proposal_id=spec.proposal_ref["id"],
                            actor_id=spec.actor_id, ttl=self.lease_seconds,
                            invitation_id=spec.invitation_id)
                        if claim["status"] != "accepted":
                            self.capacity.yield_admission(cap_id, self.owner, admitted["generation"])
                            continue
                        active.add(asyncio.create_task(self._execute(spec, admitted, claim["fence"])))
                if not active:
                    break
                wake_task = asyncio.create_task(wake.wait())
                try:
                    done, pending = await asyncio.wait(
                        active | {wake_task}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    wake_task.cancel()
                    await asyncio.gather(wake_task, return_exceptions=True)
                active = pending - {wake_task}
                for task in done - {wake_task}:
                    try:
                        observed.append(task.result())
                    except Exception as exc:
                        observed.append({"status": "runner_error", "error_class": type(exc).__name__})
        finally:
            with _WORK_LISTENERS_LOCK:
                listeners = _WORK_LISTENERS.get(key)
                if listeners is not None:
                    listeners.discard((loop, wake))
                    if not listeners:
                        _WORK_LISTENERS.pop(key, None)
            if active:
                for task in active:
                    task.cancel()
                await asyncio.gather(*active, return_exceptions=True)
        work = self._work(mission_id)
        states = {}
        wakes = []
        open_proposals = self.board.open_work(mission_id)
        claim_expiry = {item["proposal_ref"]["id"]: item["current_claim"]["expires_at"]
                        for item in open_proposals if item["current_claim"] is not None}
        for record in work:
            spec = self._spec_from(record)
            cap = self.capacity.get(self._capacity_id(spec))
            phase = (record["body"]["phase"]
                     if record["body"]["phase"] in {"completed", "result_unaccepted"}
                     else (cap["phase"] if cap else "pending"))
            states[spec.work_id] = phase
            if cap and cap["phase"] == "waiting" and cap["wake_at"] is not None:
                wakes.append(max(cap["wake_at"], claim_expiry.get(spec.proposal_ref["id"], 0)))
            if cap and cap["phase"] in {"admitted", "dispatched"} and cap["lease_until"] is not None:
                wakes.append(cap["lease_until"])
        peer_requests = [record["id"] for record in self._records("mail_request")
                         if record["body"].get("mission_id") == mission_id
                         and record["body"].get("status") in {"unanswered", "awaiting_review", "partial_awaiting_review", "partly_answered"}]
        wakes.extend(claim_expiry.values())
        return {"mission_status": "open", "work": states, "observed": observed,
                "next_wake": min(wakes) if wakes else None,
                "open_proposals": [item["proposal_ref"] for item in open_proposals],
                "pending_peer_requests": peer_requests}
