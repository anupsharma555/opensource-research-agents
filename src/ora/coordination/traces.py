"""Save correlated execution facts and export readable, privacy-scoped timelines.

Required event records commit with research state. Viewers page through them
independently, so an absent or slow viewer cannot hold a worker slot. Returned
model reasoning is a separate restricted artifact, never a generated explanation
or a prerequisite for scientific work.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime

from ora.agents.capabilities import content_hash
from ora.retrieval.redact import redact, redact_text
from ora.state import StateConflict, StateError, StateStore
from ora.state.source_lineage import inherited_public_source
from ora.state.steps import explain_event
from ora.state.store import Transaction, canonical

_CONTACT = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|https?://[^\s<>]+")
_RELATIONS = {"input_consumed", "reply_to", "revision_of", "retry_of"}
_GAP_KINDS = {"omitted", "delayed", "unavailable"}
_GAP_REASON_CODE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")


def _exact_ref(pointer: dict) -> dict:
    if (not isinstance(pointer, dict) or set(pointer) != {"kind", "id", "version"}
            or any(not isinstance(pointer[key], str) or not pointer[key] for key in ("kind", "id"))
            or type(pointer["version"]) is not int or pointer["version"] < 1):
        raise StateError("A trace relationship needs an exact record reference")
    return dict(pointer)


def public_text(value: str | None) -> str | None:
    """Keep trace prose useful without copying contact details or embedded links."""
    if value is None:
        return None
    clean = redact_text(value)[0]
    return _CONTACT.sub("[contact or link omitted; inspect the source record]", clean)


def ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _id(prefix: str, value) -> str:
    return prefix + hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class TraceContext:
    mission_id: str
    run_id: str
    task_id: str
    attempt_id: str
    instance_id: str
    operation_id: str
    turn_id: str | None = None
    call_id: str | None = None
    parent_operation_id: str | None = None
    configuration_ref: dict | None = None
    call_version: int | None = None


class TraceJournal:
    """Use the shared store as the event authority; exports are disposable views."""

    def __init__(self, store: StateStore):
        self.store = store

    def _context(self, tx: Transaction, context: TraceContext) -> dict:
        for value in (context.mission_id, context.run_id, context.task_id,
                      context.attempt_id, context.instance_id, context.operation_id):
            if not isinstance(value, str) or not value:
                raise StateError("Trace context needs exact mission, run, work and actor identities")
        actor = tx.get("agent_instance", context.instance_id, 1)
        if actor is None or actor["body"]["mission_id"] != context.mission_id or actor["body"]["run_id"] != context.run_id:
            raise StateError("Trace actor does not belong to this mission and run")
        if context.configuration_ref:
            configuration = tx.get(**context.configuration_ref)
            if configuration is None or configuration["kind"] != "agent_configuration" or configuration["body"].get("instance_id") != context.instance_id:
                raise StateError("Trace configuration does not resolve to its actor")
        if context.call_id is not None:
            if type(context.call_version) is not int or context.call_version < 1:
                raise StateError("Trace call needs an exact manifest version")
            call = tx.get("agent_call", context.call_id, context.call_version)
            if call is None or any(call["body"].get(key) != value for key, value in (
                ("mission_id", context.mission_id), ("instance_id", context.instance_id),
                ("task_id", context.task_id), ("attempt_id", context.attempt_id),
            )):
                raise StateError("Trace call does not match its actor and work")
            if context.configuration_ref and call["body"].get("configuration_id") != context.configuration_ref["id"]:
                raise StateError("Trace call and configuration differ")
        return actor

    def append_tx(
        self, tx: Transaction, event_key: str, *, context: TraceContext,
        category: str, status: str, action: str, summary: str,
        inputs: list[dict] | None = None, outputs: list[dict] | None = None,
        capability_state: str | None = None, timing: dict | None = None,
        usage: list[dict] | None = None, reason_summary: str | None = None,
        next_action: str | None = None, proof: str = "recorded_local_event",
        relationships: list[dict] | None = None,
    ) -> dict:
        """Append a minimal event inside a caller's artifact/message transaction."""
        actor = self._context(tx, context)
        if not all(isinstance(x, str) and x for x in (event_key, category, status, action, summary, proof)):
            raise StateError("A trace needs an event key, category, action, status and explanation")
        if capability_state not in {None, "assigned", "available", "loaded", "exposed", "invoked", "attempted", "successful", "failed", "unavailable"}:
            raise StateError("Unknown capability-state observation")
        if proof == "provider_observed" and (context.call_id is None or not outputs):
            raise StateError("Provider observations need an exact saved call and outcome")
        safe_timing = {}
        for key, value in (timing or {}).items():
            if key.endswith(("_seconds", "_ms")) and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise StateError("Measured durations must be finite and nonnegative")
            if re.fullmatch(r"[a-z][a-z_]*(?:_seconds|_ms)", key):
                safe_timing[key] = value
            elif key == "basis" and value in {"observed_local", "observed_monotonic", "controlled_clock", "provider_reported", "unknown"}:
                safe_timing[key] = value
            elif key == "latency_category" and value in {"minimal", "medium", "long", "very_long"}:
                safe_timing[key] = value
        targets = []
        pinned_call = (
            tx.get("agent_call", context.call_id, context.call_version)
            if context.call_id is not None else None
        )
        for pointer in [*(inputs or []), *(outputs or [])]:
            record = tx.get(**pointer)
            if record is None:
                raise StateError("Trace input/output reference is unavailable")
            if record["kind"] == "restricted_reasoning":
                raise StateError("Returned reasoning uses the separate restricted lookup")
            inherited_input = (
                pointer in (inputs or []) and pointer not in (outputs or [])
                and record["kind"] == "source_copy" and context.call_id is not None
                and any(
                    {key: item.get(key) for key in ("kind", "id", "version")} == pointer
                    and item.get("sha256") == content_hash(record["body"])
                    for item in pinned_call["body"].get("input_refs", [])
                )
                and inherited_public_source(
                    tx, mission_id=context.mission_id, run_id=context.run_id,
                    copy_ref=pointer, actor_id=context.instance_id,
                    task_id=context.task_id,
                )
            )
            if (record["body"].get("mission_id") not in (None, context.mission_id)
                    and not inherited_input):
                raise StateError("Trace reference belongs to another mission")
            targets.append(record)
        typed_links = []
        for item in relationships or []:
            if not isinstance(item, dict) or set(item) != {"relation", "reference"} or item["relation"] not in _RELATIONS:
                raise StateError("Unknown or malformed trace relationship")
            pointer = _exact_ref(item["reference"])
            target = tx.get(**pointer)
            if target is None or target["kind"] == "restricted_reasoning":
                raise StateError("Trace relationship target is unavailable or restricted")
            if target["body"].get("mission_id") not in (None, context.mission_id):
                raise StateError("Trace relationship belongs to another mission")
            typed_links.append((item["relation"], pointer, target))
        typed_links.sort(key=lambda item: (item[0], item[1]["kind"], item[1]["id"], item[1]["version"]))
        typed_links = [item for index, item in enumerate(typed_links)
                       if index == 0 or (item[0], item[1]) != (typed_links[index - 1][0], typed_links[index - 1][1])]
        safe, redacted_paths = redact({"action": action, "summary": summary,
                                      "reason_summary": reason_summary, "next_action": next_action})
        for key, value in safe.items():
            cleaned = public_text(value)
            if cleaned != value:
                redacted_paths.append("$." + key)
            safe[key] = cleaned
        body = {**asdict(context), "schema": "ora.trace.v1", "category": category,
                "status": status, **safe, "input_refs": inputs or [], "output_refs": outputs or [],
                "capability_state": capability_state, "timing": safe_timing, "proof": proof,
                "timing_fields_omitted": len(timing or {}) - len(safe_timing),
                "redacted_paths": redacted_paths, "usage_refs": []}
        if typed_links:
            body["relationships"] = [{"relation": relation, "reference": pointer}
                                     for relation, pointer, _ in typed_links]
        for item in usage or []:
            if not isinstance(item, dict) or set(item) != {"receipt_id", "unit", "amount", "basis", "scope"}:
                raise StateError("Usage needs receipt identity, native unit, amount, basis and scope")
            if item["basis"] not in {"observed", "documented", "estimated"} or type(item["amount"]) not in (int, float) or not math.isfinite(item["amount"]) or item["amount"] < 0:
                raise StateError("Invalid usage observation")
            if not all(isinstance(item[k], str) and item[k] for k in ("receipt_id", "unit", "scope")):
                raise StateError("Usage identity and scope must be explicit")
            usage_body = {"mission_id": context.mission_id, **item}
            usage_id = _id("usage-", [context.mission_id, item["receipt_id"], item["scope"], item["unit"], item["basis"]])
            saved_usage = tx.get("trace_usage", usage_id)
            if saved_usage and saved_usage["body"] != usage_body:
                raise StateConflict("Usage receipt identity already has a different amount")
            saved_usage = saved_usage or tx.put("trace_usage", usage_id, usage_body)
            body["usage_refs"].append(ref(saved_usage))
        identifier = _id("trace-", [context.mission_id, event_key])
        existing = tx.get("coordination_trace", identifier)
        if existing:
            if existing["body"] != body:
                raise StateConflict("Trace event identity already has different content")
            return ref(existing)
        saved = tx.put("coordination_trace", identifier, body)
        tx.link(saved, actor, "performed_by")
        for target in targets:
            tx.link(saved, target, "describes")
        for relation, _, target in typed_links:
            tx.link(saved, target, relation)
        tx.emit(context.mission_id, "coordination_trace_recorded", {"trace_ref": ref(saved)})
        return ref(saved)

    def append(self, event_key: str, **kwargs) -> dict:
        context = kwargs["context"]
        request = {**kwargs, "context": asdict(context), "event_key": event_key}
        return self.store.transact("trace-append-" + _id("", [context.mission_id, event_key]), request,
                                   lambda tx: self.append_tx(tx, event_key, **kwargs), mission_id=context.mission_id)

    def save_reasoning(self, event_key: str, *, context: TraceContext,
                       status: str, text: str | None = None) -> dict:
        if status not in {"returned", "absent", "truncated", "unsupported", "unknown"}:
            raise StateError("Reasoning availability must describe the provider response")
        if status in {"absent", "unsupported", "unknown"} and text is not None:
            raise StateError("Unavailable reasoning cannot contain invented text")
        if status == "returned" and not text:
            raise StateError("Returned reasoning needs actual provider material")
        if text is not None and not isinstance(text, str):
            raise StateError("Returned reasoning must be text")
        safe, changed = redact_text(text) if text is not None else (None, False)
        blob = self.store.put_blob(safe.encode()) if safe is not None else None
        body = {**asdict(context), "schema": "ora.reasoning.v1", "status": status,
                "blob": blob, "redacted": bool(changed), "origin": "provider_returned",
                "access": "restricted_diagnostics"}

        def save(tx):
            self._context(tx, context)
            return ref(tx.put("restricted_reasoning", _id("reasoning-", [context.mission_id, event_key]), body))
        return self.store.transact("reasoning-save-" + _id("", [context.mission_id, event_key]), body, save, mission_id=context.mission_id)

    def read_reasoning(self, pointer: dict, *, mission_id: str,
                       diagnostic_granted: bool = False, independent_first_pass: bool = False) -> dict:
        """Host-authenticated callers supply grants; peer message data cannot grant them."""
        if diagnostic_granted is not True or independent_first_pass is not False:
            raise PermissionError("Provider reasoning is unavailable to this research input")
        record = self.store.get(**pointer)
        if record is None or record["kind"] != "restricted_reasoning" or record["body"]["mission_id"] != mission_id:
            raise PermissionError("Restricted diagnostic reference is unavailable")
        body = record["body"]
        return {"status": body["status"], "text": self.store.read_blob(body["blob"]["sha256"]).decode() if body["blob"] else None,
                "origin": "provider_returned", "redacted": body["redacted"]}

    def export(self, mission_id: str, *, after: int = 0, limit: int = 100) -> dict:
        """Project saved events as existing ResearchStep records for any review client."""
        events = self.store.events(mission_id, after=after, limit=limit)
        steps, missing, details = [], [], []
        for event in events:
            if event["type"] != "coordination_trace_recorded":
                continue
            pointer = event["body"]["trace_ref"]
            trace = self.store.get(**pointer)
            if trace is None:
                missing.append(pointer)
                continue
            body = trace["body"]
            parent = body["parent_operation_id"]
            lineage_state = self._lineage(mission_id, body["run_id"], body["operation_id"], parent)
            details.append({"reference": pointer, "data": {**body,
                "lineage_status": lineage_state}})
            steps.append(explain_event(self.store, event))
        cursor = events[-1]["sequence"] if events else after
        more = bool(self.store.events(mission_id, after=cursor, limit=1))
        return {"schemaVersion": "1.0", "missionId": mission_id, "steps": steps,
                "records": details,
                "nextCursor": cursor, "hasMore": more, "missingReferences": missing,
                "completeness": "missing_records" if missing else "more_available" if more else "caught_up",
                "reasoningAccess": "separate_restricted_lookup"}

    def _lineage(self, mission_id: str, run_id: str, operation_id: str, parent: str | None) -> str:
        """Resolve only this page's ancestors, without loading the mission history."""
        if parent is None:
            return "root_event"
        visited = {operation_id}
        db = self.store._connect()
        try:
            while parent is not None:
                if parent in visited:
                    return "cyclic_parentage"
                visited.add(parent)
                rows = db.execute(
                    "SELECT DISTINCT json_extract(body,'$.parent_operation_id') AS parent "
                    "FROM records WHERE kind='coordination_trace' "
                    "AND json_extract(body,'$.mission_id')=? AND json_extract(body,'$.run_id')=? "
                    "AND json_extract(body,'$.operation_id')=? LIMIT 2",
                    (mission_id, run_id, parent),
                ).fetchall()
                if not rows:
                    return "unresolved_parent"
                if len(rows) > 1:
                    return "ambiguous_parentage"
                parent = rows[0]["parent"]
            return "resolved"
        finally:
            db.close()

    @staticmethod
    def _trace_event_sequence(tx: Transaction, mission_id: str, pointer: dict) -> int | None:
        row = tx.db.execute(
            "SELECT sequence FROM events WHERE mission_id=? "
            "AND event_type='coordination_trace_recorded' "
            "AND json_extract(body,'$.trace_ref.kind')=? "
            "AND json_extract(body,'$.trace_ref.id')=? "
            "AND json_extract(body,'$.trace_ref.version')=? LIMIT 1",
            (mission_id, pointer["kind"], pointer["id"], pointer["version"]),
        ).fetchone()
        return row["sequence"] if row else None

    def _summary_coverage_tx(self, tx: Transaction, mission_id: str, level: str,
                             coverage: dict, source_refs: list[dict]) -> dict:
        if (not isinstance(coverage, dict) or set(coverage) - {
                "scope", "through_event_sequence", "event_refs", "generation_state", "known_gaps"}
                or "scope" not in coverage or "generation_state" not in coverage):
            raise StateError("Summary coverage needs declared scope and generation state")
        scope = coverage["scope"]
        if (not isinstance(scope, dict) or set(scope) - {"run_id", "instance_ids", "task_ids"}
                or not isinstance(scope.get("run_id"), str) or not scope["run_id"]):
            raise StateError("Summary coverage needs an exact run scope")
        instances, tasks = scope.get("instance_ids", []), scope.get("task_ids", [])
        if (not isinstance(instances, list) or not isinstance(tasks, list)
                or any(not isinstance(value, str) or not value for value in [*instances, *tasks])
                or len(set(instances)) != len(instances) or len(set(tasks)) != len(tasks)):
            raise StateError("Summary scope identities must be unique and nonempty")
        if level == "agent" and len(instances) != 1:
            raise StateError("An agent summary needs one named agent in its scope")
        if not instances and tx.db.execute(
                "SELECT 1 FROM records WHERE kind='agent_instance' AND version=1 "
                "AND json_extract(body,'$.mission_id')=? AND json_extract(body,'$.run_id')=? LIMIT 1",
                (mission_id, scope["run_id"])).fetchone() is None:
            raise StateError("Summary scope run has no registered actor in this mission")
        for instance_id in instances:
            actor = tx.get("agent_instance", instance_id, 1)
            if actor is None or actor["body"].get("mission_id") != mission_id or actor["body"].get("run_id") != scope["run_id"]:
                raise StateError("Summary scope agent belongs to another mission or run")
        cursor = coverage.get("through_event_sequence")
        if cursor is not None:
            if type(cursor) is not int or cursor < 0:
                raise StateError("Summary event boundary must be a saved nonnegative cursor")
            if cursor and tx.db.execute(
                    "SELECT 1 FROM events WHERE mission_id=? AND sequence=?",
                    (mission_id, cursor)).fetchone() is None:
                raise StateError("Summary event boundary was not read from this mission")
        event_refs = coverage.get("event_refs", [])
        if (not isinstance(event_refs, list) or (cursor is None and not event_refs)):
            raise StateError("Summary coverage needs an event cursor or exact event set")
        declared_events = []
        for raw in event_refs:
            pointer = _exact_ref(raw)
            event = tx.get(**pointer)
            if event is None or event["kind"] != "coordination_trace" or event["body"].get("mission_id") != mission_id:
                raise StateError("Covered event is unavailable or belongs to another mission")
            body = event["body"]
            if (body.get("run_id") != scope["run_id"]
                    or (instances and body.get("instance_id") not in instances)
                    or (tasks and body.get("task_id") not in tasks)):
                raise StateError("Covered event is outside the declared summary scope")
            sequence = self._trace_event_sequence(tx, mission_id, pointer)
            if sequence is None or cursor is not None and sequence > cursor:
                raise StateError("Covered event is not within the declared read boundary")
            declared_events.append(pointer)
        if len({(item["kind"], item["id"], item["version"]) for item in declared_events}) != len(declared_events):
            raise StateError("Covered event set contains duplicates")
        declared_set = {(item["kind"], item["id"], item["version"]) for item in declared_events}
        for pointer in source_refs:
            if pointer["kind"] != "coordination_trace":
                continue
            event = tx.get(**pointer)
            body = event["body"]
            if (body.get("run_id") != scope["run_id"]
                    or (instances and body.get("instance_id") not in instances)
                    or (tasks and body.get("task_id") not in tasks)):
                raise StateError("Summary source event is outside its declared scope")
            sequence = self._trace_event_sequence(tx, mission_id, pointer)
            if sequence is None or cursor is not None and sequence > cursor:
                raise StateError("Summary source event is newer than its read boundary")
            if declared_events and (pointer["kind"], pointer["id"], pointer["version"]) not in declared_set:
                raise StateError("Summary source event is absent from its declared event set")
        if coverage["generation_state"] not in {"complete", "partial"}:
            raise StateError("Summary generation state must be complete or partial")
        gaps = coverage.get("known_gaps", [])
        if not isinstance(gaps, list):
            raise StateError("Known summary gaps must be a list")
        normalized_gaps = []
        for gap in gaps:
            reason_code = gap.get("reason_code") if isinstance(gap, dict) else None
            if (not isinstance(gap, dict) or set(gap) - {"kind", "reason_code", "reference"}
                    or gap.get("kind") not in _GAP_KINDS
                    or not isinstance(reason_code, str) or len(reason_code) > 96
                    or _GAP_REASON_CODE.fullmatch(reason_code) is None):
                raise StateError("Summary gap needs a typed status and short reason code")
            normalized = {"kind": gap["kind"], "reason_code": reason_code}
            if "reference" in gap:
                pointer = _exact_ref(gap["reference"])
                target = tx.get(**pointer)
                if target is None or target["kind"] == "restricted_reasoning":
                    raise StateError("Summary gap reference is unavailable or restricted")
                if target["body"].get("mission_id") not in (None, mission_id):
                    raise StateError("Summary gap reference belongs to another mission")
                normalized["reference"] = pointer
            normalized_gaps.append(normalized)
        return {"scope": {"run_id": scope["run_id"], "instance_ids": instances,
                          "task_ids": tasks}, "through_event_sequence": cursor,
                "event_refs": declared_events, "generation_state": coverage["generation_state"],
                "known_gaps": normalized_gaps, "basis": "caller_declared_read_boundary"}

    def summarize(self, operation_id: str, *, mission_id: str, summary: str,
                  source_refs: list[dict], level: str = "run",
                  coverage: dict | None = None) -> dict:
        if level not in {"agent", "group", "run"} or not summary or not source_refs:
            raise StateError("A derived summary needs a level, explanation and exact sources")
        safe = public_text(summary)
        body = {"mission_id": mission_id, "kind": "derived_explanation", "level": level,
                "summary": safe, "source_refs": source_refs}

        def save(tx):
            for pointer in source_refs:
                source = tx.get(**_exact_ref(pointer))
                if source is None or source["kind"] == "restricted_reasoning" or source["body"].get("mission_id") not in (None, mission_id):
                    raise StateError("Summary source is unavailable or restricted")
            if coverage is not None:
                body["coverage"] = self._summary_coverage_tx(tx, mission_id, level, coverage, source_refs)
            record = tx.put("trace_summary", _id("summary-", [mission_id, operation_id]), body)
            for pointer in source_refs:
                source = tx.get(**pointer)
                tx.link(record, source, "derived_from")
            for pointer in body.get("coverage", {}).get("event_refs", []):
                tx.link(record, tx.get(**pointer), "derived_from")
            return ref(record)
        request = {**body, "coverage": coverage} if coverage is not None else body
        return self.store.transact("trace-summary-" + _id("", [mission_id, operation_id]),
                                   request, save, mission_id=mission_id)

    def summary_coverage(self, pointer: dict, *, mission_id: str) -> dict:
        """Read one declared boundary and compare it with a consistent event snapshot.

        A caller-supplied cursor describes what was read before generation. This
        method never advances it to the store's current tip or calls a model.
        """
        db = self.store._connect()
        try:
            db.execute("BEGIN")
            tx = Transaction(self.store, db)
            exact = _exact_ref(pointer)
            record = tx.get(**exact)
            if record is None or record["kind"] != "trace_summary" or record["body"].get("mission_id") != mission_id:
                raise StateError("Summary is unavailable in this mission")
            body = record["body"]
            missing_sources = [item for item in body.get("source_refs", []) if tx.get(**item) is None]
            coverage = body.get("coverage")
            if coverage is None:
                return {"reference": exact, "level": body["level"], "summary": body["summary"],
                        "coverage_status": "unknown", "currentness": "unknown_without_boundary",
                        "coverage_basis": "unknown", "source_refs": body.get("source_refs", []),
                        "source_completeness": "not_established",
                        "generation_state": "unknown", "scope": None,
                        "through_event_sequence": None, "event_refs": [],
                        "known_gaps": [], "missing_source_refs": missing_sources,
                        "missing_event_refs": [], "newer_scoped_event_count": None,
                        "newer_unclassified_event_count": None,
                        "unresolved_newer_trace_count": None}
            missing_events = [item for item in coverage["event_refs"]
                              if tx.get(**item) is None or self._trace_event_sequence(tx, mission_id, item) is None]
            cursor = coverage["through_event_sequence"]
            scoped_count = unclassified_count = unresolved_count = None
            observed_tip = None
            if cursor is not None:
                tip = db.execute("SELECT MAX(sequence) AS sequence FROM events WHERE mission_id=?", (mission_id,)).fetchone()
                observed_tip = tip["sequence"] or 0
                scope = coverage["scope"]
                filters = ["json_extract(r.body,'$.run_id')=?"]
                args: list[object] = [mission_id, cursor, scope["run_id"]]
                for field, values in (("instance_id", scope["instance_ids"]), ("task_id", scope["task_ids"])):
                    if values:
                        filters.append(f"json_extract(r.body,'$.{field}') IN ({','.join('?' for _ in values)})")
                        args.extend(values)
                join = ("FROM events e LEFT JOIN records r ON "
                        "r.kind=json_extract(e.body,'$.trace_ref.kind') AND "
                        "r.id=json_extract(e.body,'$.trace_ref.id') AND "
                        "r.version=json_extract(e.body,'$.trace_ref.version') AND "
                        "json_extract(r.body,'$.mission_id')=e.mission_id "
                        "WHERE e.mission_id=? AND e.sequence>? "
                        "AND e.event_type='coordination_trace_recorded'")
                scoped_count = db.execute(
                    "SELECT COUNT(*) AS count " + join + " AND " + " AND ".join(filters), args
                ).fetchone()["count"]
                unresolved_count = db.execute(
                    "SELECT COUNT(*) AS count " + join + " AND r.kind IS NULL",
                    (mission_id, cursor),
                ).fetchone()["count"]
                unclassified_count = db.execute(
                    "SELECT COUNT(*) AS count FROM events WHERE mission_id=? AND sequence>? "
                    "AND event_type!='coordination_trace_recorded'",
                    (mission_id, cursor),
                ).fetchone()["count"]
            if missing_sources or missing_events or unresolved_count:
                currentness = "missing_records"
            elif cursor is None:
                currentness = "unknown_without_boundary"
            elif scoped_count:
                currentness = "newer_scoped_events"
            elif unclassified_count:
                currentness = "unclassified_newer_events"
            elif coverage["known_gaps"]:
                currentness = "known_gaps"
            else:
                currentness = "as_of_boundary"
            return {"reference": exact, "level": body["level"], "summary": body["summary"],
                    "coverage_status": "declared", "currentness": currentness,
                    "coverage_basis": coverage.get("basis", "unspecified"),
                    "source_refs": body["source_refs"],
                    "source_completeness": "not_established",
                    "generation_state": coverage["generation_state"],
                    "scope": coverage["scope"], "through_event_sequence": cursor,
                    "event_refs": coverage["event_refs"], "known_gaps": coverage["known_gaps"],
                    "observed_event_sequence": observed_tip,
                    "missing_source_refs": missing_sources, "missing_event_refs": missing_events,
                    "newer_scoped_event_count": scoped_count,
                    "newer_unclassified_event_count": unclassified_count,
                    "unresolved_newer_trace_count": unresolved_count}
        finally:
            db.close()

    def usage_totals(self, mission_id: str) -> list[dict]:
        db = self.store._connect()
        try:
            rows = db.execute("SELECT r.* FROM records r JOIN heads h USING(kind,id,version) WHERE r.kind='trace_usage' AND json_extract(r.body,'$.mission_id')=?", (mission_id,))
            totals = {}
            for row in rows:
                body = json.loads(row["body"])
                key = body["scope"], body["unit"], body["basis"]
                entry = totals.setdefault(key, {"scope": key[0], "unit": key[1], "basis": key[2], "amount": 0, "constituent_refs": []})
                entry["amount"] += body["amount"]
                entry["constituent_refs"].append({"kind": row["kind"], "id": row["id"], "version": row["version"]})
            return list(totals.values())
        finally:
            db.close()

    def progress(self, mission_id: str) -> dict:
        """Read per-operation work and peer waits without adding overlapping durations."""
        now, observed_at = self.store.clock(), self.store.now()
        db = self.store._connect()
        operations, peers, first_useful, last_progress = [], [], None, None
        next_actions = []
        try:
            rows = db.execute(
                "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) "
                "WHERE r.kind IN ('capacity_operation','mail_request','executor_artifact','board_proposal') "
                "AND json_extract(r.body,'$.mission_id')=? ORDER BY r.created_at,r.kind,r.id", (mission_id,))
            for row in rows:
                body = json.loads(row["body"])
                pointer = {"kind": row["kind"], "id": row["id"], "version": row["version"]}
                if row["kind"] == "capacity_operation":
                    known = {key: body.get(key, 0) for key in (
                        "active_seconds", "queue_wait_seconds", "quota_wait_seconds",
                        "provider_wait_seconds", "retry_wait_seconds")}
                    phase = body["phase"]
                    terminal = phase in {"completed", "failed", "canceled"}
                    ended = datetime.fromisoformat(row["created_at"]).timestamp() if terminal else now
                    if terminal and body.get("attempts") and body["attempts"][-1].get("completed_at") is not None:
                        ended = body["attempts"][-1]["completed_at"]
                    # Committed totals and the currently open wait remain separate.
                    open_wait = None
                    if body.get("wait_started_at") is not None:
                        open_wait = {"kind": body.get("wait_reason") or "unknown",
                                     "seconds": max(0, now - body["wait_started_at"])}
                    operations.append({"reference": pointer, "task_id": body["task_id"],
                        "phase": phase, "elapsed_seconds": max(0, ended - body["submitted_at"]),
                        "recorded_durations": known, "current_wait": open_wait,
                        "wake_at": body.get("wake_at"), "diagnostic": body.get("diagnostic"),
                        "external_outcome": "uncertain" if phase == "uncertain" else "see_saved_phase",
                        "timing_basis": "store_clock", "expected_latency": body.get("latency_category", "not_recorded")})
                    task = self.store.get("task", body["task_id"])
                    if task and task["body"].get("mission_id") == mission_id:
                        operations[-1]["expected_latency"] = task["body"].get("latencyClass", "not_recorded")
                elif row["kind"] == "mail_request":
                    started = datetime.fromisoformat(body["created_at"]).timestamp()
                    resolved = body.get("resolved_at")
                    ended = datetime.fromisoformat(resolved).timestamp() if resolved else now
                    peers.append({"reference": pointer, "status": body["status"],
                        "waiting_seconds": max(0, ended - started),
                        "created_at": body["created_at"], "delivered_at": body.get("delivered_at"),
                        "answered_at": body.get("answered_at"), "resolved_at": resolved,
                        "expected_latency": body.get("expected_latency", "not_recorded"),
                        "last_useful_progress_at": body.get("last_useful_progress_at")})
                    if progress := body.get("last_useful_progress_at"):
                        last_progress = max(last_progress or progress, progress)
                elif row["kind"] == "executor_artifact":
                    stamp = row["created_at"]
                    first_useful = min(first_useful or stamp, stamp)
                    last_progress = max(last_progress or stamp, stamp)
                elif body.get("status") in {"open", "deferred"}:
                    next_actions.append({"reference": pointer, "status": body["status"],
                                         "next_action": public_text(body.get("next_action"))})
            return {"mission_id": mission_id, "observed_at": observed_at,
                    "operations": operations, "peer_requests": peers,
                    "first_accepted_artifact_at": first_useful,
                    "last_meaningful_progress_at": last_progress,
                    "next_actions": next_actions,
                    "interpretation": "Per-operation and peer durations can overlap. They are not summed as mission latency; artifact acceptance does not prove scientific usefulness."}
        finally:
            db.close()
