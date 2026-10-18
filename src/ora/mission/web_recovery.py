"""Durable, task-scoped negative receipts for public web-source fetches.

A known HTTP response is useful operational evidence, but does not establish
scientific absence. The caller checks this receipt before repeating an exact
work item and may create a fresh work item for a deliberate later attempt.
"""

from __future__ import annotations

import hashlib

from ora.coordination.board import ref
from ora.evidence.intake import PublicSourceHTTPError
from ora.state import StateConflict, StateError
from ora.state.store import canonical

KIND = "public_source_negative_fetch"


def _id(mission_id: str, work_id: str) -> str:
    return "public-negative-fetch-" + hashlib.sha256(canonical([mission_id, work_id]).encode()).hexdigest()


def _lineage(*, work_id: str, mission_id: str, run_id: str, actor_id: str,
             task_id: str, hit_ref: dict, response_ref: dict | None) -> dict:
    return {
        "work_id": work_id, "mission_id": mission_id, "run_id": run_id,
        "actor_id": actor_id, "task_id": task_id, "hit_ref": hit_ref,
        "response_ref": response_ref,
    }


def _reply(record: dict) -> tuple[dict, tuple[dict, ...]]:
    body = record["body"]
    status = body["http_status"]
    classification = (
        "url_not_found" if status == 404 else
        "access_blocked" if status in {401, 403} else
        "temporary_http_failure" if status in {429, 503} else
        "http_failure"
    )
    return {
        "status": "source_unavailable",
        "classification": classification,
        "hit_id": body["hit_ref"]["id"],
        "http_status": status,
        "checked_at": body["checked_at"],
        "negative_fetch_ref": ref(record),
        "evidence_scope": "URL retrieval outcome only; no source text was inspected",
        "next_action": (
            "Choose another saved hit, verify an exact identifier or corrected URL, "
            "or report the remaining evidence gap. A deliberate fresh fetch needs "
            "new work; this failed URL is not evidence of scientific absence."
        ),
    }, ()


def saved_negative_fetch(
    store, *, work_id: str, mission_id: str, run_id: str, actor_id: str,
    task_id: str, hit_ref: dict, response_ref: dict | None,
) -> tuple[dict, tuple[dict, ...]] | None:
    """Return one exact work item's known negative result without HTTP."""
    saved = store.get(KIND, _id(mission_id, work_id))
    if saved is None:
        return None
    expected = _lineage(work_id=work_id, mission_id=mission_id, run_id=run_id,
                        actor_id=actor_id, task_id=task_id, hit_ref=hit_ref,
                        response_ref=response_ref)
    if any(saved["body"].get(key) != value for key, value in expected.items()):
        raise StateConflict("Negative fetch belongs to different exact work")
    return _reply(saved)


def record_negative_fetch(
    store, *, work_id: str, mission_id: str, run_id: str, actor_id: str,
    task_id: str, hit_ref: dict, response_ref: dict | None,
    error: PublicSourceHTTPError,
) -> tuple[dict, tuple[dict, ...]]:
    """Save a known HTTP failure; never cache it across different work IDs."""
    if not isinstance(error, PublicSourceHTTPError):
        raise StateError("Negative fetch requires a known HTTP response")
    hit = store.get(**hit_ref)
    if (hit is None or hit["kind"] != "retrieval_hit"
            or hit["body"].get("mission_id") != mission_id
            or hit["body"].get("task_id") != task_id
            or hit["body"].get("agent_instance_id") != actor_id
            or hit["body"]["source"].get("original_url") != error.requested_url):
        raise StateError("Negative fetch differs from the exact saved hit or task")
    lineage = _lineage(work_id=work_id, mission_id=mission_id, run_id=run_id,
                       actor_id=actor_id, task_id=task_id, hit_ref=hit_ref,
                       response_ref=response_ref)
    body = {
        "schema": "ora.public_source_negative_fetch.v1", **lineage,
        "http_status": error.status_code,
        "requested_url_sha256": hashlib.sha256(error.requested_url.encode()).hexdigest(),
        "final_url_sha256": (hashlib.sha256(error.final_url.encode()).hexdigest()
                             if isinstance(error.final_url, str) else None),
        "redirect_count": len(error.redirect_chain),
        "checked_at": error.retrieved_at,
        "evidence_scope": "url_retrieval_only",
    }
    record_id = _id(mission_id, work_id)
    def save(tx):
        prior = tx.get(KIND, record_id)
        if prior is not None:
            if prior["body"] != body:
                raise StateConflict("Negative fetch work has a different saved response")
            return ref(prior)
        saved = tx.put(KIND, record_id, body)
        tx.emit(mission_id, "public_source_fetch_unavailable", {"negative_fetch_ref": ref(saved)})
        return ref(saved)
    pointer = store.transact("negative-fetch:" + record_id, body, save, mission_id=mission_id)
    return _reply(store.get(**pointer))
