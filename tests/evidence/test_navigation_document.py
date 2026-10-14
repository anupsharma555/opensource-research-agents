"""Retained public HTML is checked before it becomes a readable source copy."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ora.agents import AgentRegistry
from ora.evidence import (
    EvidenceError,
    EvidenceLibrary,
    IntakeService,
    checked_model_text,
    read_source_window,
)
from ora.retrieval.catalog import revise_venue, seed_catalog
from ora.retrieval.catalog_tools import navigate_catalog_lead
from ora.retrieval.normalize import canonical_bytes
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[2]


def saved_document(
    tmp_path, *, authenticated=False, html=None,
    access_basis="public_https_unauthenticated", access_note=None,
):
    store = StateStore(tmp_path)
    actor = AgentRegistry(store, ROOT).register(
        "reader", mission_id="mission-one", run_id="run-one",
        role_id="evidence_preparation_synthesis", display_name="Reader",
    )
    seed = next(row for row in seed_catalog()["entries"] if row["id"] == "venue-jmir-mental-health")
    catalog_ref = {
        "kind": "venue_seed", "id": seed["id"], "version": seed["version"],
        "sha256": hashlib.sha256(canonical_bytes(seed)).hexdigest(),
    }
    if access_note is not None:
        catalog_ref = revise_venue(
            store, seed["id"], entry={**seed, "access_note": access_note},
            expected_version=1, reason="Synthetic access note for permission test",
        )["entry_ref"]
    url = seed["links"][0]["url"]
    content = html or (
        b"<html><title>Conference program</title><main><h1>Conference program</h1>"
        b"<p>Poster 17: synthetic methods and findings.</p></main></html>"
    )
    fetched = []

    def fetch(requested, *, max_bytes):
        fetched.append(requested)
        assert requested == url and len(content) <= max_bytes
        receipt = {
            "status": 200, "content": content,
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "byte_length": len(content), "content_type": "text/html",
            "requested_url": url, "final_url": url, "redirect_chain": [],
            "access_basis": access_basis, "retrieved_at": "2026-10-11T00:00:00Z",
        }
        if authenticated is not None:
            receipt["authenticated"] = authenticated
        return receipt

    nav = navigate_catalog_lead(
        store, mission_id="mission-one", run_id="run-one", actor_id=actor["id"],
        operation_id="navigate-program", catalog_ref=catalog_ref, fetch=fetch,
    )
    return store, actor, nav["navigation_ref"], fetched, content


def inspect(store, actor, navigation_ref, *, operation_id="inspect-program"):
    return IntakeService(EvidenceLibrary(store)).inspect_navigation_document(
        operation_id, navigation_ref, mission_id="mission-one",
        run_id="run-one", actor_id=actor["id"],
    )


def test_exact_saved_html_read_window_and_revocation_without_refetch(tmp_path):
    store, actor, navigation_ref, fetches, content = saved_document(tmp_path)
    saved = inspect(store, actor, navigation_ref)
    assert saved["classification"] == "navigation_document"
    assert saved["copy_ref"] and saved["source_use_ref"]
    assert store.get(**saved["copy_ref"])["body"]["publication"] is None
    assert store.get(**saved["copy_ref"])["body"]["navigation_ref"] == navigation_ref
    quality = store.get(**saved["quality_ref"])["body"]
    assert quality["source_sha256"] == hashlib.sha256(content).hexdigest()
    assert quality["navigation_ref"] == navigation_ref
    assert quality["catalog_version"] == 1
    assert quality["source_url"] == "https://mental.jmir.org/2026"
    assert store.records("publication") == []
    library = EvidenceLibrary(store)
    window = read_source_window(library, "program-window", saved["copy_ref"], block_offset=1)
    assert window["passages"][0]["text_preview"] == "Poster 17: synthetic methods and findings."
    assert "section=Conference program" in window["passages"][0]["locator"]
    packet = checked_model_text(library, saved["copy_ref"], window["passage_refs"])
    assert "Poster 17: synthetic methods and findings." in packet["text"]
    assert library.get_passage(window["passage_refs"][0]["id"])["publication"] is None
    assert fetches == ["https://mental.jmir.org/2026"]
    repeated = IntakeService(library).inspect_navigation_document(
        "inspect-program-again", navigation_ref, mission_id="mission-one",
        run_id="run-one", actor_id=actor["id"],
    )
    assert repeated["copy_ref"] == saved["copy_ref"]
    assert len(store.records("source_copy")) == 1

    def revoke(tx):
        use = tx.get(**saved["source_use_ref"])
        tx.put("source_use", use["id"], {
            **use["body"], "allowed_uses": ["discovery"], "policy_status": "revoked",
        }, expected_version=use["version"])
        return {"revoked": True}

    store.transact("revoke-program", {}, revoke)
    with pytest.raises(EvidenceError, match="does not grant model input"):
        read_source_window(library, "program-window-after-revoke", saved["copy_ref"])
    with pytest.raises(EvidenceError, match="does not grant model input"):
        checked_model_text(library, saved["copy_ref"], window["passage_refs"])
    repeated = IntakeService(library).inspect_navigation_document(
        "inspect-program-after-revoke", navigation_ref, mission_id="mission-one",
        run_id="run-one", actor_id=actor["id"],
    )
    assert repeated["copy_ref"] is None
    assert repeated["model_input_status"] == "not_granted"
    assert len(store.records("source_copy")) == 1


@pytest.mark.parametrize("authenticated", [True, None])
def test_authenticated_or_historical_unknown_fetch_is_metadata_only(tmp_path, authenticated):
    store, actor, navigation_ref, fetches, _ = saved_document(tmp_path, authenticated=authenticated)
    saved = inspect(store, actor, navigation_ref)
    assert saved["copy_ref"] is None and saved["model_input_status"] == "not_granted"
    assert store.records("source_copy") == []
    assert fetches == ["https://mental.jmir.org/2026"]


def test_page_prohibition_and_wrong_actor_do_not_grant_copy(tmp_path):
    html = (b"<html><main><p>Useful registry notes.</p></main>"
            b"<footer><p>No text and data mining.</p></footer></html>")
    store, actor, navigation_ref, _, _ = saved_document(tmp_path, html=html)
    saved = inspect(store, actor, navigation_ref)
    assert saved["copy_ref"] is None
    assert store.get(**saved["quality_ref"])["body"]["page_restriction"] is True
    outsider = AgentRegistry(store, ROOT).register(
        "outsider", mission_id="mission-one", run_id="run-one",
        role_id="evidence_preparation_synthesis", display_name="Outsider",
    )
    with pytest.raises(EvidenceError, match="another research peer"):
        inspect(store, outsider, navigation_ref)


def test_catalog_access_note_requiring_permission_is_metadata_only(tmp_path):
    store, actor, navigation_ref, _, _ = saved_document(
        tmp_path, access_note="AI model input requires written permission.",
    )
    saved = inspect(store, actor, navigation_ref)
    assert saved["copy_ref"] is None
    assert store.get(**saved["quality_ref"])["body"]["catalog_restriction"] is True


def test_crash_after_copy_only_original_inspection_can_finish_use(tmp_path, monkeypatch):
    store, actor, navigation_ref, fetches, _ = saved_document(tmp_path)
    original_transact = store.transact
    interrupted = False

    def fail_before_use(operation_id, *args, **kwargs):
        nonlocal interrupted
        if operation_id == "inspect-program-use" and not interrupted:
            interrupted = True
            raise RuntimeError("injected shutdown before source-use commit")
        return original_transact(operation_id, *args, **kwargs)

    monkeypatch.setattr(store, "transact", fail_before_use)
    with pytest.raises(RuntimeError, match="injected shutdown"):
        inspect(store, actor, navigation_ref)
    monkeypatch.setattr(store, "transact", original_transact)
    assert len(store.records("source_copy")) == 1
    assert store.records("source_use") == []
    unrelated = inspect(store, actor, navigation_ref, operation_id="other-inspection")
    assert unrelated["copy_ref"] is None
    assert unrelated["model_input_status"] == "not_granted"
    restored = inspect(store, actor, navigation_ref)
    assert restored["copy_ref"]
    assert restored["source_use_ref"]
    assert len(store.records("source_copy")) == 1
    assert len(store.records("source_use")) == 1
    assert fetches == ["https://mental.jmir.org/2026"]
