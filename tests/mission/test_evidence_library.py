"""Synthetic, offline checks for explicit reusable-evidence selection."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from ora.coordination.board import ref
from ora.evidence import EvidenceLibrary, read_source_window
from ora.mission.evidence_library import (
    BINDING_KIND,
    current_library_use_for_intent,
    rebuild_evidence_library_index,
    retain_evidence,
    search_evidence_library,
    sync_library_source_uses,
    task_bound_library_copy,
)
from ora.state import StateStore


def _spec(mission: str, run: str, actor: str, task: str, work: str):
    return SimpleNamespace(mission_id=mission, run_id=run, actor_id=actor,
                           proposal_ref={"id": task}, work_id=work)


def _host(store, *, bheb=None):
    def control_for_spec(spec):
        control = store.get("mission_task_control", spec.proposal_ref["id"])
        if control is None or control["body"].get("work_id") != spec.work_id:
            raise ValueError("No current task control")
        return control

    return SimpleNamespace(store=store, bheb=bheb, _control_for_spec=control_for_spec)


def _fixture(store, *, bheb=False):
    text = b"Distinct source sentence about cytokine response."
    digest = hashlib.sha256(text).hexdigest()
    blob = store.put_blob(text)
    spec = _spec("mission-a", "run-a", "actor-a", "task-a", "work-a")

    def save(tx):
        publication = tx.put("publication", "pub-a", {"titles": ["Cytokine study"]})
        copy = tx.put("source_copy", "copy-a", {
            "publication": ref(publication), "blob": blob,
            "content_sha256": digest, "source_version": "v1",
            "inspection_state": "checked", "retain_bytes": True,
            "mime_type": "text/plain", "rights_status": "permitted",
        })
        quality = tx.put("intake_quality", "quality-a", {
            "source_sha256": digest, "classification": "full_text",
        })
        tx.put("source_use", "use-a", {
            "copy_ref": ref(copy), "source_sha256": digest,
            "rights_status": "permitted", "policy_status": "granted",
            "allowed_uses": ["storage", "model_input"], "quality_ref": ref(quality),
        })
        tx.bind("source_use.copy", "copy-a:1", "source_use", "use-a")
        tx.put("mission_task_control", "task-a", {
            "mission_id": spec.mission_id, "run_id": spec.run_id,
            "actor_id": spec.actor_id, "task_id": "task-a", "work_id": "work-a",
            "source_refs": [ref(copy)],
        })
        tx.put("mission_task_control", "task-b", {
            "mission_id": "mission-b", "run_id": "run-b",
            "actor_id": "actor-b", "task_id": "task-b", "work_id": "work-b",
            "source_refs": [],
        })
        tx.put("agent_instance", "actor-b", {
            "mission_id": "mission-b", "run_id": "run-b",
        })
        if bheb:
            tx.put("bheb_source_binding", "bheb-binding:copy-a:1", {
                "copy_ref": ref(copy), "config_digest": "configured-v1",
                "source_id": "synthetic-id", "source_version": "v1",
                "upload_sha256": digest,
            })
        return {"copy_ref": ref(copy), "quality_ref": ref(quality)}

    return spec, store.transact("fixture", {}, save), text


def _retain_and_select(host, curator, copy_ref):
    retained = retain_evidence(host, curator, {
        "source_refs": [copy_ref], "relevance_note": "Useful cytokine finding",
    })
    reader = _spec("mission-b", "run-b", "actor-b", "task-b", "work-b")
    search = search_evidence_library(host, reader, {"query": "cytokine"})
    selected = search_evidence_library(host, reader, {
        "entry_id": retained["entry_refs"][0]["id"],
    })
    return reader, retained, search, selected


def test_cross_mission_search_selection_and_restart(tmp_path):
    store = StateStore(tmp_path)
    curator, fixture, text = _fixture(store)
    host = _host(store)
    reader, retained, search, selected = _retain_and_select(host, curator, fixture["copy_ref"])
    assert search["status"] == "found"
    assert search["entries"][0]["title"] == "Cytokine study"
    assert text.decode() not in str(search)
    assert selected["status"] == "selected_for_source_reader"
    assert task_bound_library_copy(host, reader, fixture["copy_ref"])
    window = read_source_window(EvidenceLibrary(store), "read-after-library-selection",
                                fixture["copy_ref"])
    assert "cytokine response" in str(window)
    restarted = _host(StateStore(tmp_path))
    assert task_bound_library_copy(restarted, reader, fixture["copy_ref"])
    assert search_evidence_library(restarted, reader, {"query": "cytokine"})["entries"]
    intent = {"mission_id": "mission-b", "instance_id": "actor-b", "task_id": "task-b",
              "source_refs": [fixture["copy_ref"]], "context_source_dependency_refs": []}
    assert store.transact("dispatch-check", {}, lambda tx: {
        "current": current_library_use_for_intent(host, tx, intent)
    })["current"]


def test_library_selection_is_explicit_and_normal_reuse_remains_independent(tmp_path):
    store = StateStore(tmp_path)
    curator, fixture, _ = _fixture(store)
    host = _host(store)
    retain_evidence(host, curator, {"source_refs": [fixture["copy_ref"]],
                                    "relevance_note": "Relevant cytokine source"})
    reader = _spec("mission-b", "run-b", "actor-b", "task-b", "work-b")
    assert search_evidence_library(host, reader, {"query": "cytokine"})["entries"]
    assert not task_bound_library_copy(host, reader, fixture["copy_ref"])
    # A separately approved parent/follow-up import may already be in the
    # normal task inputs. Library curation must not revoke that route.
    control = store.get("mission_task_control", "task-b")
    store.transact("normal-import", {}, lambda tx: {
        "control": ref(tx.put("mission_task_control", "task-b", {
            **control["body"], "source_refs": [fixture["copy_ref"]],
        }, expected_version=control["version"]))
    })
    intent = {"mission_id": "mission-b", "instance_id": "actor-b", "task_id": "task-b",
              "source_refs": [fixture["copy_ref"]], "context_source_dependency_refs": []}
    assert store.transact("dispatch-before-selection", {}, lambda tx: {
        "current": current_library_use_for_intent(host, tx, intent)
    })["current"]
    with pytest.raises(ValueError, match="either"):
        search_evidence_library(host, reader, {"query": " ", "entry_id": "library-entry:test"})


def test_revocation_withdraws_binding_and_blocks_dispatch(tmp_path):
    store = StateStore(tmp_path)
    curator, fixture, _ = _fixture(store)
    host = _host(store)
    reader, retained, _, _ = _retain_and_select(host, curator, fixture["copy_ref"])
    use = store.get("source_use", "use-a")
    store.transact("revoke", {}, lambda tx: {
        "use": ref(tx.put("source_use", "use-a", {
            **use["body"], "policy_status": "revoked",
        }, expected_version=use["version"]))
    })
    assert search_evidence_library(host, reader, {"query": "cytokine"})["entries"] == []
    assert search_evidence_library(host, reader, {
        "entry_id": retained["entry_refs"][0]["id"]
    })["status"] == "unavailable"
    assert not task_bound_library_copy(host, reader, fixture["copy_ref"])
    intent = {"mission_id": "mission-b", "instance_id": "actor-b", "task_id": "task-b",
              "source_refs": [fixture["copy_ref"]], "context_source_dependency_refs": []}
    assert not store.transact("dispatch-after-revoke", {}, lambda tx: {
        "current": current_library_use_for_intent(host, tx, intent)
    })["current"]
    sync_library_source_uses(host)
    assert store.records(BINDING_KIND)[0]["body"]["status"] == "withdrawn"


def test_stale_copy_and_unchecked_candidates_are_not_promoted(tmp_path):
    store = StateStore(tmp_path)
    curator, fixture, _ = _fixture(store)
    host = _host(store)
    retain_evidence(host, curator, {"source_refs": [fixture["copy_ref"]],
                                    "relevance_note": "Useful cytokine finding"})
    copy = store.get(**fixture["copy_ref"])
    store.transact("new-copy-version", {}, lambda tx: {
        "copy": ref(tx.put("source_copy", "copy-a", copy["body"],
                           expected_version=copy["version"]))
    })
    reader = _spec("mission-b", "run-b", "actor-b", "task-b", "work-b")
    assert search_evidence_library(host, reader, {"query": "cytokine"})["entries"] == []
    with pytest.raises(ValueError, match="current checked"):
        retain_evidence(host, curator, {"source_refs": [fixture["copy_ref"]],
                                        "relevance_note": "Stale candidate"})


def test_unmounted_bheb_source_is_withheld(tmp_path):
    store = StateStore(tmp_path)
    curator, fixture, _ = _fixture(store, bheb=True)
    available = SimpleNamespace(summary=lambda: {"config_digest": "configured-v1"},
                                current_source_use=lambda *_: True)
    host = _host(store, bheb=available)
    reader, _, _, _ = _retain_and_select(host, curator, fixture["copy_ref"])
    assert task_bound_library_copy(host, reader, fixture["copy_ref"])
    host.bheb = None
    assert not task_bound_library_copy(host, reader, fixture["copy_ref"])
    assert search_evidence_library(host, reader, {"query": "cytokine"})["entries"] == []


def test_disposable_index_rebuild_and_false_candidates_do_not_grant_access(tmp_path):
    store = StateStore(tmp_path)
    curator, fixture, _ = _fixture(store)
    host = _host(store)
    retained = retain_evidence(host, curator, {
        "source_refs": [fixture["copy_ref"]], "relevance_note": "Useful cytokine finding",
    })
    reader = _spec("mission-b", "run-b", "actor-b", "task-b", "work-b")
    entry_id = retained["entry_refs"][0]["id"]
    db = store._connect()
    try:
        db.execute(
            "INSERT INTO evidence_library_term_index(term,entry_id,entry_version) "
            "VALUES (?,?,?)", ("fabricated", entry_id, 1),
        )
        db.commit()
    finally:
        db.close()
    assert search_evidence_library(host, reader, {"query": "fabricated"})["entries"] == []

    db = store._connect()
    try:
        db.execute("DELETE FROM evidence_library_term_index")
        db.commit()
    finally:
        db.close()
    assert search_evidence_library(host, reader, {"query": "cytokine"})["entries"] == []
    assert rebuild_evidence_library_index(host)["indexed_entries"] == 1
    assert search_evidence_library(host, reader, {"query": "cytokine"})["entries"]

    db = store._connect()
    try:
        db.execute("DROP TABLE evidence_library_term_index")
        db.commit()
    finally:
        db.close()
    assert search_evidence_library(host, reader, {"query": "cytokine"})["entries"]

    use = store.get("source_use", "use-a")
    store.transact("index-test-revocation", {}, lambda tx: {
        "use": ref(tx.put("source_use", "use-a", {
            **use["body"], "policy_status": "revoked",
        }, expected_version=use["version"]))
    })
    assert search_evidence_library(host, reader, {"query": "cytokine"})["entries"] == []
