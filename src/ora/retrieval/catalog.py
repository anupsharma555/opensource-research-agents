"""Versioned venue navigation leads; membership never controls research eligibility."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from importlib.resources import files
from urllib.parse import urlsplit

from ora.state import StateStore

from .normalize import canonical_bytes

KINDS = frozenset({"journal", "conference", "repository", "registry", "index", "search_provider"})
QUALIFICATIONS = frozenset(
    {
        "discovered",
        "metadata_verified",
        "navigation_verified_by_plan",
        "navigation_verified",
        "adapter_tested",
        "unavailable",
        "terms_pending",
    }
)
OUTCOMES = frozenset({"navigable", "unavailable", "blocked", "terms_pending", "redirected"})
LINK_TYPES = {
    "journal": {
        "journal_navigation", "journal_volume", "journal_issue", "journal_archive",
        "journal_new_online",
    },
    "conference": {
        "conference_program",
        "conference_program_navigation",
        "conference_meeting_navigation",
        "conference_abstract",
    },
    "repository": {
        "repository_search", "repository_results", "repository_hub",
        "repository_collection", "repository_dataset",
    },
    "registry": {"registry_search", "registry_study"},
    "index": {"index_search"},
    "search_provider": {"provider_api"},
}


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _id(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()[:32]


def _all_records(store: StateStore, kind: str) -> list[dict]:
    output: list[dict] = []
    after = ""
    while True:
        page = store.records(kind, after_id=after)
        output.extend(page)
        if len(page) < 1000:
            return output
        after = page[-1]["id"]


def _url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Navigation URL must be text")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Navigation URL must be an absolute public HTTPS URL")
    return value


def seed_catalog() -> dict:
    """The 25 planning seeds, with source-attributed 2026-10-07 navigation status."""
    data = json.loads(files("ora.retrieval").joinpath("venue_seeds.json").read_text())
    if data.get("schema") != "ora.venue_catalog.v1" or len(data.get("entries", [])) != 25:
        raise ValueError("The reviewed venue seed export is incomplete")
    for item in data["entries"]:
        validate_venue(item)
    return data


def validate_venue(item: dict) -> None:
    if not isinstance(item, dict) or not re.fullmatch(r"venue-[a-z0-9-]+", item.get("id", "")):
        raise ValueError("Venue needs a stable ID")
    if (
        item.get("type") not in KINDS
        or not isinstance(item.get("name"), str)
        or not item["name"].strip()
    ):
        raise ValueError("Venue needs a name and type")
    if type(item.get("version")) is not int or item["version"] < 1:
        raise ValueError("Venue needs a positive version")
    if not isinstance(item.get("access_note"), str) or not item["access_note"].strip():
        raise ValueError("Venue needs an access note")
    for scope in ("topic_scope", "geographic_scope"):
        if item.get(scope) is not None and not isinstance(item[scope], str):
            raise ValueError("Venue scope must be text or unknown")
    links = item.get("links")
    if not isinstance(links, list) or not links:
        raise ValueError("Venue needs at least one dated navigation link")
    for link in links:
        _url(link.get("url"))
        if link.get("qualification") not in QUALIFICATIONS or not isinstance(
            link.get("last_checked"), str
        ):
            raise ValueError("Venue link needs qualification and last checked date")
        try:
            date.fromisoformat(link["last_checked"])
        except ValueError as exc:
            raise ValueError("Venue link needs a real checked date") from exc
        if link.get("type") not in LINK_TYPES[item["type"]] | {"unverified_navigation"}:
            raise ValueError("Venue link type does not match resource type")
        year = link.get("resource_year")
        if year is not None and (type(year) is not int or not 1800 <= year <= 2500):
            raise ValueError("Dated resource year is invalid")


def resolve_venue(
    store: StateStore, venue_id: str, *, mission_id: str | None = None
) -> dict | None:
    saved = store.get("venue_entry", venue_id)
    if saved:
        return saved["body"]["entry"]
    seed = next((x for x in seed_catalog()["entries"] if x["id"] == venue_id), None)
    if seed or mission_id is None:
        return seed
    return next(
        (
            record["body"]["entry"]
            for record in _all_records(store, "venue_proposal")
            if record["body"].get("mission_id") == mission_id
            and record["body"]["entry"]["id"] == venue_id
        ),
        None,
    )


def list_venues(
    store: StateStore,
    *,
    text: str = "",
    kinds: set[str] | None = None,
    include_proposals: bool = True,
    mission_id: str | None = None,
) -> list[dict]:
    """List navigation leads; an absent entry is never an exclusion decision."""
    seeds = seed_catalog()["entries"]
    saved = {record["id"]: record["body"]["entry"] for record in _all_records(store, "venue_entry")}
    entries = [saved.pop(seed["id"], seed) for seed in seeds] + list(saved.values())
    if include_proposals:
        entries += [
            record["body"]["entry"]
            for record in _all_records(store, "venue_proposal")
            if mission_id is None or record["body"].get("mission_id") == mission_id
        ]
    term = text.casefold().strip()
    return [
        entry
        for entry in entries
        if (kinds is None or entry["type"] in kinds)
        and (
            not term
            or term in (entry["name"] + " " + str(entry.get("topic_scope") or "")).casefold()
        )
    ]


def catalog_export(store: StateStore, *, mission_id: str | None = None) -> dict:
    """Export every lead with dated navigation history, preserving earlier checks."""
    history: dict[str, list[dict]] = {}
    for record in _all_records(store, "venue_navigation"):
        item = record["body"]
        if mission_id is not None and item.get("mission_id") not in {None, mission_id}:
            continue
        history.setdefault(item["venue_id"], []).append({"navigation_ref": _ref(record), **item})
    return {
        "schema": "ora.venue_catalog_export.v1",
        "seed_count": 25,
        "entries": [
            {
                **entry,
                "navigation_history": sorted(
                    history.get(entry["id"], []), key=lambda item: item["checked_at"]
                ),
            }
            for entry in list_venues(store, mission_id=mission_id)
        ],
    }


def propose_venue(
    store: StateStore,
    *,
    name: str,
    kind: str,
    url: str,
    mission_id: str,
    rationale: str,
    proposed_at: str,
    actor_id: str | None = None,
    run_id: str | None = None,
    operation_id: str | None = None,
) -> dict:
    """Record an unlisted lead that can be used immediately without admission."""
    if kind not in KINDS or not name.strip() or not rationale.strip():
        raise ValueError("Proposal needs name, type and scientific relevance rationale")
    _url(url)
    item = {
        "id": "venue-" + _id({"name": name, "url": url}),
        "version": 1,
        "name": name,
        "type": kind,
        "topic_scope": None,
        "geographic_scope": None,
        "links": [
            {
                "url": url,
                "type": "unverified_navigation",
                "last_checked": proposed_at[:10],
                "qualification": "discovered",
                "source": "agent_proposal",
            }
        ],
        "access_note": "Rights and navigation still need qualification.",
    }
    validate_venue(item)
    record_id = "venue-proposal-" + _id({"mission": mission_id, "entry": item["id"]})
    actor = store.get("agent_instance", actor_id, 1) if actor_id else None
    if actor_id and (
        actor is None
        or actor["body"].get("mission_id") != mission_id
        or actor["body"].get("run_id") != run_id
    ):
        raise ValueError("Venue proposer must be an agent in this run")
    body = {
        "schema": "ora.venue_proposal.v1",
        "mission_id": mission_id,
        "rationale": rationale,
        "proposed_at": proposed_at,
        "entry": item,
    }
    if actor:
        body.update(actor_id=actor_id, run_id=run_id, actor_ref=_ref(actor))

    def action(tx):
        if actor_id:
            current_actor = tx.get("agent_instance", actor_id, 1)
            if current_actor is None or _ref(current_actor) != body["actor_ref"]:
                raise ValueError("Venue proposer must be an agent in this run")
        return {"proposal_ref": _ref(tx.put("venue_proposal", record_id, body)), "entry": item}

    return store.transact(
        operation_id or "venue-propose-" + record_id, body, action, mission_id=mission_id
    )


def revise_venue(
    store: StateStore, venue_id: str, *, entry: dict, expected_version: int, reason: str
) -> dict:
    """Add a catalog version; earlier discovery encounters retain their old refs."""
    original = resolve_venue(store, venue_id)
    if original is None or expected_version != original["version"] or not reason.strip():
        raise ValueError("Revision needs the current venue version and reason")
    revised = {**entry, "id": venue_id, "version": expected_version + 1}
    validate_venue(revised)
    body = {
        "schema": "ora.venue_entry.v1",
        "entry": revised,
        "previous_entry_sha256": _id(original),
        "reason": reason,
    }

    def action(tx):
        previous = tx.get("venue_entry", venue_id)
        saved = tx.put(
            "venue_entry", venue_id, body, expected_version=previous["version"] if previous else 0
        )
        return {"entry_ref": _ref(saved), "entry": revised}

    return store.transact(
        "venue-revise-" + _id({"id": venue_id, "version": revised["version"]}), body, action
    )


def record_navigation(
    store: StateStore,
    *,
    venue_id: str,
    url: str,
    outcome: str,
    checked_at: str,
    status_code: int | None = None,
    alternate_url: str | None = None,
    mission_id: str | None = None,
) -> dict:
    """Retain each navigation outcome, including dead or restricted routes."""
    entry = resolve_venue(store, venue_id, mission_id=mission_id)
    if entry is None or outcome not in OUTCOMES:
        raise ValueError("Unknown venue or navigation outcome")
    _url(url)
    if not isinstance(checked_at, str):
        raise ValueError("Navigation check needs a dated time")
    try:
        checked_time = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Navigation check needs a real dated time") from exc
    if checked_time.tzinfo is None or checked_time.utcoffset() is None:
        raise ValueError("Navigation check needs a timezone")
    if status_code is not None and (type(status_code) is not int or not 100 <= status_code <= 599):
        raise ValueError("Navigation HTTP status must be an actual status code")
    if alternate_url is not None:
        _url(alternate_url)
    body = {
        "schema": "ora.venue_navigation.v1",
        "venue_id": venue_id,
        "catalog_version": entry["version"],
        "url": url,
        "outcome": outcome,
        "checked_at": checked_at,
        "status_code": status_code,
        "alternate_url": alternate_url,
    }
    if mission_id is not None:
        body["mission_id"] = mission_id
    record_id = "venue-navigation-" + _id(body)

    def action(tx):
        return {"navigation_ref": _ref(tx.put("venue_navigation", record_id, body))}

    return store.transact("venue-navigation-op-" + record_id, body, action)
