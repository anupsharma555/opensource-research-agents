"""Provider-shaped, offline responses into one internal retrieval envelope."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from .redact import redact, redact_text, redact_url, sensitive_name

SCHEMA = "ora.retrieval.envelope.v1"
SURFACES = frozenset(
    {"metadata", "teaser", "abstract", "passage", "full_text", "provider_summary"}
)
ORIGINS = frozenset({"original_source", "provider_generated"})
ADAPTERS = frozenset({"count_offset", "opaque_cursor"})
FAILURE_CODES = frozenset(
    {"route_unavailable", "rate_limited", "unauthorized", "not_found", "timeout", "upstream_error"}
)
MAPPING_VERSION = {
    "count_offset": "reference-count-offset.scientific-v1",
    "opaque_cursor": "reference-opaque-cursor.scientific-v1",
}
ITEM_FIELDS = frozenset({
    "id", "url", "title", "content", "surface", "origin", "rank", "rights_status",
    "access_status", "scientific_metadata", "structured_content",
})
SCIENTIFIC_METADATA_FIELDS = frozenset({
    "authors", "doi", "pmid", "pmcid", "arxiv_id", "journal", "publication_date",
    "year", "keywords", "registry_ids", "dataset_ids", "citation_count",
    "publication_type", "study_type", "language", "venue", "volume", "issue",
    "pages", "funding",
})
STRUCTURED_CONTENT_FIELDS = frozenset({"tables", "figures", "sections", "equations"})
CONTENT_LIKE_FIELDS = frozenset({
    "abstract", "abstracttext", "body", "content", "equations", "figures",
    "fulltext", "passages", "sections", "snippets", "tables", "text",
})
SAFE_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}")
RESPONSE_FIELDS = frozenset({
    "status", "items", "results", "count", "total", "offset", "cursor",
    "next_cursor", "has_more", "max_visible", "error_code", "retry_after_seconds",
    "usage", "error",
})
SECRET_USAGE_NAME = re.compile(
    r"(?:^|[_-])(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"password|passwd|secret|session|cookie|client[_-]?secret|signature|bearer)(?:$|[_-])",
    re.IGNORECASE,
)


class RetrievalFormatError(ValueError):
    """The declared request or adapter shape cannot be normalized safely."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _nonnegative(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


def _optional_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RetrievalFormatError(f"{label} must be a nonnegative integer or absent")
    return value


def _request(request: Mapping[str, Any]) -> dict[str, Any]:
    required = (
        "mission_id", "task_id", "request_id", "attempt_id", "route", "provider",
        "operation", "requested_at", "requested_window",
    )
    if not isinstance(request, Mapping) or any(
        not isinstance(request.get(name), str) or not request[name] for name in required[:-1]
    ):
        raise RetrievalFormatError("Request needs stable IDs, route, provider, operation and time")
    if request["operation"] not in {"search", "fetch"}:
        raise RetrievalFormatError("Operation must be search or fetch")
    window = request["requested_window"]
    if not isinstance(window, Mapping):
        raise RetrievalFormatError("Requested window must be an object")
    offset = _optional_int(window.get("offset"), "offset")
    limit = _optional_int(window.get("limit"), "limit")
    if limit == 0:
        raise RetrievalFormatError("Requested limit must be positive")
    cursor = window.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise RetrievalFormatError("Cursor must be nonempty text or absent")
    if offset is not None and cursor is not None:
        raise RetrievalFormatError("Use either offset or cursor for one requested window")
    filters = request.get("filters", {})
    if not isinstance(filters, Mapping):
        raise RetrievalFormatError("Filters must be an object")
    query = request.get("query")
    if query is not None and not isinstance(query, str):
        raise RetrievalFormatError("Query must be text or explicitly absent")
    agent_id = request.get("agent_instance_id")
    if agent_id is not None and (not isinstance(agent_id, str) or not agent_id):
        raise RetrievalFormatError("Agent instance ID must be text or absent")
    index = request.get("index")
    if index is not None and (not isinstance(index, str) or not index):
        raise RetrievalFormatError("Index must be text or explicitly absent")
    resume_esearch_ref = request.get("resume_esearch_ref")
    if resume_esearch_ref is not None and (
        not isinstance(resume_esearch_ref, Mapping)
        or set(resume_esearch_ref) != {"kind", "id", "version"}
        or resume_esearch_ref.get("kind") != "discovery_esearch_leg"
        or not isinstance(resume_esearch_ref.get("id"), str)
        or not re.fullmatch(r"discovery-esearch-[0-9a-f]{32}", resume_esearch_ref["id"])
        or type(resume_esearch_ref.get("version")) is not int
        or resume_esearch_ref["version"] != 1
    ):
        raise RetrievalFormatError("Resume needs an exact saved PubMed ESearch reference")
    try:
        canonical_bytes(dict(filters))
    except (TypeError, ValueError) as exc:
        raise RetrievalFormatError("Filters must contain JSON values") from exc
    return {
        "mission_id": request["mission_id"],
        "task_id": request["task_id"],
        "request_id": request["request_id"],
        "attempt_id": request["attempt_id"],
        "agent_instance_id": agent_id,
        "route": request["route"],
        "provider": request["provider"],
        "index": index,
        "operation": request["operation"],
        "query": query,
        "query_sha256": hashlib.sha256(query.encode()).hexdigest() if query is not None else None,
        "resume_esearch_ref": dict(resume_esearch_ref) if resume_esearch_ref is not None else None,
        "filters": dict(filters),
        "requested_at": request["requested_at"],
        "requested_window": {"offset": offset, "cursor": cursor, "limit": limit},
    }


def _usage(raw: object, documented: object) -> tuple[list[dict[str, object]], bool]:
    output: list[dict[str, object]] = []
    invalid_observed = False
    if raw is not None:
        if not isinstance(raw, Mapping):
            invalid_observed = True
        else:
            for metric, amount in raw.items():
                if (
                    not isinstance(metric, str) or not metric
                    or SECRET_USAGE_NAME.search(metric) or sensitive_name(metric)
                    or not _nonnegative(amount)
                ):
                    invalid_observed = True
                    continue
                output.append({"unit": metric, "amount": amount, "basis": "observed"})
    if documented is not None:
        if not isinstance(documented, list):
            raise RetrievalFormatError("Documented usage must be a list")
        for entry in documented:
            if (
                not isinstance(entry, Mapping)
                or not isinstance(entry.get("unit"), str)
                or not entry["unit"]
                or SECRET_USAGE_NAME.search(entry["unit"]) or sensitive_name(entry["unit"])
                or not _nonnegative(entry.get("amount"))
            ):
                raise RetrievalFormatError("Documented usage needs named, nonnegative units")
            output.append(
                {"unit": entry["unit"], "amount": entry["amount"], "basis": "documented"}
            )
    return output, invalid_observed


_OMITTED = object()


def _field_label(value: object) -> str:
    if not isinstance(value, str) or not SAFE_FIELD.fullmatch(value):
        return "[unsafe-field]"
    return "[sensitive-field]" if sensitive_name(value) or value.casefold() in {
        "email", "email_address", "phone", "phone_number", "contact_email",
    } else value


def _omission_reason(label: str) -> str:
    if label == "[sensitive-field]":
        return "sensitive"
    if label == "[unsafe-field]":
        return "invalid_field_name"
    return "unsupported"


def _scientific_json(
    value: object, path: str, omissions: list[dict[str, str]], *, metadata_only: bool = False,
) -> object:
    """Retain explicit scientific JSON while removing unsafe keys and values."""
    if isinstance(value, Mapping):
        output: dict[str, object] = {}
        for key, item in value.items():
            label = _field_label(key)
            if label.startswith("["):
                omissions.append({"field": f"{path}.{label}", "reason": _omission_reason(label)})
                continue
            if metadata_only and re.sub(r"[^a-z0-9]", "", label.casefold()) in CONTENT_LIKE_FIELDS:
                omissions.append({
                    "field": f"{path}.{label}", "reason": "content_requires_structured_content",
                })
                continue
            child = _scientific_json(
                item, f"{path}.{label}", omissions, metadata_only=metadata_only,
            )
            if child is not _OMITTED:
                output[key] = child
        return output
    if isinstance(value, list):
        output = []
        for index, item in enumerate(value):
            child = _scientific_json(
                item, f"{path}[{index}]", omissions, metadata_only=metadata_only,
            )
            if child is _OMITTED:
                # Dropping a row or cell would silently change table alignment.
                return _OMITTED
            output.append(child)
        return output
    if isinstance(value, str):
        return redact_text(value)[0]
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    omissions.append({"field": path, "reason": "invalid_json_value"})
    return _OMITTED


def _scientific_fields(
    raw: Mapping[str, Any], rights: str,
) -> tuple[dict[str, object], dict[str, object], list[dict[str, str]]]:
    omissions: list[dict[str, str]] = []
    metadata: dict[str, object] = {}
    structured: dict[str, object] = {}
    for source_key, target, allowed in (
        ("scientific_metadata", metadata, True),
        ("structured_content", structured, rights == "permitted"),
    ):
        if source_key not in raw:
            continue
        if not allowed:
            omissions.append({"field": source_key, "reason": "rights_not_confirmed"})
            continue
        if not isinstance(raw[source_key], Mapping):
            omissions.append({"field": source_key, "reason": "invalid_shape"})
            continue
        value = _scientific_json(
            raw[source_key], source_key, omissions,
            metadata_only=source_key == "scientific_metadata",
        )
        if isinstance(value, dict):
            target.update(value)
    for key in sorted(SCIENTIFIC_METADATA_FIELDS | STRUCTURED_CONTENT_FIELDS):
        if key not in raw:
            continue
        target = metadata if key in SCIENTIFIC_METADATA_FIELDS else structured
        if target is structured and rights != "permitted":
            omissions.append({"field": key, "reason": "rights_not_confirmed"})
            continue
        value = raw[key]
        if key == "authors" and not isinstance(value, list):
            omissions.append({"field": key, "reason": "invalid_shape"})
            continue
        if key in STRUCTURED_CONTENT_FIELDS and not isinstance(value, list):
            omissions.append({"field": key, "reason": "invalid_shape"})
            continue
        clean = _scientific_json(value, key, omissions)
        if clean is _OMITTED:
            continue
        if key in target and target[key] != clean:
            omissions.append({"field": key, "reason": "conflicting_mapping"})
            # Keep both observations, without pretending that mapping precedence
            # resolves a scientific disagreement. This internal namespace cannot
            # come from a provider field (the accepted field syntax starts with a letter).
            target.setdefault("_mapping_conflicts", []).append({
                "field": key,
                "selected_from": "scientific_metadata" if target is metadata else "structured_content",
                "alternative_from": key,
                "alternative_value": clean,
            })
            continue
        target[key] = clean
    recognized = ITEM_FIELDS | SCIENTIFIC_METADATA_FIELDS | STRUCTURED_CONTENT_FIELDS
    for key in raw:
        if key not in recognized:
            label = _field_label(key)
            omissions.append({
                "field": label,
                "reason": _omission_reason(label),
            })
    return metadata, structured, omissions


def _item(raw: object, position: int, offset: int | None) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise RetrievalFormatError("Item is not an object")
    if "error" in raw:
        raise RetrievalFormatError("Provider marked this item as failed")
    provider_id = raw.get("id")
    url = raw.get("url")
    if provider_id is not None and (not isinstance(provider_id, str) or not provider_id):
        raise RetrievalFormatError("Provider item ID is invalid")
    if url is not None and (not isinstance(url, str) or not url):
        raise RetrievalFormatError("Original URL is invalid")
    if not provider_id and not url:
        raise RetrievalFormatError("Item needs a provider ID or URL")
    surface = raw.get("surface")
    origin = raw.get("origin")
    if surface not in SURFACES or origin not in ORIGINS:
        raise RetrievalFormatError("Item needs explicit evidence surface and origin")
    if origin == "provider_generated" and surface != "provider_summary":
        raise RetrievalFormatError("Provider summary cannot be original-source evidence")
    content = raw.get("content")
    if content is not None and not isinstance(content, str):
        raise RetrievalFormatError("Item content must be text or absent")
    title = raw.get("title")
    if title is not None and not isinstance(title, str):
        raise RetrievalFormatError("Item title must be text or absent")
    rank = raw.get("rank")
    if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or rank < 1):
        raise RetrievalFormatError("Provider rank must be a positive integer")
    if rank is None:
        rank = (offset or 0) + position + 1
        rank_scope = "global_offset" if offset is not None else "page"
    else:
        rank_scope = "provider_reported"
    safe_url, url_redacted = redact_url(url) if url is not None else (None, False)
    rights = raw.get("rights_status", "unknown")
    access = raw.get("access_status", "unknown")
    if rights not in {"unknown", "permitted", "restricted", "not_retained"}:
        raise RetrievalFormatError("Item rights status is invalid")
    if access not in {"unknown", "available", "limited", "unavailable"}:
        raise RetrievalFormatError("Item access status is invalid")
    retain_content = rights == "permitted"
    scientific_metadata, structured_content, omitted_fields = _scientific_fields(raw, rights)
    retained = {
        "provider_item_id": provider_id,
        "original_url": safe_url,
        "original_url_sha256": hashlib.sha256(url.encode()).hexdigest() if url else None,
        "url_redacted": url_redacted,
        "rank": rank,
        "rank_scope": rank_scope,
        "title": title,
        "content": content if retain_content else None,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest() if content else None,
        "content_retained": retain_content and content is not None,
        "content_unretained_reason": None if retain_content or content is None else "rights_not_confirmed",
        "access_surface": surface,
        "origin": origin,
        "rights_status": rights,
        "access_status": access,
        "scientific_metadata": scientific_metadata,
        "structured_content": structured_content,
        "omitted_fields": omitted_fields,
    }
    clean, changed = redact(retained)
    if url_redacted:
        changed.append("$.original_url")
    clean["redacted_fields"] = sorted(set(changed))
    return clean  # type: ignore[return-value]


def normalize_response(
    request: Mapping[str, Any],
    response: object,
    *,
    adapter: str,
    retrieved_at: str,
    continuation: Mapping[str, str] | None = None,
    documented_usage: list[dict[str, object]] | None = None,
    oversized_response: bool = False,
) -> dict[str, Any]:
    """Normalize two *simulated* response layouts without claiming a live provider API."""
    if adapter not in ADAPTERS or not isinstance(retrieved_at, str) or not retrieved_at:
        raise RetrievalFormatError("Known adapter and retrieval time are required")
    clean_request, request_redactions = redact(_request(request))
    for name in list(clean_request["filters"]):
        if sensitive_name(name):
            del clean_request["filters"][name]
            request_redactions.append(f"$.filters.{name}")
    if continuation is not None and (
        continuation.get("action") not in {"continue", "stop", "switch_route"}
        or not isinstance(continuation.get("reason"), str)
        or not continuation["reason"]
    ):
        raise RetrievalFormatError("Continuation needs a peer-selected action and reason")
    safe_continuation, continuation_redactions = redact(dict(continuation or {}))
    if not isinstance(response, Mapping):
        response = {"status": "malformed", "error": "Top-level payload is not an object"}
    status = response.get("status")
    collection_name = "items" if adapter == "count_offset" else "results"
    collection = response.get(collection_name)
    if not isinstance(status, str) or status not in {"ok", "error", "quota_wait"}:
        status = "malformed"
    if status == "ok" and not isinstance(collection, list):
        status = "malformed"
    if oversized_response and status != "malformed":
        raise RetrievalFormatError("Oversized response cannot contain normalized results")
    if status != "ok":
        collection = []
    response_omissions: list[dict[str, str]] = []
    for key in response:
        label = _field_label(key)
        if key == "error":
            response_omissions.append({"field": "error", "reason": "unsafe_diagnostic"})
        elif key not in RESPONSE_FIELDS:
            response_omissions.append({
                "field": label,
                "reason": _omission_reason(label),
            })
    window_errors: list[str] = []
    if adapter == "count_offset" and status == "ok":
        try:
            returned_offset = _optional_int(response.get("offset"), "returned offset")
        except RetrievalFormatError:
            returned_offset = None
            window_errors.append("invalid_returned_offset")
        requested_offset = clean_request["requested_window"]["offset"]  # type: ignore[index]
        if returned_offset is not None and (requested_offset or 0) != returned_offset:
            window_errors.append("returned_window_mismatch")
    else:
        returned_offset = None
    accepted: list[dict[str, object]] = []
    item_errors: list[dict[str, object]] = []
    for position, entry in enumerate(collection):
        try:
            accepted.append(_item(entry, position, returned_offset))
        except RetrievalFormatError as exc:
            item_errors.append({"position": position, "code": "invalid_item", "reason": str(exc)})
    total_field = "count" if adapter == "count_offset" else "total"
    try:
        total = _optional_int(response.get(total_field), "provider total") if status == "ok" else None
    except RetrievalFormatError:
        total = None
        window_errors.append("invalid_provider_total")
    try:
        provider_limit = (
            _optional_int(response.get("max_visible"), "provider exposure limit")
            if status == "ok" else None
        )
    except RetrievalFormatError:
        provider_limit = None
        window_errors.append("invalid_provider_exposure_limit")
    returned = len(collection)
    next_cursor = response.get("next_cursor") if adapter == "opaque_cursor" else None
    if not isinstance(next_cursor, str) or not next_cursor:
        next_cursor = None
    else:
        next_cursor, _ = redact_text(next_cursor)
    returned_cursor = response.get("cursor") if adapter == "opaque_cursor" else None
    if not isinstance(returned_cursor, str) or not returned_cursor:
        returned_cursor = None
    else:
        returned_cursor, _ = redact_text(returned_cursor)
    has_more = response.get("has_more")
    if not isinstance(has_more, bool):
        has_more = None
    visible_end = (returned_offset or 0) + returned
    known_unseen = (
        max(total - visible_end, 0)
        if total is not None and adapter == "count_offset" and returned_offset is not None
        else None
    )
    if status != "ok":
        more = "unknown"
    elif (known_unseen is not None and known_unseen > 0) or next_cursor or has_more is True:
        more = "yes"
    elif (known_unseen == 0 and total is not None) or has_more is False:
        more = "no"
    else:
        more = "unknown"
    limit_reached = provider_limit is not None and visible_end >= provider_limit
    if adapter == "count_offset":
        next_window = (
            {"offset": visible_end, "limit": clean_request["requested_window"]["limit"]}  # type: ignore[index]
            if more == "yes" and not limit_reached and returned > 0
            and returned_offset is not None else None
        )
    else:
        next_window = {"cursor": next_cursor} if next_cursor and not limit_reached else None
    if status == "quota_wait":
        outcome = "provider_failure"
        provider_stop = "quota_wait"
    elif status == "error":
        outcome = "provider_failure"
        provider_stop = "error"
    elif status == "malformed":
        outcome = "oversized_response" if oversized_response else "malformed_payload"
        provider_stop = "error"
    else:
        outcome = (
            "partial" if accepted and (item_errors or window_errors) else
            "item_failure" if item_errors else
            "success" if accepted else "empty"
        )
        provider_stop = (
            "exhausted" if more == "no" else
            "provider_limit" if more == "yes" and limit_reached else
            "more_available" if more == "yes" and next_window else "unknown"
        )
    raw_error_code = response.get("error_code")
    error_code = (
        raw_error_code if isinstance(raw_error_code, str)
        and raw_error_code in FAILURE_CODES
        else "unspecified"
    )
    retry_after = response.get("retry_after_seconds")
    if not _nonnegative(retry_after):
        retry_after = None
    failure = (
        {"category": "quota_wait" if status == "quota_wait" else
         "provider_error" if status == "error" else
         "oversized_response" if oversized_response else "malformed_payload",
         "code": error_code, "retry_after_seconds": retry_after}
        if status != "ok" else None
    )
    usage, invalid_observed_usage = _usage(response.get("usage"), documented_usage)
    if invalid_observed_usage:
        window_errors.append("invalid_observed_usage")
        if accepted:
            outcome = "partial"
    projected = {
        "status": status,
        "provider_total": total,
        "provider_exposure_limit": provider_limit,
        "returned_item_count": returned,
        "accepted_items": accepted,
        "item_errors": item_errors,
        "usage": usage,
    }
    safe_projected, payload_redactions = redact(projected)
    envelope = {
        "schema": SCHEMA,
        "adapter": adapter,
        "mapping_version": MAPPING_VERSION[adapter],
        "omitted_fields": response_omissions,
        "request": clean_request,
        "retrieved_at": retrieved_at,
        "outcome": outcome,
        "failure": failure,
        "window": {
            "returned_count": returned,
            "returned_offset": returned_offset,
            "returned_cursor": returned_cursor,
            "accepted_count": len(accepted),
            "provider_total": total,
            "known_unseen_count": known_unseen,
            "more_available": more,
            "next_window": next_window,
            "provider_exposure_limit": provider_limit,
            "provider_stop_reason": provider_stop,
            "continuation": safe_continuation or None,
            "window_errors": window_errors,
        },
        "items": accepted,
        "item_errors": item_errors,
        "usage": usage,
        "raw_projection": safe_projected,
        "redacted_paths": sorted(set(
            request_redactions + continuation_redactions + payload_redactions
            + [f"$.items[{i}]{path[1:]}" for i, item in enumerate(accepted)
               for path in item["redacted_fields"]]
        )),
        "raw_projection_note": "Mapped scientific and allowlisted provider fields; original bytes are not retained",
    }
    return envelope
