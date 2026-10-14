"""Save and read the same accepted research view for Atlas and command-line review.

The base snapshot and its input/output references are immutable. Inspection
details resolve exact stored versions; missing records and corrupt retained bytes
are reported explicitly. This reader never starts a model or provider operation.
"""

from __future__ import annotations

import json
from contextlib import closing
from typing import Callable

from ora.contracts.models import ResearchSnapshot, SnapshotInspection

from .blobs import IntegrityFault
from .snapshot_storage import FORMAT as SNAPSHOT_STORAGE_FORMAT
from .snapshot_storage import accept_components, prepare_snapshot, read_snapshot
from .source_lineage import inherited_public_source
from .steps import explain_event
from .store import StateError, StateStore, Transaction, canonical, reference
from .view_policy import project_record, visible_in_mission


def ref(record):
    return {key: record[key] for key in ("kind", "id", "version")}


def save_snapshot(store: StateStore, operation_id: str, snapshot: dict, *, source_provenance: list[dict], attempt_provenance: list[dict], detail_refs: list[dict], accepted_outputs: list[dict], parent_ref: dict | None = None, followup_target: dict | None = None, initial_source_ids: list[str] | None = None, timing_basis: str = "observed_local", run_id: str | None = None, validate_output_projection: Callable[[Transaction, dict, dict], bool] | None = None) -> dict:
    validated = ResearchSnapshot.model_validate(snapshot)
    base = validated.model_dump(mode="json", by_alias=True, exclude={"inspection"})
    snapshot_ref = {"kind": "research_snapshot", "id": validated.snapshot_id, "version": 1}
    inspection = SnapshotInspection.model_validate({
        "snapshotReference": snapshot_ref, "initialSourceIds": initial_source_ids or [],
        "sources": source_provenance, "attempts": attempt_provenance,
        "records": [], "acceptedOutputReferences": accepted_outputs,
        "followupTarget": followup_target, "timingBasis": timing_basis,
    }).model_dump(mode="json", by_alias=True)
    blob, components = prepare_snapshot(store, base)
    request = {"mission_id": validated.mission.id, "blob": blob, "storage_format": SNAPSHOT_STORAGE_FORMAT, "inspection": inspection, "detail_refs": detail_refs, "parent": parent_ref}

    def save(tx):
        if bool(parent_ref) != bool(validated.parent_snapshot_id):
            raise StateError("Snapshot parent identity and exact parent reference must agree")
        parent = None
        if parent_ref:
            parent = tx.get(**parent_ref)
            if not parent or validated.parent_snapshot_id != parent["id"]:
                raise StateError("Follow-up needs its exact saved parent snapshot")
            parent_inspection = parent["body"]["inspection"]
            if followup_target not in parent_inspection["acceptedOutputReferences"]:
                raise StateError("Follow-up target is not an accepted parent output")
        if set(initial_source_ids or []) - {source.id for source in validated.sources}:
            raise StateError("Initial sources must exist in this snapshot")
        if len({item["sourceId"] for item in source_provenance}) != len(source_provenance):
            raise StateError("Source provenance is ambiguous within this snapshot")
        if {item["sourceId"] for item in source_provenance} != {source.id for source in validated.sources}:
            raise StateError("Every displayed source needs its exact provenance")
        if len({item["attemptId"] for item in attempt_provenance}) != len(attempt_provenance) or {item["attemptId"] for item in attempt_provenance} != {attempt.id for attempt in validated.attempts}:
            raise StateError("Every displayed attempt needs unambiguous provenance")
        for item in source_provenance:
            source = next((source for source in validated.sources if source.id == item["sourceId"]), None)
            copy_record = tx.get(**item["copyReference"])
            if not source or not copy_record or copy_record["kind"] != "source_copy" or source.copy_id != copy_record["id"] or source.version != copy_record["version"]:
                raise StateError("Source view does not identify its exact retained copy")
            if source.content_hash != copy_record["body"]["content_sha256"]:
                raise StateError("Source hash differs from its retained copy")
            copy_body = copy_record["body"]
            if copy_body["encounter"] not in item["encounterReferences"]:
                raise StateError("Source provenance must include the encounter that supplied this copy")
            inherited_source = None
            if item["origin"] == "reused":
                if parent is None or item.get("fromSnapshotId") != parent["id"]:
                    raise StateError("Reused source does not name the exact parent snapshot")
                inherited_source = next(
                    (previous for previous in parent["body"]["inspection"]["sources"]
                     if previous["copyReference"] == item["copyReference"]),
                    None,
                )
                if (
                    inherited_source is None
                    or inherited_source["sourceId"] != item["sourceId"]
                ):
                    raise StateError("Reused source differs from its declared saved origin")
            inherited_public = bool(
                inherited_source is not None and run_id
                and inherited_public_source(
                    tx, mission_id=validated.mission.id, run_id=run_id,
                    copy_ref=item["copyReference"],
                )
            )
            origin_mission_id = (
                copy_body.get("mission_id") if inherited_public else validated.mission.id
            )
            for pointer in item["encounterReferences"]:
                encounter = tx.get(**pointer)
                publication = encounter["body"].get("publication") if encounter else None
                navigation_ref = copy_body.get("navigation_ref")
                document_ref = copy_body.get("document_ref")
                if not encounter or encounter["kind"] != "source_encounter":
                    raise StateError("Source encounter is missing")
                if navigation_ref:
                    navigation = tx.get(**navigation_ref)
                    if (
                        publication is not None
                        or encounter["body"].get("navigation_ref") != navigation_ref
                        or navigation is None
                        or navigation["body"].get("mission_id") != origin_mission_id
                        or navigation["body"].get("content_sha256") != copy_body["content_sha256"]
                        or navigation["body"].get("final_url") != copy_body["reference"]
                    ):
                        raise StateError("Source encounter belongs to a different navigation")
                elif document_ref:
                    document = tx.get(**document_ref)
                    if (
                        publication is not None
                        or encounter["body"].get("document_ref") != document_ref
                        or document is None
                        or document["body"].get("mission_id") != origin_mission_id
                        or document["body"].get("content_sha256") != copy_body["content_sha256"]
                        or document["body"].get("final_url") != copy_body["reference"]
                    ):
                        raise StateError("Source encounter belongs to a different public fetch")
                elif (
                    not publication or publication["kind"] != "publication"
                    or publication["id"] != copy_body["publication"]["id"]
                ):
                    raise StateError("Source encounter belongs to a different publication")
                hit_ref = encounter["body"].get("retrieval_hit")
                if hit_ref and (
                    not (hit := tx.get(**hit_ref))
                    or hit["kind"] != "retrieval_hit"
                    or not (
                        visible_in_mission(hit, validated.mission.id)
                        or (inherited_source and pointer in inherited_source["encounterReferences"])
                    )
                ):
                    raise StateError("Source encounter does not resolve to an accessible retrieval hit")
            if (
                inherited_source is not None
                and inherited_source["encounterReferences"] != item["encounterReferences"]
            ):
                raise StateError("Reused source differs from its declared saved origin")
            if copy_body.get("blob"):
                store.read_blob(copy_body["blob"]["sha256"])
            for passage in source.passages:
                saved_passage = tx.get("source_passage", f"{copy_record['id']}:{passage.span_id}", version=1)
                if not saved_passage or saved_passage["body"]["text"] != passage.text or saved_passage["body"]["locator"] != passage.locator:
                    raise StateError("Passage differs from its exact saved evidence")
        passage_map = {(source.id, passage.span_id) for source in validated.sources for passage in source.passages}
        for claim in validated.claims:
            saved_claim = tx.get("claim", claim.id, claim.version)
            if not saved_claim or any(saved_claim["body"].get(key) != value for key, value in claim.model_dump(by_alias=True, exclude_none=True).items()):
                raise StateError("Claim differs from its exact accepted record")
        for item in [*validated.claims, *validated.artifacts]:
            citations = item.supports if hasattr(item, "supports") else item.citations
            if any((citation.source_id, citation.span_id) not in passage_map for citation in citations):
                raise StateError("Citation does not resolve within its own snapshot")
        for item in attempt_provenance:
            attempt = next((attempt for attempt in validated.attempts if attempt.id == item["attemptId"]), None)
            actor = tx.get(**item["instanceReference"])
            config = tx.get(**item["configurationReference"]) if item.get("configurationReference") else None
            if not attempt or not actor or actor["id"] != attempt.agent_id or not visible_in_mission(actor, validated.mission.id):
                raise StateError("Attempt instance does not match this mission's saved actor")
            if config and (config["body"]["instance_id"] != actor["id"] or not visible_in_mission(config, validated.mission.id)):
                raise StateError("Attempt configuration belongs to another actor or mission")
            for pointer in item.get("acceptedOutputReferences", []):
                output = tx.get(**pointer)
                producer = output["body"].get("producer_attempt_ref") if output else None
                recorded_attempt = tx.get(**producer) if producer else None
                if pointer not in accepted_outputs or not recorded_attempt or recorded_attempt["id"] != attempt.id or recorded_attempt["body"].get("agentId") != actor["id"]:
                    raise StateError("Accepted output does not identify this producer attempt")
            for pointer in item.get("callReferences", []):
                call = tx.get(**pointer)
                if not call or call["body"].get("attempt_id") != attempt.id or call["body"].get("task_id") != attempt.task_id or call["body"].get("instance_id") != actor["id"] or not config or call["body"].get("configuration_id") != config["id"]:
                    raise StateError("Call lineage does not match the saved attempt and configuration")
                actual_inputs = [{key: reference[key] for key in ("kind", "id", "version")} for reference in call["body"]["input_refs"]]
                if actual_inputs != item.get("inputReferences", []):
                    raise StateError("Displayed attempt inputs differ from its saved call manifest")
        for output in accepted_outputs:
            record = tx.get(**output)
            artifact = next((item for item in validated.artifacts if item.id == output["id"] and item.version == output["version"]), None)
            if not record or output["kind"] != "report" or artifact is None:
                raise StateError("Accepted output must resolve to this exact report version")
            projected = artifact.model_dump(by_alias=True)
            if validate_output_projection is not None:
                if not validate_output_projection(tx, record, projected):
                    raise StateError("Report projection differs from accepted output")
            else:
                for field in ("title", "body", "citations", "limitations"):
                    if record["body"].get(field) != projected[field]:
                        raise StateError("Report projection differs from accepted output")
        all_refs = detail_refs + accepted_outputs
        for item in source_provenance:
            all_refs += [item["copyReference"], *item["encounterReferences"]]
        for item in attempt_provenance:
            all_refs += [item["instanceReference"], *item.get("callReferences", []), *item.get("inputReferences", []), *item.get("acceptedOutputReferences", [])]
            if item.get("configurationReference"):
                all_refs.append(item["configurationReference"])
        for pointer in all_refs:
            target = tx.get(**pointer)
            if target is None:
                raise StateError("Inspection reference does not resolve to a saved version")
            if not visible_in_mission(target, validated.mission.id):
                raise StateError("Inspection reference belongs to another mission")
        saved_components = accept_components(tx, components)
        event_id = tx.emit(validated.mission.id, "snapshot_accepted", {"snapshot": snapshot_ref, "outputs": accepted_outputs})
        cursor = tx.db.execute("SELECT sequence FROM events WHERE id=?", (event_id,)).fetchone()[0]
        resolved_refs = list({(item["kind"], item["id"], item["version"]): item for item in all_refs}.values())
        saved = tx.put("research_snapshot", validated.snapshot_id, {**request, "detail_refs": resolved_refs, "event_cursor": cursor})
        for component in saved_components:
            tx.link(saved, component, "snapshot_part")
        for pointer in accepted_outputs:
            tx.link(saved, pointer, "depends_on")
        if parent_ref:
            tx.link(saved, parent_ref, "follows_snapshot")
        return ref(saved)

    return store.transact(operation_id, request, save, mission_id=validated.mission.id)


class _InspectionReader:
    """Reuse exact records inside one view's database read, never across views."""

    def __init__(self, store: StateStore, db):
        self.db = db
        self.records = Transaction(store, db)
        self.resolved: dict[tuple, dict | None] = {}

    def get(self, kind: str, id: str, version: int | None = None) -> dict | None:
        key = kind, id, version
        if key not in self.resolved:
            self.resolved[key] = self.records.get(kind, id, version)
        return self.resolved[key]

    def events(self, mission_id: str, *, after: int, limit: int) -> list[dict]:
        return [StateStore._event(row) for row in self.db.execute(
            "SELECT * FROM events WHERE mission_id=? AND sequence>? ORDER BY sequence LIMIT ?",
            (mission_id, after, limit),
        )]

    def review_status(self, record: dict) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT reason,upstream_kind,upstream_id,upstream_version "
            "FROM review_requirements WHERE kind=? AND id=? AND version=?",
            reference(record),
        )]


def load_snapshot(store: StateStore, snapshot_id: str, version: int = 1) -> dict:
    # A fresh, query-only transaction gives one internally consistent inspection
    # without opening a connection for every repeated event/detail reference.
    # Blob integrity is still checked on each view; nothing survives this call.
    with closing(store._connect()) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        return _load_snapshot(store, _InspectionReader(store, db), snapshot_id, version)


def _load_snapshot(store: StateStore, reader: _InspectionReader, snapshot_id: str, version: int) -> dict:
    record = reader.get("research_snapshot", snapshot_id, version)
    if record is None:
        raise StateError("Saved research snapshot is unavailable")
    snapshot = read_snapshot(store, record)
    inspection = json.loads(canonical(record["body"]["inspection"]))
    cursor, ceiling = 0, record["body"]["event_cursor"]
    while cursor < ceiling:
        page = reader.events(snapshot["mission"]["id"], after=cursor, limit=250)
        if not page:
            break
        for event in page:
            # Publishing a review view is not an agent work step. Keep the
            # immutable event in the store without replaying every prior view
            # (and its snapshot reference) into a later view's inspection.
            if event["sequence"] <= ceiling and event["type"] != "snapshot_accepted":
                inspection["steps"].append(explain_event(reader, event))
        cursor = page[-1]["sequence"]
    step_refs = [pointer for step in inspection["steps"] for pointer in [*step["inputReferences"], *step["outputReferences"]]]
    detail_refs = list({(item["kind"], item["id"], item["version"]): item for item in [*record["body"]["detail_refs"], *step_refs]}.values())
    for pointer in detail_refs:
        detail = reader.get(**pointer)
        if detail is None or not visible_in_mission(detail, snapshot["mission"]["id"]):
            data = {"availability": "missing", "explanation": "Exact saved version does not resolve"}
        else:
            data = project_record(detail)
            data["recordedAt"] = detail["created_at"]
            data["reviewRequired"] = reader.review_status(detail)
            blob = data.get("blob")
            if blob:
                try:
                    store.read_blob(blob["sha256"])
                    data["contentIntegrity"] = "verified"
                except IntegrityFault:
                    data["contentIntegrity"] = "unavailable_or_corrupt"
                    for source in snapshot["sources"]:
                        if source["copyId"] == pointer["id"]:
                            source["retrievalStatus"] = "content_integrity_fault"
                            source["passages"] = []
                            snapshot["diagnostics"].append({"id": f"integrity-{source['id']}", "kind": "source_integrity", "status": "degraded", "message": "The saved source bytes are unavailable or corrupt. Historical findings remain visible, but citation replay requires repair.", "occurredAt": store.now()})
        inspection["records"].append({"reference": pointer, "label": f"{pointer['kind'].replace('_', ' ')} · {pointer['id']} · v{pointer['version']}", "data": data})
    snapshot["inspection"] = inspection
    for attempt in snapshot["attempts"]:
        if attempt["returnedReasoning"].get("text"):
            attempt["returnedReasoning"] = {"availability": "restricted", "text": None}
    return ResearchSnapshot.model_validate(snapshot).model_dump(mode="json", by_alias=True)
