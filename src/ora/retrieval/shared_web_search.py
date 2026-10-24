"""Opt-in mission-scoped physical web queries with task-scoped result projections.

One executor child owns each real provider attempt. Subscriptions own only local
views and cloned hit/delivery provenance, so a producer task cannot grant another
task access or cancel a still-needed shared request.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace

from ora.contracts.models import (
    CancelSearchInput,
    ReadSearchResultsInput,
    SearchOperationResult,
    SearchWebInput,
)
from ora.coordination.board import ref
from ora.coordination.executor import WorkResult, WorkSpec
from ora.mission.shared_materials import shared_hit_access
from ora.state import StateError
from ora.state.store import canonical

from .discovery import run_discovery_page
from .providers import ROUTES
from .web_search import KIND, identity

JOB = "web_shared_query_job"
FAMILY = "web_shared_query_family"


def _hash(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class SharedWebSearch:
    def __init__(self, owner):
        self.owner = owner
        self.store = owner.store
        self.executor = owner.executor
        self.executor.handlers[("web-shared-query", "1")] = self.execute

    def _credential_binding(self, provider: str) -> str:
        name = {
            "exa": "EXA_API_KEY",
            "tavily": "TAVILY_API_KEY",
            "searxng": "OSRA_SEARXNG_BASE_URL",
        }.get(provider, "")
        return _hash(self.owner.values.get(name, ""))

    def _source_signature(self, spec: WorkSpec) -> str:
        if not spec.call_id:
            return _hash([])
        intent = self.store.get(
            "model_request_intent", f"model-intent:{spec.call_id}:{spec.call_version}"
        )
        if intent is None:
            raise StateError("Shared search lost its exact model input")
        refs = [
            intent["body"].get(key) for key in ("source_refs", "context_source_dependency_refs")
        ]
        if any(not isinstance(value, list) for value in refs):
            raise StateError("Shared search has incomplete source permission lineage")
        selected_uses = []
        for group in refs:
            for pointer in group:
                if isinstance(pointer, dict) and pointer.get("kind") == "source_copy":
                    use = self.store.resolve(
                        "source_use.copy", f"{pointer.get('id')}:{pointer.get('version')}"
                    )
                    selected_uses.append(ref(use) if use else None)
        return _hash({"sources": refs, "current_uses": selected_uses})

    def _query_key(self, spec: WorkSpec, args: dict, query: str, source_signature: str) -> str:
        provider = args["provider"]
        options = {"limit": args["limit"], "search_mode": args["mode"]}
        if provider == "exa":
            options.update(mode="auto" if args["mode"] == "research" else "fast", highlights=True)
        return identity(
            "web-shared-family-",
            spec.mission_id,
            provider,
            query,
            options,
            source_signature,
            self._credential_binding(provider),
        )

    def start(self, spec: WorkSpec, arguments: dict) -> dict:
        args = SearchWebInput.model_validate(arguments).model_dump()
        operation_id = identity("web-search-", spec.mission_id, spec.work_id)
        source_signature = self._source_signature(spec)
        body = {
            "schema": "ora.web_search.operation.v1",
            "shared_version": 1,
            "mission_id": spec.mission_id,
            "run_id": spec.run_id,
            "actor_id": spec.actor_id,
            "task_id": spec.proposal_ref["id"],
            "arguments": args,
            "parent_spec": asdict(spec),
            "source_signature": source_signature,
            "cancel_requested": False,
            "items": [],
            "deliveries": [],
            "created_at": self.store.now(),
        }
        self.owner._require_source_scope(body)

        def subscribe(tx):
            parent = tx.get("board_proposal", spec.proposal_ref["id"])
            if (
                parent is None
                or ref(parent) != spec.proposal_ref
                or parent["body"].get("status") != "open"
            ):
                raise StateError("Search source task is no longer open")
            self.owner._require_source_scope(body)
            prior = tx.get(KIND, operation_id)
            if prior is not None:
                if (
                    prior["body"].get("arguments") != args
                    or prior["body"].get("source_signature") != source_signature
                ):
                    raise StateError("Search invocation cannot be reused with changed inputs")
                return {
                    "operation_id": operation_id,
                    "reuse_status": "reused",
                    "job_ids": prior["body"]["items"],
                }
            jobs = []
            any_new = False
            for index, query in enumerate(args["queries"]):
                family_id = self._query_key(spec, args, query, source_signature)
                family = tx.get(FAMILY, family_id)
                previous = tx.get(JOB, family["body"]["job_id"]) if family else None
                if (
                    family is not None
                    and not args["refresh"]
                    and previous is not None
                    and previous["body"]["phase"] != "known_no_dispatch"
                ):
                    job_id = family["body"]["job_id"]
                    job = previous
                else:
                    any_new = True
                    job_id = identity("web-shared-job-", family_id, operation_id)
                    job = tx.put(
                        JOB,
                        job_id,
                        {
                            "schema": "ora.web_search.shared_job.v1",
                            "mission_id": spec.mission_id,
                            "provider": args["provider"],
                            "mode": args["mode"],
                            "query": query,
                            "limit": args["limit"],
                            "source_signature": source_signature,
                            "credential_binding": self._credential_binding(args["provider"]),
                            "owner_spec": asdict(spec),
                            "subscribers": [],
                            "phase": "pending",
                            "result": None,
                            "created_at": self.store.now(),
                        },
                    )
                    tx.put(
                        FAMILY,
                        family_id,
                        {"job_id": job_id, "mission_id": spec.mission_id},
                        expected_version=family["version"] if family else 0,
                    )
                    tx.emit(spec.mission_id, "web_shared_query_created", {"job_ref": ref(job)})
                subscribers = list(job["body"]["subscribers"])
                if not any(x["operation_id"] == operation_id for x in subscribers):
                    subscribers.append({"operation_id": operation_id, "query_index": index})
                    tx.put(
                        JOB,
                        job_id,
                        {**job["body"], "subscribers": subscribers},
                        expected_version=job["version"],
                    )
                jobs.append(job_id)
            tx.put(KIND, operation_id, {**body, "items": jobs})
            tx.emit(
                spec.mission_id,
                "web_search_started",
                {"operation_id": operation_id, "shared_queries": len(jobs)},
            )
            return {
                "operation_id": operation_id,
                "reuse_status": "refreshed" if args["refresh"] else "new" if any_new else "reused",
                "job_ids": jobs,
            }

        chosen = self.store.transact(
            "web-shared-start:" + operation_id,
            {"spec": asdict(spec), "arguments": args, "source_signature": source_signature},
            subscribe,
            mission_id=spec.mission_id,
        )
        self.owner._require_source_scope(body)
        for job_id in dict.fromkeys(chosen["job_ids"]):
            self._submit_job(job_id)
        view = self.read(spec, {"operation_id": operation_id, "wait": True})
        return {**view, "reuse_status": chosen["reuse_status"]}

    def _submit_job(self, job_id: str) -> None:
        job = self.store.get(JOB, job_id)
        if job is None or job["body"]["phase"] != "pending":
            return
        if any(
            work["body"]["spec"]["work_id"] == job_id
            for work in self.executor._work(job["body"]["mission_id"])
        ):
            return
        owner = job["body"]["owner_spec"]
        proposed = self.store.get("board_proposal", job_id)
        if proposed is None:
            self.executor.board.propose(
                "web-shared-propose:" + job_id,
                mission_id=job["body"]["mission_id"],
                proposal_id=job_id,
                actor_id=owner["actor_id"],
                objective="Execute one shared selected web query",
                expected_artifact="Shared retrieval receipt",
                next_action="Run selected provider",
                branch_of=None,
            )
            proposed = self.store.get("board_proposal", job_id)
            if proposed is None or proposed["body"].get("mission_id") != job["body"]["mission_id"]:
                raise StateError("Shared query proposal could not be saved")
        provider = job["body"]["provider"]
        scopes = (
            ("web:tavily", "web:tavily:minute") if provider == "tavily" else (f"web:{provider}",)
        )
        child = WorkSpec(
            work_id=job_id,
            mission_id=job["body"]["mission_id"],
            run_id=owner["run_id"],
            actor_id=owner["actor_id"],
            proposal_ref=ref(proposed),
            configuration_ref=owner["configuration_ref"],
            input_refs=(),
            handler_id="web-shared-query",
            handler_version="1",
            kind="retrieval",
            lane=owner["actor_id"],
            quota_scopes=scopes,
            attempt_id=job_id,
        )
        self.executor.submit(child)

    def _active_subscribers(self, job: dict) -> list[dict]:
        from ora.mission.context import ContextError

        active = []
        for subscriber in job["body"]["subscribers"]:
            operation = self.store.get(KIND, subscriber["operation_id"])
            if operation is None or operation["body"].get("cancel_requested"):
                continue
            parent = self.store.get("board_proposal", operation["body"]["task_id"])
            if parent is None or parent["body"].get("status") != "open":
                continue
            try:
                self.owner._require_source_scope(operation["body"])
                if (
                    self._source_signature(WorkSpec(**operation["body"]["parent_spec"]))
                    != job["body"]["source_signature"]
                ):
                    continue
            except (ContextError, StateError):
                continue
            active.append(operation)
        return active

    @staticmethod
    def _mark_no_dispatch(tx, job_id: str) -> dict:
        job = tx.get(JOB, job_id)
        if job is not None and job["body"].get("result") is None:
            return tx.put(
                JOB,
                job_id,
                {**job["body"], "phase": "known_no_dispatch"},
                expected_version=job["version"],
            )
        if job is None:
            raise StateError("Shared web query is missing")
        return job

    def execute(self, spec: WorkSpec) -> WorkResult:
        job = self.store.get(JOB, spec.work_id)
        if job is None or job["body"].get("mission_id") != spec.mission_id:
            raise StateError("Shared web query is missing")
        if job["body"].get("result") is not None:
            return WorkResult(payload={"shared_job_ref": ref(job)})
        active = self._active_subscribers(job)
        if not active:
            self.store.transact(
                "web-shared-no-dispatch:" + spec.work_id,
                {"job_id": spec.work_id},
                lambda tx: self._mark_no_dispatch(tx, spec.work_id),
                mission_id=spec.mission_id,
            )
            return WorkResult(disposition="terminal")
        body = job["body"]
        provider = body["provider"]
        if self._credential_binding(provider) != body["credential_binding"]:
            return WorkResult(disposition="terminal")
        credential = self.owner.values.get(
            {"exa": "EXA_API_KEY", "tavily": "TAVILY_API_KEY"}.get(provider, "")
        )
        if (
            provider in {"exa", "tavily"}
            and not credential
            or provider == "searxng"
            and not self.owner.values.get("OSRA_SEARXNG_BASE_URL")
        ):
            result = {
                "outcome": "unavailable",
                "reason": "optional_provider_not_configured",
                "hits": [],
            }
        else:
            cap = self.executor.capacity.get(self.executor._capacity_id(spec))
            generation = cap["generation"]
            request = {
                "mission_id": spec.mission_id,
                "task_id": spec.work_id,
                "agent_instance_id": active[0]["body"]["actor_id"],
                "request_id": spec.work_id,
                "attempt_id": f"{spec.work_id}:{generation}",
                "route": provider + ".search",
                "provider": provider,
                "index": ROUTES[provider + ".search"]["index"],
                "operation": "search",
                "query": body["query"],
                "filters": {"mode": "auto", "highlights": True} if provider == "exa" else {},
                "requested_at": body["created_at"],
                "requested_window": {"limit": body["limit"]},
            }
            result = run_discovery_page(
                self.store, request, credential=credential, transport=self.owner._http
            )
        if result.get("outcome") == "quota_wait" or any(
            leg.get("status") in {429, 503} for leg in result.get("http_legs", [])
        ):
            until = result.get("retry_at")
            seconds = (
                max(
                    1,
                    datetime.fromisoformat(until.replace("Z", "+00:00")).timestamp()
                    - self.store.clock(),
                )
                if until
                else 60
            )
            return WorkResult(
                disposition="rate_limited", retry_after=seconds, rate_scope=f"web:{provider}"
            )

        def save(tx):
            current = tx.get(JOB, spec.work_id)
            if current["body"].get("result") is None:
                saved = tx.put(
                    JOB,
                    spec.work_id,
                    {**current["body"], "phase": "completed", "result": result},
                    expected_version=current["version"],
                )
                tx.emit(spec.mission_id, "web_shared_query_completed", {"job_ref": ref(saved)})
                return {"shared_job_ref": ref(saved)}
            return {"shared_job_ref": ref(current)}

        saved = self.store.transact(
            "web-shared-result:" + spec.work_id,
            {"job_id": spec.work_id, "result": result},
            save,
            mission_id=spec.mission_id,
        )
        self._ensure_fallback(self.store.get(JOB, spec.work_id))
        return WorkResult(payload=saved)

    def _ensure_fallback(self, exa_job: dict) -> None:
        if (
            exa_job is None
            or exa_job["body"]["provider"] != "exa"
            or not self.owner.values.get("TAVILY_API_KEY")
        ):
            return
        result = exa_job["body"].get("result")
        if not isinstance(result, dict) or not self.owner._should_fallback(
            "exa", "quick", result, "tavily"
        ):
            return
        created_jobs = []
        for subscriber in exa_job["body"]["subscribers"]:
            operation_id = subscriber["operation_id"]
            operation = self.store.get(KIND, operation_id)
            if (
                operation is None
                or operation["body"]["cancel_requested"]
                or operation["body"]["arguments"]["mode"] != "quick"
            ):
                continue
            try:
                self.owner._require_source_scope(operation["body"])
            except StateError:
                continue
            body = operation["body"]
            fallback_args = {**body["arguments"], "provider": "tavily"}
            family_id = self._query_key(
                SimpleNamespace(mission_id=body["mission_id"]),
                fallback_args,
                exa_job["body"]["query"],
                body["source_signature"],
            )

            def attach(tx):
                current = tx.get(KIND, operation_id)
                if current is None or current["body"]["cancel_requested"]:
                    return {"job_id": None}
                family = tx.get(FAMILY, family_id)
                previous = tx.get(JOB, family["body"]["job_id"]) if family else None
                if (
                    family is not None
                    and not body["arguments"]["refresh"]
                    and previous is not None
                    and previous["body"]["phase"] != "known_no_dispatch"
                ):
                    job_id = family["body"]["job_id"]
                    job = previous
                else:
                    job_id = identity("web-shared-job-", family_id, operation_id)
                    job = tx.put(
                        JOB,
                        job_id,
                        {
                            "schema": "ora.web_search.shared_job.v1",
                            "mission_id": body["mission_id"],
                            "provider": "tavily",
                            "mode": "quick",
                            "query": exa_job["body"]["query"],
                            "limit": body["arguments"]["limit"],
                            "source_signature": body["source_signature"],
                            "credential_binding": self._credential_binding("tavily"),
                            "owner_spec": body["parent_spec"],
                            "subscribers": [],
                            "phase": "pending",
                            "result": None,
                            "created_at": self.store.now(),
                        },
                    )
                    tx.put(
                        FAMILY,
                        family_id,
                        {"job_id": job_id, "mission_id": body["mission_id"]},
                        expected_version=family["version"] if family else 0,
                    )
                    tx.emit(body["mission_id"], "web_shared_query_created", {"job_ref": ref(job)})
                subs = list(job["body"]["subscribers"])
                if not any(x["operation_id"] == operation_id for x in subs):
                    subs.append(
                        {
                            "operation_id": operation_id,
                            "query_index": subscriber["query_index"],
                            "fallback_of": exa_job["id"],
                        }
                    )
                    tx.put(
                        JOB,
                        job_id,
                        {**job["body"], "subscribers": subs},
                        expected_version=job["version"],
                    )
                if job_id not in current["body"]["items"]:
                    tx.put(
                        KIND,
                        operation_id,
                        {**current["body"], "items": [*current["body"]["items"], job_id]},
                        expected_version=current["version"],
                    )
                return {"job_id": job_id}

            chosen = self.store.transact(
                "web-shared-fallback:" + operation_id + ":" + exa_job["id"],
                {"operation_id": operation_id, "exa_job_id": exa_job["id"]},
                attach,
                mission_id=body["mission_id"],
            )
            if chosen["job_id"]:
                created_jobs.append(chosen["job_id"])
        for job_id in dict.fromkeys(created_jobs):
            self._submit_job(job_id)

    def _project(self, operation: dict, job: dict, index: int) -> dict:
        """Create one local delivery with per-task hits linked to physical receipts."""
        body = operation["body"]
        result = job["body"].get("result") or {"outcome": "uncertain_dispatch", "hits": []}
        projection_id = identity("web-shared-projection-", operation["id"], job["id"])

        def save(tx):
            current = tx.get(KIND, operation["id"])
            self.owner._require_source_scope(current["body"])
            previous = next(
                (x for x in current["body"]["deliveries"] if x["item_id"] == job["id"]), None
            )
            if previous is not None:
                return previous
            projected = []
            for candidate in result.get("hits", []):
                pointer = candidate.get("hit_ref")
                original = tx.get(**pointer) if isinstance(pointer, dict) else None
                if original is None or not shared_hit_access(tx, original):
                    continue
                original_delivery = tx.get(**original["body"]["delivery_ref"])
                if original_delivery is None:
                    continue
                delivery_id = identity(
                    "web-shared-delivery-", operation["id"], job["id"], original_delivery["id"]
                )
                delivery = tx.get("retrieval_delivery", delivery_id)
                if delivery is None:
                    delivery = tx.put(
                        "retrieval_delivery",
                        delivery_id,
                        {
                            **original_delivery["body"],
                            "shared_projection": True,
                            "shared_origin_ref": ref(original_delivery),
                            "consumer_scope": {
                                k: body[k] for k in ("mission_id", "run_id", "actor_id", "task_id")
                            },
                        },
                    )
                    tx.link(delivery, original_delivery, "derived_from")
                hit_id = identity("retrieval-hit-shared-", operation["id"], original["id"])
                hit = tx.get("retrieval_hit", hit_id)
                if hit is None:
                    hit = tx.put(
                        "retrieval_hit",
                        hit_id,
                        {
                            **original["body"],
                            "task_id": body["task_id"],
                            "agent_instance_id": body["actor_id"],
                            "request_id": operation["id"],
                            "delivery_ref": ref(delivery),
                            "shared_origin_ref": ref(original),
                        },
                    )
                    tx.link(hit, delivery, "derived_from")
                    tx.link(hit, original, "derived_from")
                projected.append({**candidate, "hit_ref": ref(hit)})
            subscriber = next(
                (x for x in job["body"]["subscribers"] if x["operation_id"] == operation["id"]), {}
            )
            delivery = {
                "item_id": job["id"],
                "provider": job["body"]["provider"],
                "query": job["body"]["query"],
                "information_need": body["arguments"]["information_need"],
                "result": {
                    **result,
                    "hits": projected,
                    "shared_job_ref": ref(job),
                    "shared_projection": True,
                },
                "completed_at": self.store.now(),
                "fallback_of": subscriber.get("fallback_of"),
                "query_index": index,
            }
            tx.put(
                KIND,
                operation["id"],
                {**current["body"], "deliveries": [*current["body"]["deliveries"], delivery]},
                expected_version=current["version"],
            )
            tx.emit(
                body["mission_id"],
                "web_search_progress",
                {"operation_id": operation["id"], "shared_job_ref": ref(job)},
            )
            return delivery

        return self.store.transact(
            "web-shared-project:" + projection_id,
            {"operation_id": operation["id"], "job_id": job["id"]},
            save,
            mission_id=body["mission_id"],
        )

    def read(self, spec: WorkSpec, arguments: dict) -> dict:
        args = ReadSearchResultsInput.model_validate(arguments)
        operation = self.owner._get(spec, args.operation_id)
        body = operation["body"]
        self.owner._require_source_scope(body)
        if self._source_signature(WorkSpec(**body["parent_spec"])) != body["source_signature"]:
            raise StateError("Search source permission binding changed")
        if body["shared_version"] != 1:
            raise StateError("Search operation is not a shared query subscription")
        if not body["cancel_requested"]:
            for job_id in body["items"]:
                self._submit_job(job_id)
                job = self.store.get(JOB, job_id)
                if job and job["body"].get("result") is not None:
                    self._ensure_fallback(job)
                    operation = self.owner._get(spec, args.operation_id)
                    body = operation["body"]
                    index = body["items"].index(job_id)
                    self._project(operation, job, index)
                    operation = self.owner._get(spec, args.operation_id)
                    body = operation["body"]
        # A cursor indexes progressively appended results, not query order.
        # Sorting when an earlier slow query finishes would move already-read
        # results and cause a later page to skip or repeat evidence.
        deliveries = body["deliveries"]
        units = []
        for delivery in deliveries:
            for hit in delivery["result"].get("hits") or [None]:
                units.append(
                    {**delivery, "result": {**delivery["result"], "hits": [hit] if hit else []}}
                )
        if args.cursor > len(units):
            raise StateError("Cursor is beyond saved search results")
        page = units[args.cursor : args.cursor + args.limit]
        available = []
        for unit in page:
            safe = {
                **unit,
                "result": {
                    key: value
                    for key, value in unit["result"].items()
                    if key
                    in {
                        "outcome",
                        "reason",
                        "route",
                        "page_ref",
                        "delivery_ref",
                        "attempt_ref",
                        "provider_meta",
                        "recovery",
                        "shared_projection",
                    }
                },
            }
            hits = []
            for candidate in unit["result"].get("hits", []):
                saved = self.store.get(**candidate["hit_ref"])
                if (
                    saved is None
                    or any(
                        saved["body"].get(k) != v
                        for k, v in (
                            ("task_id", body["task_id"]),
                            ("agent_instance_id", body["actor_id"]),
                        )
                    )
                    or not shared_hit_access(self.store, saved)
                ):
                    continue
                source = saved["body"]["source"]
                hits.append(
                    {
                        **{k: v for k, v in candidate.items() if k != "discovery_excerpt"},
                        "hit_id": saved["id"],
                        "candidate_excerpt": (source.get("discovery_excerpt") or "")[:300],
                        "excerpt_status": "uninspected_candidate",
                        "route": saved["body"]["route"],
                        "retrieved_at": saved["body"]["retrieved_at"],
                    }
                )
            safe["result"]["hits"] = hits
            available.append(safe)
        pending = 0
        failed = 0
        uncertain = False
        delivered = {x["item_id"] for x in deliveries}
        for job_id in body["items"]:
            if job_id in delivered:
                job = self.store.get(JOB, job_id)
                result = job["body"].get("result") or {}
                failed += result.get("outcome") not in {"success", "partial", "empty"}
                uncertain |= (
                    result.get("outcome") == "uncertain_dispatch"
                    or result.get("provider_payload_condition") == "transport_error"
                )
                continue
            cap = self.executor.capacity.get(
                self.executor._capacity_id(
                    SimpleNamespace(mission_id=spec.mission_id, work_id=job_id)
                )
            )
            if cap and cap["phase"] in {"uncertain", "failed", "canceled", "known_unaccepted"}:
                failed += 1
                uncertain |= cap["phase"] == "uncertain"
            else:
                pending += 1
        status = (
            "cancelled"
            if body["cancel_requested"]
            else "pending"
            if pending
            else "uncertain"
            if uncertain
            else "failed"
            if failed == len(body["items"]) and failed
            else "completed"
        )
        return SearchOperationResult(
            operation_ref=ref(operation),
            status=status,
            available_results=available,
            continuation_cursor=args.cursor + len(page),
            has_more=args.cursor + len(page) < len(units),
            pending=pending,
            failed=failed,
            wait_requested=args.wait and not page and status == "pending",
        ).model_dump()

    def cancel(self, spec: WorkSpec, arguments: dict) -> dict:
        args = CancelSearchInput.model_validate(arguments)
        operation = self.owner._get(spec, args.operation_id)

        def detach(tx):
            current = tx.get(KIND, operation["id"])
            if not current["body"]["cancel_requested"]:
                tx.put(
                    KIND,
                    operation["id"],
                    {**current["body"], "cancel_requested": True},
                    expected_version=current["version"],
                )
                for job_id in current["body"]["items"]:
                    job = tx.get(JOB, job_id)
                    if job is not None:
                        subs = [
                            x
                            for x in job["body"]["subscribers"]
                            if x["operation_id"] != operation["id"]
                        ]
                        cap_id = self.executor._capacity_id(
                            SimpleNamespace(mission_id=spec.mission_id, work_id=job_id)
                        )
                        cap = tx.get("capacity_operation", cap_id)
                        no_dispatch = (
                            not subs
                            and job["body"].get("result") is None
                            and (
                                cap is None
                                or cap["body"]["phase"] in {"queued", "waiting", "admitted"}
                            )
                        )
                        tx.put(
                            JOB,
                            job_id,
                            {
                                **job["body"],
                                "subscribers": subs,
                                "phase": "known_no_dispatch"
                                if no_dispatch
                                else job["body"]["phase"],
                            },
                            expected_version=job["version"],
                        )
                tx.emit(spec.mission_id, "web_search_cancelled", {"operation_id": operation["id"]})
            return {"cancel_requested": True}

        self.store.transact(
            "web-shared-cancel:" + operation["id"],
            {"operation_id": operation["id"]},
            detach,
            mission_id=spec.mission_id,
        )
        for job_id in operation["body"]["items"]:
            job = self.store.get(JOB, job_id)
            if job is None or job["body"]["subscribers"]:
                continue
            cap_id = self.executor._capacity_id(
                SimpleNamespace(mission_id=spec.mission_id, work_id=job_id)
            )
            cap = self.executor.capacity.get(cap_id)
            if cap and cap["phase"] in {"queued", "waiting", "admitted"}:
                self.executor.capacity.cancel(cap_id, reason="last_shared_subscriber_cancelled")
        self.executor.notify(spec.mission_id)
        return self.read(spec, {"operation_id": operation["id"]})
