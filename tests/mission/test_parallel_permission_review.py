"""Offline permission regressions across library metadata and peer activity."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from ora.coordination.board import ref
from ora.mission.bheb_tools import current_bheb_use_for_intent
from ora.mission.evidence_library import retain_evidence, search_evidence_library
from ora.mission.research_activity import (
    activity_digest,
    current_activity_use_for_intent,
    read_research_activity,
)
from ora.mission.service import MissionService
from ora.retrieval.web_search import WebSearch
from ora.state import StateError, StateStore
from ora.state.store import Transaction, canonical
from tests.mission.test_evidence_library import _fixture, _spec
from tests.mission.test_evidence_library import _host as library_host
from tests.mission.test_research_activity import _host as activity_host


def _current(host, function, intent):
    db = host.store._connect()
    try:
        return function(host, Transaction(host.store, db), intent)
    finally:
        db.close()


def test_library_metadata_does_not_admit_source_text_and_rechecks_external_use(tmp_path):
    store = StateStore(tmp_path)
    curator, fixture, _ = _fixture(store, bheb=True)
    permitted = [True]
    bheb = SimpleNamespace(
        summary=lambda: {"config_digest": "configured-v1"},
        current_source_use=lambda *_: permitted[0],
    )
    host = library_host(store, bheb=bheb)
    retained = retain_evidence(host, curator, {
        "source_refs": [fixture["copy_ref"]], "relevance_note": "Useful cytokine finding",
    })
    reader = _spec("mission-b", "run-b", "actor-b", "task-b", "work-b")
    found = search_evidence_library(host, reader, {"query": "cytokine"})
    assert found["entries"] and "Distinct source sentence" not in str(found)
    pointer = fixture["copy_ref"]
    metadata_intent = {"mission_id": "mission-b", "instance_id": "actor-b",
                       "task_id": "task-b", "source_refs": [],
                       "context_source_dependency_refs": [pointer]}
    source_intent = {**metadata_intent, "source_refs": [pointer]}
    assert not _current(host, current_bheb_use_for_intent, metadata_intent)
    assert not _current(host, current_bheb_use_for_intent, source_intent)

    content = {**found, "source_dependency_refs": [pointer]}
    blob = store.put_blob(canonical({
        "name": "search_evidence_library", "content": canonical(content),
    }).encode())

    def save_result(tx):
        call = tx.put("agent_call", "library-metadata-call", {
            "instance_id": "actor-b", "task_id": "task-b",
        })
        response = tx.put("model_response_receipt", "library-metadata-response", {
            "call_ref": ref(call),
        })
        tx.put("context_tool_result", "library-metadata-result", {
            "mission_id": "mission-b", "response_ref": ref(response), "blob": blob,
        })
        return {"saved": True}

    store.transact("library-metadata-result", {}, save_result)
    assert _current(host, current_bheb_use_for_intent, metadata_intent)
    assert not _current(host, current_bheb_use_for_intent, source_intent)
    search_evidence_library(host, reader, {"entry_id": retained["entry_refs"][0]["id"]})
    assert _current(host, current_bheb_use_for_intent, source_intent)
    permitted[0] = False
    assert not _current(host, current_bheb_use_for_intent, metadata_intent)
    assert not _current(host, current_bheb_use_for_intent, source_intent)


def test_shared_web_guard_rechecks_external_source_before_dispatch(tmp_path):
    store = StateStore(tmp_path)
    _, fixture, _ = _fixture(store, bheb=True)
    permitted = [True]
    host = library_host(store, bheb=SimpleNamespace(
        summary=lambda: {"config_digest": "configured-v1"},
        current_source_use=lambda *_: permitted[0],
    ))

    def seed(tx):
        binding = tx.get("bheb_source_binding", "bheb-binding:copy-a:1")
        tx.put("bheb_source_binding", binding["id"], {
            **binding["body"], "mission_id": "mission-a",
        }, expected_version=binding["version"])
        tx.put("agent_instance", "actor-a", {
            "mission_id": "mission-a", "run_id": "run-a",
        })
        tx.put("model_request_intent", "model-intent:query-call:1", {
            "mission_id": "mission-a", "instance_id": "actor-a", "task_id": "task-a",
            "source_refs": [fixture["copy_ref"]], "context_source_dependency_refs": [],
        })
        return {"seeded": True}

    store.transact("shared-web-guard-seed", {}, seed)
    operation = {"parent_spec": {"call_id": "query-call", "call_version": 1}}
    MissionService._search_source_use_guard(host, operation)
    permitted[0] = False
    with pytest.raises(StateError, match="Search input source use"):
        MissionService._search_source_use_guard(host, operation)


def test_shared_web_guard_rechecks_ordinary_source_before_dispatch(tmp_path):
    store = StateStore(tmp_path)
    _, fixture, _ = _fixture(store)
    host = library_host(store)

    def seed(tx):
        tx.put("agent_instance", "actor-a", {
            "mission_id": "mission-a", "run_id": "run-a",
        })
        call = tx.put("agent_call", "ordinary-query", {
            "mission_id": "mission-a", "instance_id": "actor-a", "task_id": "task-a",
        })
        tx.put("model_request_intent", "model-intent:ordinary-query:1", {
            "mission_id": "mission-a", "instance_id": "actor-a", "task_id": "task-a",
            "call_ref": ref(call),
            "source_refs": [fixture["copy_ref"]], "context_source_dependency_refs": [],
        })
        return {"seeded": True}

    store.transact("ordinary-query-intent", {}, seed)
    operation = {"parent_spec": {"call_id": "ordinary-query", "call_version": 1}}
    host.source_use_guard = lambda body: MissionService._search_source_use_guard(host, body)
    WebSearch._require_source_scope(host, operation)

    def revoke(tx):
        use = tx.get("source_use", "use-a")
        tx.put("source_use", "use-a", {
            **use["body"], "policy_status": "revoked", "allowed_uses": [],
        }, expected_version=use["version"])
        return {"revoked": True}

    store.transact("ordinary-source-revoked", {}, revoke)
    with pytest.raises(StateError, match="source-use policy restricts"):
        WebSearch._require_source_scope(host, operation)


def test_peer_activity_withholds_bheb_derived_query_after_external_revocation(tmp_path):
    host, spec, own = activity_host(tmp_path)
    text = b"Synthetic source behind a peer query."
    digest = hashlib.sha256(text).hexdigest()
    blob = host.store.put_blob(text)
    permitted = [True]
    host.bheb = SimpleNamespace(
        summary=lambda: {"config_digest": "configured-v1"},
        current_source_use=lambda *_: permitted[0],
    )

    def seed(tx):
        copy = tx.put("source_copy", "peer-bheb-copy", {
            "inspection_state": "checked", "retain_bytes": True,
            "rights_status": "permitted", "content_sha256": digest, "blob": blob,
        })
        use = tx.put("source_use", "peer-bheb-use", {
            "copy_ref": ref(copy), "source_sha256": digest,
            "policy_status": "granted", "rights_status": "permitted",
            "allowed_uses": ["model_input"],
        })
        tx.bind("source_use.copy", f"{copy['id']}:{copy['version']}", use["kind"], use["id"])
        tx.put("bheb_source_binding", f"bheb-binding:{copy['id']}:{copy['version']}", {
            "copy_ref": ref(copy), "mission_id": "mission", "config_digest": "configured-v1",
            "source_id": "synthetic-source", "source_version": "v1", "upload_sha256": digest,
        })
        call = tx.put("agent_call", "peer-bheb-call", {"mission_id": "mission"})
        tx.put("model_request_intent", f"model-intent:{call['id']}:{call['version']}", {
            "mission_id": "mission", "call_ref": ref(call),
            "source_refs": [ref(copy)], "context_source_dependency_refs": [],
        })
        operation = tx.get("web_search_operation", "search-one")
        tx.put("web_search_operation", operation["id"], {
            **operation["body"], "parent_spec": {
                "call_id": call["id"], "call_version": call["version"],
            },
        }, expected_version=operation["version"])
        return {"copy_ref": ref(copy)}

    saved = host.store.transact("peer-bheb-activity-fixture", {}, seed, mission_id="mission")
    before = read_research_activity(host, spec, {"limit": 2})
    assert before["entries"][0]["searches"][0]["queries"]
    digest_before = activity_digest(host, own)
    assert saved["copy_ref"] in digest_before["source_dependency_refs"]
    permitted[0] = False
    after = read_research_activity(host, spec, {
        "snapshot_id": before["snapshot_id"], "limit": 2,
    })
    assert after["entries"][0]["searches"][0]["queries"] == []
    assert not _current(host, current_activity_use_for_intent, {
        "research_activity_digest": digest_before,
    })


def test_overflow_does_not_mark_omitted_peer_research_as_seen(tmp_path, monkeypatch):
    import ora.mission.research_activity as module

    host = SimpleNamespace(store=StateStore(tmp_path))
    control = {"body": {"mission_id": "m", "run_id": "r",
                        "actor_id": "self", "task_id": "self-task"}}
    refs = [{"kind": "source_copy", "id": "copy-" + "x" * 140 + str(index),
             "version": 1} for index in range(2)]
    plan_refs = [{"kind": "source_copy", "id": "plan-" + "y" * 140 + str(index),
                  "version": 1} for index in range(6)]
    entry = {
        "actor_id": "peer-" + "a" * 70, "task_id": "task-" + "b" * 70,
        "agent_name": "N" * 100, "objective": "O" * 240,
        "next_action": "A" * 180, "checked_source_refs": refs,
        "checked_source_count": 2,
        "searches": [{"provider": "exa", "queries": ["Q" * 180],
                      "query_status": "available", "_source_deps": refs}],
        "plan": {"unresolved_gaps": ["G" * 180]},
        "_plan_source_deps": plan_refs, "_proposal_source_deps": refs,
    }
    monkeypatch.setattr(module, "_entries", lambda *_a, **_k: [entry])
    monkeypatch.setattr(module, "_current_entry", lambda *_a, **_k: entry)
    digest = activity_digest(host, control)
    assert digest["entries"] and digest["entries"][0]["objective"] is None
    assert digest["entries"][0]["query"] is None
    assert digest["observed"] == {}


def test_activity_indexes_operations_and_hits_once_for_many_peers(tmp_path):
    host, _spec, own = activity_host(tmp_path)

    def more_peers(tx):
        for number in range(3, 13):
            actor = tx.put("agent_instance", f"peer-{number}", {
                "mission_id": "mission", "run_id": "run",
                "display_name": f"Researcher {number}",
            })
            task = tx.put("board_proposal", f"task-{number}", {
                "mission_id": "mission", "actor_ref": ref(actor),
                "objective": f"Review topic {number}", "status": "open",
            })
            tx.put("mission_task_control", f"control-{number}", {
                "mission_id": "mission", "run_id": "run", "actor_id": actor["id"],
                "task_id": task["id"], "phase": "ready_model",
            })
        return {"added": 10}

    host.store.transact("many-activity-peers", {}, more_peers, mission_id="mission")
    original_heads = host._heads
    iterations = {"web_search_operation": 0, "retrieval_hit": 0}

    class Counted(list):
        def __init__(self, kind, values):
            super().__init__(values)
            self.kind = kind

        def __iter__(self):
            iterations[self.kind] += 1
            return super().__iter__()

    def counted_heads(kind, mission_id=None):
        values = original_heads(kind, mission_id)
        return Counted(kind, values) if kind in iterations else values

    host._heads = counted_heads
    assert activity_digest(host, own)["entries"]
    assert iterations == {"web_search_operation": 1, "retrieval_hit": 1}
