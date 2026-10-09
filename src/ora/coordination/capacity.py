"""Durable, local admission for independent research operations.

The ledger covers calls made through this StateStore, not other consumers of a
provider account. All decisions are short SQLite transactions; external work is
performed only after ``dispatched`` has durably recorded the uncertain boundary.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from typing import Mapping

from ora.state.store import StateConflict, StateError, StateStore, Transaction

KINDS = frozenset({"model", "retrieval", "compute"})
OPERATION = "capacity_operation"
QUOTA = "capacity_quota"
CONFIG = "capacity_config"


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

        def save(tx):
            existing = tx.get(CONFIG, "default")
            if existing and existing["body"] != snapshot:
                raise StateConflict("Capacity policy changed; reconcile outstanding reservations first")
            if not existing:
                tx.put(CONFIG, "default", snapshot)
            return snapshot

        store.transact("capacity-config-v1", snapshot, save)

    def enqueue(
        self, operation_id: str, *, task_id: str, lane: str, kind: str,
        quota_scopes: tuple[str, ...] = (), estimated_tokens: int = 0,
        review: bool = False, mission_id: str = "", run_id: str = "",
        attempt_id: str = "", call_id: str = "", budget_version: str = "",
        retry_limit: int = 2,
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
        body = {
            **intent, "phase": "queued",
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
            current = tx.get(OPERATION, operation_id)
            if current:
                if any(current["body"][k] != value for k, value in intent.items()):
                    raise StateConflict("Capacity operation ID has different inputs")
                return current["body"]
            return tx.put(OPERATION, operation_id, body)["body"]

        self.store.transact(f"capacity-enqueue-{operation_id}", intent, save)
        return self.get(operation_id)

    def _quota_ready_at(self, tx: Transaction, body: dict, operations: list[dict], now: float) -> float | None:
        """Earliest known window wake; None waits on another active reservation."""
        ready = now
        for scope in body["quota_scopes"]:
            limit = self.policy.quotas[scope]
            block = tx.get(QUOTA, scope)
            if block:
                blocked_until = block["body"]["blocked_until"]
                if blocked_until is None:
                    return None
                ready = max(ready, blocked_until)
            charges = []
            pending_requests = pending_tokens = 0
            for operation in operations:
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
            def fits():
                return ((limit.requests is None or requests + 1 <= limit.requests)
                        and (limit.tokens is None or tokens + body["estimated_tokens"] <= limit.tokens))
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
            counts = {}
            for record in operations:
                item = record["body"]
                counts[item["lane"]] = counts.get(item["lane"], 0) + len(item["attempts"])
            candidates.sort(key=lambda r: (not r["body"]["review"], counts[r["body"]["lane"]], r["body"]["queued_at"], r["id"]))
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
            tx.put(QUOTA, scope, {"blocked_until": blocked_until, "source": source,
                                  "observed_at": now, "external_headroom": "unknown"},
                   expected_version=block["version"] if block else 0)
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
                        remaining_tokens: int | None, reset_after: float | None) -> dict:
        """Record observed account hints; zero headroom may delay, never grant."""
        if scope not in self.policy.quotas:
            raise StateError("Observed limit needs a configured scope")
        if any(value is not None and (type(value) is not int or value < 0)
               for value in (remaining_requests, remaining_tokens)):
            raise StateError("Remaining capacity must be nonnegative integers")
        if reset_after is not None and (not math.isfinite(reset_after) or reset_after < 0):
            raise StateError("Reset delay must be finite and nonnegative")
        now = self.store.clock()

        def save(tx):
            record = tx.get(QUOTA, scope)
            previous = record["body"]["blocked_until"] if record else 0
            exhausted = remaining_requests == 0 or remaining_tokens == 0
            if exhausted and reset_after is None:
                until = None
            elif exhausted:
                until = max(previous or 0, now + reset_after)
            else:
                until = previous if previous is not None else 0
            body = {"blocked_until": until, "source": "headers", "observed_at": now,
                    "remaining_requests": remaining_requests, "remaining_tokens": remaining_tokens,
                    "reset_after": reset_after, "external_headroom": "observed_snapshot"}
            return tx.put(QUOTA, scope, body, expected_version=record["version"] if record else 0)["body"]

        return self.store.transact(f"capacity-header-{scope}-{uuid.uuid4().hex}",
                                   {"scope": scope, "requests": remaining_requests,
                                    "tokens": remaining_tokens, "reset_after": reset_after, "at": now}, save)

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
