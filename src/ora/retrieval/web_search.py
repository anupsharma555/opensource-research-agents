"""Progressive search rounds using the shared executor, ledger and durable events.

Each query is a mechanical child of the chosen search. Its independent receipt
is available immediately, including after cancellation or restart. A round never
decides scientific sufficiency or silently replays an uncertain HTTP attempt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlsplit

from ora.contracts.models import (
    CancelSearchInput,
    ReadSearchResultsInput,
    SearchOperationResult,
    SearchWebInput,
)
from ora.coordination.board import ref
from ora.coordination.executor import WorkResult, WorkSpec
from ora.state import StateError
from ora.state.store import canonical

from .discovery import run_discovery_page
from .providers import ROUTES, http_transport
from .web_config import searx_endpoint

KIND = "web_search_operation"
ITEM = "web_search_item"
INVOCATION = "web_search_invocation"
REUSE_FAMILY = "web_search_reuse"
TOOLS = frozenset({"search_web", "read_search_results", "cancel_search"})


def identity(prefix: str, *values) -> str:
    return prefix + hashlib.sha256(canonical(values).encode()).hexdigest()[:32]


class WebSearch:
    def __init__(
        self,
        store,
        executor,
        values: dict,
        *,
        transport=None,
        shared_enabled=None,
        source_use_guard=None,
    ):
        self.store, self.executor, self.values = store, executor, values
        self.transport = transport or http_transport
        self.shared_enabled = shared_enabled or (lambda _spec: False)
        self.source_use_guard = source_use_guard
        from .shared_web_search import SharedWebSearch

        self.shared = SharedWebSearch(self)
        executor.handlers[("web-query", "1")] = self.execute

    def _get(self, spec, operation_id):
        operation = self.store.get(KIND, operation_id)
        if operation is None or any(
            operation["body"].get(k) != v
            for k, v in (
                ("mission_id", spec.mission_id),
                ("run_id", spec.run_id),
                ("actor_id", spec.actor_id),
                ("task_id", spec.proposal_ref["id"]),
            )
        ):
            raise StateError("Search operation belongs to another peer task")
        return operation

    def _require_source_scope(self, body):
        """Recheck the producing model input before using saved search text."""
        if self.source_use_guard is not None:
            self.source_use_guard(body)
        from ora.mission.context import WorkingContext

        parent = body["parent_spec"]
        call_id, version = parent.get("call_id"), parent.get("call_version")
        if not call_id:
            return
        intent = self.store.get("model_request_intent", f"model-intent:{call_id}:{version}")
        call = self.store.get("agent_call", call_id, version)
        if intent is None or call is None or intent["body"].get("call_ref") != ref(call):
            raise StateError("Search lost its producing model input lineage")
        dependencies = []
        for field in ("source_refs", "context_source_dependency_refs"):
            pointers = intent["body"].get(field)
            if not isinstance(pointers, list):
                raise StateError("Search input has incomplete source lineage")
            dependencies.extend(pointers)
        statuses = WorkingContext(self.store).require_current_source_use(dependencies)
        if any(status["model_input_status"] != "permitted" for status in statuses):
            raise StateError("Search input source use is no longer permitted")

    @staticmethod
    def _legacy_operation(tx, spec: WorkSpec, effective_args: dict) -> dict | None:
        """Index one exact pre-reuse operation without retrying its provider work."""
        rows = tx.db.execute(
            "SELECT r.id, r.body FROM records r JOIN heads h USING(kind,id,version) "
            "WHERE r.kind=? ORDER BY r.created_at DESC, r.id DESC",
            (KIND,),
        )
        for row in rows:
            body = json.loads(row["body"])
            if (
                all(
                    body.get(key) == value
                    for key, value in (
                        ("mission_id", spec.mission_id),
                        ("run_id", spec.run_id),
                        ("actor_id", spec.actor_id),
                        ("task_id", spec.proposal_ref["id"]),
                    )
                )
                and isinstance(body.get("arguments"), dict)
                and {key: value for key, value in body["arguments"].items() if key != "refresh"}
                == effective_args
            ):
                return tx.get(KIND, row["id"])
        return None

    def start(self, spec: WorkSpec, arguments: dict) -> dict:
        if self.shared_enabled(spec):
            return self.shared.start(spec, arguments)
        args = SearchWebInput.model_validate(arguments).model_dump()
        effective_args = {key: value for key, value in args.items() if key != "refresh"}
        refresh = bool(args.get("refresh", False))
        operation_id = identity("web-search-", spec.mission_id, spec.work_id)
        invocation_id = identity("web-search-invocation-", spec.mission_id, spec.work_id)
        family_id = identity(
            "web-search-reuse-",
            spec.mission_id,
            spec.run_id,
            spec.actor_id,
            spec.proposal_ref["id"],
            effective_args,
        )
        planned_items = [
            identity("web-query-", operation_id, index, args["provider"])
            for index in range(len(args["queries"]))
        ]
        body = {
            "schema": "ora.web_search.operation.v1",
            "mission_id": spec.mission_id,
            "run_id": spec.run_id,
            "actor_id": spec.actor_id,
            "task_id": spec.proposal_ref["id"],
            "arguments": args,
            "parent_spec": asdict(spec),
            "cancel_requested": False,
            "fallback_provider": "tavily" if self.values.get("TAVILY_API_KEY") else None,
            # Planned IDs are committed with the operation. A concurrent read
            # sees pending work even before executor submission completes.
            "items": planned_items,
            "deliveries": [],
            "created_at": self.store.now(),
        }
        self._require_source_scope(body)

        def choose(tx):
            current_parent = tx.get("board_proposal", spec.proposal_ref["id"])
            if (
                current_parent is None
                or ref(current_parent) != spec.proposal_ref
                or current_parent["body"].get("status") != "open"
            ):
                raise StateError("Search source task is no longer open")
            # This check is repeated under the invocation transaction. A grant
            # may change between preparation and a same-work replay.
            self._require_source_scope(body)
            invocation = tx.get(INVOCATION, invocation_id)
            if invocation is not None:
                saved = invocation["body"]
                if saved.get("arguments") != args or saved.get("scope") != {
                    "mission_id": spec.mission_id,
                    "run_id": spec.run_id,
                    "actor_id": spec.actor_id,
                    "task_id": spec.proposal_ref["id"],
                }:
                    raise StateError("Search invocation cannot be reused with changed inputs")
                return {
                    "operation_id": saved["operation_id"],
                    "reuse_status": saved["reuse_status"],
                }
            family = tx.get(REUSE_FAMILY, family_id)
            direct = tx.get(KIND, operation_id)
            if direct is not None:
                chosen_id, status = operation_id, "new"
                direct_args = {
                    **direct["body"]["arguments"],
                    "refresh": direct["body"]["arguments"].get("refresh", False),
                }
                if direct_args != args or any(
                    direct["body"].get(key) != value
                    for key, value in (
                        ("mission_id", spec.mission_id),
                        ("run_id", spec.run_id),
                        ("actor_id", spec.actor_id),
                        ("task_id", spec.proposal_ref["id"]),
                    )
                ):
                    raise StateError("Search invocation cannot be reused with changed inputs")
                if family is None:
                    tx.put(
                        REUSE_FAMILY,
                        family_id,
                        {
                            "mission_id": spec.mission_id,
                            "run_id": spec.run_id,
                            "actor_id": spec.actor_id,
                            "task_id": spec.proposal_ref["id"],
                            "arguments": effective_args,
                            "operation_id": operation_id,
                        },
                    )
            elif (
                family is None
                and not refresh
                and (legacy := self._legacy_operation(tx, spec, effective_args)) is not None
            ):
                chosen_id, status = legacy["id"], "reused"
                tx.put(
                    REUSE_FAMILY,
                    family_id,
                    {
                        "mission_id": spec.mission_id,
                        "run_id": spec.run_id,
                        "actor_id": spec.actor_id,
                        "task_id": spec.proposal_ref["id"],
                        "arguments": effective_args,
                        "operation_id": chosen_id,
                    },
                )
            elif family is not None and not refresh:
                chosen_id, status = family["body"]["operation_id"], "reused"
                chosen = tx.get(KIND, chosen_id)
                if (
                    chosen is None
                    or any(
                        chosen["body"].get(key) != value
                        for key, value in (
                            ("mission_id", spec.mission_id),
                            ("run_id", spec.run_id),
                            ("actor_id", spec.actor_id),
                            ("task_id", spec.proposal_ref["id"]),
                        )
                    )
                    or {
                        key: value
                        for key, value in chosen["body"]["arguments"].items()
                        if key != "refresh"
                    }
                    != effective_args
                ):
                    raise StateError("Saved equivalent search operation is inconsistent")
            else:
                chosen_id = operation_id
                status = "refreshed" if refresh else "new"
                saved_operation = tx.put(KIND, operation_id, body)
                family_body = {
                    "mission_id": spec.mission_id,
                    "run_id": spec.run_id,
                    "actor_id": spec.actor_id,
                    "task_id": spec.proposal_ref["id"],
                    "arguments": effective_args,
                    "operation_id": operation_id,
                }
                tx.put(
                    REUSE_FAMILY,
                    family_id,
                    family_body,
                    expected_version=family["version"] if family else 0,
                )
                tx.emit(
                    spec.mission_id, "web_search_started", {"operation_ref": ref(saved_operation)}
                )
            chosen_operation = tx.get(KIND, chosen_id)
            self._require_source_scope(chosen_operation["body"])
            tx.put(
                INVOCATION,
                invocation_id,
                {
                    "scope": {
                        "mission_id": spec.mission_id,
                        "run_id": spec.run_id,
                        "actor_id": spec.actor_id,
                        "task_id": spec.proposal_ref["id"],
                    },
                    "arguments": args,
                    "operation_id": chosen_id,
                    "reuse_status": status,
                },
            )
            return {"operation_id": chosen_id, "reuse_status": status}

        choice = self.store.transact(
            "web-start:" + invocation_id,
            {"spec": asdict(spec), "arguments": args},
            choose,
            mission_id=spec.mission_id,
        )
        # A cached transaction result bypasses choose(), including its source
        # check, so reauthorize this invocation before any further work.
        self._require_source_scope(body)
        operation = self._get(spec, choice["operation_id"])
        self._require_source_scope(operation["body"])
        if {
            key: value for key, value in operation["body"]["arguments"].items() if key != "refresh"
        } != effective_args:
            raise StateError("Search operation cannot be reused with changed inputs")
        for index, query in enumerate(args["queries"]):
            self._submit(operation, index, query, args["provider"])
        view = self.read(spec, {"operation_id": operation["id"], "wait": True})
        self._require_source_scope(body)
        return {**view, "reuse_status": choice["reuse_status"]}

    def _submit(self, operation, index, query, provider, *, fallback_of=None):
        body = operation["body"]
        item_id = identity("web-query-", operation["id"], index, provider)
        item_body = {
            "mission_id": body["mission_id"],
            "operation_id": operation["id"],
            "query_index": index,
            "query": query,
            "provider": provider,
            "fallback_of": fallback_of,
            "created_at": body["created_at"],
        }

        def register(tx):
            current = tx.get(KIND, operation["id"])
            if current["body"]["cancel_requested"]:
                return {"status": "cancelled"}
            parent = tx.get("board_proposal", body["task_id"])
            if parent is None or parent["body"].get("status") != "open":
                return {"status": "cancelled"}
            item = tx.get(ITEM, item_id) or tx.put(ITEM, item_id, item_body)
            if item_id not in current["body"]["items"]:
                tx.put(
                    KIND,
                    operation["id"],
                    {**current["body"], "items": [*current["body"]["items"], item_id]},
                    expected_version=current["version"],
                )
            return ref(item)

        pointer = self.store.transact(
            "web-item:" + item_id, item_body, register, mission_id=body["mission_id"]
        )
        if pointer.get("status") == "cancelled":
            return
        if any(
            work["body"]["spec"]["work_id"] == item_id
            for work in self.executor._work(body["mission_id"])
        ):
            return
        parent = body["parent_spec"]
        proposed = self.executor.board.propose(
            "web-propose:" + item_id,
            mission_id=body["mission_id"],
            proposal_id=item_id,
            actor_id=body["actor_id"],
            objective="Execute one selected web query",
            expected_artifact="Independent retrieval receipt",
            next_action="Run selected provider",
            branch_of=body["task_id"],
            prerequisite_refs=[pointer],
        )
        if proposed["status"] != "accepted":
            parent = self.store.get("board_proposal", body["task_id"])
            if parent is not None and parent["body"].get("status") == "cancelled":
                return
            raise StateError("Search child proposal could not be saved")
        proposal = proposed["proposal_ref"]
        child = WorkSpec(
            work_id=item_id,
            mission_id=body["mission_id"],
            run_id=body["run_id"],
            actor_id=body["actor_id"],
            proposal_ref=proposal,
            configuration_ref=parent["configuration_ref"],
            input_refs=(pointer,),
            handler_id="web-query",
            handler_version="1",
            kind="retrieval",
            lane=body["actor_id"],
            quota_scopes=(
                ("web:tavily", "web:tavily:minute")
                if provider == "tavily"
                else (f"web:{provider}",)
            ),
            attempt_id=item_id,
        )
        try:
            self.executor.submit(child)
        except StateError:
            parent = self.store.get("board_proposal", body["task_id"])
            if parent is not None and parent["body"].get("status") == "cancelled":
                return
            raise

    def _http(self, method, url, **kwargs):
        if urlsplit(url).hostname == "osra-searxng.invalid":
            url = searx_endpoint(self.values) + "/search?" + urlsplit(url).query
        return self.transport(method, url, **kwargs)

    def _negative_recovery(self, *, provider, mode, result, fallback_started):
        if result.get("hits"):
            return None
        outcome = result.get("outcome")
        if (
            outcome in {"uncertain_dispatch", "quota_wait"}
            or result.get("provider_payload_condition") == "transport_error"
        ):
            return None
        if mode == "quick" and fallback_started:
            next_action = (
                "Review saved sibling hits while the single configured Tavily fallback runs."
            )
            alternatives = ["await_saved_fallback", "inspect_existing_hit"]
        else:
            next_action = (
                "Review saved sibling hits. If they do not answer the information need, "
                "choose a new query or an exact identifier lookup; report the evidence gap "
                "if no checkable source is found."
            )
            alternatives = [
                "inspect_existing_hit",
                "reformulate_query",
                "verify_exact_identifier",
                "report_gap",
            ]
            if mode == "research" and any(
                (key != provider and configured)
                for key, configured in (
                    ("tavily", self.values.get("TAVILY_API_KEY")),
                    ("searxng", self.values.get("OSRA_SEARXNG_BASE_URL")),
                    ("exa", self.values.get("EXA_API_KEY")),
                )
            ):
                alternatives.insert(1, "choose_configured_alternate_route")
        return {
            "scope": "one_query_provider_attempt",
            "provider": provider,
            "observed_outcome": outcome,
            "scientific_absence_established": False,
            "alternatives": alternatives,
            "next_action": next_action,
        }

    def _should_fallback(self, provider, mode, result, fallback_provider):
        return (
            mode == "quick"
            and provider == "exa"
            and not result.get("hits")
            and result.get("outcome") not in {"uncertain_dispatch", "quota_wait"}
            and result.get("provider_payload_condition") != "transport_error"
            and fallback_provider == "tavily"
        )

    def execute(self, spec: WorkSpec) -> WorkResult:
        from ora.mission.context import ContextError

        item = self.store.get(ITEM, spec.work_id)["body"]
        operation = self.store.get(KIND, item["operation_id"])
        body, provider = operation["body"], item["provider"]
        try:
            self._require_source_scope(body)
        except (ContextError, StateError):
            # This is a known local denial before provider I/O. The executor
            # releases the reserved slot rather than inventing HTTP uncertainty.
            return WorkResult(disposition="terminal")
        # A recorded result is locally replayable; no repeat provider call after
        # an acceptance interruption. Capacity remains the dispatch authority.
        previous = next((x for x in body["deliveries"] if x["item_id"] == spec.work_id), None)
        if previous:
            return WorkResult(payload=previous)
        if body["cancel_requested"]:
            return WorkResult(payload={"status": "cancelled_before_dispatch"})
        credential = self.values.get(
            {"exa": "EXA_API_KEY", "tavily": "TAVILY_API_KEY"}.get(provider, "")
        )
        cap = self.executor.capacity.get(self.executor._capacity_id(spec))
        generation = cap["generation"]
        route = provider + ".search"
        args = body["arguments"]
        request = {
            "mission_id": body["mission_id"],
            "task_id": body["task_id"],
            "agent_instance_id": body["actor_id"],
            "request_id": spec.work_id,
            "attempt_id": f"{spec.work_id}:{generation}",
            "route": route,
            "provider": provider,
            "index": ROUTES[route]["index"],
            "operation": "search",
            "query": item["query"],
            "filters": {"mode": "fast" if args["mode"] == "quick" else "auto", "highlights": True}
            if provider == "exa"
            else {},
            "requested_at": item["created_at"],
            "requested_window": {"limit": args["limit"]},
        }
        if (
            provider in {"exa", "tavily"}
            and not credential
            or provider == "searxng"
            and not self.values.get("OSRA_SEARXNG_BASE_URL")
        ):
            result = {
                "outcome": "unavailable",
                "reason": "optional_provider_not_configured",
                "hits": [],
            }
        else:
            result = run_discovery_page(
                self.store, request, credential=credential, transport=self._http
            )
        # A provider 429/503 is a completed discovery attempt whose normalized
        # receipt may say provider_failure. The saved HTTP leg and retry time
        # are authoritative for pacing the next, differently identified attempt.
        provider_wait = any(leg.get("status") in {429, 503} for leg in result.get("http_legs", []))
        if result.get("outcome") == "quota_wait" or provider_wait:
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
        # Only known empty/error responses may route to a fallback. An uncertain
        # transport is preserved for review, never silently repeated elsewhere.
        fallback_started = self._should_fallback(
            provider, args["mode"], result, body.get("fallback_provider")
        )
        if fallback_started:
            self._submit(
                operation, item["query_index"], item["query"], "tavily", fallback_of=spec.work_id
            )
        recovery = self._negative_recovery(
            provider=provider, mode=args["mode"], result=result, fallback_started=fallback_started
        )
        if recovery is not None:
            result = {**result, "recovery": recovery}
        delivery = {
            "item_id": spec.work_id,
            "provider": provider,
            "query": item["query"],
            "information_need": args["information_need"],
            "result": result,
            "completed_at": self.store.now(),
            "fallback_of": item["fallback_of"],
        }
        self._save_delivery(operation["id"], spec.mission_id, spec.work_id, delivery)
        return WorkResult(
            payload=delivery,
            messages=(
                {
                    "recipient_id": body["actor_id"],
                    "correlation_id": operation["id"],
                    "payload": {
                        "kind": "search_progress",
                        "operation_id": operation["id"],
                        "provider": provider,
                    },
                },
            ),
        )

    def _save_delivery(self, operation_id, mission_id, item_id, delivery):
        def save(tx):
            current = tx.get(KIND, operation_id)
            if not any(x["item_id"] == item_id for x in current["body"]["deliveries"]):
                saved = tx.put(
                    KIND,
                    operation_id,
                    {**current["body"], "deliveries": [*current["body"]["deliveries"], delivery]},
                    expected_version=current["version"],
                )
                tx.emit(
                    mission_id,
                    "web_search_progress",
                    {"operation_ref": ref(saved), "item_id": item_id},
                )
            return delivery

        return self.store.transact("web-delivery:" + item_id, delivery, save, mission_id=mission_id)

    def recover(self, spec, operation_id):
        """Accept an exact saved page after a lost callback, without provider I/O.

        The capacity record must already be uncertain. Missing or incomplete
        pages retain uncertainty; they never imply a safe repeat request.
        """
        operation = self._get(spec, operation_id)
        body = operation["body"]
        self._require_source_scope(body)
        for work in self.executor._work(spec.mission_id):
            child = self.executor._spec_from(work)
            if child.work_id not in body["items"] or work["body"]["phase"] != "pending":
                continue
            cap = self.executor.capacity.get(self.executor._capacity_id(child))
            if cap is None or cap["phase"] != "uncertain" or not cap["attempts"]:
                continue
            # Expiry increments the capacity generation. The last dispatched
            # attempt retains its original generation in the attempt ledger.
            generation = cap["attempts"][-1].get("generation")
            if generation is None:
                continue  # Older attempts lack an exact dispatch generation.
            item = self.store.get(ITEM, child.work_id)
            if item is None or item["body"]["operation_id"] != operation_id:
                continue
            route = item["body"]["provider"] + ".search"
            pages, after = [], ""
            while True:
                batch = self.store.records("discovery_page", limit=500, after_id=after)
                pages.extend(
                    page
                    for page in batch
                    if page["body"].get("request_id") == child.work_id
                    and page["body"].get("attempt_id") == f"{child.work_id}:{generation}"
                    and page["body"].get("route") == route
                )
                if len(batch) < 500:
                    break
                after = batch[-1]["id"]
            if len(pages) != 1 or pages[0]["body"]["status"] != "completed":
                continue
            page = pages[0]
            result = page["body"]["result"]
            if result.get("outcome") == "quota_wait" or any(
                leg.get("status") in {429, 503} for leg in result.get("http_legs", [])
            ):
                continue
            fallback_started = self._should_fallback(
                item["body"]["provider"],
                body["arguments"]["mode"],
                result,
                body.get("fallback_provider"),
            )
            if fallback_started:
                self._submit(
                    operation,
                    item["body"]["query_index"],
                    item["body"]["query"],
                    "tavily",
                    fallback_of=child.work_id,
                )
            recovery = self._negative_recovery(
                provider=item["body"]["provider"],
                mode=body["arguments"]["mode"],
                result=result,
                fallback_started=fallback_started,
            )
            if recovery is not None:
                result = {**result, "recovery": recovery}
            delivery = next((x for x in body["deliveries"] if x["item_id"] == child.work_id), None)
            if delivery is None:
                delivery = {
                    "item_id": child.work_id,
                    "provider": item["body"]["provider"],
                    "query": item["body"]["query"],
                    "information_need": body["arguments"]["information_need"],
                    "result": {**result, "page_ref": ref(page)},
                    "completed_at": page["body"].get("finished_at", self.store.now()),
                    "fallback_of": item["body"]["fallback_of"],
                }
                self._save_delivery(operation_id, spec.mission_id, child.work_id, delivery)
            self.executor.accept_saved_web_query_result(
                child,
                WorkResult(payload=delivery),
                ref(page),
            )

    def read(self, spec, arguments):
        from ora.mission.shared_materials import shared_hit_access

        args = ReadSearchResultsInput.model_validate(arguments)
        current = self.store.get(KIND, args.operation_id)
        if current is not None and current["body"].get("shared_version") == 1:
            return self.shared.read(spec, arguments)
        self.recover(spec, args.operation_id)
        operation = self._get(spec, args.operation_id)
        body = operation["body"]
        self._require_source_scope(body)
        if not body["cancel_requested"]:
            submitted = {
                work["body"]["spec"]["work_id"] for work in self.executor._work(spec.mission_id)
            }
            for index, query in enumerate(body["arguments"]["queries"]):
                item_id = identity(
                    "web-query-", operation["id"], index, body["arguments"]["provider"]
                )
                if item_id not in submitted:
                    cap_id = self.executor._capacity_id(
                        SimpleNamespace(
                            mission_id=spec.mission_id,
                            work_id=item_id,
                        )
                    )
                    if self.executor.capacity.get(cap_id) is not None:
                        raise StateError(
                            "Search work is missing after a saved dispatch; inspect before retry"
                        )
                    # Complete a crash-interrupted initial submission. The
                    # item, proposal and executor work are idempotent; a saved
                    # uncertain dispatch is never sent again here.
                    self._submit(operation, index, query, body["arguments"]["provider"])
            operation = self._get(spec, args.operation_id)
            body = operation["body"]
        deliveries = list(body["deliveries"])
        seen = {item["item_id"] for item in deliveries}
        terminal = []
        for work in self.executor._work(spec.mission_id):
            child = self.executor._spec_from(work)
            if child.work_id not in body["items"] or child.work_id in seen:
                continue
            cap = self.executor.capacity.get(self.executor._capacity_id(child))
            if cap and cap["phase"] in {"uncertain", "failed", "canceled", "known_unaccepted"}:
                terminal.append(cap["phase"])
        # Append-only result units keep a cursor stable as later queries finish.
        # Page source candidates, rather than entire ten-result provider pages.
        units = []
        for delivery in deliveries:
            for hit in delivery["result"].get("hits") or [None]:
                units.append(
                    {**delivery, "result": {**delivery["result"], "hits": [hit] if hit else []}}
                )
        if args.cursor > len(units):
            raise StateError("Cursor is beyond saved search results")
        page = units[args.cursor : args.cursor + args.limit]
        pending = len(body["items"]) - len(deliveries) - len(terminal)
        uncertain = "uncertain" in terminal or any(
            x["result"].get("outcome") == "uncertain_dispatch"
            or x["result"].get("provider_payload_condition") == "transport_error"
            for x in deliveries
        )
        failed = len(terminal) + sum(
            x["result"].get("outcome") not in {"success", "partial", "empty"} for x in deliveries
        )
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
        # Build route provenance over all saved units so pagination never hides
        # a second query or provider that found the same URL.
        routes_by_url = {}
        for unit in units:
            for candidate in unit["result"].get("hits", []):
                saved_hit = self.store.get(**candidate["hit_ref"])
                if (
                    saved_hit is None
                    or not shared_hit_access(self.store, saved_hit)
                    or saved_hit["body"]["source"].get("rights_status")
                    in {"restricted", "not_retained"}
                ):
                    continue
                discovery = {
                    "query": unit["query"],
                    "provider": unit["provider"],
                    "hit_ref": candidate["hit_ref"],
                    "route": saved_hit["body"]["route"],
                    "retrieved_at": saved_hit["body"]["retrieved_at"],
                    **{
                        k: candidate[k]
                        for k in ("provider_item_id", "scientific_metadata", "content_sha256")
                    },
                }
                routes_by_url.setdefault(candidate["original_url"], []).append(discovery)
        # Group only exact normalized URLs. This never merges publications or
        # study families that have different original URLs.
        available = []
        for delivery in page:
            safe = {
                **delivery,
                "result": {
                    key: value
                    for key, value in delivery["result"].items()
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
                    }
                },
            }
            hits = []
            for candidate in delivery["result"].get("hits", []):
                saved_hit = self.store.get(**candidate["hit_ref"])
                if (
                    saved_hit is None
                    or not shared_hit_access(self.store, saved_hit)
                    or saved_hit["body"]["source"].get("rights_status")
                    in {"restricted", "not_retained"}
                ):
                    continue
                source = saved_hit["body"]["source"]
                hits.append(
                    {
                        **{k: v for k, v in candidate.items() if k != "discovery_excerpt"},
                        "hit_id": saved_hit["id"],
                        "candidate_excerpt": (source.get("discovery_excerpt") or "")[:300],
                        "excerpt_status": "uninspected_candidate",
                        "route": saved_hit["body"]["route"],
                        "retrieved_at": saved_hit["body"]["retrieved_at"],
                    }
                )
            safe["result"]["hits"] = hits
            if hits:
                hit = hits[0]
                duplicate = next(
                    (
                        entry
                        for entry in available
                        if entry["result"].get("hits")
                        and entry["result"]["hits"][0]["original_url"] == hit["original_url"]
                    ),
                    None,
                )
                if duplicate is not None:
                    continue
                hit["discovery_routes"] = routes_by_url[hit["original_url"]]
            available.append(safe)
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

    def wake_ready(self, control):
        """Host-only progress read: never asks the model to poll."""
        body = control["body"]
        wait = body["search_wait"]
        spec = SimpleNamespace(
            mission_id=body["mission_id"],
            run_id=body["run_id"],
            actor_id=body["actor_id"],
            proposal_ref={"id": body["task_id"]},
        )
        view = self.read(
            spec,
            {"operation_id": wait["operation_ref"]["id"], "cursor": wait["continuation_cursor"]},
        )
        return bool(view["available_results"] or view["status"] != "pending")

    def cancel(self, spec, arguments):
        args = CancelSearchInput.model_validate(arguments)
        current = self.store.get(KIND, args.operation_id)
        if current is not None and current["body"].get("shared_version") == 1:
            return self.shared.cancel(spec, arguments)
        operation = self._get(spec, args.operation_id)

        def cancel(tx):
            current = tx.get(KIND, operation["id"])
            if not current["body"]["cancel_requested"]:
                tx.put(
                    KIND,
                    operation["id"],
                    {**current["body"], "cancel_requested": True},
                    expected_version=current["version"],
                )
                tx.emit(spec.mission_id, "web_search_cancelled", {"operation_id": operation["id"]})
            return {"cancel_requested": True}

        self.store.transact(
            "web-cancel:" + operation["id"],
            {"operation_id": operation["id"]},
            cancel,
            mission_id=spec.mission_id,
        )
        for work in self.executor._work(spec.mission_id):
            if work["body"]["spec"]["work_id"] not in operation["body"]["items"]:
                continue
            child = self.executor._spec_from(work)
            cap_id = self.executor._capacity_id(child)
            cap = self.executor.capacity.get(cap_id)
            if cap and cap["phase"] in {"queued", "waiting", "admitted", "dispatched", "uncertain"}:
                self.executor.capacity.cancel(cap_id, reason="user_cancel")
        self.executor.notify(spec.mission_id)
        return self.read(spec, {"operation_id": operation["id"]})
