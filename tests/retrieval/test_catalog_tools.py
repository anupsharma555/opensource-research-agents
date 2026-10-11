"""Model-facing catalog helpers retain actor provenance without ranking venues."""

import hashlib
from pathlib import Path

import pytest

from ora.agents import AgentRegistry
from ora.evidence.intake import PublicSourceHTTPError
from ora.retrieval.catalog import propose_venue, record_navigation
from ora.retrieval.catalog_tools import (
    CATALOG_TOOL_SCHEMAS,
    list_catalog_leads,
    navigate_catalog_lead,
    propose_catalog_lead,
)
from ora.state import StateConflict, StateStore

ROOT = Path(__file__).resolve().parents[2]


def actor(store):
    return AgentRegistry(store, ROOT).register(
        "catalog-actor",
        mission_id="synthetic-mission",
        run_id="synthetic-run",
        role_id="evidence_preparation_synthesis",
        display_name="Synthetic Researcher",
    )


def test_agent_can_page_typed_seeds_and_propose_unlisted_venue(tmp_path):
    store = StateStore(tmp_path)
    peer = actor(store)
    context = dict(mission_id="synthetic-mission", run_id="synthetic-run", actor_id=peer["id"])
    page = list_catalog_leads(store, **context, kinds=["journal"], limit=2)
    assert len(page["entries"]) == 2
    assert page["next_offset"] == 2
    assert page["catalog_is_whitelist"] is False
    assert all(entry["type"] == "journal" for entry in page["entries"])
    assert CATALOG_TOOL_SCHEMAS["propose_venue"]["required"] == ["name", "kind", "url", "rationale"]
    proposed = propose_catalog_lead(
        store,
        **context,
        operation_id="propose-synthetic-venue",
        proposed_at="2026-10-09T12:00:00Z",
        name="Synthetic Specialist Venue",
        kind="journal",
        url="https://example.org/specialist",
        rationale="Relevant trial reports",
    )
    assert proposed["status"] == "recorded" and proposed["available_for_use"]
    saved = store.get(**proposed["proposal_ref"])["body"]
    assert saved["actor_ref"]["id"] == peer["id"]
    assert saved["entry"]["links"][0]["qualification"] == "discovered"
    navigated = record_navigation(
        store,
        venue_id=proposed["entry"]["id"],
        url=proposed["entry"]["links"][0]["url"],
        outcome="navigable",
        checked_at="2026-10-09T12:01:00Z",
        status_code=200,
        mission_id="synthetic-mission",
    )
    assert navigated["navigation_ref"]
    found = list_catalog_leads(store, **context, text="Synthetic Specialist")
    assert [item["id"] for item in found["entries"]] == [proposed["entry"]["id"]]
    replay = propose_catalog_lead(
        store,
        **context,
        operation_id="propose-synthetic-venue",
        proposed_at="2026-10-09T12:00:00Z",
        name="Synthetic Specialist Venue",
        kind="journal",
        url="https://example.org/specialist",
        rationale="Relevant trial reports",
    )
    assert replay["proposal_ref"] == proposed["proposal_ref"]


def test_catalog_proposal_rejects_cross_run_actor_and_changed_replay(tmp_path):
    store = StateStore(tmp_path)
    peer = actor(store)
    context = dict(mission_id="synthetic-mission", run_id="synthetic-run", actor_id=peer["id"])
    with pytest.raises(ValueError, match="selected mission run"):
        list_catalog_leads(store, **{**context, "run_id": "other-run"})
    with pytest.raises(ValueError, match="selected mission run"):
        propose_catalog_lead(
            store,
            **{**context, "mission_id": "other-mission"},
            operation_id="wrong-mission",
            proposed_at="2026-10-09T12:00:00Z",
            name="Synthetic",
            kind="journal",
            url="https://example.org/one",
            rationale="Relevant",
        )
    options = dict(
        **context,
        operation_id="stable-proposal",
        proposed_at="2026-10-09T12:00:00Z",
        name="Synthetic",
        kind="journal",
        url="https://example.org/one",
        rationale="Relevant",
    )
    propose_catalog_lead(store, **options)
    with pytest.raises(StateConflict, match="different inputs"):
        propose_catalog_lead(store, **{**options, "rationale": "Changed rationale"})
    propose_venue(
        store,
        name="Other Mission Private Venue",
        kind="journal",
        url="https://example.org/private",
        mission_id="other-mission",
        rationale="Other work",
        proposed_at="2026-10-09T12:00:00Z",
    )
    assert list_catalog_leads(store, **context, text="Other Mission Private Venue")["entries"] == []


def test_unlisted_venue_navigation_saves_actual_page_and_pages_links_without_refetch(tmp_path):
    store = StateStore(tmp_path)
    peer = actor(store)
    context = dict(mission_id="synthetic-mission", run_id="synthetic-run", actor_id=peer["id"])
    proposed = propose_catalog_lead(
        store,
        **context,
        operation_id="propose-unlisted-program",
        proposed_at="2026-10-09T12:00:00Z",
        name="Synthetic Research Meeting",
        kind="conference",
        url="https://example.org/program",
        rationale="Potential program abstracts",
    )
    page = (
        b"<html><head><title>Actual Meeting Program</title></head><body>"
        b"<h1>2026 Program</h1><a href='/abstracts/1'>First abstract</a>"
        b"<a href='https://another.example.org/abstracts/2'>Second abstract</a>"
        b"<a href='http://127.0.0.1/private'>Private link</a></body></html>"
    )
    calls = []

    def fetch(url, *, max_bytes):
        calls.append((url, max_bytes))
        return {
            "status": 200,
            "content": page,
            "content_sha256": hashlib.sha256(page).hexdigest(),
            "byte_length": len(page),
            "content_type": "text/html; charset=utf-8",
            "requested_url": url,
            "final_url": "https://www.example.org/meeting/2026",
            "redirect_chain": [
                {"from_url": url, "to_url": "https://www.example.org/meeting/2026", "status": 302}
            ],
            "access_basis": "public_https_unauthenticated",
            "authenticated": False,
            "retrieved_at": "2026-10-09T12:01:00Z",
        }

    args = dict(**context, operation_id="navigate-unlisted", catalog_ref=proposed["catalog_ref"])
    first = navigate_catalog_lead(store, **args, link_limit=1, fetch=fetch)
    assert first["status"] == "navigable"
    assert first["evidence_status"] == "navigation_lead_only"
    assert first["navigation_context"] == {
        "resource_type": "conference",
        "catalog_version": 1,
        "link_type": "unverified_navigation",
        "resource_year": None,
        "link_qualification": "discovered",
        "verified_route": None,
        "access_note": "Rights and navigation still need qualification.",
        "content_use": "navigation_lead_only",
    }
    assert first["title"] == "Actual Meeting Program"
    assert first["heading"] == "2026 Program"
    assert first["requested_url"] == "https://example.org/program"
    assert first["final_url"] == "https://www.example.org/meeting/2026"
    assert first["redirect_chain"][0]["status"] == 302
    assert first["links"] == [
        {"url": "https://www.example.org/abstracts/1", "text": "First abstract"}
    ]
    assert first["link_count"] == 2 and first["next_link_offset"] == 1
    second = navigate_catalog_lead(store, **args, link_offset=1, link_limit=1, fetch=fetch)
    assert second["links"][0]["url"] == "https://another.example.org/abstracts/2"
    assert second["next_link_offset"] is None
    assert calls == [("https://example.org/program", 2 * 1024 * 1024)]
    saved = store.get(**first["navigation_ref"])["body"]
    assert store.read_blob(saved["blob"]["sha256"]) == page
    assert saved["catalog_version"] == 1
    listed = list_catalog_leads(store, **context, text="Synthetic Research Meeting")
    assert listed["entries"][0]["navigation_history"][0]["status_code"] == 200
    with pytest.raises(StateConflict, match="different inputs"):
        navigate_catalog_lead(store, **{**args, "max_bytes": 1024}, fetch=fetch)


@pytest.mark.parametrize("code, expected", [(403, "blocked"), (404, "unavailable")])
def test_navigation_retains_http_failure_and_redirect_receipt(tmp_path, code, expected):
    store = StateStore(tmp_path)
    peer = actor(store)
    context = dict(mission_id="synthetic-mission", run_id="synthetic-run", actor_id=peer["id"])
    seed = list_catalog_leads(store, **context, limit=1)["entries"][0]
    url = seed["links"][0]["url"]

    def fetch(_url, *, max_bytes):
        raise PublicSourceHTTPError(
            code,
            requested_url=url,
            final_url="https://publisher.example.org/moved",
            redirect_chain=[
                {"from_url": url, "to_url": "https://publisher.example.org/moved", "status": 301}
            ],
        )

    result = navigate_catalog_lead(
        store, **context, operation_id=f"http-{code}", catalog_ref=seed["catalog_ref"], fetch=fetch
    )
    assert result["status"] == expected and result["status_code"] == code
    assert result["final_url"] == "https://publisher.example.org/moved"
    assert result["redirect_chain"][0]["status"] == 301
    assert result["links"] == []
    assert result["navigation_context"]["resource_type"] == seed["type"]
    assert result["navigation_context"]["access_note"] == seed["access_note"]
    assert store.get(**result["navigation_ref"])["body"].get("blob") is None


def test_restricted_conference_navigation_keeps_access_note_separate_from_page_text(tmp_path):
    store = StateStore(tmp_path)
    peer = actor(store)
    context = dict(mission_id="synthetic-mission", run_id="synthetic-run", actor_id=peer["id"])
    seed = next(
        entry for entry in list_catalog_leads(store, **context, kinds=["conference"])["entries"]
        if "generative-AI input" in entry["access_note"]
    )
    page = (b"<html><title>Program navigation</title><body><h1>Conference links</h1>"
            b"<p>Restricted abstract body is not a navigation lead.</p></body></html>")

    def fetch(url, *, max_bytes):
        assert url == seed["links"][0]["url"] and max_bytes >= len(page)
        return {
            "status": 200, "content": page, "content_sha256": hashlib.sha256(page).hexdigest(),
            "byte_length": len(page), "content_type": "text/html",
            "requested_url": url, "final_url": url, "redirect_chain": [],
            "access_basis": "public_https_unauthenticated", "authenticated": False,
            "retrieved_at": "2026-10-09T12:01:00Z",
        }

    result = navigate_catalog_lead(
        store, **context, operation_id="conference-navigation",
        catalog_ref=seed["catalog_ref"], fetch=fetch,
    )
    assert result["status"] == "navigable"
    assert result["navigation_context"]["verified_route"] == "navigation_only"
    assert "generative-AI input" in result["navigation_context"]["access_note"]
    assert result["navigation_context"]["content_use"] == "navigation_lead_only"
    assert "Restricted abstract body" not in str(result)
    assert store.records("source_copy") == []


def test_navigation_rejects_other_mission_proposal_and_records_private_url_failure(tmp_path):
    store = StateStore(tmp_path)
    peer = actor(store)
    context = dict(mission_id="synthetic-mission", run_id="synthetic-run", actor_id=peer["id"])
    foreign = propose_venue(
        store,
        name="Foreign Lead",
        kind="index",
        url="https://example.org/foreign",
        mission_id="other-mission",
        rationale="Different mission",
        proposed_at="2026-10-09T12:00:00Z",
    )
    with pytest.raises(ValueError, match="not available"):
        navigate_catalog_lead(
            store, **context, operation_id="cross-mission", catalog_ref=foreign["proposal_ref"]
        )
    local = propose_catalog_lead(
        store,
        **context,
        operation_id="propose-private-host",
        proposed_at="2026-10-09T12:00:00Z",
        name="Private Host Lead",
        kind="index",
        url="https://127.0.0.1/index",
        rationale="Test unusable lead",
    )
    result = navigate_catalog_lead(
        store, **context, operation_id="private-host", catalog_ref=local["catalog_ref"]
    )
    assert result["status"] == "fetch_error"
    assert "public addresses" in result["diagnostic"]
    assert result["status_code"] is None


def test_interrupted_navigation_does_not_automatically_repeat_external_fetch(tmp_path):
    store = StateStore(tmp_path)
    peer = actor(store)
    context = dict(mission_id="synthetic-mission", run_id="synthetic-run", actor_id=peer["id"])
    seed = list_catalog_leads(store, **context, limit=1)["entries"][0]
    calls = []

    def interrupted(_url, *, max_bytes):
        calls.append(max_bytes)
        raise RuntimeError("simulated process interruption")

    args = dict(**context, operation_id="lost-after-dispatch", catalog_ref=seed["catalog_ref"])
    with pytest.raises(RuntimeError, match="interruption"):
        navigate_catalog_lead(store, **args, fetch=interrupted)
    replay = navigate_catalog_lead(store, **args, fetch=interrupted)
    assert replay["status"] == "uncertain_dispatch"
    assert replay["dispatch_ref"]["kind"] == "venue_navigation_dispatch"
    assert calls == [2 * 1024 * 1024]
