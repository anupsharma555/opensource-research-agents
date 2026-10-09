"""Versioned evidence records over the shared state store.

Publication identity, study identity, discovery encounters, and inspected copies
are different objects. This module never infers identity from a matching title.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
import uuid
from difflib import SequenceMatcher
from typing import Any

from .identity import normalize_identifier, normalize_identifiers


class EvidenceError(ValueError):
    """A source assertion lacks the evidence needed for this operation."""


PUBLICATION_KINDS = frozenset(
    {"journal_report", "preprint", "abstract", "correction", "retraction", "supplement", "dataset", "registry_report"}
)
FAMILY_RELATIONS = frozenset(
    {"reports_study", "preprint_of_study", "abstract_of_study", "registry_for_study", "supplement_to_study", "dataset_from_study"}
)
PUBLICATION_RELATIONS = frozenset(
    {"preprint_of", "abstract_of", "supplement_to", "correction_of", "retraction_of", "same_cohort_report"}
)
ACCESS_RANK = {
    "provider_summary": 0,
    "metadata": 1,
    "teaser": 2,
    "abstract": 3,
    "passage": 4,
    "full_text": 5,
}
_WORDS = re.compile(r"[\w]+", re.UNICODE)
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _ref(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in ("kind", "id", "version")}


def _require_ref(record: dict[str, Any], kind: str | None = None) -> dict[str, Any]:
    if not isinstance(record, dict) or not all(key in record for key in ("kind", "id", "version")):
        raise EvidenceError("An exact kind, ID, and version reference is required")
    if kind is not None and record["kind"] != kind:
        raise EvidenceError(f"Expected a {kind} reference")
    if not isinstance(record["id"], str) or not record["id"] or not isinstance(record["version"], int):
        raise EvidenceError("Invalid record reference")
    return {key: record[key] for key in ("kind", "id", "version")}


def _exact(tx: Any, ref: dict[str, Any]) -> dict[str, Any]:
    found = tx.get(ref["kind"], ref["id"], ref["version"])
    if found is None:
        raise EvidenceError(f"Missing exact {ref['kind']} version")
    return found


def _identity_anchor(tx: Any, ref: dict[str, Any]) -> dict[str, Any]:
    """Identity relations attach to v1, while mutable metadata has later heads."""
    first = tx.get(ref["kind"], ref["id"], 1)
    if first is None:
        raise EvidenceError(f"Missing {ref['kind']} identity anchor")
    return first


class EvidenceLibrary:
    """Domain operations; ``ora.state`` owns all transactions and file bytes."""

    def __init__(self, store: Any):
        self.store = store

    def register_publication(
        self,
        operation_id: str,
        *,
        identifiers: dict[str, str],
        title: str,
        kind: str = "journal_report",
        verification: str,
    ) -> dict[str, Any]:
        """Bind explicitly verified identifiers; preserve conflicts for review."""
        if kind not in PUBLICATION_KINDS or not title.strip() or not verification.strip():
            raise EvidenceError("Publication kind, title, and verification basis are required")
        normalized = normalize_identifiers(identifiers)
        if kind == "dataset" and any(key != "dataset_doi" for key in normalized):
            raise EvidenceError("Dataset identity must not bind publication aliases")
        if kind != "dataset" and "dataset_doi" in normalized:
            raise EvidenceError("Dataset DOI must not bind a publication")
        request = {"kind": kind, "title": title, "identifiers": normalized, "verification": verification}

        def action(tx: Any) -> dict[str, Any]:
            matches = {
                (record["kind"], record["id"]): record
                for key, value in normalized.items()
                if (record := tx.resolve(f"publication:{key}", value)) is not None
            }
            if len(matches) > 1:
                review = tx.put(
                    "identity_review",
                    f"identity-review-{uuid.uuid4().hex}",
                    {"assertion": request, "candidate_refs": [_ref(x) for x in matches.values()]},
                )
                return {"status": "ambiguous", "review_ref": _ref(review), "candidate_refs": [_ref(x) for x in matches.values()]}
            if matches:
                bound = next(iter(matches.values()))
                current = tx.get("publication", bound["id"])
                if current["kind"] != "publication":
                    raise EvidenceError("Identifier alias does not resolve to a publication")
                body = dict(current["body"])
                combined = dict(body["verified_identifiers"])
                combined.update(normalized)
                titles = list(body["titles"])
                if title not in titles:
                    titles.append(title)
                if combined != body["verified_identifiers"] or titles != body["titles"]:
                    body.update(verified_identifiers=combined, titles=titles)
                    current = tx.put("publication", current["id"], body, expected_version=current["version"])
                status = "matched"
            else:
                current = tx.put(
                    "publication",
                    f"publication-{uuid.uuid4().hex}",
                    {"report_kind": kind, "titles": [title], "verified_identifiers": normalized, "verification": verification, "accepted_copy": None},
                )
                status = "created" if normalized else "provisional"
            for key, value in normalized.items():
                tx.bind(f"publication:{key}", value, "publication", current["id"])
            return {"status": status, "publication_ref": _ref(current)}

        return self.store.transact(operation_id, request, action)

    def resolve_publication(self, kind: str, value: str) -> dict[str, Any] | None:
        normalized = normalize_identifier(kind, value)
        return self.store.resolve(f"publication:{kind}", normalized)

    def suggest_title_matches(self, title: str) -> list[dict[str, Any]]:
        """Offer close-title candidates for review without merging."""
        if not isinstance(title, str) or not title.strip():
            raise EvidenceError("Title query must be nonempty")
        key = " ".join(_WORDS.findall(title.casefold()))
        matches = []
        after_id = ""
        while True:
            page = self.store.records("publication", limit=1000, after_id=after_id)
            if not page:
                break
            for record in page:
                if any(
                    (candidate := " ".join(_WORDS.findall(value.casefold()))) == key
                    or (len(key) >= 12 and SequenceMatcher(None, key, candidate).ratio() >= 0.9)
                    for value in record["body"]["titles"]
                ):
                    matches.append(_ref(record))
            after_id = page[-1]["id"]
        return matches

    def register_study_family(
        self,
        operation_id: str,
        *,
        registry_ids: list[str],
        verification: str,
    ) -> dict[str, Any]:
        """Bind known registry aliases, retaining a conflict rather than merging it."""
        if not verification.strip():
            raise EvidenceError("Study-family verification basis is required")
        identifiers = sorted({normalize_identifier("registry", value) for value in registry_ids})
        if len(identifiers) > 1:
            raise EvidenceError("Register registry IDs separately, then link them with exact evidence")
        request = {"registry_ids": identifiers, "verification": verification}

        def action(tx: Any) -> dict[str, Any]:
            matches = {
                record["id"]: record
                for value in identifiers
                if (record := tx.resolve("study_registry", value)) is not None
            }
            if len(matches) > 1:
                review = tx.put(
                    "family_review",
                    f"family-review-{uuid.uuid4().hex}",
                    {"assertion": request, "candidate_refs": [_ref(x) for x in matches.values()]},
                )
                return {"status": "ambiguous", "review_ref": _ref(review), "candidate_refs": [_ref(x) for x in matches.values()]}
            if matches:
                bound = next(iter(matches.values()))
                current = tx.get("study_family", bound["id"])
                body = dict(current["body"])
                merged = sorted(set(body["registry_ids"]) | set(identifiers))
                if merged != body["registry_ids"]:
                    body["registry_ids"] = merged
                    current = tx.put("study_family", current["id"], body, expected_version=current["version"])
                status = "matched"
            else:
                current = tx.put(
                    "study_family",
                    f"study-{uuid.uuid4().hex}",
                    {"registry_ids": identifiers, "verification": verification},
                )
                status = "created" if identifiers else "provisional"
            for value in identifiers:
                tx.bind("study_registry", value, "study_family", current["id"])
            return {"status": status, "family_ref": _ref(current)}

        return self.store.transact(operation_id, request, action)

    def link_publication_to_family(
        self,
        operation_id: str,
        publication_ref: dict[str, Any],
        family_ref: dict[str, Any],
        *,
        relation: str,
        basis: str,
    ) -> dict[str, Any]:
        if relation not in FAMILY_RELATIONS or not basis.strip():
            raise EvidenceError("Typed study/report relation and linkage basis are required")
        publication_ref = _require_ref(publication_ref, "publication")
        family_ref = _require_ref(family_ref, "study_family")
        request = {"publication": publication_ref, "family": family_ref, "relation": relation, "basis": basis}

        def action(tx: Any) -> dict[str, Any]:
            publication = _identity_anchor(tx, publication_ref)
            family = _identity_anchor(tx, family_ref)
            tx.link(publication, family, relation)
            return {"publication_ref": publication_ref, "family_ref": family_ref, "relation": relation}

        return self.store.transact(operation_id, request, action)

    def link_publications(
        self,
        operation_id: str,
        source_ref: dict[str, Any],
        target_ref: dict[str, Any],
        *,
        relation: str,
        basis: str,
    ) -> dict[str, Any]:
        """Record a typed report relationship without merging publication IDs."""
        source_ref = _require_ref(source_ref, "publication")
        target_ref = _require_ref(target_ref, "publication")
        if source_ref["id"] == target_ref["id"] or relation not in PUBLICATION_RELATIONS or not basis.strip():
            raise EvidenceError("Distinct publications need a typed relation and evidence basis")
        request = {"source": source_ref, "target": target_ref, "relation": relation, "basis": basis}

        def action(tx: Any) -> dict[str, Any]:
            source = _identity_anchor(tx, source_ref)
            target = _identity_anchor(tx, target_ref)
            tx.link(source, target, relation)
            assertion = tx.put("publication_link_evidence", f"publication-link-{uuid.uuid4().hex}", request)
            return {"relation": relation, "assertion_ref": _ref(assertion)}

        return self.store.transact(operation_id, request, action)

    def link_registry_families(
        self,
        operation_id: str,
        left_ref: dict[str, Any],
        right_ref: dict[str, Any],
        *,
        verified: bool,
        basis: str,
        evidence_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """A verified cross-registry edge can group pre-existing family records."""
        left_ref = _require_ref(left_ref, "study_family")
        right_ref = _require_ref(right_ref, "study_family")
        if left_ref["id"] == right_ref["id"] or not basis.strip():
            raise EvidenceError("Distinct family records and explicit linkage basis are required")
        evidence_ref = _require_ref(evidence_ref) if evidence_ref else None
        if verified and evidence_ref is None:
            raise EvidenceError("Verified cross-registry link needs an exact evidence record")
        if verified and evidence_ref["kind"] != "source_passage":
            raise EvidenceError("Verified cross-registry link needs a checked source passage")
        relation = "same_trial_verified" if verified else "possible_same_trial"
        request = {"left": left_ref, "right": right_ref, "relation": relation,
                   "basis": basis, "evidence_ref": evidence_ref}

        def action(tx: Any) -> dict[str, Any]:
            left = _identity_anchor(tx, left_ref)
            right = _identity_anchor(tx, right_ref)
            evidence = _exact(tx, evidence_ref) if evidence_ref else None
            if verified and evidence["body"].get("inspection_state") != "checked":
                raise EvidenceError("Verified cross-registry link needs a checked source passage")
            assertion = tx.put("family_link_evidence", f"family-link-{uuid.uuid4().hex}", request)
            tx.link(assertion, left, "links_family")
            tx.link(assertion, right, "links_family")
            if evidence:
                tx.link(assertion, evidence, "depends_on")
            return {"relation": relation, "assertion_ref": _ref(assertion)}

        return self.store.transact(operation_id, request, action)

    def study_cluster(self, family_ref: dict[str, Any]) -> list[str]:
        """Return IDs joined by verified edges only; candidates stay separate."""
        family_ref = _require_ref(family_ref, "study_family")
        adjacency: dict[str, set[str]] = {}
        after_id = ""
        while True:
            page = self.store.records("family_link_evidence", limit=1000, after_id=after_id)
            if not page:
                break
            for assertion in page:
                body = assertion["body"]
                if body["relation"] != "same_trial_verified" or self.store.review_status(assertion):
                    continue
                left, right = body["left"]["id"], body["right"]["id"]
                adjacency.setdefault(left, set()).add(right)
                adjacency.setdefault(right, set()).add(left)
            after_id = page[-1]["id"]
        pending = [family_ref["id"]]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            pending.extend(adjacency.get(current, set()) - visited)
        return sorted(visited)

    def study_families_for_publication(self, publication_ref: dict[str, Any]) -> list[dict[str, Any]]:
        """Expose typed study links with a stable verified-cluster count key."""
        publication_ref = _require_ref(publication_ref, "publication")
        anchor = {**publication_ref, "version": 1}
        if self.store.get(**anchor) is None:
            raise EvidenceError("Publication identity anchor is missing")
        relationships = []
        for edge in self.store.links(anchor):
            if edge["relation"] not in FAMILY_RELATIONS:
                continue
            cluster = self.study_cluster(edge["target"])
            relationships.append({
                "relation": edge["relation"], "family_ref": edge["target"],
                "verified_cluster_ids": cluster, "count_key": cluster[0],
            })
        return relationships

    def record_encounter(
        self,
        operation_id: str,
        *,
        publication_ref: dict[str, Any] | None,
        retrieval_hit_ref: dict[str, Any] | None,
        status: str,
        access_surface: str,
        retrieved_at: str,
        route: str,
        actor_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"accepted", "partial", "failed", "unresolved"}:
            raise EvidenceError("Unknown encounter status")
        if access_surface not in ACCESS_RANK or not retrieved_at or not route:
            raise EvidenceError("Encounter needs an access surface, time, and route")
        publication_ref = _require_ref(publication_ref, "publication") if publication_ref else None
        retrieval_hit_ref = _require_ref(retrieval_hit_ref, "retrieval_hit") if retrieval_hit_ref else None
        actor_ref = _require_ref(actor_ref, "agent_instance") if actor_ref else None
        request = {
            "publication": publication_ref, "retrieval_hit": retrieval_hit_ref, "status": status,
            "access_surface": access_surface, "retrieved_at": retrieved_at, "route": route, "actor": actor_ref,
        }

        def action(tx: Any) -> dict[str, Any]:
            publication = _identity_anchor(tx, publication_ref) if publication_ref else None
            hit = _exact(tx, retrieval_hit_ref) if retrieval_hit_ref else None
            actor = _exact(tx, actor_ref) if actor_ref else None
            encounter_key = None
            if hit:
                source = hit["body"]["source"]
                hit_status = {
                    "available": "accepted", "limited": "partial",
                    "unavailable": "failed", "unknown": "unresolved",
                }[source["access_status"]]
                if (
                    status != hit_status
                    or access_surface != source["access_surface"]
                    or retrieved_at != hit["body"]["retrieved_at"]
                    or route != hit["body"]["route"]
                ):
                    raise EvidenceError("Encounter disagrees with its saved retrieval hit")
                if publication:
                    # A retry may reach this handoff under a new operation ID.
                    # One exact hit/publication pair is one encounter; different
                    # hits and routes keep their own encounters.
                    encounter_key = hashlib.sha256(
                        f"{publication['id']}:{hit['id']}:{hit['version']}".encode()
                    ).hexdigest()
                    prior = tx.resolve("source_encounter.retrieval_hit", encounter_key)
                    if prior:
                        if actor:
                            tx.link(prior, actor, "observed_by")
                        return {"encounter_ref": _ref(prior), "status": prior["body"]["status"]}
            encounter = tx.put("source_encounter", f"encounter-{uuid.uuid4().hex}", request)
            if encounter_key:
                tx.bind("source_encounter.retrieval_hit", encounter_key, "source_encounter", encounter["id"])
            if publication:
                tx.link(encounter, publication, "encounter_of_publication")
            if hit:
                tx.link(encounter, hit, "from_retrieval_hit")
            if actor:
                tx.link(encounter, actor, "observed_by")
            return {"encounter_ref": _ref(encounter), "status": status}

        return self.store.transact(operation_id, request, action)

    def record_retrieval_hit(
        self,
        operation_id: str,
        publication_ref: dict[str, Any],
        retrieval_hit_ref: dict[str, Any],
    ) -> dict[str, Any]:
        """Accept one retrieval candidate as an encounter, never as study identity."""
        retrieval_hit_ref = _require_ref(retrieval_hit_ref, "retrieval_hit")
        hit = self.store.get(**retrieval_hit_ref)
        if hit is None or hit["body"].get("schema") != "ora.retrieval.hit.v1":
            raise EvidenceError("Expected an exact accepted retrieval-hit record")
        source = hit["body"]["source"]
        status = {
            "available": "accepted", "limited": "partial",
            "unavailable": "failed", "unknown": "unresolved",
        }[source["access_status"]]
        return self.record_encounter(
            operation_id,
            publication_ref=publication_ref,
            retrieval_hit_ref=retrieval_hit_ref,
            status=status,
            access_surface=source["access_surface"],
            retrieved_at=hit["body"]["retrieved_at"],
            route=hit["body"]["route"],
        )

    def retain_retrieval_hit_copy(
        self,
        operation_id: str,
        publication_ref: dict[str, Any],
        encounter_ref: dict[str, Any],
        *,
        source_version: str,
        passages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Retain only the normalized hit's allowed content and exact lineage."""
        encounter_ref = _require_ref(encounter_ref, "source_encounter")
        encounter = self.store.get(**encounter_ref)
        if encounter is None or not encounter["body"].get("retrieval_hit"):
            raise EvidenceError("Encounter has no saved retrieval-hit source")
        hit = self.store.get(**encounter["body"]["retrieval_hit"])
        if hit is None:
            raise EvidenceError("Saved retrieval hit is unavailable")
        source = hit["body"]["source"]
        if source["origin"] != "original_source":
            raise EvidenceError("Provider-generated summary is not a source copy")
        if encounter["body"]["status"] == "failed":
            raise EvidenceError("Unavailable hit cannot become an inspected copy")
        retained_text = source["content"]
        can_retain = source["rights_status"] == "permitted" and retained_text is not None
        if not can_retain and passages:
            raise EvidenceError("No passage text may be retained from a rights-limited hit")
        content = retained_text.encode("utf-8") if can_retain else None
        reference = source["original_url"] or f"retrieval-hit:{hit['id']}"
        effective_surface = source["access_surface"] if content is not None else "metadata"
        return self.add_copy(
            operation_id,
            publication_ref,
            encounter_ref,
            content=content,
            content_sha256=None if content is not None else source["content_sha256"],
            mime_type="text/plain" if content is not None else "application/x-source-locator",
            source_version=source_version,
            access_surface=effective_surface,
            retrieved_at=hit["body"]["retrieved_at"],
            rights_basis=f"Normalized retrieval rights={source['rights_status']}; see exact retrieval hit",
            retain_bytes=can_retain,
            reference=reference,
            passages=passages or [],
            inspection_state="provider_asserted",
            validation_record=None,
            scientific_metadata=source.get("scientific_metadata", {}),
            structured_content=source.get("structured_content", {}) if can_retain else {},
            omitted_fields=source.get("omitted_fields", []),
            mapping_version=hit["body"].get("mapping_version"),
        )

    def add_copy(
        self,
        operation_id: str,
        publication_ref: dict[str, Any],
        encounter_ref: dict[str, Any],
        *,
        content: bytes | None,
        content_sha256: str | None,
        mime_type: str,
        source_version: str,
        access_surface: str,
        retrieved_at: str,
        rights_basis: str,
        retain_bytes: bool,
        reference: str,
        passages: list[dict[str, Any]],
        inspection_state: str = "provider_asserted",
        validation_record: str | None = None,
        scientific_metadata: dict[str, Any] | None = None,
        structured_content: dict[str, Any] | None = None,
        omitted_fields: list[dict[str, str]] | None = None,
        mapping_version: str | None = None,
    ) -> dict[str, Any]:
        """Retain an inspected copy and exact passages without overwriting old ones."""
        publication_ref = _require_ref(publication_ref, "publication")
        encounter_ref = _require_ref(encounter_ref, "source_encounter")
        if access_surface not in ACCESS_RANK or not mime_type or not source_version or not retrieved_at or not rights_basis:
            raise EvidenceError("Copy needs source version, access depth, MIME type, retrieval time, and rights basis")
        if not reference:
            raise EvidenceError("Copy needs a replay URL or allowed locator")
        if inspection_state not in {"provider_asserted", "checked"}:
            raise EvidenceError("Unknown source inspection state")
        if inspection_state == "checked" and not validation_record:
            raise EvidenceError("Checked copy requires a validation record")
        if retain_bytes and content is None:
            raise EvidenceError("Cannot retain absent source bytes")
        if (scientific_metadata is not None and not isinstance(scientific_metadata, dict)) or (
            structured_content is not None and not isinstance(structured_content, dict)
        ):
            raise EvidenceError("Scientific metadata and structured content must be objects")
        if structured_content and not retain_bytes:
            raise EvidenceError("Structured source content needs permitted retained bytes")
        if omitted_fields is not None and not isinstance(omitted_fields, list):
            raise EvidenceError("Omitted source fields must be a list")
        if not retain_bytes and passages:
            raise EvidenceError("Locator-only copy cannot retain passage text")
        if content is None and not passages and ACCESS_RANK[access_surface] > ACCESS_RANK["metadata"]:
            raise EvidenceError("Unretained content cannot claim an inspected text surface")
        actual_hash = hashlib.sha256(content).hexdigest() if content is not None else None
        if content_sha256 is not None and (not _DIGEST.fullmatch(content_sha256) or (actual_hash and actual_hash != content_sha256)):
            raise EvidenceError("Supplied source checksum disagrees with inspected bytes")
        checksum = content_sha256 or actual_hash
        seen_spans: set[str] = set()
        text = None
        if content is not None and mime_type.startswith("text/"):
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise EvidenceError("Text copy is not valid UTF-8") from exc
        for passage in passages:
            if not isinstance(passage, dict) or not all(passage.get(key) for key in ("span_id", "locator", "text")):
                raise EvidenceError("Each retained passage needs span ID, locator, and text")
            if passage["span_id"] in seen_spans:
                raise EvidenceError("Passage IDs must be unique within a copy")
            seen_spans.add(passage["span_id"])
            start, end = passage.get("start"), passage.get("end")
            if (start is None) != (end is None):
                raise EvidenceError("Text offsets must supply both start and end")
            if start is not None:
                if text is None or not isinstance(start, int) or not isinstance(end, int) or text[start:end] != passage["text"]:
                    raise EvidenceError("Passage offsets do not match retained text")
            elif not passage.get("validation_record"):
                raise EvidenceError("Non-offset passage needs an extraction validation record")
        # Refuse disallowed bytes before put_blob; the transaction checks again
        # because rights or identity assertions cannot rely on a stale read.
        observed_encounter = self.store.get(**encounter_ref)
        if observed_encounter is None or observed_encounter["body"]["publication"] != publication_ref:
            raise EvidenceError("Encounter belongs to a different or missing publication")
        if observed_encounter["body"]["status"] == "failed":
            raise EvidenceError("Failed encounter cannot yield an inspected copy")
        if observed_encounter["body"]["status"] == "unresolved" and retain_bytes:
            raise EvidenceError("Unresolved encounter permits locator-only retention")
        observed_hit_ref = observed_encounter["body"].get("retrieval_hit")
        if observed_hit_ref:
            observed_hit = self.store.get(**observed_hit_ref)
            if observed_hit is None:
                raise EvidenceError("Saved retrieval hit is unavailable")
            source = observed_hit["body"]["source"]
            if source["origin"] != "original_source":
                raise EvidenceError("Provider-generated summary is not a source copy")
            if retain_bytes and source["rights_status"] != "permitted":
                raise EvidenceError("Retrieval receipt did not permit retaining source bytes")
        blob = self.store.put_blob(content) if retain_bytes and content is not None else None
        request = {
            "publication": publication_ref, "encounter": encounter_ref, "content_sha256": checksum,
            "mime_type": mime_type, "source_version": source_version, "access_surface": access_surface, "retrieved_at": retrieved_at,
            "rights_basis": rights_basis, "retain_bytes": retain_bytes, "reference": reference,
            "passages": passages, "blob": blob, "inspection_state": inspection_state,
            "validation_record": validation_record,
            "scientific_metadata": scientific_metadata or {},
            "structured_content": structured_content or {},
            "omitted_fields": omitted_fields or [],
            "mapping_version": mapping_version,
        }

        def action(tx: Any) -> dict[str, Any]:
            _exact(tx, publication_ref)
            publication = tx.get("publication", publication_ref["id"])
            encounter = _exact(tx, encounter_ref)
            if encounter["body"]["status"] == "failed":
                raise EvidenceError("Failed encounter cannot yield an inspected copy")
            if encounter["body"]["status"] == "unresolved" and (retain_bytes or passages):
                raise EvidenceError("Unresolved encounter permits locator-only retention")
            if encounter["body"]["publication"] != publication_ref:
                raise EvidenceError("Encounter belongs to a different publication")
            hit_ref = encounter["body"].get("retrieval_hit")
            if hit_ref:
                hit_source = _exact(tx, hit_ref)["body"]["source"]
                same_surface = access_surface == hit_source["access_surface"]
                locator_downgrade = access_surface == "metadata" and not retain_bytes and not passages
                if hit_source["origin"] != "original_source" or not (same_surface or locator_downgrade):
                    raise EvidenceError("Provider summary or changed access depth is not a source copy")
                if retain_bytes and hit_source["rights_status"] != "permitted":
                    raise EvidenceError("Retrieval receipt did not permit retaining source bytes")
            copy_id = f"source-copy-{uuid.uuid4().hex}"
            passage_ids = [f"{copy_id}:{item['span_id']}" for item in passages]
            body = {key: value for key, value in request.items() if key != "passages"}
            body["passage_ids"] = passage_ids
            copy = tx.put("source_copy", copy_id, body)
            tx.link(copy, _identity_anchor(tx, publication_ref), "copy_of_publication")
            tx.link(copy, encounter, "copy_from_encounter")
            passage_refs = []
            for item, passage_id in zip(passages, passage_ids, strict=True):
                passage = tx.put("source_passage", passage_id, {
                    **item, "copy_ref": _ref(copy), "inspection_state": inspection_state,
                })
                tx.link(passage, copy, "depends_on")
                passage_refs.append(_ref(passage))
            accepted = publication["body"].get("accepted_copy")
            old_rank = ACCESS_RANK[accepted["access_surface"]] if accepted else -1
            promoted = (
                inspection_state == "checked"
                and encounter["body"]["status"] in {"accepted", "partial"}
                and ACCESS_RANK[access_surface] > old_rank
            )
            if promoted:
                updated = dict(publication["body"])
                updated["accepted_copy"] = {**_ref(copy), "access_surface": access_surface}
                tx.put("publication", publication["id"], updated, expected_version=publication["version"])
            return {"copy_ref": _ref(copy), "passage_refs": passage_refs, "promoted": promoted}

        return self.store.transact(operation_id, request, action)

    def link_claim(
        self, operation_id: str, claim_ref: dict[str, Any], evidence_ref: dict[str, Any]
    ) -> dict[str, Any]:
        """Connect a specific claim version to a specific inspected copy or passage."""
        claim_ref = _require_ref(claim_ref)
        evidence_ref = _require_ref(evidence_ref)
        if evidence_ref["kind"] not in {"source_copy", "source_passage"}:
            raise EvidenceError("Claim support requires an inspected copy or passage")
        request = {"claim": claim_ref, "evidence": evidence_ref}

        def action(tx: Any) -> dict[str, Any]:
            claim = _exact(tx, claim_ref)
            evidence = _exact(tx, evidence_ref)
            tx.link(claim, evidence, "depends_on")
            return request

        return self.store.transact(operation_id, request, action)

    def record_correction(
        self,
        operation_id: str,
        affected_copy_ref: dict[str, Any],
        notice_copy_ref: dict[str, Any],
        *,
        kind: str,
        reason: str,
        affected_passage_refs: list[dict[str, Any]] | None = None,
        mission_id: str = "",
    ) -> dict[str, Any]:
        """Keep old records, then queue only claims depending on affected bytes."""
        affected_copy_ref = _require_ref(affected_copy_ref, "source_copy")
        notice_copy_ref = _require_ref(notice_copy_ref, "source_copy")
        if affected_copy_ref == notice_copy_ref or kind not in {"correction", "retraction"} or not reason.strip():
            raise EvidenceError("Correction or retraction needs a precise reason")
        passage_refs = [_require_ref(ref, "source_passage") for ref in (affected_passage_refs or [])]
        request = {"affected": affected_copy_ref, "notice": notice_copy_ref, "kind": kind,
                   "reason": reason, "affected_passages": passage_refs, "mission_id": mission_id}

        def action(tx: Any) -> dict[str, Any]:
            affected = _exact(tx, affected_copy_ref)
            notice = _exact(tx, notice_copy_ref)
            notice_publication_ref = notice["body"]["publication"]
            notice_publication = _exact(tx, notice_publication_ref)
            if notice_publication["body"]["report_kind"] != kind:
                raise EvidenceError("Notice publication kind does not match the asserted source change")
            targets = []
            for passage_ref in passage_refs:
                passage = _exact(tx, passage_ref)
                if passage["body"]["copy_ref"] != affected_copy_ref:
                    raise EvidenceError("Correction passage belongs to a different copy")
                targets.append(passage)
            tx.link(notice, affected, kind)
            review_refs = []
            for target in targets or [affected]:
                review_refs.extend(tx.invalidate(target, f"{kind}: {reason}"))
            if mission_id:
                tx.emit(mission_id, "source_review_required", {
                    "kind": kind, "affected_copy_ref": affected_copy_ref,
                    "notice_copy_ref": notice_copy_ref, "review_refs": review_refs,
                })
            return {"affected_copy_ref": affected_copy_ref, "notice_copy_ref": notice_copy_ref,
                    "affected_passage_refs": passage_refs, "review_refs": review_refs}

        return self.store.transact(operation_id, request, action, mission_id=mission_id)

    def get_passage(self, passage_id: str) -> dict[str, Any] | None:
        passage = self.store.get("source_passage", passage_id)
        if passage is None:
            return None
        copy = self.store.get(**passage["body"]["copy_ref"])
        if copy is None:
            raise EvidenceError("Passage points to a missing retained copy")
        publication = self.store.get(**copy["body"]["publication"])
        return {
            "passage": passage, "copy": copy, "publication": publication,
            "needs_review": self.store.review_status(passage),
        }

    def lookup_exact(
        self, kind: str, identifier: str, version: int | None = None, *,
        queued_at: float | None = None, include_content: bool = False,
    ) -> dict[str, Any]:
        """Resolve exact evidence, timing only phases actually performed.

        ``queued_at`` is an optional same-process monotonic enqueue timestamp.
        Source bytes are read only for an explicitly requested retained copy.
        """
        if kind not in {"publication", "study_family", "source_encounter", "source_copy", "source_passage"}:
            raise EvidenceError("Unsupported exact evidence lookup kind")
        started = time.perf_counter()
        if queued_at is not None and (
            isinstance(queued_at, bool) or not isinstance(queued_at, (int, float))
            or not math.isfinite(queued_at) or queued_at > started
        ):
            raise EvidenceError("Queue timestamp must be a prior monotonic time from this process")
        queue_ms = (started - queued_at) * 1000 if queued_at is not None else None
        measured = self.store.get_measured(kind, identifier, version)
        record = measured["record"]
        artifact_ms = None
        artifact_status = "not_requested"
        content_bytes = None
        if include_content:
            if kind != "source_copy":
                artifact_status = "not_applicable"
            elif record is None:
                artifact_status = "not_found"
            elif not record["body"].get("retain_bytes") or not record["body"].get("blob"):
                artifact_status = "not_retained"
            else:
                read_started = time.perf_counter()
                content_bytes = self.store.read_blob(record["body"]["blob"]["sha256"])
                artifact_ms = (time.perf_counter() - read_started) * 1000
                artifact_status = "read_and_verified"
        response = {
            "record": record,
            "needs_review": self.store.review_status(record) if record else [],
            "metadata_lookup_ms": measured["elapsed_ms"],
            "connection_ms": measured["connection_ms"],
            "index_ms": measured["index_ms"],
            "decode_ms": measured["decode_ms"],
            "artifact_read_ms": artifact_ms,
            "artifact_read_status": artifact_status,
            "queue_ms": queue_ms,
        }
        if content_bytes is not None:
            response["content_bytes"] = content_bytes
        return response

    def search_passages(self, query: str, *, limit: int = 10, checked_only: bool = False) -> dict[str, Any]:
        """Find retained source passages, labeling inspection state without inferring validity."""
        if not query.strip() or limit < 1:
            raise EvidenceError("Search needs a query and positive result limit")
        started = time.perf_counter()
        terms = set(_WORDS.findall(query.casefold()))
        scored = []
        examined = 0
        after_id = ""
        while True:
            page = self.store.records("source_passage", limit=1000, after_id=after_id)
            if not page:
                break
            for record in page:
                examined += 1
                body = record["body"]
                if checked_only and body["inspection_state"] != "checked":
                    continue
                words = set(_WORDS.findall(f"{body['text']} {body['locator']}".casefold()))
                score = len(terms & words)
                if score:
                    scored.append((score, record))
            after_id = page[-1]["id"]
        scored.sort(key=lambda item: (-item[0], item[1]["id"]))
        return {
            "results": [{"passage_ref": _ref(record), "score": score,
                         "inspection_state": record["body"]["inspection_state"],
                         "needs_review": self.store.review_status(record)} for score, record in scored[:limit]],
            "examined": examined,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "method": "token_overlap_v1",
            "checked_only": checked_only,
        }
