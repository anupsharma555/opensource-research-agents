"""Durable, local admission for independent research operations.

The ledger covers calls made through this StateStore, not other consumers of a
provider account. All decisions are short SQLite transactions; external work is
performed only after ``dispatched`` has durably recorded the uncertain boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Mapping

from ora.models.token_estimate import LEGACY_BASIS, estimate_request_tokens
from ora.state.blobs import IntegrityFault
from ora.state.store import StateConflict, StateError, StateStore, Transaction, canonical

KINDS = frozenset({"model", "retrieval", "compute"})
OPERATION = "capacity_operation"
QUOTA = "capacity_quota"
CONFIG = "capacity_config"
BRIDGE = "capacity_quota_bridge"
_RESET_PART = re.compile(r"(\d+(?:\.\d+)?)([dhms])")


def _reset_seconds(value: object) -> float | None:
    """Parse Groq's documented duration headers, including `2m59.56s`."""
    if not isinstance(value, str) or not value:
        return None
    position = 0
    seconds = 0.0
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    for match in _RESET_PART.finditer(value):
        if match.start() != position:
            return None
        seconds += float(match.group(1)) * units[match.group(2)]
        position = match.end()
    return seconds if position == len(value) and math.isfinite(seconds) else None


def _remaining(value: object) -> int | None:
    return int(value) if isinstance(value, str) and value.isdecimal() else None


def _saved_token_limit(tx: Transaction, snapshot: dict) -> int | None:
    """Read an older quota head's limit from its exact captured response."""
    stored = snapshot.get("observed_token_limit")
    if type(stored) is int:
        return stored
    source = snapshot.get("source_operation_id")
    operation = tx.get(OPERATION, source) if isinstance(source, str) and source else None
    call_id = operation["body"].get("call_id") if operation else None
    call = tx.get("agent_call", call_id) if isinstance(call_id, str) and call_id else None
    if call is None:
        return None
    pointer = {"kind": "agent_call", "id": call["id"], "version": call["version"]}
    receipt = tx.get(
        "model_response_receipt", f"model-response:model-intent:{call['id']}:{call['version']}",
    )
    if receipt is None or receipt["body"].get("call_ref") != pointer:
        return None
    return _remaining((receipt["body"].get("rate_headers") or {}).get("x-ratelimit-limit-tokens"))


@dataclass(frozen=True)
class QuotaLimit:
    """A configured local ceiling; None means external headroom is unknown."""

    window_seconds: float
    requests: int | None = None
    tokens: int | None = None
    basis: str = "configured"

    def __post_init__(self):
        if not math.isfinite(self.window_seconds) or self.window_seconds <= 0:
            raise ValueError("Quota window must be positive and finite")
        for value in (self.requests, self.tokens):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Quota ceilings must be nonnegative integers")
        if self.basis not in {"configured", "observed"}:
            raise ValueError("Quota basis must be configured or observed")


@dataclass(frozen=True)
class CapacityPolicy:
    permits: Mapping[str, int]
    quotas: Mapping[str, QuotaLimit] = field(default_factory=dict)
    review_reserve: int = 1

    def __post_init__(self):
        if set(self.permits) != KINDS or any(type(v) is not int or v < 0 for v in self.permits.values()):
            raise ValueError("Permits must give nonnegative model, retrieval and compute slots")
        if type(self.review_reserve) is not int or not 0 <= self.review_reserve <= self.permits["model"]:
            raise ValueError("Review reserve must fit model capacity")
        if any(not isinstance(k, str) or not k or not isinstance(v, QuotaLimit) for k, v in self.quotas.items()):
            raise ValueError("Quota scopes need names and QuotaLimit values")

    def snapshot(self) -> dict:
        return {
            "permits": dict(self.permits),
            "quotas": {key: vars(value) for key, value in sorted(self.quotas.items())},
            "review_reserve": self.review_reserve,
        }


def _heads(tx: Transaction, kind: str) -> list[dict]:
    rows = tx.db.execute(
        "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) WHERE r.kind=? ORDER BY r.id",
        (kind,),
    ).fetchall()
    return [{"kind": kind, "id": row["id"], "version": row["version"], "body": json.loads(row["body"])} for row in rows]


class CapacityScheduler:
    """One admission topology shared by all dispatchers using the same database."""

    def __init__(self, store: StateStore, policy: CapacityPolicy):
        self.store = store
        self.policy = CapacityPolicy(dict(policy.permits), dict(policy.quotas), policy.review_reserve)
        snapshot = self.policy.snapshot()

        existing = store.get(CONFIG, "default")
        if existing:
            if existing["body"] != snapshot:
                raise StateConflict("Capacity policy changed; reconcile outstanding reservations first")
            return

        def save(tx):
            existing = tx.get(CONFIG, "default")
            if existing and existing["body"] != snapshot:
                raise StateConflict("Capacity policy changed; reconcile outstanding reservations first")
            if not existing:
                tx.put(CONFIG, "default", snapshot)
            return snapshot

        store.transact("capacity-config-v1", snapshot, save)

    @staticmethod
    def reconcile_additive_quota_domain(
        store: StateStore, policy: CapacityPolicy, *, from_scope: str, to_scope: str,
    ) -> dict:
        """Explicitly add one verified scope with a conservative legacy-debt bridge.

        The caller establishes the nonsecret provider-account evidence. This
        operation never changes old operation scopes, quota heads, or holds.
        """
        if (
            not isinstance(from_scope, str) or not isinstance(to_scope, str)
            or not from_scope or not to_scope or from_scope == to_scope
        ):
            raise StateError("Quota migration needs distinct named scopes")
        desired = policy.snapshot()
        request = {"schema": "capacity.quota_bridge.v1", "policy": desired,
                   "from_scope": from_scope, "to_scope": to_scope}

        def save(tx):
            existing = tx.get(CONFIG, "default")
            if existing is None:
                raise StateConflict("Initialize the original capacity policy before migration")
            old = existing["body"]
            if (
                from_scope not in old["quotas"] or to_scope in old["quotas"]
                or desired["permits"] != old["permits"]
                or desired["review_reserve"] != old["review_reserve"]
                or desired["quotas"] != {**old["quotas"], to_scope: desired["quotas"].get(to_scope)}
                or to_scope not in desired["quotas"]
                or tx.get(BRIDGE, to_scope) is not None
            ):
                raise StateConflict("Quota migration must add one scope without changing existing limits")
            inherited = [
                {"kind": OPERATION, "id": record["id"], "version": record["version"]}
                for record in _heads(tx, OPERATION)
                if from_scope in record["body"]["quota_scopes"]
            ]
            prior_quota = tx.get(QUOTA, from_scope)
            cutover_at = store.clock()
            updated = tx.put(CONFIG, "default", desired, expected_version=existing["version"])
            bridge = tx.put(BRIDGE, to_scope, {
                "schema": "capacity.quota_bridge.v1",
                "from_scope": from_scope, "to_scope": to_scope,
                "cutover_at": cutover_at,
                "prior_config_ref": {"kind": CONFIG, "id": existing["id"],
                                     "version": existing["version"]},
                "new_config_ref": {"kind": CONFIG, "id": updated["id"],
                                   "version": updated["version"]},
                "cutover_quota_ref": ({"kind": QUOTA, "id": prior_quota["id"],
                                       "version": prior_quota["version"]}
                                      if prior_quota else None),
                "inherited_operation_refs": inherited,
                "later_quota_events": [],
            })
            return {"bridge_ref": {"kind": BRIDGE, "id": bridge["id"],
                                   "version": bridge["version"]},
                    "config_ref": {"kind": CONFIG, "id": updated["id"],
                                   "version": updated["version"]}}

        return store.transact(f"capacity-quota-bridge-v1-{to_scope}", request, save)

    def _ensure_current_policy(self, tx: Transaction) -> None:
        config = tx.get(CONFIG, "default")
        if config is None or config["body"] != self.policy.snapshot():
            raise StateConflict("Capacity policy changed; reload before admission")

    def enqueue(
        self, operation_id: str, *, task_id: str, lane: str, kind: str,
        quota_scopes: tuple[str, ...] = (), estimated_tokens: int = 0,
        review: bool = False, mission_id: str = "", run_id: str = "",
        attempt_id: str = "", call_id: str = "", budget_version: str = "",
        retry_limit: int = 2,
        request_token_estimate: dict | None = None,
    ) -> dict:
        if kind not in KINDS or not isinstance(task_id, str) or not task_id or not isinstance(lane, str) or not lane:
            raise StateError("Capacity work needs task, lane and known resource kind")
        if any(not isinstance(value, str) for value in (mission_id, run_id, attempt_id, call_id, budget_version)):
            raise StateError("Capacity lineage references must be text")
        if type(review) is not bool or not isinstance(quota_scopes, tuple) or any(not isinstance(scope, str) for scope in quota_scopes):
            raise StateError("Capacity review flag and quota scopes are invalid")
        if type(estimated_tokens) is not int or estimated_tokens < 0:
            raise StateError("Estimated tokens must be nonnegative")
        if type(retry_limit) is not int or retry_limit < 0:
            raise StateError("Transport retry limit must be nonnegative")
        if kind != "model" and (estimated_tokens or review):
            raise StateError("Non-model work uses request quotas and no review reserve")
        if request_token_estimate is not None and (
            kind != "model" or not isinstance(request_token_estimate, dict)
            or type(request_token_estimate.get("tokens")) is not int
            or not 0 <= request_token_estimate["tokens"] <= estimated_tokens
            or not isinstance(request_token_estimate.get("basis"), str)
        ):
            raise StateError("Capacity request estimate needs a valid model input count")
        if len(set(quota_scopes)) != len(quota_scopes) or set(quota_scopes) - set(self.policy.quotas):
            raise StateError("Every quota scope must have one configured ceiling")
        for scope in quota_scopes:
            limit = self.policy.quotas[scope]
            if limit.tokens is not None and estimated_tokens > limit.tokens:
                raise StateError(f"Request exceeds configured token ceiling for {scope}")
        intent = {
            "mission_id": mission_id, "run_id": run_id, "attempt_id": attempt_id,
            "call_id": call_id, "budget_version": budget_version, "retry_limit": retry_limit,
            "task_id": task_id, "lane": lane, "kind": kind, "quota_scopes": list(quota_scopes),
            "estimated_tokens": estimated_tokens, "review": review,
        }
        if request_token_estimate is not None:
            intent["request_token_estimate"] = request_token_estimate
        body = {
            **intent, "phase": "queued",
            "estimate_basis": (request_token_estimate["basis"] if request_token_estimate
                               else "caller_prepared_request_estimate" if kind == "model" else None),
            "submitted_at": self.store.clock(), "queued_at": self.store.clock(),
            "queue_wait_seconds": 0, "provider_wait_seconds": 0,
            "quota_wait_seconds": 0,
            "retry_wait_seconds": 0, "active_seconds": 0,
            "wait_started_at": None, "wait_reason": None,
            "generation": 0, "owner": None,
            "lease_until": None, "wake_at": None, "attempts": [], "reservation": False,
            "local_active": False, "external_active": False, "cancel_requested": False,
            "diagnostic": None, "output_ref": None,
        }

        def save(tx):
            self._ensure_current_policy(tx)
            current = tx.get(OPERATION, operation_id)
            if current:
                if any(
                    current["body"][k] != value
                    and not (
                        k == "estimated_tokens"
                        and current["body"].get("estimated_tokens_original") == value
                    )
                    for k, value in intent.items()
                ):
                    raise StateConflict("Capacity operation ID has different inputs")
                return current["body"]
            return tx.put(OPERATION, operation_id, body)["body"]

        self.store.transact(f"capacity-enqueue-{operation_id}", intent, save)
        return self.get(operation_id)

    def reestimate_legacy_pending_models(self) -> list[dict]:
        """Correct old 8192 placeholders from exact saved request bytes.

        This changes only queued admission metadata. Prepared requests and any
        dispatched, uncertain, or accepted external effect stay untouched.
        """
        changed = []
        work_by_capacity_id = {}
        work_after = ""
        while True:
            work_page = self.store.records("executor_work", limit=500, after_id=work_after)
            for work in work_page:
                spec = work["body"].get("spec") or {}
                if isinstance(spec.get("mission_id"), str) and isinstance(spec.get("work_id"), str):
                    capacity_id = "executor-cap:" + hashlib.sha256(
                        canonical([spec["mission_id"], spec["work_id"]]).encode()
                    ).hexdigest()
                    work_by_capacity_id[capacity_id] = work
            if len(work_page) < 500:
                break
            work_after = work_page[-1]["id"]
        after = ""
        while True:
            page = self.store.records(OPERATION, limit=500, after_id=after)
            for record in page:
                body = record["body"]
                if not (
                    body.get("kind") == "model"
                    and body.get("phase") in {"queued", "waiting"}
                    and not body.get("attempts")
                    and body.get("estimated_tokens") == 8192
                    and not body.get("estimate_basis")
                    and body.get("call_id")
                ):
                    continue
                work = work_by_capacity_id.get(record["id"])
                spec = (work or {}).get("body", {}).get("spec") or {}
                if (
                    spec.get("call_id") != body["call_id"]
                    or spec.get("mission_id") != body["mission_id"]
                    or spec.get("run_id") != body["run_id"]
                    or spec.get("attempt_id") != body["attempt_id"]
                    or (spec.get("proposal_ref") or {}).get("id") != body["task_id"]
                    or (spec.get("configuration_ref") or {}).get("id") != body["budget_version"]
                    or type(spec.get("call_version")) is not int
                ):
                    continue
                call = self.store.get("agent_call", body["call_id"], spec["call_version"])
                if call is None or call["body"].get("mission_id") != body["mission_id"]:
                    continue
                intent_id = f"model-intent:{call['id']}:{call['version']}"
                intent = self.store.get("model_request_intent", intent_id)
                if intent is None or intent["body"].get("call_ref") != {
                    "kind": "agent_call", "id": call["id"], "version": call["version"]
                } or any(
                    intent["body"].get(key) != body[key]
                    for key in ("mission_id", "task_id", "attempt_id")
                ):
                    continue
                raw = self.store.read_blob(intent["body"]["blob"]["sha256"])
                if hashlib.sha256(raw).hexdigest() != intent["body"].get("request_sha256"):
                    raise StateError("Saved model intent bytes changed before reservation recovery")
                prior_completion = 0
                parent = intent["body"].get("parent_call_ref")
                if parent:
                    prior = self.store.get(
                        "model_response_receipt",
                        f"model-response:model-intent:{parent['id']}:{parent['version']}",
                    )
                    if prior and prior["body"].get("status") in {"complete", "tool_requested"}:
                        observed = (prior["body"].get("usage") or {}).get("completion_tokens")
                        if type(observed) is int and observed >= 0:
                            prior_completion = observed
                estimate = max(1, estimate_request_tokens(raw, basis=LEGACY_BASIS).tokens
                               + prior_completion)

                def save(tx):
                    current = tx.get(OPERATION, record["id"])
                    if (
                        current is None
                        or current["version"] != record["version"]
                        or current["body"].get("phase") not in {"queued", "waiting"}
                        or current["body"].get("attempts")
                        or tx.get("executor_work", work["id"]) != work
                        or tx.get("model_request_intent", intent["id"]) != intent
                    ):
                        return {"changed": False}
                    updated = dict(
                        current["body"],
                        estimated_tokens=estimate,
                        estimated_tokens_original=8192,
                        estimate_basis="recovered_from_exact_saved_intent_bytes_v1",
                        wake_at=self.store.clock() if current["body"]["phase"] == "waiting"
                                else current["body"].get("wake_at"),
                        estimate_intent_ref={"kind": intent["kind"], "id": intent["id"],
                                             "version": intent["version"]},
                    )
                    saved = tx.put(OPERATION, record["id"], updated,
                                   expected_version=current["version"])
                    return {"changed": True, "capacity_ref": {
                        "kind": saved["kind"], "id": saved["id"], "version": saved["version"]
                    }}

                result = self.store.transact(
                    f"capacity-reestimate-legacy:{record['id']}:{record['version']}",
                    {"operation_id": record["id"], "version": record["version"],
                     "intent_ref": {"kind": intent["kind"], "id": intent["id"],
                                    "version": intent["version"]}, "estimate": estimate},
                    save,
                    mission_id=body["mission_id"],
                )
                if result["changed"]:
                    changed.append(result["capacity_ref"])
            if len(page) < 500:
                return changed
            after = page[-1]["id"]

    def legacy_overlimit_status_tx(
        self, tx: Transaction, capacity_id: str, work_record_id: str
    ) -> dict:
        """Classify one recovered old reservation against a full request ceiling.

        Call again inside the transaction that fences old work: a read-only
        inspection alone cannot establish that no dispatcher claimed it later.
        Remaining tokens in a current window are deliberately not a ceiling.
        """
        capacity = tx.get(OPERATION, capacity_id)
        if capacity is None:
            return {"status": "not_applicable"}
        body = capacity["body"]
        if (
            body.get("kind") != "model"
            or body.get("estimate_basis") != "recovered_from_exact_saved_intent_bytes_v1"
            or body.get("estimated_tokens_original") != 8192
            or type(body.get("estimated_tokens")) is not int
        ):
            return {"status": "not_applicable"}
        limits = []
        for scope in body.get("quota_scopes", []):
            configured = self.policy.quotas.get(scope)
            if configured is None:
                return {"status": "unsafe", "reason": "unknown_quota_scope"}
            if configured.tokens is not None:
                config_record = tx.get(CONFIG, "default")
                if config_record is None:
                    return {"status": "unsafe", "reason": "capacity_config_missing"}
                limits.append((configured.tokens, {
                    "kind": CONFIG, "id": config_record["id"],
                    "version": config_record["version"],
                }))
            quota = tx.get(QUOTA, scope)
            observed = _saved_token_limit(tx, quota["body"]) if quota else None
            if type(observed) is int:
                limits.append((observed, {
                    "kind": QUOTA, "id": quota["id"], "version": quota["version"],
                }))
        if not limits:
            return {"status": "ordinary_capacity_path"}
        ceiling, limit_ref = min(limits, key=lambda item: item[0])
        summary = {"estimated_tokens": body["estimated_tokens"],
                   "per_request_limit": ceiling, "limit_evidence_ref": limit_ref}
        if body["estimated_tokens"] <= ceiling:
            return {"status": "ordinary_capacity_path", **summary}

        def unsafe(reason: str) -> dict:
            return {"status": "unsafe", "reason": reason, **summary}

        if (
            body.get("phase") not in {"queued", "waiting"}
            or body.get("owner") is not None
            or body.get("reservation") is not False
            or body.get("local_active") is not False
            or body.get("external_active") is not False
            or body.get("cancel_requested")
            or body.get("attempts")
        ):
            return unsafe("claimed_or_external_effect_uncertain")
        work = tx.get("executor_work", work_record_id)
        spec = work["body"].get("spec") if work else None
        if (
            work is None or work["body"].get("phase") != "pending"
            or work["body"].get("result_ref") is not None
            or not isinstance(spec, dict)
            or spec.get("kind") != "model"
            or not isinstance(spec.get("work_id"), str)
            or capacity_id != "executor-cap:" + hashlib.sha256(
                canonical([body.get("mission_id"), spec["work_id"]]).encode()
            ).hexdigest()
            or any(spec.get(key) != body.get(key) for key in (
                "mission_id", "run_id", "call_id", "attempt_id"
            ))
            or (spec.get("proposal_ref") or {}).get("id") != body.get("task_id")
            or (spec.get("configuration_ref") or {}).get("id") != body.get("budget_version")
            or spec.get("quota_scopes") != body.get("quota_scopes")
            or type(spec.get("call_version")) is not int
        ):
            return unsafe("work_lineage_mismatch")
        call = tx.get("agent_call", body["call_id"], spec["call_version"])
        if call is None or any(
            call["body"].get(key) != value for key, value in (
                ("mission_id", body["mission_id"]),
                ("instance_id", spec.get("actor_id")),
                ("task_id", body["task_id"]),
                ("attempt_id", body["attempt_id"]),
                ("configuration_id", body["budget_version"]),
            )
        ):
            return unsafe("call_lineage_mismatch")
        call_ref = {"kind": "agent_call", "id": call["id"], "version": call["version"]}
        intent_id = f"model-intent:{call['id']}:{call['version']}"
        intent = tx.get("model_request_intent", intent_id)
        if (
            intent is None or intent["body"].get("call_ref") != call_ref
            or any(intent["body"].get(key) != body.get(key) for key in (
                "mission_id", "task_id", "attempt_id"
            ))
        ):
            return unsafe("intent_lineage_mismatch")
        if (
            tx.get("model_dispatch_attempt", f"model-dispatch:{intent_id}")
            or tx.get("model_response_receipt", f"model-response:{intent_id}")
        ):
            return unsafe("external_effect_uncertain")
        blob = intent["body"].get("blob")
        if (
            not isinstance(blob, dict)
            or not isinstance(blob.get("sha256"), str)
            or type(blob.get("size")) is not int
        ):
            return unsafe("intent_blob_unverifiable")
        try:
            raw = self.store.read_blob(blob["sha256"])
        except IntegrityFault:
            return unsafe("intent_blob_unverifiable")
        if (
            len(raw) != blob["size"]
            or hashlib.sha256(raw).hexdigest() != intent["body"].get("request_sha256")
        ):
            return unsafe("intent_blob_unverifiable")
        prior_completion = 0
        parent = intent["body"].get("parent_call_ref")
        if parent:
            if (
                not isinstance(parent, dict)
                or set(parent) != {"kind", "id", "version"}
                or parent["kind"] != "agent_call"
                or type(parent["id"]) is not str
                or type(parent["version"]) is not int
            ):
                return unsafe("parent_lineage_mismatch")
            prior = tx.get(
                "model_response_receipt",
                f"model-response:model-intent:{parent['id']}:{parent['version']}",
            )
            if prior and prior["body"].get("call_ref") != parent:
                return unsafe("parent_lineage_mismatch")
            if prior and prior["body"].get("status") in {"complete", "tool_requested"}:
                observed = (prior["body"].get("usage") or {}).get("completion_tokens")
                if type(observed) is int and observed >= 0:
                    prior_completion = observed
        exact_estimate = max(1, estimate_request_tokens(raw, basis=LEGACY_BASIS).tokens
                             + prior_completion)
        if exact_estimate != body["estimated_tokens"]:
            return unsafe("estimate_mismatch")
        return {
            "status": "replan_allowed", **summary,
            "capacity_ref": {"kind": OPERATION, "id": capacity["id"],
                             "version": capacity["version"]},
            "work_ref": {"kind": "executor_work", "id": work["id"],
                         "version": work["version"]},
            "call_ref": call_ref,
            "intent_ref": {"kind": "model_request_intent", "id": intent["id"],
                           "version": intent["version"]},
        }

    def legacy_overlimit_status(self, capacity_id: str, work_record_id: str) -> dict:
        """Read-only preview; use legacy_overlimit_status_tx for the final CAS."""
        db = self.store._connect()
        try:
            return self.legacy_overlimit_status_tx(
                Transaction(self.store, db), capacity_id, work_record_id
            )
        finally:
            db.close()

    def _quota_ready_at(self, tx: Transaction, body: dict, operations: list[dict], now: float) -> float | None:
        """Earliest known window wake; None waits on another active reservation."""
        ready = now
        for scope in body["quota_scopes"]:
            limit = self.policy.quotas[scope]
            scoped_operations, inherited_heads = self._bridged_operations(
                tx, scope, body, operations, now, limit.window_seconds,
            )
            if scoped_operations is None:
                return None
            block = tx.get(QUOTA, scope)
            if block:
                blocked_until = block["body"]["blocked_until"]
                if blocked_until is None:
                    return None
                ready = max(ready, blocked_until)
                observed_ready = self._observed_ready_at(
                    block["body"], scope, body, scoped_operations, now,
                )
                if observed_ready is None:
                    return None
                ready = max(ready, observed_ready)
            charges = []
            pending_requests = pending_tokens = 0
            for operation in scoped_operations:
                item = operation["body"]
                if scope not in item["quota_scopes"]:
                    continue
                for attempt in item["attempts"]:
                    if attempt["at"] > now - limit.window_seconds:
                        charges.append((attempt["at"] + limit.window_seconds, attempt["charged_tokens"]))
                if item["reservation"] and item["phase"] == "admitted":
                    pending_requests += 1
                    pending_tokens += item["estimated_tokens"]
            requests = len(charges) + pending_requests
            tokens = sum(amount for _, amount in charges) + pending_tokens
            token_ceiling = limit.tokens
            if block:
                observed = _saved_token_limit(tx, block["body"])
                reset_at = block["body"].get("tokens_reset_at")
                # Once a provider headroom snapshot expires, its reported limit
                # still bounds simultaneous local reservations in the new window.
                # A fresh headroom receipt takes over again when one arrives.
                if (
                    type(observed) is int
                    and (not isinstance(reset_at, (int, float)) or now >= reset_at)
                ):
                    token_ceiling = observed if token_ceiling is None else min(token_ceiling, observed)
            for inherited in inherited_heads:
                blocked_until = inherited.get("blocked_until")
                if blocked_until is None:
                    return None  # An inherited provider hold has no known wake.
                ready = max(ready, blocked_until)
                observed_ready = self._observed_ready_at(
                    inherited, scope, body, scoped_operations, now,
                )
                if observed_ready is None:
                    return None
                ready = max(ready, observed_ready)
                inherited_limit = _saved_token_limit(tx, inherited)
                if inherited_limit is not None:
                    token_ceiling = (inherited_limit if token_ceiling is None
                                     else min(token_ceiling, inherited_limit))
            def fits():
                return ((limit.requests is None or requests + 1 <= limit.requests)
                        and (token_ceiling is None or tokens + body["estimated_tokens"] <= token_ceiling))
            if not fits():
                for expiry, amount in sorted(charges):
                    requests -= 1
                    tokens -= amount
                    if fits():
                        ready = max(ready, expiry)
                        break
                else:
                    return None
        return ready

    @staticmethod
    def _bridged_operations(
        tx: Transaction, scope: str, candidate: dict, operations: list[dict], now: float,
        window_seconds: float,
    ) -> tuple[list[dict] | None, list[dict]]:
        bridge = tx.get(BRIDGE, scope)
        if bridge is None:
            return operations, []
        saved = bridge["body"]
        by_id = {record["id"]: record for record in operations}
        inherited_ids = {item["id"] for item in saved["inherited_operation_refs"]}
        if len(inherited_ids) != len(saved["inherited_operation_refs"]):
            raise StateConflict("Quota bridge has duplicate inherited operations")
        merged = []
        recent_inherited_charge = False
        for record in operations:
            if record["id"] not in inherited_ids:
                merged.append(record)
                continue
            if saved["from_scope"] not in record["body"]["quota_scopes"]:
                raise StateConflict("Inherited quota operation changed scope")
            item = record["body"]
            if item["reservation"] or item["phase"] in {"admitted", "dispatched", "uncertain"}:
                return None, []
            recent_inherited_charge |= any(
                attempt["at"] > now - window_seconds for attempt in item["attempts"]
            )
            merged.append({**record, "body": {
                **item, "quota_scopes": [*item["quota_scopes"], scope],
            }})
        if inherited_ids - by_id.keys():
            raise StateConflict("Inherited quota operation is unavailable")
        heads = []
        if saved["cutover_quota_ref"] is not None:
            pointer = saved["cutover_quota_ref"]
            record = tx.get(pointer["kind"], pointer["id"], pointer["version"])
            if record is None or record["kind"] != QUOTA or record["id"] != saved["from_scope"]:
                raise StateConflict("Inherited provider headroom is unavailable")
            heads.append(record["body"])
        for event in saved["later_quota_events"]:
            pointer = event["quota_ref"]
            if pointer is not None:
                record = tx.get(pointer["kind"], pointer["id"], pointer["version"])
                if record is None or record["kind"] != QUOTA or record["id"] != saved["from_scope"]:
                    raise StateConflict("Inherited provider headroom is unavailable")
            heads.append(event["constraint"])
        future_head = any(
            head.get("blocked_until") is None
            or head["blocked_until"] > now
            or CapacityScheduler._observed_ready_at(
                head, scope, candidate, merged, now,
            ) is None
            or any(
                (isinstance(head.get(f"{dimension}_reset_at"), (int, float))
                    and head[f"{dimension}_reset_at"] > now)
                for dimension in ("requests", "tokens")
            )
            for head in heads
        )
        if not recent_inherited_charge and not future_head:
            return operations, []
        return merged, heads

    @staticmethod
    def _record_inherited_quota_tx(
        tx: Transaction, scope: str, source_operation_id: str | None,
        quota: dict | None, constraint: dict, observation_id: str | None = None,
    ) -> None:
        if not source_operation_id:
            return
        pointer = ({"kind": QUOTA, "id": quota["id"], "version": quota["version"]}
                   if quota is not None else None)
        for bridge in _heads(tx, BRIDGE):
            body = bridge["body"]
            if (body["from_scope"] != scope or source_operation_id not in {
                item["id"] for item in body["inherited_operation_refs"]
            }):
                continue
            tx.put(BRIDGE, bridge["id"], {
                **body, "later_quota_events": [*body["later_quota_events"], {
                    "source_operation_id": source_operation_id,
                    "quota_ref": pointer,
                    "observation_id": observation_id,
                    "constraint": constraint,
                }],
            }, expected_version=bridge["version"])

    @staticmethod
    def _inherited_provider_hold(tx: Transaction, scope: str, now: float) -> bool:
        bridge = tx.get(BRIDGE, scope)
        if bridge is None:
            return False
        saved = bridge["body"]
        heads = []
        if saved["cutover_quota_ref"] is not None:
            pointer = saved["cutover_quota_ref"]
            quota = tx.get(pointer["kind"], pointer["id"], pointer["version"])
            if quota is None:
                raise StateConflict("Inherited provider headroom is unavailable")
            heads.append(quota["body"])
        heads.extend(event["constraint"] for event in saved["later_quota_events"])
        for head in heads:
            blocked_until = head.get("blocked_until")
            if blocked_until is None or blocked_until > now:
                return True
        return False

    @staticmethod
    def _observed_ready_at(
        snapshot: dict, scope: str, candidate: dict, operations: list[dict], now: float
    ) -> float | None:
        """Reserve local calls not accounted for by a fresh provider snapshot."""
        observed_at = snapshot.get("observed_at")
        if not isinstance(observed_at, (int, float)):
            for dimension, needed in (
                ("requests", 1), ("tokens", candidate["estimated_tokens"]),
            ):
                remaining = snapshot.get(f"remaining_{dimension}")
                if type(remaining) is int and needed > remaining:
                    reset_at = snapshot.get(f"{dimension}_reset_at")
                    if not isinstance(reset_at, (int, float)):
                        return None
                    if now < reset_at:
                        return reset_at
            return now
        extra_requests = extra_tokens = 0
        source_operation_id = snapshot.get("source_operation_id")
        source_attempt_count = snapshot.get("source_attempt_count")
        for record in operations:
            item = record["body"]
            if scope not in item["quota_scopes"]:
                continue
            counted_active = False
            for index, attempt in enumerate(item["attempts"]):
                if record["id"] == source_operation_id:
                    if type(source_attempt_count) is int and index < source_attempt_count:
                        continue
                    if source_attempt_count is None and attempt["at"] < observed_at:
                        continue  # Older heads lack attempt count; equality is conservatively charged.
                if (
                    attempt["at"] >= observed_at
                    or attempt.get("completed_at", -1) >= observed_at
                    or (item["reservation"] and attempt["status"] == "dispatched")
                ):
                    extra_requests += 1
                    extra_tokens += attempt["charged_tokens"]
                    if item["reservation"] and attempt["status"] == "dispatched":
                        counted_active = True
            if item["reservation"] and not counted_active and item["phase"] == "admitted":
                extra_requests += 1
                extra_tokens += item["estimated_tokens"]
        ready = now
        for dimension, needed in (
            ("requests", extra_requests + 1),
            ("tokens", extra_tokens + candidate["estimated_tokens"]),
        ):
            remaining = snapshot.get(f"remaining_{dimension}")
            reset_at = snapshot.get(f"{dimension}_reset_at")
            if type(remaining) is int and needed > remaining:
                if not isinstance(reset_at, (int, float)):
                    return None
                if now < reset_at:
                    ready = max(ready, reset_at)
        return ready

    def admit(self, owner: str, kind: str, *, mission_id: str | None = None,
              eligible_operation_ids: frozenset[str] | None = None,
              lease_seconds: float = 60) -> dict | None:
        """Atomically choose a queued call, reserving all configured local scopes."""
        if not owner or kind not in KINDS or (mission_id is not None and not isinstance(mission_id, str)) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise StateError("Admission needs owner, resource kind and positive lease")
        if eligible_operation_ids is not None and (not isinstance(eligible_operation_ids, frozenset) or any(not isinstance(value, str) for value in eligible_operation_ids)):
            raise StateError("Eligible operation IDs must be a frozen set of IDs")
        now = self.store.clock()

        def choose(tx):
            self._ensure_current_policy(tx)
            operations = _heads(tx, OPERATION)
            active = [r["body"] for r in operations if r["body"]["kind"] == kind and r["body"]["reservation"]]
            if len(active) >= self.policy.permits[kind]:
                return {"admitted": None}
            candidates = [r for r in operations if r["body"]["kind"] == kind
                          and (mission_id is None or r["body"]["mission_id"] == mission_id)
                          and (eligible_operation_ids is None or r["id"] in eligible_operation_ids)
                          and r["body"]["phase"] in {"queued", "waiting"}
                          and (r["body"]["wake_at"] is None or r["body"]["wake_at"] <= now)
                          and not r["body"]["cancel_requested"]]
            # The reserve is shared by missions. A mission-filtered caller must
            # still leave a slot for a ready review queued by another mission.
            # Future wakes and quota-blocked reviews do not idle a usable slot.
            review_waiting = kind == "model" and any(
                r["body"]["kind"] == "model" and r["body"]["review"]
                and (eligible_operation_ids is None or r["id"] in eligible_operation_ids
                     or (mission_id is not None and r["body"]["mission_id"] != mission_id))
                and r["body"]["phase"] in {"queued", "waiting"}
                and (r["body"]["wake_at"] is None or r["body"]["wake_at"] <= now)
                and not r["body"]["cancel_requested"]
                and self._quota_ready_at(tx, r["body"], operations, now) == now
                for r in operations
            )
            recent_allocations = {}
            for record in operations:
                item = record["body"]
                if item["kind"] != kind:
                    continue
                window = max(
                    (self.policy.quotas[scope].window_seconds for scope in item["quota_scopes"]),
                    default=0,
                )
                recent = sum(
                    max(1, attempt["charged_tokens"])
                    for attempt in item["attempts"] if attempt["at"] > now - window
                )
                allocation = recent + (
                    max(1, item["estimated_tokens"]) if item["reservation"] else 0
                )
                recent_allocations[item["lane"]] = (
                    recent_allocations.get(item["lane"], 0) + allocation
                )
            # Old retries must not permanently penalize an older ready request.
            # A quiet lane gets its turn; among equally recent users, queue age wins.
            candidates.sort(key=lambda r: (
                not r["body"]["review"],
                recent_allocations.get(r["body"]["lane"], 0),
                r["body"]["queued_at"], r["id"],
            ))
            for record in candidates:
                item = record["body"]
                if review_waiting and not item["review"] and kind == "model":
                    normal_active = sum(not a["review"] for a in active)
                    if normal_active >= self.policy.permits[kind] - self.policy.review_reserve:
                        continue
                ready = self._quota_ready_at(tx, item, operations, now)
                if ready is None:
                    continue
                if ready > now:
                    if item["phase"] != "waiting" or item["wake_at"] != ready:
                        provider_blocked = any(
                            (block := tx.get(QUOTA, scope)) is not None
                            and (block["body"]["blocked_until"] is None
                                 or block["body"]["blocked_until"] > now)
                            or self._inherited_provider_hold(tx, scope, now)
                            for scope in item["quota_scopes"]
                        )
                        reason = "provider" if provider_blocked else "quota"
                        wait_started = item["wait_started_at"] if item["phase"] == "waiting" else now
                        prior = {}
                        if item["phase"] == "queued":
                            prior["queue_wait_seconds"] = item["queue_wait_seconds"] + max(0, now - item["queued_at"])
                        if item["phase"] == "waiting" and item["wait_reason"] != reason:
                            prior_key = {"provider": "provider_wait_seconds", "quota": "quota_wait_seconds",
                                         "retry": "retry_wait_seconds"}[item["wait_reason"]]
                            prior[prior_key] = item[prior_key] + max(0, now - item["wait_started_at"])
                            wait_started = now
                        tx.put(OPERATION, record["id"], dict(item, phase="waiting", wake_at=ready,
                               wait_started_at=wait_started, wait_reason=reason,
                               diagnostic="quota_or_provider_wait", **prior), expected_version=record["version"])
                    continue
                wait = {}
                if item["phase"] == "waiting" and item["wait_started_at"] is not None:
                    reason = item["wait_reason"]
                    end = min(now, item["wake_at"] or now)
                    key = {"provider": "provider_wait_seconds", "quota": "quota_wait_seconds",
                           "retry": "retry_wait_seconds"}[reason]
                    wait[key] = item[key] + max(0, end - item["wait_started_at"])
                    wait["queue_wait_seconds"] = item["queue_wait_seconds"] + max(0, now - end)
                else:
                    wait["queue_wait_seconds"] = item["queue_wait_seconds"] + max(0, now - item["queued_at"])
                updated = dict(item, phase="admitted", owner=owner, generation=item["generation"] + 1,
                               lease_until=now + lease_seconds, wake_at=None, reservation=True,
                               local_active=True, external_active=False, admitted_at=now,
                               last_progress_at=now, last_heartbeat_at=now,
                               wait_started_at=None, wait_reason=None, **wait)
                tx.put(OPERATION, record["id"], updated, expected_version=record["version"])
                return {"admitted": {"operation_id": record["id"], **updated}}
            return {"admitted": None}

        return self.store.transact(f"capacity-admit-{uuid.uuid4().hex}",
                                   {"owner": owner, "kind": kind, "mission_id": mission_id,
                                    "eligible_ids": sorted(eligible_operation_ids) if eligible_operation_ids is not None else None,
                                    "at": now}, choose)["admitted"]

    def yield_admission(self, operation_id: str, owner: str, generation: int) -> dict:
        """Release an unused permit after a competing board claim wins."""
        now = self.store.clock()

        def save(tx):
            record = self._owned(tx, operation_id, owner, generation, now)
            body = record["body"]
            if body["phase"] != "admitted":
                raise StateConflict("A dispatched effect cannot yield its reservation")
            updated = dict(body, phase="queued", owner=None, lease_until=None,
                           generation=generation + 1, reservation=False,
                           local_active=False, external_active=False,
                           queued_at=now,
                           diagnostic="board_claim_unavailable")
            return tx.put(OPERATION, operation_id, updated, expected_version=record["version"])["body"]

        self.store.transact(f"capacity-yield-{operation_id}-{generation}",
                            {"owner": owner, "generation": generation}, save)
        return self.get(operation_id)

    @staticmethod
    def _owned(tx: Transaction, operation_id: str, owner: str, generation: int, now: float) -> dict:
        record = tx.get(OPERATION, operation_id)
        if not record or record["body"]["owner"] != owner or record["body"]["generation"] != generation or (record["body"]["lease_until"] is not None and record["body"]["lease_until"] <= now):
            raise StateConflict("Capacity owner generation expired or changed")
        return record

    def dispatched(self, operation_id: str, owner: str, generation: int) -> dict:
        """Durably mark the boundary immediately before an external call."""
        now = self.store.clock()

        def save(tx):
            self._ensure_current_policy(tx)
            record = self._owned(tx, operation_id, owner, generation, now)
            body = record["body"]
            if body["phase"] != "admitted":
                raise StateConflict("Only admitted work may dispatch")
            attempt = {"at": now, "reserved_tokens": body["estimated_tokens"],
                       "charged_tokens": body["estimated_tokens"], "usage_known": False,
                       "cached_tokens": None, "status": "dispatched"}
            updated = dict(body, phase="dispatched", attempts=[*body["attempts"], attempt], external_active=True)
            return tx.put(OPERATION, operation_id, updated, expected_version=record["version"])["body"]

        self.store.transact(f"capacity-dispatch-{operation_id}-{generation}", {"owner": owner, "generation": generation}, save)
        return self.get(operation_id)

    def settle(self, operation_id: str, owner: str, generation: int, *, used_tokens: int | None,
               outcome: str, output_ref: str | None = None, cached_tokens: int = 0) -> dict:
        """Reconcile a confirmed external result; unknown usage keeps its reserve."""
        self._check_usage(outcome, used_tokens, cached_tokens)
        def save(tx):
            return self.settle_tx(tx, operation_id, owner, generation, used_tokens=used_tokens,
                                  outcome=outcome, output_ref=output_ref, cached_tokens=cached_tokens)

        self.store.transact(f"capacity-settle-{operation_id}-{generation}", {"owner": owner, "generation": generation, "outcome": outcome, "used_tokens": used_tokens, "cached_tokens": cached_tokens, "output_ref": output_ref}, save)
        return self.get(operation_id)

    def settle_tx(self, tx: Transaction, operation_id: str, owner: str, generation: int, *,
                  used_tokens: int | None, outcome: str, output_ref: str | None = None,
                  cached_tokens: int = 0) -> dict:
        """Release capacity inside a caller's accepted-result transaction."""
        self._check_usage(outcome, used_tokens, cached_tokens)
        now = self.store.clock()
        record = self._owned(tx, operation_id, owner, generation, now)
        body = record["body"]
        if body["phase"] != "dispatched":
            raise StateConflict("Only dispatched work may settle")
        attempts = list(body["attempts"])
        attempts[-1] = dict(attempts[-1], charged_tokens=used_tokens if used_tokens is not None else attempts[-1]["charged_tokens"],
                            usage_known=used_tokens is not None, cached_tokens=cached_tokens if used_tokens is not None else None,
                            status=outcome, completed_at=now)
        updated = dict(body, phase=outcome, attempts=attempts, reservation=False,
                       local_active=False, external_active=False, output_ref=output_ref,
                       active_seconds=body["active_seconds"] + max(0, now - attempts[-1]["at"]))
        return tx.put(OPERATION, operation_id, updated, expected_version=record["version"])["body"]

    def record_unaccepted_tx(self, tx: Transaction, operation_id: str, owner: str,
                             generation: int, *, used_tokens: int | None,
                             output_ref: str, cached_tokens: int = 0) -> dict:
        """Release a known external result without accepting scientific work."""
        self._check_usage("completed", used_tokens, cached_tokens)
        now = self.store.clock()
        record = self._owned(tx, operation_id, owner, generation, now)
        body = record["body"]
        if body["phase"] != "dispatched":
            raise StateConflict("Only dispatched work may record an unaccepted result")
        attempts = list(body["attempts"])
        attempts[-1] = dict(attempts[-1],
                            charged_tokens=used_tokens if used_tokens is not None else attempts[-1]["charged_tokens"],
                            usage_known=used_tokens is not None,
                            cached_tokens=cached_tokens if used_tokens is not None else None,
                            status="known_unaccepted", completed_at=now)
        updated = dict(body, phase="known_unaccepted", attempts=attempts,
                       reservation=False, local_active=False, external_active=False,
                       output_ref=output_ref, owner=None, lease_until=None,
                       active_seconds=body["active_seconds"] + max(0, now - attempts[-1]["at"]),
                       diagnostic="local_acceptance_rejected")
        return tx.put(OPERATION, operation_id, updated,
                      expected_version=record["version"])["body"]

    @staticmethod
    def _check_usage(outcome: str, used_tokens: int | None, cached_tokens: int) -> None:
        if outcome not in {"completed", "failed"} or (used_tokens is not None and (type(used_tokens) is not int or used_tokens < 0)):
            raise StateError("Settlement needs a confirmed outcome and nonnegative usage")
        if type(cached_tokens) is not int or cached_tokens < 0 or (used_tokens is None and cached_tokens != 0) or (used_tokens is not None and cached_tokens > used_tokens):
            raise StateError("Cached tokens must be included in known total usage")

    def rate_limited(self, operation_id: str, owner: str, generation: int, *, scope: str,
                     retry_after: float, source: str = "retry_after",
                     used_tokens: int | None = None) -> dict:
        """Persist 429 and wake; no model call or local permit remains active."""
        if scope not in self.policy.quotas or not math.isfinite(retry_after) or retry_after < 0 or source not in {"retry_after", "reset"}:
            raise StateError("Rate limit needs configured scope and finite delay")
        if used_tokens is not None and (type(used_tokens) is not int or used_tokens < 0):
            raise StateError("Rate-limit usage must be a nonnegative observed total")
        now = self.store.clock()

        def save(tx):
            record = self._owned(tx, operation_id, owner, generation, now)
            body = record["body"]
            if body["phase"] != "dispatched" or scope not in body["quota_scopes"]:
                raise StateConflict("Rate limit must match a dispatched call and scope")
            attempts = list(body["attempts"])
            attempts[-1] = dict(attempts[-1],
                                charged_tokens=used_tokens if used_tokens is not None else attempts[-1]["charged_tokens"],
                                usage_known=used_tokens is not None,
                                cached_tokens=0 if used_tokens is not None else None,
                                status="rate_limited", completed_at=now)
            wake = now + retry_after
            block = tx.get(QUOTA, scope)
            old_until = block["body"]["blocked_until"] if block else 0
            blocked_until = max(wake, old_until or 0)
            prior_snapshot = block["body"] if block else {}
            quota = tx.put(QUOTA, scope, {**prior_snapshot, "blocked_until": blocked_until,
                                          "explicit_block_until": blocked_until,
                                          "source": source, "rate_limited_at": now,
                                          "external_headroom": prior_snapshot.get("external_headroom", "unknown")},
                           expected_version=block["version"] if block else 0)
            self._record_inherited_quota_tx(
                tx, scope, operation_id, quota,
                {"blocked_until": wake, "source": source},
            )
            exhausted = sum(a["status"] == "rate_limited" for a in attempts) > body["retry_limit"]
            updated = dict(body, phase="failed" if exhausted else "waiting", attempts=attempts,
                           wake_at=None if exhausted else blocked_until,
                           reservation=False, local_active=False, external_active=False,
                           owner=None, lease_until=None,
                           active_seconds=body["active_seconds"] + max(0, now - attempts[-1]["at"]),
                           wait_started_at=None if exhausted else now,
                           wait_reason=None if exhausted else "provider",
                           diagnostic="transport_retry_limit" if exhausted else "provider_rate_limit")
            return tx.put(OPERATION, operation_id, updated, expected_version=record["version"])["body"]

        self.store.transact(f"capacity-429-{operation_id}-{generation}", {"owner": owner, "generation": generation, "scope": scope, "retry_after": retry_after, "source": source, "used_tokens": used_tokens}, save)
        return self.get(operation_id)

    def observe_headers(self, scope: str, *, remaining_requests: int | None,
                        remaining_tokens: int | None, reset_after: float | None = None,
                        reset_after_requests: float | None = None,
                        reset_after_tokens: float | None = None,
                        token_limit: int | None = None,
                        observed_at: float | None = None,
                        source_operation_id: str | None = None,
                        observation_id: str | None = None) -> dict:
        """Save provider headroom with independent expiry and exact replay identity.

        `reset_after` retains the earlier shared-clock API for existing callers.
        A provider snapshot can delay an admission but cannot grant one past a
        local configured ceiling or a later explicit Retry-After block.
        """
        if scope not in self.policy.quotas:
            raise StateError("Observed limit needs a configured scope")
        if any(value is not None and (type(value) is not int or value < 0)
               for value in (remaining_requests, remaining_tokens, token_limit)):
            raise StateError("Remaining capacity must be nonnegative integers")
        if any(
            value is not None and (not math.isfinite(value) or value < 0)
            for value in (reset_after, reset_after_requests, reset_after_tokens)
        ):
            raise StateError("Reset delay must be finite and nonnegative")
        if observed_at is not None and (not math.isfinite(observed_at) or observed_at < 0):
            raise StateError("Observation time must be finite and nonnegative")
        if source_operation_id is not None and not source_operation_id:
            raise StateError("Observation source operation must be identified")
        now = self.store.clock() if observed_at is None else observed_at
        requests_delay = reset_after_requests if reset_after_requests is not None else reset_after
        tokens_delay = reset_after_tokens if reset_after_tokens is not None else reset_after

        def save(tx):
            record = tx.get(QUOTA, scope)
            previous = record["body"] if record else {}
            source_operation = tx.get(OPERATION, source_operation_id) if source_operation_id else None
            source_attempt_count = (
                len(source_operation["body"]["attempts"])
                if source_operation and scope in source_operation["body"]["quota_scopes"]
                else None
            )
            inherited_constraint = {
                "blocked_until": 0,
                "observed_at": now,
                "source_operation_id": source_operation_id,
                "source_attempt_count": source_attempt_count,
                "remaining_requests": remaining_requests,
                "remaining_tokens": remaining_tokens,
                "observed_token_limit": token_limit,
                "requests_reset_at": now + requests_delay if requests_delay is not None else None,
                "tokens_reset_at": now + tokens_delay if tokens_delay is not None else None,
            }
            if previous.get("observed_at", -1) > now:
                self._record_inherited_quota_tx(
                    tx, scope, source_operation_id, None,
                    inherited_constraint, observation_id,
                )
                return previous
            explicit_block = previous.get("explicit_block_until")
            if explicit_block is None:
                explicit_block = previous.get("blocked_until", 0) if previous.get("source") in {
                    "retry_after", "reset"
                } else 0
            body = {
                "blocked_until": explicit_block,
                "explicit_block_until": explicit_block,
                "source": "headers",
                "observed_at": now,
                "source_operation_id": source_operation_id,
                "source_attempt_count": source_attempt_count,
                "remaining_requests": remaining_requests,
                "remaining_tokens": remaining_tokens,
                "observed_token_limit": token_limit if token_limit is not None
                else _saved_token_limit(tx, previous),
                "requests_reset_at": now + requests_delay if requests_delay is not None else None,
                "tokens_reset_at": now + tokens_delay if tokens_delay is not None else None,
                "reset_after": reset_after,
                "external_headroom": "observed_snapshot",
            }
            quota = tx.put(QUOTA, scope, body, expected_version=record["version"] if record else 0)
            self._record_inherited_quota_tx(
                tx, scope, source_operation_id, quota,
                inherited_constraint, observation_id,
            )
            return quota["body"]

        request = {"scope": scope, "requests": remaining_requests,
                   "tokens": remaining_tokens, "request_reset": requests_delay,
                   "token_reset": tokens_delay, "source_operation_id": source_operation_id,
                   "at": now}
        # Keep old receipt replay identities unchanged; the richer observation
        # gets a distinct idempotency key so an earlier host's saved operation
        # cannot conflict when its rate headers are revisited.
        operation_prefix = "capacity-header-"
        if token_limit is not None:
            operation_prefix = "capacity-header-v2-"
            request["token_limit"] = token_limit
        return self.store.transact(
            f"{operation_prefix}{scope}-{observation_id or uuid.uuid4().hex}", request, save
        )

    def observe_rate_headers(
        self, scope: str, headers: Mapping[str, str], *,
        source_operation_id: str, observation_id: str, observed_at: float,
    ) -> dict | None:
        """Interpret the documented Groq request/day and token/minute headers."""
        requests = _remaining(headers.get("x-ratelimit-remaining-requests"))
        tokens = _remaining(headers.get("x-ratelimit-remaining-tokens"))
        token_limit = _remaining(headers.get("x-ratelimit-limit-tokens"))
        if requests is None and tokens is None and token_limit is None:
            return None
        return self.observe_headers(
            scope,
            remaining_requests=requests,
            remaining_tokens=tokens,
            token_limit=token_limit,
            reset_after_requests=_reset_seconds(headers.get("x-ratelimit-reset-requests")),
            reset_after_tokens=_reset_seconds(headers.get("x-ratelimit-reset-tokens")),
            observed_at=observed_at,
            source_operation_id=source_operation_id,
            observation_id=observation_id,
        )

    def timeout(self, operation_id: str, owner: str, generation: int) -> dict:
        """A local timeout frees its worker slot but keeps external uncertainty."""
        now = self.store.clock()

        def save(tx):
            record = self._owned(tx, operation_id, owner, generation, now)
            body = record["body"]
            if body["phase"] != "dispatched":
                raise StateConflict("Only dispatched work may time out")
            updated = dict(body, phase="uncertain", owner=None, lease_until=None,
                           generation=generation + 1, local_active=False,
                           external_active=True, diagnostic="external_outcome_unknown")
            return tx.put(OPERATION, operation_id, updated, expected_version=record["version"])["body"]

        self.store.transact(f"capacity-timeout-{operation_id}-{generation}", {"owner": owner, "generation": generation}, save)
        return self.get(operation_id)

    def heartbeat(self, operation_id: str, owner: str, generation: int, *,
                  lease_seconds: float, useful_progress: bool = False) -> dict:
        """Renew live ownership; liveness and useful progress remain distinct."""
        if not math.isfinite(lease_seconds) or lease_seconds <= 0 or type(useful_progress) is not bool:
            raise StateError("Heartbeat needs positive lease and explicit progress state")
        now = self.store.clock()

        def save(tx):
            record = self._owned(tx, operation_id, owner, generation, now)
            body = record["body"]
            if body["phase"] not in {"admitted", "dispatched", "uncertain"}:
                raise StateConflict("Only active ownership may heartbeat")
            updated = dict(body, lease_until=now + lease_seconds, last_heartbeat_at=now,
                           last_progress_at=now if useful_progress else body["last_progress_at"])
            return tx.put(OPERATION, operation_id, updated, expected_version=record["version"])["body"]

        return self.store.transact(f"capacity-heartbeat-{operation_id}-{uuid.uuid4().hex}",
                                   {"owner": owner, "generation": generation,
                                    "lease_seconds": lease_seconds, "useful_progress": useful_progress,
                                    "at": now}, save)

    def diagnose_stalls(self, *, after_seconds: float) -> list[str]:
        """Save a replan opportunity; an elapsed category is never a timeout."""
        if not math.isfinite(after_seconds) or after_seconds <= 0:
            raise StateError("Diagnosis threshold must be positive and finite")
        now = self.store.clock()

        def save(tx):
            diagnosed = []
            for record in _heads(tx, OPERATION):
                body = record["body"]
                if (body["phase"] != "dispatched" or body["lease_until"] <= now
                        or body["last_heartbeat_at"] <= body["admitted_at"]
                        or now - body["last_progress_at"] < after_seconds
                        or body["diagnostic"] == "live_worker_no_progress"):
                    continue
                updated = dict(body, diagnostic="live_worker_no_progress", diagnosis_at=now)
                tx.put(OPERATION, record["id"], updated, expected_version=record["version"])
                diagnosed.append(record["id"])
            return {"diagnosed": diagnosed}

        return self.store.transact(f"capacity-diagnose-{uuid.uuid4().hex}",
                                   {"after_seconds": after_seconds, "at": now}, save)["diagnosed"]

    def transport_failure(self, operation_id: str, owner: str, generation: int, *,
                          classification: str, retry_after: float = 0) -> dict:
        """Only confirmed no-effect failures may retry; uncertainty must reconcile."""
        if classification == "uncertain":
            return self.timeout(operation_id, owner, generation)
        if classification == "terminal":
            return self.settle(operation_id, owner, generation, used_tokens=None, outcome="failed")
        if classification != "temporary_no_effect" or not math.isfinite(retry_after) or retry_after < 0:
            raise StateError("Transport failure needs temporary_no_effect, terminal or uncertain")
        now = self.store.clock()

        def save(tx):
            record = self._owned(tx, operation_id, owner, generation, now)
            body = record["body"]
            if body["phase"] != "dispatched":
                raise StateConflict("Only dispatched work may report transport failure")
            attempts = list(body["attempts"])
            attempts[-1] = dict(attempts[-1], charged_tokens=0, usage_known=True,
                                cached_tokens=0, status="temporary_no_effect", completed_at=now)
            retries = sum(a["status"] in {"temporary_no_effect", "rate_limited"} for a in attempts)
            exhausted = retries > body["retry_limit"]
            updated = dict(body, phase="failed" if exhausted else "waiting", attempts=attempts,
                           wake_at=None if exhausted else now + retry_after,
                           reservation=False, local_active=False, external_active=False,
                           owner=None, lease_until=None,
                           active_seconds=body["active_seconds"] + max(0, now - attempts[-1]["at"]),
                           wait_started_at=None if exhausted else now,
                           wait_reason=None if exhausted else "retry",
                           diagnostic="transport_retry_limit" if exhausted else "temporary_transport_failure")
            return tx.put(OPERATION, operation_id, updated, expected_version=record["version"])["body"]

        self.store.transact(f"capacity-transport-{operation_id}-{generation}",
                            {"owner": owner, "generation": generation,
                             "classification": classification, "retry_after": retry_after}, save)
        return self.get(operation_id)

    def expire(self) -> list[dict]:
        """Recover unused admissions; retain dispatched external reservations."""
        now = self.store.clock()

        def save(tx):
            changed = []
            for record in _heads(tx, OPERATION):
                body = record["body"]
                if body["phase"] not in {"admitted", "dispatched"} or body["lease_until"] is None or body["lease_until"] > now:
                    continue
                if body["phase"] == "admitted":
                    updated = dict(body, phase="queued", reservation=False, local_active=False,
                                   external_active=False, queued_at=now,
                                   diagnostic="owner_lost_before_dispatch")
                else:
                    updated = dict(body, phase="uncertain", reservation=True, local_active=False,
                                   external_active=True, diagnostic="external_outcome_unknown")
                updated.update(owner=None, lease_until=None, generation=body["generation"] + 1)
                tx.put(OPERATION, record["id"], updated, expected_version=record["version"])
                changed.append({"operation_id": record["id"], "phase": updated["phase"]})
            return {"changed": changed}

        return self.store.transact(f"capacity-expire-{uuid.uuid4().hex}", {"at": now}, save)["changed"]

    def claim_uncertain(self, operation_id: str, owner: str, *, lease_seconds: float = 60) -> dict:
        """Give one new owner reconciliation authority, without freeing capacity."""
        if not owner or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise StateError("Reconciliation requires owner and positive lease")
        now = self.store.clock()

        def save(tx):
            record = tx.get(OPERATION, operation_id)
            if not record or record["body"]["phase"] != "uncertain" or (record["body"]["lease_until"] is not None and record["body"]["lease_until"] > now):
                raise StateConflict("External result is not available for reconciliation")
            body = dict(record["body"], owner=owner, generation=record["body"]["generation"] + 1,
                        lease_until=now + lease_seconds)
            return tx.put(OPERATION, operation_id, body, expected_version=record["version"])["body"]

        return self.store.transact(f"capacity-reconcile-claim-{operation_id}-{uuid.uuid4().hex}", {"owner": owner, "at": now}, save)

    def reconcile(self, operation_id: str, owner: str, generation: int, *, effect: str,
                  used_tokens: int | None = None, output_ref: str | None = None,
                  cached_tokens: int = 0) -> dict:
        """Confirmed completion/failure/no effect releases uncertain external capacity."""
        if effect not in {"completed", "failed", "no_effect", "known_unaccepted"}:
            raise StateError("Reconciliation needs confirmed effect and valid usage")
        if effect == "known_unaccepted" and not output_ref:
            raise StateError("Unaccepted reconciliation needs an exact observation ID")
        self._check_usage("completed", used_tokens, cached_tokens)
        now = self.store.clock()

        def save(tx):
            record = self._owned(tx, operation_id, owner, generation, now)
            body = record["body"]
            if body["phase"] != "uncertain":
                raise StateConflict("Only uncertain work may be reconciled")
            attempts = list(body["attempts"])
            attempts[-1] = dict(attempts[-1], status=effect,
                                charged_tokens=used_tokens if used_tokens is not None else attempts[-1]["charged_tokens"],
                                usage_known=used_tokens is not None,
                                cached_tokens=cached_tokens if used_tokens is not None else None,
                                completed_at=now)
            phase = "queued" if effect == "no_effect" and not body["cancel_requested"] else ("canceled" if effect == "no_effect" else effect)
            updated = dict(body, phase=phase, attempts=attempts, reservation=False,
                           local_active=False, external_active=False, output_ref=output_ref,
                           owner=None, lease_until=None,
                           queued_at=now if phase == "queued" else body["queued_at"])
            return tx.put(OPERATION, operation_id, updated, expected_version=record["version"])["body"]

        return self.store.transact(f"capacity-reconciled-{operation_id}-{generation}", {"owner": owner, "generation": generation, "effect": effect, "used_tokens": used_tokens, "cached_tokens": cached_tokens, "output_ref": output_ref}, save)

    def settle_known_uncertain_tx(
        self, tx: Transaction, operation_id: str, owner: str, generation: int, *,
        outcome: str, output_ref: str, used_tokens: int | None = None,
        cached_tokens: int = 0,
    ) -> dict:
        """Settle a recovered effect alongside its accepted or unaccepted record.

        The caller must hold a fresh reconciliation claim and save the exact
        result in this same transaction. An uncertain operation is never
        requeued as a claimed no-effect when its result has been found.
        """
        if outcome not in {"completed", "known_unaccepted"} or not output_ref:
            raise StateError("Known uncertain effect needs a saved output and outcome")
        self._check_usage("completed", used_tokens, cached_tokens)
        now = self.store.clock()
        record = self._owned(tx, operation_id, owner, generation, now)
        body = record["body"]
        if body["phase"] != "uncertain":
            raise StateConflict("Only uncertain work may use known-result settlement")
        if outcome == "completed" and body["cancel_requested"]:
            raise StateConflict("Canceled uncertain work cannot accept a late result")
        attempts = list(body["attempts"])
        if not attempts or attempts[-1]["status"] not in {"dispatched", "uncertain"}:
            raise StateConflict("Uncertain work has no dispatched attempt to reconcile")
        attempts[-1] = dict(
            attempts[-1], status=outcome,
            charged_tokens=used_tokens if used_tokens is not None else attempts[-1]["charged_tokens"],
            usage_known=used_tokens is not None,
            cached_tokens=cached_tokens if used_tokens is not None else None,
            completed_at=now,
        )
        updated = dict(
            body, phase=outcome, attempts=attempts, reservation=False,
            local_active=False, external_active=False, output_ref=output_ref,
            owner=None, lease_until=None,
        )
        return tx.put(OPERATION, operation_id, updated, expected_version=record["version"])["body"]

    def cancel(self, operation_id: str, *, reason: str = "user_cancel") -> dict:
        """Stop future dispatch; retain an already dispatched uncertain effect."""
        if reason not in {"user_cancel", "hard_limit", "branch_cancel"}:
            raise StateError("Cancellation reason must be a known status code")
        now = self.store.clock()

        def save(tx):
            record = tx.get(OPERATION, operation_id)
            if not record:
                raise StateError("Unknown capacity operation")
            body = record["body"]
            if body["cancel_requested"] or body["phase"] in {"completed", "known_unaccepted", "failed", "canceled"}:
                return body
            if body["phase"] in {"queued", "waiting", "admitted"}:
                updated = dict(body, phase="canceled", reservation=False, local_active=False,
                               external_active=False, owner=None, lease_until=None,
                               generation=body["generation"] + 1, cancel_requested=True, diagnostic=reason)
            else:
                updated = dict(body, cancel_requested=True, diagnostic=reason)
            return tx.put(OPERATION, operation_id, updated, expected_version=record["version"])["body"]

        return self.store.transact(f"capacity-cancel-{operation_id}-{uuid.uuid4().hex}", {"reason": reason, "at": now}, save)

    def due(self) -> list[str]:
        """Read saved wake conditions after restart; duplicate reads have no effect."""
        now = self.store.clock()
        due = []
        after = ""
        while True:
            records = self.store.records(OPERATION, limit=500, after_id=after)
            for record in records:
                item = record["body"]
                if item["phase"] == "waiting" and item["wake_at"] <= now:
                    if all((block := self.store.get(QUOTA, scope)) is None or (block["body"]["blocked_until"] is not None and block["body"]["blocked_until"] <= now)
                           for scope in item["quota_scopes"]):
                        due.append(record["id"])
            if len(records) < 500:
                break
            after = records[-1]["id"]
        return due

    def get(self, operation_id: str) -> dict | None:
        record = self.store.get(OPERATION, operation_id)
        return record["body"] if record else None
