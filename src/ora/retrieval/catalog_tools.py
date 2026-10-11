"""Authenticated, model-facing venue lookup and proposal helpers.

Catalog entries are leads, not a whitelist or a quality ranking. The mission
host supplies the actor, run and durable operation identity; the peer supplies
its search/filter intent and the rationale for any new venue.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import uuid
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from ora.evidence.intake import (
    OversizedSourceError,
    PublicSourceHTTPError,
    fetch_public_source,
)
from ora.evidence.library import EvidenceError
from ora.state import StateStore
from ora.state.store import StateConflict

from .catalog import KINDS, catalog_export, propose_venue, seed_catalog
from .normalize import canonical_bytes
from .redact import redact_url

CATALOG_TOOL_SCHEMAS = {
    "list_venues": {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "kinds": {"type": "array", "items": {"type": "string", "enum": sorted(KINDS)}},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": [],
        "additionalProperties": False,
    },
    "propose_venue": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": sorted(KINDS)},
            "url": {"type": "string"},
            "rationale": {"type": "string"},
        },
        "required": ["name", "kind", "url", "rationale"],
        "additionalProperties": False,
    },
    "navigate_venue": {
        "type": "object",
        "properties": {
            "catalog_ref": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "id": {"type": "string"},
                    "version": {"type": "integer"},
                    "sha256": {"type": "string"},
                },
                "required": ["kind", "id", "version"],
                "additionalProperties": False,
            },
            "link_index": {"type": "integer", "minimum": 0},
            "link_offset": {"type": "integer", "minimum": 0},
            "link_limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["catalog_ref"],
        "additionalProperties": False,
    },
    "page_venue_navigation": {
        "type": "object",
        "properties": {
            "navigation_ref": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["venue_navigation"]},
                    "id": {"type": "string"},
                    "version": {"type": "integer", "minimum": 1},
                },
                "required": ["kind", "id", "version"],
                "additionalProperties": False,
            },
            "link_offset": {"type": "integer", "minimum": 0},
            "link_limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["navigation_ref", "link_offset", "link_limit"],
        "additionalProperties": False,
    },
}

CATALOG_TOOL_DESCRIPTIONS = {
    "list_venues": "Browse typed, dated venue navigation leads; an absent venue can still be discovered and proposed.",
    "propose_venue": "Save an unlisted relevant venue with rationale and use its URL as a discovery lead immediately.",
    "navigate_venue": "Open an exact saved venue lead or proposal; report the actual page, redirects, and links as navigation leads only.",
    "page_venue_navigation": "Page links from an exact saved navigation result without fetching the venue again.",
}


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _seed_ref(entry: dict) -> dict:
    return {
        "kind": "venue_seed",
        "id": entry["id"],
        "version": entry["version"],
        "sha256": hashlib.sha256(canonical_bytes(entry)).hexdigest(),
    }


def _entry_ref(store: StateStore, entry: dict, mission_id: str) -> dict:
    saved = store.get("venue_entry", entry["id"])
    if saved and saved["body"]["entry"] == {
        k: v for k, v in entry.items() if k != "navigation_history"
    }:
        return _ref(saved)
    for proposal in store.records("venue_proposal"):
        if (
            proposal["body"].get("mission_id") == mission_id
            and proposal["body"]["entry"]["id"] == entry["id"]
        ):
            return _ref(proposal)
    seed = next((item for item in seed_catalog()["entries"] if item["id"] == entry["id"]), None)
    if seed is not None:
        return _seed_ref(seed)
    raise ValueError("Catalog entry has no exact saved reference")


def _resolve_ref(store: StateStore, catalog_ref: dict, mission_id: str) -> dict:
    if not isinstance(catalog_ref, dict):
        raise ValueError("Navigation needs an exact catalog reference")
    kind = catalog_ref.get("kind")
    if kind == "venue_seed":
        entry = next(
            (item for item in seed_catalog()["entries"] if item["id"] == catalog_ref.get("id")),
            None,
        )
        if entry is None or catalog_ref != _seed_ref(entry):
            raise ValueError("Catalog seed reference changed or is unknown")
        return entry
    if kind not in {"venue_entry", "venue_proposal"} or set(catalog_ref) != {
        "kind",
        "id",
        "version",
    }:
        raise ValueError("Unsupported catalog reference")
    record = store.get(**catalog_ref)
    if record is None or (
        kind == "venue_proposal" and record["body"].get("mission_id") != mission_id
    ):
        raise ValueError("Catalog reference is not available in this mission")
    return record["body"]["entry"]


def _public_link(base: str, href: str) -> str | None:
    try:
        resolved = urljoin(base, href)
        parsed = urlsplit(resolved)
        host = parsed.hostname
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass  # Domain DNS is checked when a lead is actually fetched.
        return redact_url(resolved)[0]
    except ValueError:
        return None


class _PageLeads(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.title = ""
        self.heading = ""
        self.links: list[dict[str, str]] = []
        self._tag = ""
        self._parts: list[str] = []
        self._href: str | None = None
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
        if self._skip or tag not in {"title", "h1", "a"}:
            return
        self._tag, self._parts = tag, []
        self._href = dict(attrs).get("href") if tag == "a" else None

    def handle_data(self, data: str) -> None:
        if self._tag and not self._skip:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._tag:
            value = " ".join(" ".join(self._parts).split())[:240]
            if tag == "title" and not self.title:
                self.title = value
            elif tag == "h1" and not self.heading:
                self.heading = value
            elif tag == "a" and self._href:
                url = _public_link(self.base, self._href)
                if url:
                    self.links.append({"url": url, "text": value})
            self._tag, self._parts, self._href = "", [], None
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)


def _page_leads(content: bytes, content_type: str, base: str) -> _PageLeads:
    page = _PageLeads(base)
    if "html" in content_type.lower():
        page.feed(content.decode("utf-8", errors="replace"))
    return page


def _agent_in_run(store: StateStore, mission_id: str, run_id: str, actor_id: str) -> dict:
    actor = store.get("agent_instance", actor_id, 1)
    if (
        actor is None
        or actor["body"].get("mission_id") != mission_id
        or actor["body"].get("run_id") != run_id
    ):
        raise ValueError("Catalog actor must belong to the selected mission run")
    return actor


def list_catalog_leads(
    store: StateStore,
    *,
    mission_id: str,
    run_id: str,
    actor_id: str,
    text: str = "",
    kinds: list[str] | None = None,
    offset: int = 0,
    limit: int = 25,
) -> dict:
    """Page matching leads without conferring source-use permission or rank."""
    _agent_in_run(store, mission_id, run_id, actor_id)
    if not isinstance(text, str) or type(offset) is not int or offset < 0:
        raise ValueError("Catalog lookup needs text and nonnegative offset")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Catalog page size must be 1–100")
    if kinds is not None and (
        not isinstance(kinds, list) or any(kind not in KINDS for kind in kinds)
    ):
        raise ValueError("Catalog kinds must name supported resource types")
    term = text.casefold().strip()
    entries = [
        entry
        for entry in catalog_export(store, mission_id=mission_id)["entries"]
        if (kinds is None or entry["type"] in kinds)
        and (
            not term
            or term in (entry["name"] + " " + str(entry.get("topic_scope") or "")).casefold()
        )
    ]
    selected = [
        {**entry, "catalog_ref": _entry_ref(store, entry, mission_id)}
        for entry in entries[offset : offset + limit]
    ]
    return {
        "status": "ok",
        "entries": selected,
        "total_matching": len(entries),
        "next_offset": offset + len(selected) if offset + len(selected) < len(entries) else None,
        "catalog_is_whitelist": False,
        "next_action": "Use a lead or propose a relevant unlisted venue; verify each source separately",
    }


def propose_catalog_lead(
    store: StateStore,
    *,
    mission_id: str,
    run_id: str,
    actor_id: str,
    operation_id: str,
    proposed_at: str,
    name: str,
    kind: str,
    url: str,
    rationale: str,
) -> dict:
    """Save one agent-authored unlisted lead with a stable replay identity."""
    _agent_in_run(store, mission_id, run_id, actor_id)
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("Catalog proposal needs a stable operation ID")
    result = propose_venue(
        store,
        mission_id=mission_id,
        actor_id=actor_id,
        run_id=run_id,
        operation_id=operation_id,
        proposed_at=proposed_at,
        name=name,
        kind=kind,
        url=url,
        rationale=rationale,
    )
    return {
        "status": "recorded",
        **result,
        "catalog_ref": result["proposal_ref"],
        "available_for_use": True,
        "qualification": "discovered",
        "next_action": "Navigate the lead under its actual access conditions; catalog membership is not required",
    }


def navigate_catalog_lead(
    store: StateStore,
    *,
    mission_id: str,
    run_id: str,
    actor_id: str,
    operation_id: str,
    catalog_ref: dict,
    link_index: int = 0,
    link_offset: int = 0,
    link_limit: int = 25,
    max_bytes: int = 2 * 1024 * 1024,
    fetch: Callable[..., dict[str, Any]] = fetch_public_source,
) -> dict:
    """Navigate an exact catalog lead once; page saved links without refetching.

    A dispatch without a completed result is uncertain. The caller can choose a
    new operation ID after inspecting that state; the host never silently retries.
    """
    _agent_in_run(store, mission_id, run_id, actor_id)
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise ValueError("Navigation needs a durable operation ID")
    if type(link_index) is not int or link_index < 0:
        raise ValueError("Navigation link index must be nonnegative")
    if type(link_offset) is not int or link_offset < 0:
        raise ValueError("Navigation link offset must be nonnegative")
    if type(link_limit) is not int or not 1 <= link_limit <= 100:
        raise ValueError("Navigation link page size must be 1–100")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 2 * 1024 * 1024:
        raise ValueError("Navigation read limit must be 1–2097152 bytes")
    entry = _resolve_ref(store, catalog_ref, mission_id)
    if link_index >= len(entry["links"]):
        raise ValueError("Catalog reference has no link at that index")
    link = entry["links"][link_index]
    requested_url = link["url"]
    navigation_context = {
        "resource_type": entry["type"],
        "catalog_version": entry["version"],
        "link_type": link["type"],
        "resource_year": link.get("resource_year"),
        "link_qualification": link["qualification"],
        "verified_route": link.get("verified_route"),
        "access_note": entry["access_note"],
        "content_use": "navigation_lead_only",
    }
    catalog_entry_sha256 = hashlib.sha256(canonical_bytes(entry)).hexdigest()
    request = {
        "mission_id": mission_id,
        "run_id": run_id,
        "actor_id": actor_id,
        "catalog_ref": catalog_ref,
        "link_index": link_index,
        "requested_url": requested_url,
        "max_bytes": max_bytes,
    }
    key = hashlib.sha256(
        canonical_bytes({"mission_id": mission_id, "operation_id": operation_id})
    ).hexdigest()[:32]
    record_id = "venue-navigation-" + key
    marker_id = "venue-navigation-dispatch-" + key
    completed = store.get("venue_navigation", record_id)
    if completed:
        if completed["body"]["request"] != request:
            raise StateConflict("Navigation operation ID was used with different inputs")
        return _navigation_result(store, completed, link_offset, link_limit)
    marker = store.get("venue_navigation_dispatch", marker_id)
    if marker and marker["body"]["request"] != request:
        raise StateConflict("Navigation operation ID was used with different inputs")
    if marker:
        return {
            "status": "uncertain_dispatch",
            "dispatch_ref": _ref(marker),
            "requested_url": requested_url,
            "next_action": "Review the incomplete attempt, then choose a new operation ID if a retry is warranted",
        }
    nonce = uuid.uuid4().hex

    def dispatch(tx):
        saved = tx.put(
            "venue_navigation_dispatch",
            marker_id,
            {
                "schema": "ora.venue_navigation_dispatch.v1",
                "request": request,
                "catalog_entry_snapshot": entry,
                "catalog_entry_sha256": catalog_entry_sha256,
                "dispatched_at": store.now(),
            },
        )
        return {"dispatch_ref": _ref(saved), "nonce": nonce}

    admission = store.transact(
        "venue-navigation-dispatch-op-" + key, request, dispatch, mission_id=mission_id
    )
    if admission["nonce"] != nonce:
        return {
            "status": "uncertain_dispatch",
            "dispatch_ref": admission["dispatch_ref"],
            "requested_url": requested_url,
            "next_action": "Review the incomplete attempt, then choose a new operation ID if a retry is warranted",
        }
    content = None
    blob = None
    response: dict[str, Any] = {}
    try:
        fetched = fetch(requested_url, max_bytes=max_bytes)
        content = fetched["content"]
        if not isinstance(content, bytes) or len(content) > max_bytes:
            raise ValueError("Public fetch returned invalid or oversized content")
        actual_hash = hashlib.sha256(content).hexdigest()
        if fetched.get("content_sha256") != actual_hash or fetched.get("byte_length") != len(
            content
        ):
            raise ValueError("Public fetch receipt does not match returned bytes")
        blob = store.put_blob(content)
        page = _page_leads(content, fetched["content_type"], fetched["final_url"])
        response = {
            "status": "navigable",
            "status_code": fetched["status"],
            "requested_url": fetched.get("requested_url", requested_url),
            "final_url": fetched["final_url"],
            "redirect_chain": fetched.get("redirect_chain", []),
            "checked_at": fetched["retrieved_at"],
            "content_type": fetched["content_type"],
            "content_sha256": actual_hash,
            "byte_length": len(content),
            "title": page.title,
            "heading": page.heading,
            "link_count": len(page.links),
            "link_preview": page.links[:50],
            "link_preview_truncated": len(page.links) > 50,
            "access_basis": fetched.get("access_basis"),
        }
    except PublicSourceHTTPError as exc:
        response = {
            "status": "blocked"
            if exc.status_code in {401, 403}
            else "unavailable"
            if exc.status_code == 404
            else "http_error",
            "status_code": exc.status_code,
            "requested_url": exc.requested_url,
            "final_url": exc.final_url,
            "redirect_chain": exc.redirect_chain,
            "checked_at": exc.retrieved_at,
        }
    except OversizedSourceError as exc:
        response = {
            "status": "oversized_response",
            "status_code": None,
            "requested_url": requested_url,
            "final_url": None,
            "redirect_chain": [],
            "checked_at": store.now(),
            "size_receipt": exc.receipt(),
        }
    except (EvidenceError, ValueError, KeyError) as exc:
        response = {
            "status": "fetch_error",
            "status_code": None,
            "requested_url": requested_url,
            "final_url": None,
            "redirect_chain": [],
            "checked_at": store.now(),
            "error_code": type(exc).__name__,
            "diagnostic": str(exc)[:240],
        }
    body = {
        "schema": "ora.venue_navigation_attempt.v1",
        "mission_id": mission_id,
        "run_id": run_id,
        "actor_id": actor_id,
        "venue_id": entry["id"],
        "catalog_version": entry["version"],
        "navigation_context": navigation_context,
        "request": request,
        "dispatch_ref": admission["dispatch_ref"],
        "catalog_entry_snapshot": entry,
        "catalog_entry_sha256": catalog_entry_sha256,
        **response,
    }
    if blob is not None:
        body["blob"] = blob

    def finish(tx):
        saved = tx.put("venue_navigation", record_id, body)
        return {"navigation_ref": _ref(saved)}

    saved = store.transact(
        "venue-navigation-complete-op-" + key, body, finish, mission_id=mission_id
    )
    return _navigation_result(store, store.get(**saved["navigation_ref"]), link_offset, link_limit)


def _navigation_result(
    store: StateStore, record: dict, offset: int, limit: int, *,
    parsed_links: list[dict[str, str]] | None = None,
    catalog_history_status: str | None = None,
) -> dict:
    body = record["body"]
    links: list[dict[str, str]] = parsed_links if parsed_links is not None else []
    if parsed_links is None and body.get("blob"):
        content = store.read_blob(body["blob"]["sha256"])
        links = _page_leads(content, body["content_type"], body["final_url"]).links
    selected = links[offset : offset + limit]
    next_offset = offset + len(selected) if offset + len(selected) < len(links) else None
    return {
        "status": body["status"],
        "navigation_ref": _ref(record),
        "catalog_ref": body["request"]["catalog_ref"],
        "navigation_context": body.get("navigation_context"),
        "catalog_history_status": catalog_history_status,
        "requested_url": body["requested_url"],
        "final_url": body["final_url"],
        "redirect_chain": body["redirect_chain"],
        "checked_at": body["checked_at"],
        "status_code": body["status_code"],
        "content_type": body.get("content_type"),
        "content_sha256": body.get("content_sha256"),
        "byte_length": body.get("byte_length"),
        "title": body.get("title"),
        "heading": body.get("heading"),
        "links": selected,
        "link_count": len(links),
        "next_link_offset": next_offset,
        "next_request": ({
            "tool": "page_venue_navigation",
            "navigation_ref": _ref(record),
            "link_offset": next_offset,
            "link_limit": limit,
        } if next_offset is not None else None),
        "diagnostic": body.get("diagnostic"),
        "error_code": body.get("error_code"),
        "size_receipt": body.get("size_receipt"),
        "evidence_status": "navigation_lead_only",
        "next_action": (
            "Request page_venue_navigation if needed, then use next_request to read retained links; "
            "verify any article and its source copy separately"
            if next_offset is not None else
            "Follow useful links or search the venue, then verify any article and its source copy separately"
        ),
    }


def page_venue_navigation(
    store: StateStore, *, mission_id: str, run_id: str, actor_id: str,
    navigation_ref: dict, link_offset: int, link_limit: int,
) -> dict:
    """Page an exact completed navigation's retained links, without a fetch."""
    _agent_in_run(store, mission_id, run_id, actor_id)
    if (
        not isinstance(navigation_ref, dict)
        or set(navigation_ref) != {"kind", "id", "version"}
        or navigation_ref.get("kind") != "venue_navigation"
        or not isinstance(navigation_ref.get("id"), str)
        or not navigation_ref["id"]
        or type(navigation_ref.get("version")) is not int
        or navigation_ref["version"] < 1
    ):
        raise ValueError("Paging needs an exact venue-navigation reference")
    if type(link_offset) is not int or link_offset < 0:
        raise ValueError("Link offset must be nonnegative")
    if type(link_limit) is not int or not 1 <= link_limit <= 100:
        raise ValueError("Link page size must be 1–100")
    record = store.get(**navigation_ref)
    if record is None:
        raise ValueError("Exact venue navigation is unavailable")
    body = record["body"]
    request = body.get("request")
    if (
        body.get("schema") != "ora.venue_navigation_attempt.v1"
        or body.get("status") != "navigable"
        or body.get("status_code") != 200
        or body.get("mission_id") != mission_id
        or body.get("run_id") != run_id
        or not isinstance(request, dict)
        or request.get("mission_id") != mission_id
        or request.get("run_id") != run_id
        or request.get("actor_id") != body.get("actor_id")
        or request.get("requested_url") != body.get("requested_url")
    ):
        raise ValueError("Saved navigation is unavailable in this run")
    _agent_in_run(store, mission_id, run_id, body["actor_id"])
    catalog_ref = request.get("catalog_ref")
    index = request.get("link_index")
    context = body.get("navigation_context")
    if (
        type(index) is not int or index < 0
        or not isinstance(catalog_ref, dict)
        or catalog_ref.get("id") != body.get("venue_id")
        or not isinstance(context, dict)
        or context.get("catalog_version") != body.get("catalog_version")
        or context.get("resource_type") not in KINDS
        or not isinstance(context.get("link_type"), str)
        or not isinstance(context.get("link_qualification"), str)
    ):
        raise ValueError("Saved navigation disagrees with its pinned catalog entry")
    dispatch_ref = body.get("dispatch_ref")
    dispatch = store.get(**dispatch_ref) if (
        isinstance(dispatch_ref, dict)
        and set(dispatch_ref) == {"kind", "id", "version"}
        and dispatch_ref["kind"] == "venue_navigation_dispatch"
    ) else None
    if (
        dispatch is None
        or dispatch["body"].get("schema") != "ora.venue_navigation_dispatch.v1"
        or dispatch["body"].get("request") != request
        or not record["id"].startswith("venue-navigation-")
        or dispatch_ref["id"] != record["id"].replace(
            "venue-navigation-", "venue-navigation-dispatch-", 1
        )
    ):
        raise ValueError("Saved navigation has no matching dispatch provenance")
    snapshot = body.get("catalog_entry_snapshot")
    snapshot_hash = body.get("catalog_entry_sha256")
    dispatched_snapshot = dispatch["body"].get("catalog_entry_snapshot")
    dispatched_hash = dispatch["body"].get("catalog_entry_sha256")
    if any(value is not None for value in (
        snapshot, snapshot_hash, dispatched_snapshot, dispatched_hash,
    )):
        if (
            not isinstance(snapshot, dict)
            or snapshot != dispatched_snapshot
            or not isinstance(snapshot_hash, str)
            or snapshot_hash != dispatched_hash
            or hashlib.sha256(canonical_bytes(snapshot)).hexdigest() != snapshot_hash
        ):
            raise ValueError("Saved catalog snapshot disagrees with its dispatch")
        if catalog_ref.get("kind") == "venue_seed":
            if catalog_ref != {
                "kind": "venue_seed", "id": snapshot.get("id"),
                "version": snapshot.get("version"), "sha256": snapshot_hash,
            }:
                raise ValueError("Saved catalog snapshot disagrees with seed reference")
        elif _resolve_ref(store, catalog_ref, mission_id) != snapshot:
            raise ValueError("Saved catalog snapshot disagrees with exact catalog record")
        entry = snapshot
        history_status = "pinned_snapshot"
    elif catalog_ref.get("kind") == "venue_seed":
        # Older seed navigations did not retain the complete entry. Their
        # original seed digest, request, dispatch, context and URL remain exact
        # historical provenance; today's seed file is not a replay authority.
        if (
            set(catalog_ref) != {"kind", "id", "version", "sha256"}
            or type(catalog_ref.get("version")) is not int
            or body.get("catalog_version") != catalog_ref["version"]
            or not isinstance(catalog_ref.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", catalog_ref["sha256"]) is None
        ):
            raise ValueError("Legacy seed navigation has invalid saved identity")
        entry = None
        history_status = "legacy_seed_snapshot_unavailable"
    else:
        entry = _resolve_ref(store, catalog_ref, mission_id)
        history_status = "exact_saved_catalog_record"
    if entry is not None and (
        index >= len(entry["links"])
        or entry["links"][index]["url"] != request["requested_url"]
        or body.get("venue_id") != entry["id"]
        or body.get("catalog_version") != entry["version"]
        or context.get("resource_type") != entry["type"]
        or context.get("link_type") != entry["links"][index]["type"]
        or context.get("link_qualification") != entry["links"][index]["qualification"]
    ):
        raise ValueError("Saved navigation disagrees with its pinned catalog entry")
    blob = body.get("blob")
    if (
        not isinstance(blob, dict)
        or blob.get("sha256") != body.get("content_sha256")
        or blob.get("size") != body.get("byte_length")
        or not isinstance(body.get("content_type"), str)
        or not isinstance(body.get("final_url"), str)
    ):
        raise ValueError("Saved navigation content receipt disagrees with its blob")
    content = store.read_blob(blob["sha256"])
    if hashlib.sha256(content).hexdigest() != blob["sha256"] or len(content) != blob["size"]:
        raise ValueError("Saved navigation content differs from its receipt")
    links = _page_leads(content, body["content_type"], body["final_url"]).links
    if link_offset > len(links):
        raise ValueError("Link offset is past the retained navigation links")
    return _navigation_result(
        store, record, link_offset, link_limit, parsed_links=links,
        catalog_history_status=history_status,
    )


__all__ = [
    "CATALOG_TOOL_SCHEMAS",
    "CATALOG_TOOL_DESCRIPTIONS",
    "list_catalog_leads",
    "propose_catalog_lead",
    "navigate_catalog_lead",
    "page_venue_navigation",
]
