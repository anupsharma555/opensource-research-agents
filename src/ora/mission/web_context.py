"""Recheck search leads and prepare bounded, pageable model views.

These projections do not fetch a source or add results absent from a saved
reply. Complete permitted detail remains available through saved-result pages.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy

from ora.evidence.hit_use import hit_allows_reinspection
from ora.state import StateError
from ora.state.store import canonical

MAX_WEB_RESULT_CHARS = 3600
MAX_WEB_EXCERPT_CHARS = 60
MAX_WEB_TITLE_CHARS = 70


def require_search_operation(store, value: dict, scope: dict) -> dict:
    """Reject stale or cross-task operation handles before replaying them."""
    pointer = value.get("operation_ref")
    operation = store.get(**pointer) if (
        isinstance(pointer, dict) and set(pointer) == {"kind", "id", "version"}
        and pointer.get("kind") == "web_search_operation"
        and isinstance(pointer.get("id"), str)
        and type(pointer.get("version")) is int
    ) else None
    if operation is None:
        raise StateError("Saved web search operation reference is unavailable")
    if any(operation["body"].get(key) != scope[expected] for key, expected in (
        ("mission_id", "mission_id"), ("actor_id", "actor_id"), ("task_id", "task_id"),
    )):
        raise StateError("Saved web search operation belongs to another peer task")
    return operation


def compact_search_result(value: dict, *, result_ref: dict,
                          max_chars: int = MAX_WEB_RESULT_CHARS) -> dict:
    """Give the model navigable discovery leads, keeping the saved reply intact.

    A teaser remains an uninspected candidate. The cursor refers to the saved
    operation, so omitted candidates can be read without another provider call.
    """
    projected = {key: deepcopy(value[key]) for key in (
        "schema_version", "operation_ref", "status", "continuation_cursor",
        "has_more", "pending", "failed", "wait_requested", "reuse_status",
    ) if key in value}
    projected["available_results"] = []
    projected["saved_tool_result_ref"] = result_ref
    projected["permitted_projection_sha256"] = hashlib.sha256(canonical(value).encode()).hexdigest()
    projected["result_status"] = "uninspected_discovery_leads_not_source_evidence"
    projected["detail_request"] = {
        "tool": "read_tool_result", "result_id": result_ref["id"],
        "offset": 0, "limit": 1000, "projection_sha256": "",
    }
    total = len(value.get("available_results", []))

    def with_navigation(entries: list[dict]) -> dict:
        view = {**projected, "available_results": entries}
        omitted = total - len(entries)
        if omitted:
            view["omitted_candidate_count"] = omitted
        return view

    for delivery in value.get("available_results", []):
        result = delivery.get("result", {})
        entry = {"provider": delivery.get("provider")}
        if delivery.get("fallback_of"):
            entry["fallback_of"] = delivery["fallback_of"]
        entry["result"] = {key: deepcopy(result[key]) for key in (
            "outcome", "reason", "recovery", "permission_status",
        ) if key in result}
        entry["result"]["hits"] = []
        for hit in result.get("hits", []):
            lead = {key: deepcopy(hit[key]) for key in (
                "hit_ref", "hit_id", "original_url", "route", "rights_status",
            ) if key in hit}
            for key, limit in (("title", MAX_WEB_TITLE_CHARS),
                               ("candidate_excerpt", MAX_WEB_EXCERPT_CHARS)):
                if isinstance(hit.get(key), str):
                    lead[key + "_preview"] = hit[key][:limit]
            lead["excerpt_status"] = hit.get("excerpt_status", "uninspected_candidate")
            entry["result"]["hits"].append(lead)
        proposed = with_navigation([*projected["available_results"], entry])
        if len(canonical(proposed)) > max_chars:
            if not projected["available_results"] and entry["result"]["hits"]:
                # Even an unusual long URL must leave the first candidate's
                # exact fetch handle visible; its full detail is pageable.
                hit = entry["result"]["hits"][0]
                minimal = {key: hit[key] for key in ("hit_id", "hit_ref", "route")
                           if key in hit}
                minimal["status"] = "candidate_detail_in_saved_result"
                entry = {"provider": delivery.get("provider"), "result": {
                    "outcome": result.get("outcome"), "hits": [minimal],
                }}
                if len(canonical(with_navigation([entry]))) <= max_chars:
                    projected["available_results"].append(entry)
            break
        projected["available_results"].append(entry)
    return with_navigation(projected["available_results"])


def archived_search_navigation(value: dict, *, tool_name: str, result_ref: dict,
                               response_ref: dict) -> dict | None:
    """Retain exact operation and candidate handles after a tool turn is archived."""
    if tool_name not in {"search_web", "read_search_results", "cancel_search"}:
        return None
    operation = value.get("operation_ref")
    if not isinstance(operation, dict) or not isinstance(operation.get("id"), str):
        return None
    leads = []
    for delivery in value.get("available_results", []):
        for hit in delivery.get("result", {}).get("hits", []):
            if isinstance(hit.get("hit_id"), str):
                lead = {key: hit[key] for key in ("hit_id", "hit_ref") if key in hit}
                for key, limit in (("title", 90), ("original_url", 400)):
                    if isinstance(hit.get(key), str):
                        lead[key + "_preview"] = hit[key][:limit]
                leads.append(lead)
    return {
        "response_ref": response_ref,
        "operation_id": operation["id"], "status": value.get("status"),
        "continuation_cursor": value.get("continuation_cursor"),
        "has_more": value.get("has_more"), "pending": value.get("pending"),
        "result_ref": result_ref, "candidate_count": len(leads),
        "first_candidates": leads[:5],
        "detail_request": {"tool": "read_tool_result", "result_id": result_ref["id"],
                           "offset": 0, "limit": 1000, "projection_sha256": ""},
        "next_request": {"tool": "read_search_results", "operation_id": operation["id"],
                         "cursor": value.get("continuation_cursor", 0)},
        "result_status": "saved_discovery_leads_not_checked_source_evidence",
    }


def permitted_scholarly_result(store, value: dict, scope: dict) -> dict:
    """Recheck each discovery lead before returning a saved scholarly listing."""
    projected = deepcopy(value)
    kept = []
    for hit in projected.get("hits", []):
        hit_id = hit.get("hit_id") if isinstance(hit, dict) else None
        pointer = hit.get("hit_ref") if isinstance(hit, dict) else None
        record = store.get(**pointer) if (
            isinstance(pointer, dict) and set(pointer) == {"kind", "id", "version"}
            and pointer.get("kind") == "retrieval_hit"
        ) else store.get("retrieval_hit", hit_id) if isinstance(hit_id, str) else None
        if (record is not None
                and record == store.get("retrieval_hit", record["id"])
                and (not isinstance(hit_id, str) or hit_id == record["id"])
                and all(record["body"].get(key) == scope[expected] for key, expected in (
                    ("mission_id", "mission_id"), ("task_id", "task_id"),
                    ("agent_instance_id", "actor_id"),
                ))
                and hit_allows_reinspection(store, record)):
            kept.append(hit)
    if len(kept) != len(projected.get("hits", [])):
        projected["permission_status"] = "some_candidates_withheld_due_current_source_use"
    projected["hits"] = kept
    return projected


def compact_scholarly_result(value: dict, *, result_ref: dict,
                             max_chars: int = MAX_WEB_RESULT_CHARS) -> dict:
    """Show bounded scholarly lead handles and page full permitted detail."""
    view = {key: deepcopy(value[key]) for key in (
        "route", "requested_route", "outcome", "window", "next_request",
        "esearch_ref", "retry_at", "selection", "permission_status",
    ) if key in value}
    view.update(hits=[], saved_tool_result_ref=result_ref,
                result_status="uninspected_discovery_leads_not_source_evidence",
                detail_request={"tool": "read_tool_result", "result_id": result_ref["id"],
                                "offset": 0, "limit": 1000, "projection_sha256": ""})
    for hit in value.get("hits", []):
        lead = {key: deepcopy(hit[key]) for key in (
            "hit_id", "hit_ref", "url", "rights_status", "access_surface",
        ) if key in hit}
        if isinstance(hit.get("title"), str):
            lead["title_preview"] = hit["title"][:70]
        proposed = {**view, "hits": [*view["hits"], lead],
                    "omitted_hit_count": len(value["hits"]) - len(view["hits"]) - 1}
        if len(canonical(proposed)) > max_chars:
            if not view["hits"] and isinstance(hit.get("hit_id"), str):
                view["hits"] = [{"hit_id": hit["hit_id"],
                                 "status": "candidate_detail_in_saved_result"}]
            break
        view["hits"].append(lead)
    if len(view["hits"]) < len(value.get("hits", [])):
        view["omitted_hit_count"] = len(value["hits"]) - len(view["hits"])
    return view


def permitted_search_result(store, value: dict, scope: dict) -> dict:
    projected = deepcopy(value)
    for delivery in projected.get("available_results", []):
        result = delivery.get("result", {})
        kept = []
        for hit in result.get("hits", []):
            pointer = hit.get("hit_ref")
            record = store.get(**pointer) if isinstance(pointer, dict) and set(pointer) == {"kind", "id", "version"} else None
            if (record is None or record["kind"] != "retrieval_hit"
                    or any(record["body"].get(k) != scope[v] for k, v in (
                        ("mission_id", "mission_id"), ("task_id", "task_id"), ("agent_instance_id", "actor_id")))
                    or not hit_allows_reinspection(store, record)):
                continue
            routes = []
            for route in hit.get("discovery_routes", []):
                route_ref = route.get("hit_ref") if isinstance(route, dict) else None
                route_hit = store.get(**route_ref) if (
                    isinstance(route_ref, dict) and set(route_ref) == {"kind", "id", "version"}
                    and route_ref.get("kind") == "retrieval_hit"
                ) else None
                if (route_hit is not None and hit_allows_reinspection(store, route_hit)
                        and all(route_hit["body"].get(k) == scope[v] for k, v in (
                            ("mission_id", "mission_id"), ("task_id", "task_id"), ("agent_instance_id", "actor_id")))):
                    routes.append(route)
            hit["discovery_routes"] = routes
            kept.append(hit)
        if len(kept) != len(result.get("hits", [])):
            result["permission_status"] = "some_candidates_withheld_due_current_source_use"
        result["hits"] = kept
    return projected
