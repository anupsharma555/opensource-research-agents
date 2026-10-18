"""One caller-selected discovery page with durable dispatch, delivery and route wait."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from ora.state import StateStore

from .normalize import canonical_bytes, normalize_response
from .providers import (
    HTTPResult,
    available_routes,
    build_call,
    http_transport,
    map_response,
    parse_pubmed_abstract_xml,
    source_use_notices,
)
from .receipts import record_delivery
from .redact import redact, redact_url


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()[:32]


def _retry_seconds(headers: Mapping[str, str]) -> float | None:
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(
                0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            )
        except (ValueError, TypeError, OverflowError):
            return None


def _wait_id(route: str) -> str:
    group = "ncbi.eutils" if route.startswith("pubmed.") else route
    return "discovery-wait-" + _digest(group)


def _ncbi_interval(credential: str | None, requests_per_second: int | None) -> float:
    maximum = 10 if credential else 3
    if requests_per_second is not None and (
        type(requests_per_second) is not int or requests_per_second < 1
    ):
        raise ValueError("NCBI requests per second must be a positive integer")
    rate = min(requests_per_second or maximum, maximum)
    return max(0.1 if credential else 0.36, 1 / rate)


def _admit_ncbi(
    store: StateStore, credential: str | None = None,
    requests_per_second: int | None = None,
) -> dict:
    """Reserve a slot in the local shared IP or keyed E-utilities pool."""
    record_id = (
        "ncbi-eutils-key-" + hashlib.sha256(credential.encode()).hexdigest()
        if credential else "ncbi-eutils"
    )
    interval = _ncbi_interval(credential, requests_per_second)

    def action(tx):
        previous = tx.get("discovery_capacity", record_id)
        now = time.time()
        scheduled = max(now, previous["body"]["next_allowed_epoch"] if previous else now)
        body = {
            "schema": "ora.discovery.capacity.v1",
            "provider": "ncbi",
            "next_allowed_epoch": scheduled + interval,
            "documented_limit": (
                "10 requests/second per API key" if credential
                else "3 requests/second per IP without API key"
            ),
        }
        saved = tx.put(
            "discovery_capacity",
            record_id,
            body,
            expected_version=previous["version"] if previous else 0,
        )
        return {"wait_seconds": max(0.0, scheduled - now), "capacity_ref": _ref(saved)}

    return store.transact(
        "ncbi-admit-" + uuid.uuid4().hex,
        {"provider": "ncbi", "mode": "keyed" if credential else "public_no_key"}, action,
    )


def _route_wait(store: StateStore, route: str) -> dict | None:
    record = store.get("discovery_wait", _wait_id(route))
    if record and float(record["body"]["until_epoch"]) > time.time():
        return {
            "outcome": "quota_wait",
            "route": route,
            "retry_at": record["body"]["retry_at"],
            "wait_ref": _ref(record),
        }
    return None


def _save_wait(store: StateStore, route: str, seconds: float) -> dict:
    record_id = _wait_id(route)
    until = time.time() + seconds
    body = {
        "schema": "ora.discovery.wait.v1",
        "route": route,
        "retry_at": datetime.fromtimestamp(until, timezone.utc).isoformat(),
        "until_epoch": until,
        "reason": "provider_retry_after",
    }

    def action(tx):
        previous = tx.get("discovery_wait", record_id)
        saved = tx.put(
            "discovery_wait",
            record_id,
            body,
            expected_version=previous["version"] if previous else 0,
        )
        return {"wait_ref": _ref(saved), "retry_at": body["retry_at"]}

    return store.transact(
        "discovery-wait-op-" + _digest({"route": route, "until": until}),
        {"route": route, "until": until},
        action,
    )


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _http_leg(result: HTTPResult, elapsed_ms: float, *, name: str) -> dict:
    digest = hashlib.sha256(result.body).hexdigest()
    complete = len(result.body) <= 8 * 1024 * 1024
    return {
        "name": name,
        "status": result.status,
        "sha256": digest,
        "hash_scope": "complete_body" if complete else "observed_prefix",
        "complete_body_sha256": digest if complete else None,
        "bytes": len(result.body),
        "elapsed_ms": round(elapsed_ms, 3),
    }


def _read(
    transport: Callable[..., HTTPResult],
    method: str,
    url: str,
    body: dict | None,
    headers: dict,
    *,
    name: str,
    store: StateStore | None = None,
    credential: str | None = None,
    ncbi_requests_per_second: int | None = None,
) -> tuple[HTTPResult, dict]:
    admission_ms = 0.0
    if store is not None and name.startswith("pubmed."):
        reservation = _admit_ncbi(store, credential, ncbi_requests_per_second)
        admission_ms = reservation["wait_seconds"] * 1000
        if admission_ms:
            time.sleep(reservation["wait_seconds"])
    start = time.perf_counter()
    result = transport(method, url, body=body, headers=headers)
    leg = _http_leg(result, (time.perf_counter() - start) * 1000, name=name)
    leg["admission_wait_ms"] = round(admission_ms, 3)
    return result, leg


def _json(result: HTTPResult) -> dict:
    if len(result.body) > 8 * 1024 * 1024:
        raise ValueError("oversized_response")
    payload = json.loads(result.body)
    if not isinstance(payload, dict):
        raise ValueError("malformed_response")
    return payload


def _error_response(status: int | None, headers: Mapping[str, str], reason: str) -> dict:
    if status in {429, 503}:
        return {
            "status": "quota_wait",
            "error_code": "rate_limited",
            "retry_after_seconds": _retry_seconds(headers),
        }
    if status == 404:
        return {"status": "error", "error_code": "not_found"}
    if status == 402:
        return {"status": "error", "error_code": "route_unavailable"}
    if status in {401, 403}:
        return {"status": "error", "error_code": "unauthorized"}
    return {"status": "error", "error_code": "timeout" if reason == "timeout" else "upstream_error"}


def _pubmed_summary_call(ids: list[str], credential: str | None = None) -> str:
    from urllib.parse import urlencode

    return "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?" + urlencode(
        {"db": "pubmed", "id": ",".join(ids), "retmode": "json",
         **({"api_key": credential} if credential else {})}
    )


def _esearch_selection(request: Mapping[str, Any]) -> dict[str, Any]:
    """Exact scientific request identity, excluding only attempt and observation time."""
    return {
        key: request.get(key)
        for key in (
            "mission_id", "task_id", "agent_instance_id", "route", "provider",
            "index", "operation", "query", "filters", "requested_window",
        )
    }


def _esearch_id(page_id: str) -> str:
    return "discovery-esearch-" + page_id.removeprefix("discovery-page-")


def _load_esearch(store: StateStore, request: Mapping[str, Any]) -> dict | None:
    pointer = request.get("resume_esearch_ref")
    if pointer is None:
        return None
    if request.get("route") != "pubmed.search":
        raise ValueError("Saved ESearch continuation is only valid for PubMed search")
    if (
        not isinstance(pointer, Mapping)
        or set(pointer) != {"kind", "id", "version"}
        or pointer.get("kind") != "discovery_esearch_leg"
        or not isinstance(pointer.get("id"), str)
        or not pointer["id"].startswith("discovery-esearch-")
        or type(pointer.get("version")) is not int
        or pointer["version"] != 1
    ):
        raise ValueError("Resume needs an exact saved PubMed ESearch reference")
    record = store.get(**pointer)
    if record is None or record["body"].get("selection") != _esearch_selection(request):
        raise ValueError("Saved ESearch does not match this exact task, query, filters and page")
    body = record["body"]
    ids = body.get("id_list")
    count = body.get("reported_count")
    if (
        not isinstance(ids, list) or not 1 <= len(ids) <= 100
        or any(not isinstance(value, str) or not value.isdigit() for value in ids)
        or type(count) is not int or count < len(ids)
    ):
        raise ValueError("Saved ESearch ID list is invalid")
    return record


def _save_esearch(
    store: StateStore, request: Mapping[str, Any], page_ref: dict,
    ids: list[str], count: int, leg: dict,
) -> dict:
    record_id = _esearch_id(page_ref["id"])
    body = {
        "schema": "ora.discovery.pubmed_esearch_leg.v1",
        "selection": _esearch_selection(request),
        "origin_page_ref": page_ref,
        "id_list": ids,
        "reported_count": count,
        "retrieved_at": _now(),
        "http_leg": leg,
    }

    def save(tx):
        page = tx.get(**page_ref)
        if page is None or page["body"].get("request_sha256") != _digest(request):
            raise ValueError("Original ESearch page changed before saving its ID list")
        saved = tx.put("discovery_esearch_leg", record_id, body)
        tx.link(saved, page, "selected_ids_from")
        return _ref(saved)

    return store.transact(
        "discovery-esearch-save-" + record_id,
        body,
        save,
        mission_id=request["mission_id"],
    )


def _saved_hits(store: StateStore, refs: list[dict]) -> list[dict]:
    hits = []
    for ref in refs:
        hit = store.get(**ref)["body"]
        source = hit["source"]
        hits.append(
            {
                "hit_ref": ref,
                "source_use_policy": hit.get("source_use_policy"),
                **{
                    key: source.get(key)
                    for key in (
                        "provider_item_id",
                        "original_url",
                        "title",
                        "access_surface",
                        "origin",
                        "rights_status",
                        "access_status",
                        "content_sha256",
                        "discovery_excerpt",
                        "scientific_metadata",
                    )
                },
            }
        )
    return hits


def run_discovery_page(
    store: StateStore,
    request: Mapping[str, Any],
    *,
    credential: str | None = None,
    ncbi_requests_per_second: int | None = None,
    transport: Callable[..., HTTPResult] = http_transport,
) -> dict:
    """Execute one requested window. No implicit route switch, query expansion or retry.

    A dispatch marker is committed before HTTP. An interrupted dispatch is
    uncertain; a new caller-selected attempt ID is required to retry it.
    The same completed attempt replays its saved result without another call.
    """
    request = dict(request)
    route = request.get("route")
    if isinstance(route, str) and route.startswith("pubmed."):
        _ncbi_interval(credential, ncbi_requests_per_second)
    page_identity = {
        key: request.get(key)
        for key in (
            "mission_id", "task_id", "request_id", "attempt_id", "route",
            "operation", "query", "filters", "requested_window",
        )
    }
    if request.get("resume_esearch_ref") is not None:
        page_identity["resume_esearch_ref"] = request["resume_esearch_ref"]
    page_id = "discovery-page-" + _digest(page_identity)
    previous = store.get("discovery_page", page_id)
    if previous:
        saved = previous["body"]
        if saved["request_sha256"] != _digest(request):
            raise ValueError("Page attempt was already used with different request details")
        return (
            {**saved["result"], "page_ref": _ref(previous)}
            if saved["status"] == "completed"
            else {
                "outcome": "uncertain_dispatch", "page_ref": _ref(previous), "route": route,
                "esearch_ref": _ref(search_leg) if (
                    search_leg := store.get("discovery_esearch_leg", _esearch_id(page_id), 1)
                ) else None,
                "esearch_retrieved_at": (
                    search_leg["body"]["retrieved_at"] if search_leg else None
                ),
            }
        )
    resumed_esearch = _load_esearch(store, request)
    wait = _route_wait(store, route)
    if wait:
        return {
            **wait,
            "esearch_ref": _ref(resumed_esearch) if resumed_esearch else None,
            "esearch_retrieved_at": (
                resumed_esearch["body"]["retrieved_at"] if resumed_esearch else None
            ),
            "resumed_from_esearch_ref": _ref(resumed_esearch) if resumed_esearch else None,
        }
    method, url, body, headers = build_call(request, credential)
    if resumed_esearch is not None:
        method, url, body, headers = (
            "GET", _pubmed_summary_call(resumed_esearch["body"]["id_list"], credential), None, {}
        )
    adapter = available_routes()[route]["adapter"]
    blank = {"status": "ok", "results" if adapter == "opaque_cursor" else "items": []}
    normalize_response(request, blank, adapter=adapter, retrieved_at=_now())
    try:
        requested_at = datetime.fromisoformat(request["requested_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Requested time must be ISO 8601 with timezone") from exc
    if requested_at.tzinfo is None or requested_at.utcoffset() is None:
        raise ValueError("Requested time must be ISO 8601 with timezone")
    safe_url, _ = redact_url(url)
    safe_body, _ = redact(body)
    marker = {
        "schema": "ora.discovery.page.v1",
        "status": "dispatched",
        "request_sha256": _digest(request),
        "route": route,
        "request_id": request.get("request_id"),
        "attempt_id": request.get("attempt_id"),
        "started_at": _now(),
    }

    def dispatch(tx):
        return {"page_ref": _ref(tx.put("discovery_page", page_id, marker))}

    dispatch_result = store.transact(
        "discovery-dispatch-" + page_id,
        {"request_sha256": marker["request_sha256"]},
        dispatch,
        mission_id=request["mission_id"],
    )
    legs: list[dict] = []
    mapped: dict
    provider_meta: dict = {}
    notices: dict = {}
    esearch_ref = _ref(resumed_esearch) if resumed_esearch else None
    try:
        response, leg = _read(
            transport,
            method,
            url,
            body,
            headers,
            name="pubmed.esummary" if resumed_esearch is not None else route,
            store=store if route.startswith("pubmed.") else None,
            credential=credential if route.startswith("pubmed.") else None,
            ncbi_requests_per_second=ncbi_requests_per_second,
        )
        legs.append(leg)
        if resumed_esearch is not None:
            if response.status >= 400:
                mapped = _error_response(response.status, response.headers, "http")
            else:
                summaries = _json(response)
                summaries["_search_count"] = resumed_esearch["body"]["reported_count"]
                mapped = map_response(
                    route, summaries, offset=request["requested_window"].get("offset") or 0,
                    selected_ids=resumed_esearch["body"]["id_list"],
                )
            provider_meta = {
                "resumed_from_esearch_ref": esearch_ref,
                "esearch_retrieved_at": resumed_esearch["body"]["retrieved_at"],
            }
        elif response.status == 404 and route in {"crossref.doi", "pubmed.pmid", "pubmed.abstract"}:
            mapped = {"status": "ok", "items": [], "count": 0, "offset": 0}
        elif response.status >= 400:
            mapped = _error_response(response.status, response.headers, "http")
        else:
            if route == "pubmed.abstract":
                if len(response.body) > 8 * 1024 * 1024:
                    raise ValueError("oversized_response")
                selected_id = (
                    request["filters"].get("selected_id")
                    or request["filters"].get("pmid")
                    or request.get("query")
                ).strip()
                payload = parse_pubmed_abstract_xml(response.body, selected_id)
                provider_meta = {
                    "identifier_conflicts": payload.get("identifier_conflicts", []),
                    "abstract_availability": "present" if (payload.get("article") or {}).get("abstract") else "absent",
                    "abstract_truncated": (payload.get("article") or {}).get("abstract_truncated") is True,
                }
            else:
                payload = _json(response)
            notices = source_use_notices(route, payload)
            if route.startswith("exa."):
                statuses = payload.get("statuses")
                if not isinstance(statuses, list):
                    statuses = []
                results = payload.get("results")
                if not isinstance(results, list):
                    results = []
                returned_lengths = [
                    len(item["text"])
                    for item in results
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                ]
                max_chars = request.get("filters", {}).get("max_characters")
                provider_meta = {
                    "request_id": payload.get("requestId"),
                    "search_time_ms": payload.get("searchTime"),
                    "statuses": [
                        {key: item.get(key) for key in ("id", "status", "source")}
                        for item in statuses
                        if isinstance(item, dict)
                    ],
                    "returned_text_characters": returned_lengths,
                    "truncation": "possible_at_requested_limit"
                    if isinstance(max_chars, int) and max_chars in returned_lengths
                    else "not_observed",
                }
            if route == "pubmed.search":
                search = payload.get("esearchresult", {})
                ids = search.get("idlist", [])
                if not isinstance(ids, list) or any(
                    not isinstance(x, str) or not x.isdigit() for x in ids
                ):
                    raise ValueError("malformed_pubmed_ids")
                count = int(search.get("count", 0))
                if count < len(ids) or len(ids) > (request["requested_window"].get("limit") or 10):
                    raise ValueError("malformed_pubmed_ids")
                if ids:
                    esearch_ref = _save_esearch(
                        store, request, dispatch_result["page_ref"], ids, count, leg,
                    )
                    summary, summary_leg = _read(
                        transport,
                        "GET",
                        _pubmed_summary_call(ids, credential),
                        None,
                        {},
                        name="pubmed.esummary",
                        store=store,
                        credential=credential,
                        ncbi_requests_per_second=ncbi_requests_per_second,
                    )
                    legs.append(summary_leg)
                    if summary.status >= 400:
                        mapped = _error_response(summary.status, summary.headers, "http")
                    else:
                        summaries = _json(summary)
                        summaries["_search_count"] = count
                        mapped = map_response(
                            route, summaries, offset=request["requested_window"].get("offset") or 0,
                            selected_ids=ids,
                        )
                else:
                    mapped = {
                        "status": "ok",
                        "items": [],
                        "count": count,
                        "offset": request["requested_window"].get("offset") or 0,
                    }
            else:
                mapped = map_response(
                    route,
                    payload,
                    offset=request["requested_window"].get("offset") or 0,
                    cursor=request["requested_window"].get("cursor"),
                )
    except (urllib.error.URLError, TimeoutError) as exc:
        mapped = _error_response(
            None, {}, "timeout" if isinstance(exc, TimeoutError) else "transport"
        )
    except (ValueError, json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
        mapped = {"status": "malformed", "error_code": "upstream_error"}
        legs.append(
            {
                "name": "mapping",
                "error_type": type(exc).__name__,
                "reason": str(exc)[:80]
                if str(exc) in {"oversized_response", "malformed_response", "malformed_pubmed_ids",
                                    "malformed_pubmed_xml", "unexpected_pubmed_record",
                                    "pubmed_identifier_mismatch"}
                else "invalid_provider_shape",
            }
        )
    if any(leg.get("reason") == "oversized_response" for leg in legs):
        provider_payload_condition = "oversized"
    elif any(leg.get("name") == "mapping" for leg in legs):
        provider_payload_condition = "malformed"
    elif any(leg.get("status", 0) >= 400 for leg in legs):
        provider_payload_condition = "http_error"
    elif not legs:
        provider_payload_condition = "transport_error"
    else:
        provider_payload_condition = "decoded"
    provider_meta, _ = redact(provider_meta)
    receipt = record_delivery(
        store,
        request,
        mapped,
        delivery_id="delivery-" + page_id[15:],
        adapter=adapter,
        retrieved_at=_now(),
        provider_response_sha256=next(
            (leg["complete_body_sha256"] for leg in reversed(legs)
             if leg.get("status") == 200 and leg.get("complete_body_sha256")), None
        ),
        upstream_payload_condition=(
            "oversized" if provider_payload_condition == "oversized" else None
        ),
        source_use_notices=notices,
    )
    result = {
        **receipt,
        "route": route,
        "page_ref": dispatch_result["page_ref"],
        "hits": _saved_hits(store, receipt["accepted_hit_refs"] + receipt["reused_hit_refs"]),
        "http_legs": legs,
        "http_call": {"method": method, "url": safe_url, "body": safe_body},
        "provider_meta": provider_meta,
        "provider_payload_condition": provider_payload_condition,
        "esearch_ref": esearch_ref,
        "esearch_retrieved_at": (
            store.get(**esearch_ref)["body"]["retrieved_at"] if esearch_ref else None
        ),
        "resumed_from_esearch_ref": _ref(resumed_esearch) if resumed_esearch else None,
    }
    if route == "exa.contents":
        filters = request["filters"]
        selection, _ = redact(
            {
                "selected_id": filters.get("id"),
                "selected_url": filters.get("url"),
                "reason": filters.get("selection_reason", "not supplied"),
                "reason_status": "explicit"
                if "selection_reason" in filters
                else "not_supplied",
                "max_characters": filters.get("max_characters", 5000),
                "max_age_hours": filters.get("max_age_hours"),
                "freshness_basis": "explicit_max_age_hours"
                if filters.get("max_age_hours") is not None
                else "provider_default_unqualified",
            }
        )
        result["selection"] = selection
    if mapped.get("status") == "quota_wait" and isinstance(
        mapped.get("retry_after_seconds"), (int, float)
    ):
        result.update(_save_wait(store, route, mapped["retry_after_seconds"]))

    def complete(tx):
        page = tx.get("discovery_page", page_id)
        saved = tx.put(
            "discovery_page",
            page_id,
            {**marker, "status": "completed", "result": result, "finished_at": _now()},
            expected_version=page["version"],
        )
        if esearch_ref is not None:
            tx.link(saved, tx.get(**esearch_ref), "uses_esearch_id_list")
        return {"page_ref": _ref(saved)}

    completed = store.transact(
        "discovery-complete-" + page_id,
        {"delivery_ref": receipt["delivery_ref"]},
        complete,
        mission_id=request["mission_id"],
    )
    result["page_ref"] = completed["page_ref"]
    return result


__all__ = ["available_routes", "run_discovery_page"]
