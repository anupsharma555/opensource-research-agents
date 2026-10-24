"""Peer activity is scoped, pageable, and never a source permission grant."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from ora.coordination.board import ref
from ora.evidence import EvidenceLibrary, IntakeService
from ora.mission.context import WorkingContext
from ora.mission.planning import planning_note_id
from ora.mission.research_activity import (
    activity_digest,
    current_activity_use_for_intent,
    read_research_activity,
    record_activity_delivery,
)
from ora.models.token_estimate import estimate_context_tokens
from ora.state import StateError, StateStore
from ora.state.store import Transaction, canonical


def _host(tmp_path):
    store = StateStore(tmp_path / "state")
    host = SimpleNamespace(store=store, context=WorkingContext(store))

    def heads(kind, mission_id=None):
        values = store.records(kind)
        return [item for item in values if mission_id is None
                or item["body"].get("mission_id") == mission_id]

    host._heads = heads
    host._controls = lambda run_id: [item for item in heads("mission_task_control")
                                     if item["body"].get("run_id") == run_id]

    def seed(tx):
        controls = []
        for suffix in ("self", "one", "two"):
            actor = tx.put("agent_instance", f"peer-{suffix}", {
                "mission_id": "mission", "run_id": "run",
                "display_name": f"Researcher {suffix}",
            })
            task = tx.put("board_proposal", f"task-{suffix}", {
                "mission_id": "mission", "actor_ref": ref(actor),
                "objective": f"Review {suffix} topic", "next_action": "Search the evidence",
                "status": "open",
            })
            control = tx.put("mission_task_control", f"control-{suffix}", {
                "mission_id": "mission", "run_id": "run", "actor_id": actor["id"],
                "task_id": task["id"], "phase": "ready_model", "scope_status": "required",
            })
            controls.append(ref(control))
        tx.put("web_search_operation", "search-one", {
            "mission_id": "mission", "run_id": "run", "actor_id": "peer-one",
            "task_id": "task-one", "arguments": {
                "queries": ["synthetic behavioral intervention"], "provider": "exa",
            }, "items": ["job-one"], "deliveries": [],
        })
        tx.put("working_note", planning_note_id("mission", "peer-one", "task-one"), {
            "mission_id": "mission", "actor_id": "peer-one", "task_id": "task-one",
            "category": "checkpoint", "input_refs": [], "planning": {"run_id": "run"},
            "text": json.dumps({
                "status": "active", "uncertainties": ["Outcome duration remains unclear"],
                "steps": [{"status": "in_progress", "description": "Check duration"}],
            }),
        })
        return {"controls": controls}

    controls = store.transact("seed-activity", {}, seed, mission_id="mission")["controls"]
    own = store.get(**controls[0])
    spec = SimpleNamespace(mission_id="mission", run_id="run", actor_id="peer-self",
                           proposal_ref={"kind": "board_proposal", "id": "task-self", "version": 1},
                           work_id="activity-tool-work")
    return host, spec, own


def test_page_snapshot_is_stable_and_scoped(tmp_path):
    host, spec, _own = _host(tmp_path)
    first = read_research_activity(host, spec, {"cursor": 0, "limit": 1})
    assert first["total"] == 2 and first["next_cursor"] == 1
    assert first["entries"][0]["objective"] == "Review one topic"
    assert first["entries"][0]["searches"][0]["queries"] == [
        "synthetic behavioral intervention",
    ]
    assert first["entries"][0]["plan"]["unresolved_gaps"] == [
        "Outcome duration remains unclear",
    ]

    def revise(tx):
        old = tx.get("board_proposal", "task-two")
        return ref(tx.put("board_proposal", old["id"], {
            **old["body"], "objective": "Changed after snapshot",
        }, expected_version=old["version"]))

    host.store.transact("revise-peer-two", {}, revise, mission_id="mission")
    second = read_research_activity(host, spec, {
        "snapshot_id": first["snapshot_id"], "cursor": 1, "limit": 1,
    })
    assert second["entries"][0]["objective"] == "Review two topic"
    wrong = SimpleNamespace(**{**vars(spec), "actor_id": "peer-one",
                               "proposal_ref": {"kind": "board_proposal",
                                                "id": "task-one", "version": 1}})
    with pytest.raises(StateError, match="another peer task"):
        read_research_activity(host, wrong, {
            "snapshot_id": first["snapshot_id"], "cursor": 0, "limit": 1,
        })

    def cancel_reader(tx):
        old = tx.get("mission_task_control", "control-self")
        return ref(tx.put("mission_task_control", old["id"], {
            **old["body"], "phase": "cancelled",
        }, expected_version=old["version"]))

    host.store.transact("cancel-activity-reader", {}, cancel_reader, mission_id="mission")
    with pytest.raises(StateError, match="outside this peer task"):
        read_research_activity(host, spec, {"snapshot_id": first["snapshot_id"]})


def test_activity_keeps_complete_queries_and_counts_fallback_progress(tmp_path):
    host, spec, _own = _host(tmp_path)
    queries = [f"synthetic distinct query {number} " + "extended source terminology " * 10
               for number in range(10)]

    def expand(tx):
        old = tx.get("web_search_operation", "search-one")
        return tx.put("web_search_operation", old["id"], {
            **old["body"], "arguments": {"provider": "exa", "queries": queries},
            "items": [f"job-{number}" for number in range(10)],
        }, expected_version=old["version"])

    host.store.transact("expand-queries", {}, expand)
    page = read_research_activity(host, spec, {"limit": 1})
    assert page["entries"][0]["searches"][0]["queries"] == queries

    def fallback(tx):
        old = tx.get("web_search_operation", "search-one")
        return tx.put("web_search_operation", old["id"], {
            **old["body"], "arguments": {"provider": "exa", "queries": [queries[0]]},
            "items": ["exa-job", "tavily-job"], "deliveries": [{}, {}],
        }, expected_version=old["version"])

    host.store.transact("fallback-progress", {}, fallback)
    later = SimpleNamespace(**{**vars(spec), "work_id": "later-activity"})
    search = read_research_activity(host, later, {"limit": 1})["entries"][0]["searches"][0]
    assert search["query_count"] == 1
    assert search["completed_count"] == search["planned_count"] == 2
    assert search["progress_units"] == "provider_requests_including_fallback"


def test_digest_marks_only_successful_response_and_ignores_phase_churn(tmp_path):
    host, spec, own = _host(tmp_path)
    before = host.store.records("research_activity_delivery")
    digest = activity_digest(host, own)
    assert digest and len(digest["entries"]) == 2
    assert estimate_context_tokens(canonical(digest)) <= 512
    assert host.store.records("research_activity_delivery") == before

    def response(tx):
        intent = tx.put("model_request_intent", "intent-activity", {
            "mission_id": "mission", "run_id": "run", "instance_id": "peer-self",
            "task_id": "task-self", "research_activity_digest": digest,
        })
        tx.put("model_response_receipt", "model-response:intent-activity", {
            "mission_id": "mission", "intent_ref": ref(intent), "status": "complete",
        })
        return ref(intent)

    intent_ref = host.store.transact("accept-activity-response", {}, response,
                                     mission_id="mission")
    intent = host.store.get(**intent_ref)
    record_activity_delivery(host, spec, intent)
    assert activity_digest(host, own) is None

    def phase_only(tx):
        old = tx.get("mission_task_control", "control-one")
        return ref(tx.put("mission_task_control", old["id"], {
            **old["body"], "phase": "pending_tool",
        }, expected_version=old["version"]))

    host.store.transact("phase-only", {}, phase_only, mission_id="mission")
    assert activity_digest(host, own) is None

    def next_action(tx):
        old = tx.get("board_proposal", "task-one")
        return ref(tx.put("board_proposal", old["id"], {
            **old["body"], "next_action": "Check follow-up durability",
        }, expected_version=old["version"]))

    host.store.transact("new-next-action", {}, next_action, mission_id="mission")
    changed = activity_digest(host, own)
    assert changed and len(changed["entries"]) == 1
    assert changed["entries"][0]["next_action"] == "Check follow-up durability"


def test_checked_source_revocation_withholds_page_and_blocks_prepared_digest(tmp_path):
    host, spec, own = _host(tmp_path)
    url = "https://example.org/synthetic"
    content = b"<html><body><p>Synthetic checked finding.</p></body></html>"

    def lead(tx):
        delivery = tx.put("retrieval_delivery", "activity-delivery", {})
        hit = tx.put("retrieval_hit", "activity-hit", {
            "schema": "ora.retrieval.hit.v1", "mission_id": "mission",
            "task_id": "task-one", "agent_instance_id": "peer-one",
            "route": "exa.search", "delivery_ref": ref(delivery),
            "source": {"origin": "original_source", "original_url": url,
                       "rights_status": "unknown", "content": None},
        })
        tx.link(hit, delivery, "derived_from")
        return {"hit": ref(hit), "response": ref(delivery)}

    lead_refs = host.store.transact("activity-source-lead", {}, lead, mission_id="mission")
    intake = IntakeService(EvidenceLibrary(host.store))
    document = intake.save_public_document_fetch(
        "activity-public-fetch", lead_refs["hit"], lead_refs["response"],
        mission_id="mission", run_id="run", actor_id="peer-one", task_id="task-one",
        fetched={"status": 200, "content": content,
                 "content_sha256": hashlib.sha256(content).hexdigest(),
                 "byte_length": len(content), "content_type": "text/html",
                 "requested_url": url, "final_url": url, "redirect_chain": [],
                 "access_basis": "public_https_unauthenticated", "authenticated": False,
                 "retrieved_at": "2026-10-22T12:00:00Z"},
    )
    inspected = intake.inspect_public_document(
        "activity-inspect", document, mission_id="mission", run_id="run",
        actor_id="peer-one", task_id="task-one",
    )
    assert inspected["copy_ref"]

    def source_linked_work(tx):
        call = tx.put("agent_call", "activity-producer-call", {"mission_id": "mission"})
        tx.put("model_request_intent", "model-intent:activity-producer-call:1", {
            "mission_id": "mission", "call_ref": ref(call),
            "source_refs": [inspected["copy_ref"]], "context_source_dependency_refs": [],
        })
        search = tx.get("web_search_operation", "search-one")
        tx.put("web_search_operation", search["id"], {
            **search["body"], "parent_spec": {
                "call_id": call["id"], "call_version": call["version"],
            },
        }, expected_version=search["version"])
        note = tx.get("working_note", planning_note_id("mission", "peer-one", "task-one"))
        tx.put("working_note", note["id"], {
            **note["body"], "input_refs": [inspected["copy_ref"]],
        }, expected_version=note["version"])
        return {"linked": True}

    host.store.transact("activity-source-linked-work", {}, source_linked_work,
                        mission_id="mission")
    page = read_research_activity(host, spec, {"limit": 2})
    assert page["entries"][0]["checked_source_refs"] == [inspected["copy_ref"]]
    assert page["source_dependency_refs"] == [inspected["copy_ref"]]
    assert page["entries"][0]["searches"][0]["queries"]
    assert page["entries"][0]["plan"]["unresolved_gaps"]
    digest = activity_digest(host, own)
    assert inspected["copy_ref"] in digest["source_dependency_refs"]
    db = host.store._connect()
    try:
        assert current_activity_use_for_intent(host, Transaction(host.store, db), {
            "research_activity_digest": digest,
        })
    finally:
        db.close()

    external_allowed = [True]
    host.bheb = SimpleNamespace(
        summary=lambda: {"config_digest": "synthetic-config-v1"},
        current_source_use=lambda *_args: external_allowed[0],
    )

    def bind_external(tx):
        return ref(tx.put("bheb_source_binding",
                          f"bheb-binding:{inspected['copy_ref']['id']}:{inspected['copy_ref']['version']}", {
            "copy_ref": inspected["copy_ref"], "config_digest": "synthetic-config-v1",
            "source_id": "synthetic-source", "source_version": "v1",
            "upload_sha256": hashlib.sha256(content).hexdigest(),
        }))

    host.store.transact("bind-external-source", {}, bind_external, mission_id="mission")
    external_allowed[0] = False
    externally_withheld = read_research_activity(host, spec, {
        "snapshot_id": page["snapshot_id"], "limit": 2,
    })
    assert externally_withheld["entries"][0]["checked_source_refs"] == []
    assert externally_withheld["entries"][0]["searches"][0]["queries"] == []
    assert externally_withheld["entries"][0]["plan"]["status"] == (
        "withheld_due_current_source_use"
    )
    assert externally_withheld["source_dependency_refs"] == []
    db = host.store._connect()
    try:
        assert not current_activity_use_for_intent(host, Transaction(host.store, db), {
            "research_activity_digest": digest,
        })
    finally:
        db.close()
    external_allowed[0] = True

    use = host.store.resolve("source_use.copy",
                             f"{inspected['copy_ref']['id']}:{inspected['copy_ref']['version']}")

    def revoke(tx):
        old = tx.get("source_use", use["id"])
        return ref(tx.put("source_use", old["id"], {
            **old["body"], "policy_status": "revoked", "allowed_uses": [],
        }, expected_version=old["version"]))

    host.store.transact("revoke-activity-source", {}, revoke, mission_id="mission")
    page_after = read_research_activity(host, spec, {
        "snapshot_id": page["snapshot_id"], "limit": 2,
    })
    assert page_after["entries"][0]["checked_source_refs"] == []
    assert page_after["source_dependency_refs"] == []
    assert page_after["entries"][0]["searches"][0]["queries"] == []
    assert page_after["entries"][0]["plan"]["status"] == "withheld_due_current_source_use"
    assert page_after["entries"][0]["source_use_status"] == (
        "some_sources_withheld_due_current_use"
    )
    db = host.store._connect()
    try:
        assert not current_activity_use_for_intent(host, Transaction(host.store, db), {
            "research_activity_digest": digest,
        })
    finally:
        db.close()


def test_digest_overflow_leaves_unshown_changes_for_later_turn(tmp_path):
    host, spec, own = _host(tmp_path)

    def add_peers(tx):
        for number in range(3, 9):
            actor = tx.put("agent_instance", f"peer-{number}", {
                "mission_id": "mission", "run_id": "run",
                "display_name": f"Researcher {number}",
            })
            task = tx.put("board_proposal", f"task-{number}", {
                "mission_id": "mission", "actor_ref": ref(actor),
                "objective": "Inspect a distinct synthetic topic " + str(number),
                "next_action": "Find a distinct source", "status": "open",
            })
            tx.put("mission_task_control", f"control-{number}", {
                "mission_id": "mission", "run_id": "run", "actor_id": actor["id"],
                "task_id": task["id"], "phase": "ready_model",
            })
        return {"added": 6}

    host.store.transact("more-peers", {}, add_peers, mission_id="mission")
    first = activity_digest(host, own)
    assert first and 1 <= len(first["entries"]) <= 4
    assert first["more_changed"]
    assert estimate_context_tokens(canonical(first)) <= 512

    def response(tx):
        intent = tx.put("model_request_intent", "intent-overflow", {
            "mission_id": "mission", "run_id": "run", "instance_id": "peer-self",
            "task_id": "task-self", "research_activity_digest": first,
        })
        tx.put("model_response_receipt", "model-response:intent-overflow", {
            "mission_id": "mission", "intent_ref": ref(intent), "status": "complete",
        })
        return ref(intent)

    intent_ref = host.store.transact("overflow-response", {}, response,
                                     mission_id="mission")
    record_activity_delivery(host, spec, host.store.get(**intent_ref))
    later = activity_digest(host, own)
    assert later and later["entries"]
    assert set(first["observed"]).isdisjoint(later["observed"])


def test_failed_model_receipt_does_not_mark_activity_seen(tmp_path):
    host, spec, own = _host(tmp_path)
    digest = activity_digest(host, own)

    def failed(tx):
        intent = tx.put("model_request_intent", "intent-failed", {
            "mission_id": "mission", "run_id": "run", "instance_id": "peer-self",
            "task_id": "task-self", "research_activity_digest": digest,
        })
        tx.put("model_response_receipt", "model-response:intent-failed", {
            "mission_id": "mission", "intent_ref": ref(intent), "status": "failed",
        })
        return ref(intent)

    intent_ref = host.store.transact("failed-response", {}, failed, mission_id="mission")
    with pytest.raises(StateError, match="not delivered"):
        record_activity_delivery(host, spec, host.store.get(**intent_ref))
    assert host.store.records("research_activity_delivery") == []


def test_peer_task_checked_copy_appears_without_a_rewritten_discovery_hit(tmp_path):
    host, spec, own = _host(tmp_path)
    content = b"Synthetic source accepted by the peer task."
    blob = host.store.put_blob(content)

    def retain(tx):
        copy = tx.put("source_copy", "task-only-checked-copy", {
            "blob": blob, "content_sha256": blob["sha256"],
            "inspection_state": "checked", "retain_bytes": True,
            "rights_status": "permitted", "mime_type": "text/plain",
        })
        use = tx.put("source_use", "task-only-source-use", {
            "copy_ref": ref(copy), "source_sha256": blob["sha256"],
            "policy_status": "granted", "allowed_uses": ["storage", "model_input"],
        })
        tx.bind("source_use.copy", f"{copy['id']}:{copy['version']}", use["kind"], use["id"])
        control = tx.get("mission_task_control", "control-one")
        tx.put("mission_task_control", control["id"], {
            **control["body"], "source_refs": [ref(copy)],
        }, expected_version=control["version"])
        return ref(copy)

    copy_ref = host.store.transact("task-only-checked-source", {}, retain,
                                   mission_id="mission")
    page = read_research_activity(host, spec, {"limit": 1})
    assert page["entries"][0]["checked_source_refs"] == [copy_ref]
    assert page["source_dependency_refs"] == [copy_ref]
    digest = activity_digest(host, own)
    assert copy_ref in digest["source_dependency_refs"]
