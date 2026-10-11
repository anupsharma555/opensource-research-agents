"""Saved venue links remain pageable across calls without another fetch."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ora.agents import AgentRegistry
from ora.retrieval.catalog import revise_venue, seed_catalog
from ora.retrieval.catalog_tools import navigate_catalog_lead, page_venue_navigation
from ora.retrieval.normalize import canonical_bytes
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[2]


def actor(store: StateStore, name: str, run_id: str = "run-one") -> dict:
    return AgentRegistry(store, ROOT).register(
        name, mission_id="mission-one", run_id=run_id,
        role_id="evidence_preparation_synthesis", display_name=name,
    )


def test_saved_navigation_pages_later_links_for_same_run_peer_after_catalog_revision(tmp_path):
    store = StateStore(tmp_path)
    creator, reader = actor(store, "creator"), actor(store, "reader")
    seed = next(row for row in seed_catalog()["entries"] if row["id"] == "venue-jmir-mental-health")
    issue_url = "https://mental.jmir.org/2026/1"
    v2 = revise_venue(
        store, seed["id"], entry={**seed, "links": [*seed["links"], {
            "url": issue_url, "type": "journal_issue", "resource_year": 2026,
            "qualification": "discovered", "last_checked": "2026-10-11",
            "verified_route": "navigation_only", "source": "synthetic-test",
        }]}, expected_version=1, reason="Synthetic dated issue lead",
    )
    html = ("<html><title>Issue</title>" + "".join(
        f"<a href='/article/{index}'>Article {index}</a>" for index in range(60)
    ) + "</html>").encode()
    fetches = []

    def fetch(url, *, max_bytes):
        fetches.append(url)
        return {
            "status": 200, "content": html, "content_sha256": hashlib.sha256(html).hexdigest(),
            "byte_length": len(html), "content_type": "text/html",
            "requested_url": url, "final_url": url, "redirect_chain": [],
            "access_basis": "public_https_unauthenticated", "authenticated": False,
            "retrieved_at": "2026-10-11T00:00:00Z",
        }

    first = navigate_catalog_lead(
        store, mission_id="mission-one", run_id="run-one", actor_id=creator["id"],
        operation_id="navigate-issue", catalog_ref=v2["entry_ref"], link_index=1,
        link_limit=25, fetch=fetch,
    )
    assert [link["text"] for link in first["links"]] == [f"Article {i}" for i in range(25)]
    assert first["next_request"] == {
        "tool": "page_venue_navigation", "navigation_ref": first["navigation_ref"],
        "link_offset": 25, "link_limit": 25,
    }
    revise_venue(
        store, seed["id"], entry={**seed, "links": seed["links"]},
        expected_version=2, reason="Synthetic later catalog change",
    )
    args = dict(mission_id="mission-one", run_id="run-one", actor_id=reader["id"],
                navigation_ref=first["navigation_ref"], link_offset=25, link_limit=25)
    page = page_venue_navigation(store, **args)
    reopened = page_venue_navigation(StateStore(tmp_path), **args)
    assert page == reopened
    assert [link["text"] for link in page["links"]] == [f"Article {i}" for i in range(25, 50)]
    assert page["next_request"]["link_offset"] == 50
    assert fetches == [issue_url]
    assert len(store.records("venue_navigation")) == 1
    assert store.records("source_copy") == []
    assert store.records("publication") == []


def test_saved_navigation_rejects_wrong_scope_reference_hash_and_offset(tmp_path):
    store = StateStore(tmp_path)
    creator, outsider = actor(store, "creator"), actor(store, "outsider", "run-two")
    seed = next(row for row in seed_catalog()["entries"] if row["id"] == "venue-jmir-mental-health")
    html = b"<html><title>Issue</title><a href='/one'>One</a></html>"
    url = seed["links"][0]["url"]

    def fetch(_url, *, max_bytes):
        return {
            "status": 200, "content": html, "content_sha256": hashlib.sha256(html).hexdigest(),
            "byte_length": len(html), "content_type": "text/html",
            "requested_url": url, "final_url": url, "redirect_chain": [],
            "access_basis": "public_https_unauthenticated", "authenticated": False,
            "retrieved_at": "2026-10-11T00:00:00Z",
        }

    first = navigate_catalog_lead(
        store, mission_id="mission-one", run_id="run-one", actor_id=creator["id"],
        operation_id="navigate", catalog_ref={
            "kind": "venue_seed", "id": seed["id"], "version": seed["version"],
            "sha256": hashlib.sha256(canonical_bytes(seed)).hexdigest(),
        }, fetch=fetch,
    )
    base = dict(mission_id="mission-one", run_id="run-one", actor_id=creator["id"],
                navigation_ref=first["navigation_ref"], link_offset=0, link_limit=1)
    with pytest.raises(ValueError, match="selected mission run"):
        page_venue_navigation(store, **{**base, "actor_id": outsider["id"]})
    with pytest.raises(ValueError, match="exact venue-navigation reference"):
        page_venue_navigation(store, **{**base, "navigation_ref": {"kind": "venue_navigation", "id": "bad", "version": 0}})
    with pytest.raises(ValueError, match="unavailable"):
        page_venue_navigation(store, **{**base, "navigation_ref": {"kind": "venue_navigation", "id": "absent", "version": 1}})
    exhausted = page_venue_navigation(store, **{**base, "link_offset": 1})
    assert exhausted["links"] == [] and exhausted["next_request"] is None
    with pytest.raises(ValueError, match="past the retained"):
        page_venue_navigation(store, **{**base, "link_offset": 2})
    saved = store.get(**first["navigation_ref"])
    forged = {**saved["body"], "content_sha256": "0" * 64}
    bad = store.transact("forge-receipt", forged, lambda tx: tx.put(
        "venue_navigation", saved["id"], forged, expected_version=saved["version"],
    ))
    with pytest.raises(ValueError, match="receipt disagrees"):
        page_venue_navigation(store, **{**base, "navigation_ref": {
            "kind": bad["kind"], "id": bad["id"], "version": bad["version"],
        }})


def test_pinned_and_legacy_seed_pages_survive_seed_file_change(tmp_path, monkeypatch):
    import ora.retrieval.catalog_tools as catalog_tools

    store = StateStore(tmp_path)
    peer = actor(store, "creator")
    seed = next(row for row in seed_catalog()["entries"] if row["id"] == "venue-jmir-mental-health")
    url = seed["links"][0]["url"]
    html = b"<html><a href='/old-one'>Original link</a></html>"

    def fetch(_url, *, max_bytes):
        return {
            "status": 200, "content": html, "content_sha256": hashlib.sha256(html).hexdigest(),
            "byte_length": len(html), "content_type": "text/html",
            "requested_url": url, "final_url": url, "redirect_chain": [],
            "access_basis": "public_https_unauthenticated", "authenticated": False,
            "retrieved_at": "2026-10-11T00:00:00Z",
        }

    first = navigate_catalog_lead(
        store, mission_id="mission-one", run_id="run-one", actor_id=peer["id"],
        operation_id="seed-nav", catalog_ref={
            "kind": "venue_seed", "id": seed["id"], "version": seed["version"],
            "sha256": hashlib.sha256(canonical_bytes(seed)).hexdigest(),
        }, fetch=fetch,
    )
    original = store.get(**first["navigation_ref"])
    old_dispatch = store.get(**original["body"]["dispatch_ref"])
    # Model a pre-snapshot completed operation without changing its request,
    # page bytes, original seed digest or exact dispatch/request relationship.
    legacy_dispatch_body = {key: value for key, value in old_dispatch["body"].items()
                            if key not in {"catalog_entry_snapshot", "catalog_entry_sha256"}}
    legacy_nav_body = {key: value for key, value in original["body"].items()
                       if key not in {"catalog_entry_snapshot", "catalog_entry_sha256"}}

    def save_legacy(tx):
        marker = tx.put("venue_navigation_dispatch", "venue-navigation-dispatch-legacy", legacy_dispatch_body)
        navigation = tx.put("venue_navigation", "venue-navigation-legacy", {
            **legacy_nav_body, "dispatch_ref": {
                "kind": marker["kind"], "id": marker["id"], "version": marker["version"],
            },
        })
        return {"kind": navigation["kind"], "id": navigation["id"],
                "version": navigation["version"]}

    legacy_ref = store.transact("save-legacy", legacy_nav_body, save_legacy)
    changed_seed = {**seed, "links": [{**seed["links"][0],
                                       "url": "https://mental.jmir.org/changed"}]}
    monkeypatch.setattr(catalog_tools, "seed_catalog", lambda: {"entries": [changed_seed]})
    base = dict(mission_id="mission-one", run_id="run-one", actor_id=peer["id"],
                link_offset=0, link_limit=5)
    pinned = page_venue_navigation(store, **base, navigation_ref=first["navigation_ref"])
    legacy = page_venue_navigation(store, **base, navigation_ref=legacy_ref)
    assert pinned["links"] == legacy["links"] == [{
        "url": "https://mental.jmir.org/old-one", "text": "Original link",
    }]
    assert pinned["catalog_history_status"] == "pinned_snapshot"
    assert legacy["catalog_history_status"] == "legacy_seed_snapshot_unavailable"
    assert store.records("source_copy") == []


def test_empty_navigation_can_be_paged_at_zero_without_refetch(tmp_path):
    store = StateStore(tmp_path)
    peer = actor(store, "creator")
    seed = next(row for row in seed_catalog()["entries"] if row["id"] == "venue-jmir-mental-health")
    url = seed["links"][0]["url"]
    html = b"<html><title>No article links</title></html>"
    fetches = []

    def fetch(_url, *, max_bytes):
        fetches.append(_url)
        return {
            "status": 200, "content": html, "content_sha256": hashlib.sha256(html).hexdigest(),
            "byte_length": len(html), "content_type": "text/html",
            "requested_url": url, "final_url": url, "redirect_chain": [],
            "access_basis": "public_https_unauthenticated", "authenticated": False,
            "retrieved_at": "2026-10-11T00:00:00Z",
        }

    first = navigate_catalog_lead(
        store, mission_id="mission-one", run_id="run-one", actor_id=peer["id"],
        operation_id="empty-nav", catalog_ref={
            "kind": "venue_seed", "id": seed["id"], "version": seed["version"],
            "sha256": hashlib.sha256(canonical_bytes(seed)).hexdigest(),
        }, fetch=fetch,
    )
    page = page_venue_navigation(
        store, mission_id="mission-one", run_id="run-one", actor_id=peer["id"],
        navigation_ref=first["navigation_ref"], link_offset=0, link_limit=25,
    )
    assert page["links"] == [] and page["next_request"] is None
    assert fetches == [url]
