"""Specific catalog destinations keep dated provenance through later revisions."""

import hashlib
from pathlib import Path

import pytest

from ora.agents import AgentRegistry
from ora.retrieval.catalog import revise_venue, seed_catalog, validate_venue
from ora.retrieval.catalog_tools import list_catalog_leads, navigate_catalog_lead
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("kind, link_type", [
    ("journal", "journal_issue"),
    ("conference", "conference_abstract"),
    ("repository", "repository_collection"),
    ("repository", "repository_dataset"),
    ("registry", "registry_study"),
])
def test_specific_destination_survives_navigation_and_catalog_revision(tmp_path, kind, link_type):
    store = StateStore(tmp_path)
    actor = AgentRegistry(store, ROOT).register(
        "destination-actor", mission_id="test-mission", run_id="test-run",
        role_id="evidence_preparation_synthesis", display_name="Test Researcher",
    )
    scope = dict(mission_id="test-mission", run_id="test-run", actor_id=actor["id"])
    seed = next(entry for entry in seed_catalog()["entries"] if entry["type"] == kind)
    link = {
        "type": link_type, "url": "https://example.org/resource/2026/item",
        "resource_year": 2026, "last_checked": "2026-10-11", "qualification": "discovered",
    }
    revised = revise_venue(
        store, seed["id"], entry={**seed, "links": [link]}, expected_version=1,
        reason="Add a specific destination without claiming content verification",
    )
    listed = next(item for item in list_catalog_leads(store, **scope)["entries"]
                  if item["id"] == seed["id"])
    assert listed["links"] == [link]
    page = b"<html><title>Test resource</title><a href='/study'>Study</a></html>"
    calls = []

    def fetch(url, *, max_bytes):
        calls.append(url)
        return {
            "status": 200, "content": page, "content_sha256": hashlib.sha256(page).hexdigest(),
            "byte_length": len(page), "content_type": "text/html", "requested_url": url,
            "final_url": url, "redirect_chain": [], "retrieved_at": "2026-10-11T12:00:00Z",
        }

    args = dict(**scope, operation_id="navigate-destination", catalog_ref=revised["entry_ref"])
    result = navigate_catalog_lead(store, **args, fetch=fetch)
    assert result["navigation_context"]["resource_type"] == kind
    assert result["navigation_context"]["link_type"] == link_type
    assert result["navigation_context"]["resource_year"] == 2026
    assert result["navigation_context"]["catalog_version"] == 2
    assert result["navigation_context"]["link_qualification"] == "discovered"
    assert result["evidence_status"] == "navigation_lead_only"
    revise_venue(
        store, seed["id"], entry={**revised["entry"], "links": [{
            **link, "url": "https://example.org/resource/2027/item", "resource_year": 2027,
        }]}, expected_version=2, reason="Add next dated destination",
    )
    replay = navigate_catalog_lead(store, **args, fetch=fetch)
    assert replay == result
    assert calls == [link["url"]]
    assert store.get(**revised["entry_ref"])["body"]["entry"]["links"] == [link]
    incompatible = "registry" if kind != "registry" else "journal"
    with pytest.raises(ValueError, match="does not match resource type"):
        validate_venue({**revised["entry"], "type": incompatible})
