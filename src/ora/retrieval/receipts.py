"""Durable retrieval attempts and candidate hits over the shared StateStore."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from ora.contracts.models import RetrievalEnvelope
from ora.state import StateStore

from .abstract_use import (
    ABSTRACT_USE_POLICY_VERSION,
    PUBLIC_ABSTRACT_SOURCES,
    published_abstract_use,
)
from .normalize import canonical_bytes, normalize_response, sha256
from .redact import REDACTION_VERSION, redact

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DELIVERY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


def _ref(record: dict) -> dict[str, object]:
    return {name: record[name] for name in ("kind", "id", "version")}


def _id(label: str, value: object) -> str:
    return f"{label}-{sha256(value)[:32]}"


def _decode(response: object) -> tuple[object, str, str, int | None]:
    if isinstance(response, bytes):
        digest = hashlib.sha256(response).hexdigest()
        if len(response) > MAX_RESPONSE_BYTES:
            return None, digest, "oversized", len(response)
        try:
            return json.loads(response), digest, "decoded", len(response)
        except (UnicodeError, json.JSONDecodeError):
            return None, digest, "invalid_json", len(response)
    if isinstance(response, Mapping):
        value = dict(response)
        data = canonical_bytes(value)
        if len(data) > MAX_RESPONSE_BYTES:
            return None, hashlib.sha256(data).hexdigest(), "oversized", len(data)
        return value, hashlib.sha256(data).hexdigest(), "decoded", len(data)
    return response, sha256(response), "unsupported_type", None


def _hit_fingerprint(envelope: dict, item: dict, notice: dict | None = None) -> str:
    identity = item["provider_item_id"] or item["original_url"]
    return sha256(
        {
            "mission_id": envelope["request"]["mission_id"],
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
            **({"discovery_excerpt_sha256": sha256(item["discovery_excerpt"])}
               if item.get("discovery_excerpt") is not None else {}),
            "scientific_metadata": item["scientific_metadata"],
            "structured_content": item["structured_content"],
            "omitted_fields": item["omitted_fields"],
            "source_use_notice_sha256": sha256(notice) if notice else None,
            "abstract_use_policy_version": ABSTRACT_USE_POLICY_VERSION
            if envelope["request"]["route"] in PUBLIC_ABSTRACT_SOURCES
            and item["access_surface"] == "abstract"
            else None,
        }
    )


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
    provider_response_sha256: str | None = None,
    upstream_payload_condition: str | None = None,
    source_use_notices: Mapping[str, dict] | None = None,
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
    if provider_response_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", provider_response_sha256
    ):
        raise ValueError("Provider response hash must be SHA-256")
    if upstream_payload_condition not in {None, "oversized"}:
        raise ValueError("Upstream payload condition is unsupported")
    if source_use_notices is not None and (
        not isinstance(source_use_notices, Mapping)
        or any(
            not isinstance(key, str) or not isinstance(value, dict)
            for key, value in source_use_notices.items()
        )
    ):
        raise ValueError("Source use notices need item IDs and record objects")
    safe_notices, _ = redact(dict(source_use_notices or {}))
    decoded, original_hash, payload_condition, original_bytes = _decode(response)
    envelope = normalize_response(
        request,
        decoded,
        adapter=adapter,
        retrieved_at=retrieved_at,
        continuation=continuation,
        documented_usage=documented_usage,
        oversized_response=(
            payload_condition == "oversized" or upstream_payload_condition == "oversized"
        ),
    )
    for item in envelope["items"]:
        notice = safe_notices.get(item["provider_item_id"] or item["original_url"])
        if (
            notice
            and notice.get("explicit_prohibition_detected")
            and item["rights_status"] == "permitted"
        ):
            raise ValueError("Explicit source prohibition conflicts with retained abstract text")
        if notice and notice.get("explicit_prohibition_detected") and item.get("discovery_excerpt"):
            item["discovery_excerpt"] = None
            for projected in envelope["raw_projection"].get("accepted_items", []):
                if projected.get("provider_item_id") == item["provider_item_id"] and projected.get("original_url") == item["original_url"]:
                    projected["discovery_excerpt"] = None
        item["item_id"] = _id("retrieval-hit", _hit_fingerprint(envelope, item, notice))
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
    wire = RetrievalEnvelope.model_validate(
        {
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
        }
    )
    wire_envelope = wire.model_dump(mode="json", by_alias=True)
    attempt_id = _id(
        "retrieval-attempt",
        {
            "mission": envelope["request"]["mission_id"],
            "request": envelope["request"]["request_id"],
            "attempt": envelope["request"]["attempt_id"],
        },
    )
    delivery_record_id = _id(
        "retrieval-delivery",
        {
            "mission": envelope["request"]["mission_id"],
            "request": envelope["request"]["request_id"],
            "delivery": delivery_id,
        },
    )
    operation_id = f"retrieval-record-{delivery_record_id}"
    transaction_request = {
        "schema": "ora.retrieval.transaction.v1",
        "delivery_id": delivery_id,
        "adapter": adapter,
        "provider_response_sha256": provider_response_sha256,
        "upstream_payload_condition": upstream_payload_condition,
        "source_use_notices": safe_notices,
        "envelope": wire_envelope,
    }

    def save(tx) -> dict[str, object]:
        delivery = tx.put(
            "retrieval_delivery",
            delivery_record_id,
            {
                "blob": blob,
                "envelope": wire_envelope,
                "delivery_id": delivery_id,
                "payload_condition": payload_condition,
                "upstream_payload_condition": upstream_payload_condition,
                "original_bytes": original_bytes,
            },
        )
        accepted_refs = []
        reused_refs = []
        for item in envelope["items"]:
            notice = safe_notices.get(item["provider_item_id"] or item["original_url"])
            fingerprint = _hit_fingerprint(envelope, item, notice)
            previous = tx.resolve("retrieval.accepted_hit", fingerprint)
            if previous is not None:
                reused_refs.append(_ref(previous))
                tx.link(delivery, previous, "reobserved")
                continue
            hit = tx.put(
                "retrieval_hit",
                item["item_id"],
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
                    "source_use_policy": published_abstract_use(
                        route=envelope["request"]["route"],
                        source=item,
                        retrieved_at=envelope["retrieved_at"],
                        provider_response_sha256=provider_response_sha256,
                        source_notice=notice,
                    ),
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
        if (
            previous_body
            and previous_body["outcome"] in {"success", "partial"}
            and outcome not in {"success", "partial"}
        ):
            outcome = previous_body["outcome"]
        attempt = tx.put(
            "retrieval_attempt",
            attempt_id,
            {
                "schema": "ora.retrieval.attempt.v1",
                "request": envelope["request"],
                "outcome": outcome,
                "delivery_refs": (previous_body["delivery_refs"] if previous_body else [])
                + [_ref(delivery)],
                "last_retrieved_at": envelope["retrieved_at"],
                "usage_by_delivery": (previous_body["usage_by_delivery"] if previous_body else [])
                + [
                    {
                        "delivery_ref": _ref(delivery),
                        "usage": envelope["usage"],
                        "counted_for_attempt": previous_attempt is None,
                    }
                ],
            },
            expected_version=previous_attempt["version"] if previous_attempt else 0,
        )
        tx.link(delivery, attempt, "delivery_of")
        tx.emit(
            envelope["request"]["mission_id"],
            "retrieval_delivery_recorded",
            {
                "delivery_ref": _ref(delivery),
                "attempt_ref": _ref(attempt),
                "outcome": envelope["outcome"],
                "accepted_hit_refs": accepted_refs,
                "reused_hit_refs": reused_refs,
            },
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
            "payload_condition": payload_condition,
            "original_bytes": original_bytes,
        }

    return store.transact(
        operation_id,
        transaction_request,
        save,
        mission_id=envelope["request"]["mission_id"],
        fence=fence,
    )
