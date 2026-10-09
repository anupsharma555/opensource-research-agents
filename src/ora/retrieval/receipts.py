"""Durable retrieval attempts and candidate hits over the shared StateStore."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from ora.contracts.models import RetrievalEnvelope
from ora.state import StateStore

from .normalize import canonical_bytes, normalize_response, sha256
from .redact import REDACTION_VERSION

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DELIVERY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


def _ref(record: dict) -> dict[str, object]:
    return {name: record[name] for name in ("kind", "id", "version")}


def _id(label: str, value: object) -> str:
    return f"{label}-{sha256(value)[:32]}"


def _decode(response: object) -> tuple[object, str]:
    if isinstance(response, bytes):
        digest = hashlib.sha256(response).hexdigest()
        if len(response) > MAX_RESPONSE_BYTES:
            return None, digest
        try:
            return json.loads(response), digest
        except (UnicodeError, json.JSONDecodeError):
            return None, digest
    if isinstance(response, Mapping):
        value = dict(response)
        data = canonical_bytes(value)
        if len(data) > MAX_RESPONSE_BYTES:
            return None, hashlib.sha256(data).hexdigest()
        return value, hashlib.sha256(data).hexdigest()
    return response, sha256(response)


def _hit_fingerprint(envelope: dict, item: dict) -> str:
    identity = item["provider_item_id"] or item["original_url"]
    return sha256({
        "request_id": envelope["request"]["request_id"],
        "provider": envelope["request"]["provider"],
        "route": envelope["request"]["route"],
        "index": envelope["request"]["index"],
        "identity": identity,
        "original_url": item["original_url"],
        "title": item["title"],
        "access_surface": item["access_surface"],
        "rights_status": item["rights_status"],
        "access_status": item["access_status"],
        "content_sha256": item["content_sha256"],
        "scientific_metadata": item["scientific_metadata"],
        "structured_content": item["structured_content"],
        "omitted_fields": item["omitted_fields"],
    })


def record_delivery(
    store: StateStore,
    request: Mapping[str, Any],
    response: object,
    *,
    delivery_id: str,
    adapter: str,
    retrieved_at: str,
    continuation: Mapping[str, str] | None = None,
    documented_usage: list[dict[str, object]] | None = None,
    fence: dict | None = None,
) -> dict[str, object]:
    """Save one delivery and its valid candidates; later evidence work owns source identity.

    The sanitized, allowlisted projection is the only retained response body.
    Identical delivery retries replay the same transaction result. A distinct
    retry attempt receives its own receipt and usage, but a repeated candidate
    in the same logical request reuses its accepted hit identity.
    """
    if not isinstance(delivery_id, str) or not DELIVERY_ID.fullmatch(delivery_id):
        raise ValueError("Delivery ID must be an opaque, non-secret identifier")
    decoded, original_hash = _decode(response)
    envelope = normalize_response(
        request, decoded, adapter=adapter, retrieved_at=retrieved_at,
        continuation=continuation, documented_usage=documented_usage,
    )
    for item in envelope["items"]:
        item["item_id"] = _id("retrieval-hit", _hit_fingerprint(envelope, item))
    raw_projection = {
        "schema": "ora.retrieval.retained_projection.v1",
        "request": envelope["request"],
        "response": envelope.pop("raw_projection"),
    }
    blob = store.put_blob(canonical_bytes(raw_projection))
    raw_ref = {
        "blob": blob,
        "original_sha256": original_hash,
        "sanitized_sha256": blob["sha256"],
        "transformation_version": REDACTION_VERSION,
        "redacted_paths": envelope.pop("redacted_paths"),
        "retained_projection": "mapped_scientific_fields",
        "original_retained": False,
    }
    envelope["raw_payload_ref"] = raw_ref
    # The domain normalizer keeps adapter diagnostics locally. Only the shared
    # canonical wire model is persisted as the delivery's envelope.
    wire = RetrievalEnvelope.model_validate({
        "schemaVersion": "1.0",
        "mapping_version": envelope["mapping_version"],
        "omitted_fields": envelope["omitted_fields"],
        "request": {**envelope["request"], "delivery_id": delivery_id},
        "retrieved_at": envelope["retrieved_at"],
        "outcome": envelope["outcome"],
        "failure": envelope["failure"],
        "returned_window": envelope["window"],
        "items": envelope["items"],
        "item_errors": envelope["item_errors"],
        "usage": envelope["usage"],
        "raw_payload_ref": raw_ref,
    })
    wire_envelope = wire.model_dump(mode="json", by_alias=True)
    attempt_id = _id("retrieval-attempt", {
        "mission": envelope["request"]["mission_id"],
        "request": envelope["request"]["request_id"],
        "attempt": envelope["request"]["attempt_id"],
    })
    delivery_record_id = _id("retrieval-delivery", {
        "mission": envelope["request"]["mission_id"],
        "request": envelope["request"]["request_id"],
        "delivery": delivery_id,
    })
    operation_id = f"retrieval-record-{delivery_record_id}"
    transaction_request = {
        "schema": "ora.retrieval.transaction.v1",
        "delivery_id": delivery_id,
        "adapter": adapter,
        "envelope": wire_envelope,
    }

    def save(tx) -> dict[str, object]:
        delivery = tx.put(
            "retrieval_delivery", delivery_record_id,
            {"blob": blob, "envelope": wire_envelope, "delivery_id": delivery_id},
        )
        accepted_refs = []
        reused_refs = []
        for item in envelope["items"]:
            fingerprint = _hit_fingerprint(envelope, item)
            previous = tx.resolve("retrieval.accepted_hit", fingerprint)
            if previous is not None:
                reused_refs.append(_ref(previous))
                tx.link(delivery, previous, "reobserved")
                continue
            hit = tx.put(
                "retrieval_hit", item["item_id"],
                {
                    "schema": "ora.retrieval.hit.v1",
                    "mission_id": envelope["request"]["mission_id"],
                    "task_id": envelope["request"]["task_id"],
                    "request_id": envelope["request"]["request_id"],
                    "attempt_id": envelope["request"]["attempt_id"],
                    "agent_instance_id": envelope["request"]["agent_instance_id"],
                    "provider": envelope["request"]["provider"],
                    "route": envelope["request"]["route"],
                    "index": envelope["request"]["index"],
                    "query": envelope["request"]["query"],
                    "retrieved_at": envelope["retrieved_at"],
                    "source": item,
                    "raw_payload_ref": raw_ref,
                    "mapping_version": envelope["mapping_version"],
                    "delivery_ref": _ref(delivery),
                },
            )
            tx.bind("retrieval.accepted_hit", fingerprint, "retrieval_hit", hit["id"])
            tx.link(hit, delivery, "derived_from")
            accepted_refs.append(_ref(hit))
        previous_attempt = tx.get("retrieval_attempt", attempt_id)
        previous_body = previous_attempt["body"] if previous_attempt else None
        outcome = envelope["outcome"]
        if previous_body and previous_body["outcome"] in {"success", "partial"} and outcome not in {"success", "partial"}:
            outcome = previous_body["outcome"]
        attempt = tx.put(
            "retrieval_attempt", attempt_id,
            {
                "schema": "ora.retrieval.attempt.v1",
                "request": envelope["request"],
                "outcome": outcome,
                "delivery_refs": (previous_body["delivery_refs"] if previous_body else [])
                + [_ref(delivery)],
                "last_retrieved_at": envelope["retrieved_at"],
                "usage_by_delivery": (previous_body["usage_by_delivery"] if previous_body else [])
                + [{"delivery_ref": _ref(delivery), "usage": envelope["usage"],
                    "counted_for_attempt": previous_attempt is None}],
            },
            expected_version=previous_attempt["version"] if previous_attempt else 0,
        )
        tx.link(delivery, attempt, "delivery_of")
        tx.emit(
            envelope["request"]["mission_id"], "retrieval_delivery_recorded",
            {"delivery_ref": _ref(delivery), "attempt_ref": _ref(attempt),
             "outcome": envelope["outcome"], "accepted_hit_refs": accepted_refs,
             "reused_hit_refs": reused_refs},
        )
        return {
            "delivery_ref": _ref(delivery),
            "attempt_ref": _ref(attempt),
            "accepted_hit_refs": accepted_refs,
            "reused_hit_refs": reused_refs,
            "outcome": envelope["outcome"],
            "window": envelope["window"],
            "item_errors": envelope["item_errors"],
            "raw_payload_ref": raw_ref,
        }

    return store.transact(
        operation_id, transaction_request, save,
        mission_id=envelope["request"]["mission_id"], fence=fence,
    )
