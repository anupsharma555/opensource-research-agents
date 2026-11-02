"""Supersede only a lost local-function model response after uncertain dispatch.

The original request and dispatch marker remain immutable.  The executor owns
the atomic capacity, work and board fence transition; this module verifies the
model-only boundary and saves the continuation receipt for mission controls.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from ora.coordination.board import ref
from ora.coordination.executor import ConcurrentExecutor
from ora.models import ModelSession
from ora.state import StateConflict, StateError, StateStore
from ora.state.store import Transaction, canonical

_REQUEST_FIELDS = frozenset(
    {
        "model",
        "messages",
        "reasoning_effort",
        "reasoning_format",
        "include_reasoning",
        "max_completion_tokens",
        "stream",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "stream_options",
    }
)


def _exact(store: StateStore, pointer: dict, kind: str) -> dict:
    if (
        type(pointer) is not dict
        or set(pointer) != {"kind", "id", "version"}
        or pointer.get("kind") != kind
        or type(pointer.get("version")) is not int
        or pointer["version"] < 1
    ):
        raise StateError(f"Exact {kind} reference is required")
    record = store.get(**pointer)
    if record is None or ref(record) != pointer:
        raise StateError(f"Exact {kind} record is unavailable")
    return record


class ModelRecovery:
    """Build a fresh-call handoff without replaying or accepting an unseen reply."""

    def __init__(self, store: StateStore, executor: ConcurrentExecutor, session: ModelSession):
        self.store, self.executor, self.session = store, executor, session

    def accept_captured_response(
        self, *, control_ref: dict, work_ref: dict,
        validate_current_tx: Callable[[Transaction], None],
    ) -> dict:
        """Resume exact saved model output, without another model or tool call."""
        control = _exact(self.store, control_ref, "mission_task_control")
        work = _exact(self.store, work_ref, "executor_work")
        spec = self.executor._spec_from(work)
        call_ref = {"kind": "agent_call", "id": spec.call_id,
                    "version": spec.call_version}
        run = self.store.get("mission_run", spec.run_id)
        proposal = self.store.get("board_proposal", spec.proposal_ref["id"])
        if (
            spec.kind != "model" or (spec.handler_id, spec.handler_version)
            != ("mission-model", "1")
            or control["body"].get("phase") not in {"pending_model", "waiting"}
            or control["body"].get("work_id") != spec.work_id
            or control["body"].get("call_ref") != call_ref
            or any(control["body"].get(key) != value for key, value in (
                ("mission_id", spec.mission_id), ("run_id", spec.run_id),
                ("task_id", spec.proposal_ref["id"]), ("actor_id", spec.actor_id),
            ))
            or work["body"].get("phase") != "result_unaccepted"
            or run is None or run["body"].get("status") != "active"
            or proposal is None or ref(proposal) != spec.proposal_ref
            or proposal["body"].get("status") != "open"
        ):
            return {"status": "not_eligible"}
        observation_ref = work["body"].get("result_ref")
        observation = (self.store.get(**observation_ref)
                       if isinstance(observation_ref, dict) else None)
        intent_id = f"model-intent:{spec.call_id}:{spec.call_version}"
        intent = self.store.get("model_request_intent", intent_id)
        response = self.store.get("model_response_receipt", f"model-response:{intent_id}")
        marker = self.store.get("model_dispatch_attempt", f"model-dispatch:{intent_id}")
        cap = self.store.get("capacity_operation", self.executor._capacity_id(spec))
        if (
            observation is None or observation["kind"] != "executor_unaccepted_result"
            or intent is None or response is None or marker is None or cap is None
            or observation["body"].get("observed_receipt_ref") != ref(response)
            or observation["body"].get("capacity_reconciled") is not True
            or observation["body"].get("external_effect") != "known"
            or observation["body"].get("work_id") != spec.work_id
            or cap["body"].get("phase") != "known_unaccepted"
            or cap["body"].get("output_ref") != observation["id"]
            or cap["body"].get("cancel_requested") is True
            or intent["body"].get("call_ref") != call_ref
            or intent["body"].get("configuration_ref") != spec.configuration_ref
            or tuple(intent["body"].get("source_refs", [])) != spec.input_refs
            or marker["body"].get("status") != "captured"
            or marker["body"].get("intent_ref") != ref(intent)
            or marker["body"].get("call_ref") != call_ref
            or marker["body"].get("receipt_ref") != ref(response)
            or marker["body"].get("request_sha256") != intent["body"].get("request_sha256")
            or response["body"].get("request_sha256") != intent["body"].get("request_sha256")
        ):
            return {"status": "not_eligible"}
        return self.executor.accept_captured_model_response(
            spec, ref(response), observation_ref,
            validate_current_tx=validate_current_tx,
        )

    def supersede_unknown_response(
        self,
        operation_id: str,
        *,
        control_ref: dict,
        work_ref: dict,
    ) -> dict[str, Any]:
        """Return a durable model-only recovery receipt and nonterminal wake.

        The caller updates its control only after this transaction is accepted.
        The executor transition must fence old acceptance, release its local
        permit and retain the unknown attempt's conservative quota charge.
        """
        if not isinstance(operation_id, str) or not operation_id:
            raise StateError("Model recovery needs an operation ID")
        control = _exact(self.store, control_ref, "mission_task_control")
        work = _exact(self.store, work_ref, "executor_work")
        spec = self.executor._spec_from(work)
        recovery_id = (
            "model-response-lost-"
            + hashlib.sha256(canonical([spec.mission_id, spec.work_id]).encode()).hexdigest()[:32]
        )
        prior = self.store.get("model_response_lost", recovery_id)
        if prior is not None:
            if (
                prior["body"].get("control_ref") != control_ref
                or prior["body"].get("work_ref") != work_ref
            ):
                raise StateConflict("Recovery receipt names different model work")
            return self._result(prior)
        if (
            spec.kind != "model"
            or (spec.handler_id, spec.handler_version) != ("mission-model", "1")
            or not spec.call_id
            or type(spec.call_version) is not int
            or control["body"].get("phase") not in {"pending_model", "waiting"}
            or control["body"].get("work_id") != spec.work_id
            or any(
                control["body"].get(key) != value
                for key, value in (
                    ("mission_id", spec.mission_id),
                    ("run_id", spec.run_id),
                    ("task_id", spec.proposal_ref["id"]),
                    ("actor_id", spec.actor_id),
                )
            )
            or work["body"].get("phase") != "pending"
            or work["body"].get("result_ref") is not None
        ):
            raise StateError("Only this control's unresolved model work may be superseded")
        call_ref = {"kind": "agent_call", "id": spec.call_id, "version": spec.call_version}
        if control["body"].get("call_ref") != call_ref:
            raise StateError("Control and unresolved model call differ")
        call = _exact(self.store, call_ref, "agent_call")
        intent_ref = {
            "kind": "model_request_intent",
            "id": f"model-intent:{spec.call_id}:{spec.call_version}",
            "version": 1,
        }
        intent = _exact(self.store, intent_ref, "model_request_intent")
        if (
            intent["body"].get("call_ref") != call_ref
            or intent["body"].get("configuration_ref") != spec.configuration_ref
            or tuple(intent["body"].get("source_refs", [])) != spec.input_refs
            or intent["body"].get("phase") != "collect"
        ):
            raise StateError("Unresolved intent differs from pinned model work")
        marker_id = f"model-dispatch:{intent['id']}"
        marker = self.store.get("model_dispatch_attempt", marker_id)
        if (
            marker is None
            or marker["body"].get("intent_ref") != intent_ref
            or marker["body"].get("call_ref") != call_ref
            or marker["body"].get("request_sha256") != intent["body"].get("request_sha256")
            or marker["body"].get("status") != "attempted_external_effect"
        ):
            raise StateError("Exact attempted model dispatch is unavailable")
        if self.store.get("model_response_receipt", f"model-response:{intent['id']}"):
            raise StateError("A captured model response needs known-result handling")
        cap_id = self.executor._capacity_id(spec)
        cap = self.store.get("capacity_operation", cap_id)
        if (
            cap is None
            or cap["body"].get("phase") != "uncertain"
            or cap["body"].get("kind") != "model"
            or cap["body"].get("reservation") is not True
            or not cap["body"].get("attempts")
            or cap["body"]["attempts"][-1].get("usage_known") is not False
            or cap["body"]["attempts"][-1].get("status") != "dispatched"
        ):
            raise StateError("Model capacity is not an unresolved unknown-response attempt")

        prepared = self.session.load(intent_ref)
        request = prepared.request.body
        tools = request.get("tools")
        config = _exact(self.store, spec.configuration_ref, "agent_configuration")
        if (
            set(request) - _REQUEST_FIELDS
            or ("reasoning_format" in request and (
                request.get("model") != "qwen/qwen3.8-27b"
                or request["reasoning_format"] != "parsed"))
            or request.get("model") != call["body"].get("model_id")
            or request.get("tool_choice") != "auto"
            or request.get("parallel_tool_calls") is not False
            or not isinstance(tools, list)
            or not tools
            or any(
                type(item) is not dict
                or item.get("type") != "function"
                or not isinstance(item.get("function"), dict)
                for item in tools
            )
            or config["body"].get("provider") != "groq"
            or config["body"].get("model_id") != request.get("model")
        ):
            raise StateError("Only a saved local-function Groq model call may be superseded")
        # Local tool execution requires a saved response and a later tool work.
        # Reject inconsistent state rather than inferring that a tool was harmless.
        for item in self.executor._work(spec.mission_id):
            other = self.executor._spec_from(item)
            if other.call_id == spec.call_id and other.work_id != spec.work_id:
                raise StateError("Unresolved model call already has downstream work")

        request_fingerprint = {
            "control_ref": control_ref,
            "work_ref": work_ref,
            "intent_ref": intent_ref,
            "marker_ref": ref(marker),
        }

        def save(tx: Transaction) -> dict[str, Any]:
            prior = tx.get("model_response_lost", recovery_id)
            if prior is not None:
                if prior["body"].get("work_ref") != work_ref:
                    raise StateConflict("Recovery receipt names different model work")
                return self._result(prior)
            if (
                tx.get("mission_task_control", control["id"]) != control
                or tx.get("executor_work", work["id"]) != work
                or tx.get("capacity_operation", cap_id) != cap
                or tx.get("model_dispatch_attempt", marker_id) != marker
                or tx.get("model_response_receipt", f"model-response:{intent['id']}")
            ):
                raise StateConflict("Model response state changed before supersession")
            rows = tx.db.execute(
                "SELECT r.body FROM records r JOIN heads h USING(kind,id,version) "
                "WHERE r.kind='model_response_lost'"
            ).fetchall()
            prior_losses = sum(
                (saved := json.loads(row["body"])).get("mission_id") == spec.mission_id
                and saved.get("task_id") == spec.proposal_ref["id"]
                for row in rows
            )
            delay = min(60.0, float(2 ** min(prior_losses + 1, 6)))
            body = {
                "schema": "ora.model_response_lost.v1",
                "mission_id": spec.mission_id,
                "run_id": spec.run_id,
                "task_id": spec.proposal_ref["id"],
                "actor_id": spec.actor_id,
                "control_ref": control_ref,
                "work_ref": work_ref,
                "call_ref": call_ref,
                "intent_ref": intent_ref,
                "dispatch_marker_ref": ref(marker),
                "configuration_ref": spec.configuration_ref,
                "provider": config["body"]["provider"],
                "endpoint": config["body"]["endpoint"],
                "model_id": config["body"]["model_id"],
                "settings": config["body"]["settings"],
                "reasoning_settings": config["body"]["reasoning_settings"],
                "request_sha256": intent["body"]["request_sha256"],
                "remote_response": "unknown",
                "usage": "unknown_conservative_quota_charge_retained",
                "local_tool_effect": "none_accepted",
                "attempt_status": "uncertain",
                "backoff_seconds": delay,
                "retry_at": self.store.clock() + delay,
                "next_sequence": control["body"]["sequence"] + 1,
            }
            saved = tx.put("model_response_lost", recovery_id, body)
            self.executor.supersede_uncertain_model_tx(tx, spec, recovery_ref=ref(saved))
            tx.link(saved, work, "supersedes")
            tx.link(saved, intent, "about_request")
            tx.link(saved, marker, "retains_unknown_effect")
            tx.emit(
                spec.mission_id,
                "model_response_lost_superseded",
                {"recovery_ref": ref(saved), "work_ref": work_ref},
            )
            return self._result(saved)

        return self.store.transact(
            operation_id, request_fingerprint, save, mission_id=spec.mission_id
        )

    @staticmethod
    def _result(record: dict) -> dict[str, Any]:
        body = record["body"]
        return {
            "recovery_ref": ref(record),
            "old_call_ref": body["call_ref"],
            "old_intent_ref": body["intent_ref"],
            "pinned_configuration_ref": body["configuration_ref"],
            "pinned_model_id": body["model_id"],
            "pinned_settings": body["settings"],
            "pinned_reasoning_settings": body["reasoning_settings"],
            "retry_at": body["retry_at"],
            "next_sequence": body.get("next_sequence"),
        }
