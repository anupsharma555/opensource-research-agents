"""Make saved Groq browser-search responses available for further research.

Read the response linked to its exact model request, return short pages of its
text, and save eligible literal URLs as leads for later fetching. Browser search
runs on Groq's servers; this module processes saved results without making
network calls. Returned text and URLs remain discovery leads until their sources
are separately fetched and inspected.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlsplit

from ora.retrieval.normalize import sha256 as retrieval_sha256
from ora.retrieval.receipts import record_delivery
from ora.retrieval.redact import redact_url
from ora.state import StateStore

_URL = re.compile(r"https?://[^\s<>\[\]\"']+")
_CITATION = re.compile(r"(?:〖[^〗\n]{1,100}〗|【[^】\n]{1,100}】)")
_MAX_HITS = 16


def rejected_browser_selection(request: dict, error: dict | None) -> bool:
    """Recognize an explicit rejected selection, never executable arguments."""
    if (
        not isinstance(error, dict)
        or error.get("type") != "invalid_request_error"
        or error.get("code") != "tool_use_failed"
        or {"type": "browser_search"} in request.get("tools", [])
        or any(item.get("function", {}).get("name") == "browser_search"
               for item in request.get("tools", []))
        or not re.fullmatch(
            r"(?:Tool call validation failed: tool call validation failed: )?"
            r"attempted to call tool 'browser_search' which was not in request\.tools",
            str(error.get("message", "")),
        )
    ):
        return False
    # The provider's exact validation error identifies the attempted name.
    # Do not retain or interpret rejected arguments or private generations.
    return True


def _saved_response(store: StateStore, response_ref: dict[str, Any]) -> tuple[dict, dict, dict]:
    """Verify a stored response against the exact request that exposed search."""
    if (type(response_ref) is not dict or set(response_ref) != {"kind", "id", "version"}
        or response_ref["kind"] != "model_response_receipt"
        or not isinstance(response_ref["id"], str) or not response_ref["id"]
        or type(response_ref["version"]) is not int or response_ref["version"] < 1):
        raise ValueError("An exact model response receipt is required")
    receipt = store.get(**response_ref)
    if receipt is None:
        raise ValueError("Model response receipt is unavailable")
    body = receipt["body"]
    intent_ref = body.get("intent_ref")
    intent = store.get(**intent_ref) if isinstance(intent_ref, dict) else None
    if (intent is None or intent["body"].get("phase") != "collect"
        or intent["body"].get("request_sha256") != body.get("request_sha256")
        or body.get("status") not in {"complete", "tool_requested"}):
        raise ValueError("Response is not a completed matching collection")
    request_raw = store.read_blob(intent["body"]["blob"]["sha256"])
    if hashlib.sha256(request_raw).hexdigest() != body["request_sha256"]:
        raise ValueError("Saved request bytes changed")
    request = json.loads(request_raw)
    if {"type": "browser_search"} not in request.get("tools", []):
        raise ValueError("Browser search was not enabled for this response")
    private_raw = store.read_blob(body["blob"]["sha256"])
    if hashlib.sha256(private_raw).hexdigest() != body["blob"]["sha256"]:
        raise ValueError("Saved response content changed")
    private = json.loads(private_raw)
    if private.get("content") is not None and not isinstance(private["content"], str):
        raise ValueError("Saved response content is invalid")
    return receipt, intent, private


def project_browser_search(
    store: StateStore, response_ref: dict[str, Any], *, offset: int = 0,
    limit: int = 1800,
) -> dict[str, Any]:
    """Page saved discovery text, linked to its exact response and request.

    This records availability, not independent proof of a provider search.
    Explicit URLs and markers come from assistant content; the separate count
    reports literal URLs in the observed executed-tool metadata shape. The
    caller must enforce peer and task scope before displaying the result.
    """
    if (type(offset) is not int or offset < 0 or type(limit) is not int
        or not 1 <= limit <= 12000):
        raise ValueError("A bounded nonnegative page is required")
    receipt, _intent, private = _saved_response(store, response_ref)
    body = receipt["body"]
    content = private.get("content")
    content = content or ""
    if offset > len(content):
        raise ValueError("Page offset exceeds returned content")
    page = content[offset:offset + limit]
    urls = list(dict.fromkeys(match.rstrip(".,;:!?)") for match in _URL.findall(page)))
    markers = list(dict.fromkeys(_CITATION.findall(page)))
    content_sha256 = hashlib.sha256(content.encode()).hexdigest()
    next_offset = offset + len(page)
    return {
        "kind": "browser_search_discovery",
        "response_receipt_ref": dict(response_ref),
        "request_sha256": body["request_sha256"],
        "response_sha256": body.get("response_sha256"),
        "private_blob_sha256": body["blob"]["sha256"],
        "content": page,
        "content_chars": len(content),
        "content_sha256": content_sha256,
        "offset": offset,
        "next_offset": next_offset if next_offset < len(content) else None,
        "explicit_urls": urls[:32],
        "explicit_citation_markers": markers[:64],
        "provider_metadata_present": bool(private.get("provider_metadata")),
        "provider_metadata_url_lead_count": len(_provider_metadata_urls(private)),
        "search_execution": "not_independently_confirmed",
        "source_inspection": "not_performed",
        "next_request": ({"tool": "read_browser_search_result",
                          "response_id": response_ref["id"],
                          "offset": next_offset, "limit": limit,
                          "content_sha256": content_sha256}
                         if next_offset < len(content) else None),
    }


def _fetchable_url(candidate: str) -> bool:
    """Reject unsafe URLs locally; fetch_source does the DNS and redirect checks."""
    if len(candidate) > 2048 or any(ord(char) < 33 or ord(char) == 127 for char in candidate):
        return False
    safe, changed = redact_url(candidate)
    if changed or safe != candidate:
        return False
    try:
        parsed = urlsplit(candidate)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password
        or port not in {None, 443} or host.endswith((".local", ".localhost", ".internal"))
        or host == "localhost"):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host and all(part for part in host.split("."))


def _provider_metadata_urls(private: dict) -> list[str]:
    """Read only literal result URLs in the observed executed-tools shape.

    These are provider-reported discovery leads, not independently inspected
    pages. Ignore arbitrary output text, arguments and unknown metadata shapes.
    """
    metadata = private.get("provider_metadata")
    message = metadata.get("message") if isinstance(metadata, dict) else None
    executed = message.get("executed_tools") if isinstance(message, dict) else None
    if not isinstance(executed, list):
        return []
    urls = []
    seen = set()
    for tool in executed[:16]:
        if (not isinstance(tool, dict)
            or (tool.get("name"), tool.get("type")) not in {
                ("browser.search", "browser_search"), ("browser.open", "browser.open")
            }):
            continue
        search_results = tool.get("search_results")
        results = search_results.get("results") if isinstance(search_results, dict) else None
        if not isinstance(results, list):
            continue
        for result in results[:32]:
            url = result.get("url") if isinstance(result, dict) else None
            if isinstance(url, str) and url not in seen and _fetchable_url(url):
                seen.add(url)
                urls.append(url)
                if len(urls) == _MAX_HITS:
                    return urls
    return urls


def _existing_browser_leads(
    store: StateStore, response_ref: dict[str, Any], scope: dict
) -> dict[str, Any] | None:
    """Replay an accepted lead set without changing its historical conversion."""
    saved = store.get("browser_search_leads", f"browser-leads:{response_ref['id']}")
    if saved is None:
        return None
    body = saved["body"]
    if (body.get("response_ref") != response_ref
        or any(body.get(key) != scope.get(value) for key, value in (
            ("mission_id", "mission_id"), ("task_id", "task_id"),
            ("actor_id", "instance_id"),
        )) or not isinstance(body.get("hit_refs"), list) or not body["hit_refs"]):
        raise ValueError("Saved browser leads lost their response or task scope")
    hits = []
    delivery_ref = None
    for pointer in body["hit_refs"]:
        if (not isinstance(pointer, dict)
            or set(pointer) != {"kind", "id", "version"}
            or pointer.get("kind") != "retrieval_hit"
            or not isinstance(pointer.get("id"), str)
            or type(pointer.get("version")) is not int):
            raise ValueError("Saved browser lead has an invalid exact hit reference")
        hit = store.get(**pointer)
        if hit is None or any(hit["body"].get(key) != scope.get(value) for key, value in (
            ("mission_id", "mission_id"), ("task_id", "task_id"),
            ("agent_instance_id", "instance_id"),
        )) or hit["body"].get("route") != "groq.browser_search.url_leads":
            raise ValueError("Saved browser lead is outside its original task")
        source = hit["body"].get("source")
        if (not isinstance(source, dict)
            or source.get("access_surface") != "metadata" or source.get("content") is not None
            or not isinstance(source.get("original_url"), str)
            or not _fetchable_url(source["original_url"])
            or not any(link["relation"] == "derived_from" and link["target"] == response_ref
                       for link in store.links(hit))):
            raise ValueError("Saved browser lead lacks metadata-only response provenance")
        current_delivery_ref = hit["body"].get("delivery_ref")
        if (not isinstance(current_delivery_ref, dict)
            or set(current_delivery_ref) != {"kind", "id", "version"}
            or current_delivery_ref.get("kind") != "retrieval_delivery"):
            raise ValueError("Saved browser lead has an invalid delivery reference")
        delivery = store.get(**current_delivery_ref)
        envelope = delivery["body"].get("envelope") if delivery else None
        request = envelope.get("request") if isinstance(envelope, dict) else None
        filters = request.get("filters") if isinstance(request, dict) else None
        if (not isinstance(filters, dict)
            or filters.get("model_response_receipt_ref") != response_ref
            or any(request.get(key) != scope.get(value) for key, value in (
                ("missionId", "mission_id"), ("taskId", "task_id"),
                ("agentInstanceId", "instance_id"),
            ))):
            raise ValueError("Saved browser lead delivery lost its exact response")
        if delivery_ref is not None and delivery_ref != current_delivery_ref:
            raise ValueError("Saved browser leads span different deliveries")
        delivery_ref = current_delivery_ref
        hits.append({"hit_id": hit["id"], "hit_ref": dict(pointer),
                     "original_url": source["original_url"],
                     "access_surface": "metadata", "source_inspection": "not_performed",
                     "response_receipt_ref": dict(response_ref)})
    return {"response_receipt_ref": dict(response_ref), "hits": hits,
            "status": "url_leads_saved", "delivery_ref": delivery_ref}


def _orphan_browser_delivery(
    store: StateStore, response_ref: dict[str, Any], scope: dict, token: str,
) -> dict[str, Any] | None:
    """Find a committed delivery left before its lead-link transaction."""
    delivery_id = f"browser-search-leads-{token}"
    record_id = "retrieval-delivery-" + retrieval_sha256({
        "mission": scope["mission_id"], "request": delivery_id, "delivery": delivery_id,
    })[:32]
    delivery = store.get("retrieval_delivery", record_id, 1)
    if delivery is None:
        return None
    envelope = delivery["body"].get("envelope")
    request = envelope.get("request") if isinstance(envelope, dict) else None
    filters = request.get("filters") if isinstance(request, dict) else None
    items = envelope.get("items") if isinstance(envelope, dict) else None
    if (delivery["body"].get("delivery_id") != delivery_id
        or not isinstance(filters, dict)
        or filters.get("model_response_receipt_ref") != response_ref
        or filters.get("conversion") not in {
            "literal_url_leads_only", "literal_assistant_or_executed_metadata_url_leads",
        }
        or any(request.get(key) != scope.get(value) for key, value in (
            ("missionId", "mission_id"), ("taskId", "task_id"),
            ("agentInstanceId", "instance_id"),
        ))
        or request.get("requestId") != delivery_id
        or request.get("attemptId") != delivery_id
        or request.get("deliveryId") != delivery_id
        or request.get("route") != "groq.browser_search.url_leads"
        or request.get("provider") != "groq"
        or not isinstance(items, list) or not 1 <= len(items) <= _MAX_HITS):
        raise ValueError("Committed browser delivery lost its exact response scope")
    delivery_ref = {key: delivery[key] for key in ("kind", "id", "version")}
    hit_refs = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("itemId"), str):
            raise ValueError("Committed browser delivery has an invalid lead item")
        hit = store.get("retrieval_hit", item["itemId"])
        source = hit["body"].get("source") if hit else None
        if (hit is None or hit["version"] != 1
            or any(hit["body"].get(key) != scope.get(value) for key, value in (
                ("mission_id", "mission_id"), ("task_id", "task_id"),
                ("agent_instance_id", "instance_id"),
            ))
            or hit["body"].get("route") != "groq.browser_search.url_leads"
            or hit["body"].get("delivery_ref") != delivery_ref
            or not isinstance(source, dict)
            or source.get("original_url") != item.get("originalUrl")
            or source.get("access_surface") != "metadata"
            or source.get("content") is not None
            or not isinstance(source.get("original_url"), str)
            or not _fetchable_url(source["original_url"])
            or not any(link["relation"] == "derived_from" and link["target"] == delivery_ref
                       for link in store.links(hit))):
            raise ValueError("Committed browser delivery has an invalid exact hit")
        hit_refs.append({key: hit[key] for key in ("kind", "id", "version")})
    return {"delivery_ref": delivery_ref, "accepted_hit_refs": hit_refs,
            "reused_hit_refs": []}


def save_browser_search_hits(
    store: StateStore, response_ref: dict[str, Any],
) -> dict[str, Any]:
    """Save bounded literal response URLs as metadata-only hits for later fetch.

    `record_delivery` owns the retrieval-hit shape. This local conversion does
    not assert that Groq fetched any returned URL or that its snippets are source
    text. No network call occurs here. Replaying the same receipt is idempotent,
    including when an older delivery committed before its lead link.
    """
    receipt, intent, private = _saved_response(store, response_ref)
    body = receipt["body"]
    scope = intent["body"]
    if existing := _existing_browser_leads(store, response_ref, scope):
        return existing
    token = hashlib.sha256(
        f"{response_ref['id']}:{response_ref['version']}:{body['response_sha256']}".encode()
    ).hexdigest()[:24]
    delivery = _orphan_browser_delivery(store, response_ref, scope, token)
    if delivery is None:
        content = private.get("content") or ""
        content_urls = list(dict.fromkeys(
            url for raw in _URL.findall(content)
            if (url := raw.rstrip(".,;:!?)")) and _fetchable_url(url)
        ))
        urls = list(dict.fromkeys([*content_urls, *_provider_metadata_urls(private)]))[:_MAX_HITS]
        if not urls:
            return {"response_receipt_ref": dict(response_ref), "hits": [],
                    "status": "no_literal_fetchable_urls"}
        request = {
            "mission_id": scope["mission_id"], "task_id": scope["task_id"],
            "request_id": f"browser-search-leads-{token}",
            "attempt_id": f"browser-search-leads-{token}",
            "agent_instance_id": scope["instance_id"],
            "route": "groq.browser_search.url_leads", "provider": "groq",
            "index": None, "operation": "search", "query": None,
            "filters": {"model_response_receipt_ref": dict(response_ref),
                        "conversion": "literal_assistant_or_executed_metadata_url_leads"},
            "requested_at": receipt["created_at"],
            "requested_window": {"offset": 0, "limit": len(urls)},
        }
        mapped = {"status": "ok", "offset": 0, "items": [
            {"id": f"browser-url-{hashlib.sha256(url.encode()).hexdigest()[:24]}",
             "url": url, "title": None, "content": None, "surface": "metadata",
             "origin": "original_source", "rights_status": "unknown",
             "access_status": "unknown", "rank": index}
            for index, url in enumerate(urls, 1)
        ]}
        delivery = record_delivery(
            store, request, mapped, delivery_id=f"browser-search-leads-{token}",
            adapter="count_offset", retrieved_at=receipt["created_at"],
            provider_response_sha256=body.get("response_sha256"),
        )
    hit_refs = [*delivery["accepted_hit_refs"], *delivery["reused_hit_refs"]]

    def link(tx):
        source = tx.get(**response_ref)
        if source is None or source["body"] != body:
            raise ValueError("Model response changed before hit provenance was linked")
        for pointer in hit_refs:
            hit = tx.get(**pointer)
            if hit is None or hit["body"].get("mission_id") != scope["mission_id"]:
                raise ValueError("Browser URL lead is missing from its mission")
            tx.link(hit, source, "derived_from")
        tx.put("browser_search_leads", f"browser-leads:{response_ref['id']}", {
            "schema": "ora.browser_search_leads.v1",
            "mission_id": scope["mission_id"], "task_id": scope["task_id"],
            "actor_id": scope["instance_id"], "response_ref": dict(response_ref),
            "hit_refs": hit_refs,
        })
        return {"linked_hit_refs": hit_refs}

    store.transact(
        f"browser-search-hit-provenance-{token}",
        {"response_ref": response_ref, "hit_refs": hit_refs}, link,
        mission_id=scope["mission_id"],
    )
    hits = [{"hit_id": pointer["id"], "hit_ref": pointer,
             "original_url": store.get(**pointer)["body"]["source"]["original_url"],
             "access_surface": "metadata", "source_inspection": "not_performed",
             "response_receipt_ref": dict(response_ref)} for pointer in hit_refs]
    return {"response_receipt_ref": dict(response_ref), "hits": hits,
            "status": "url_leads_saved", "delivery_ref": delivery["delivery_ref"]}
