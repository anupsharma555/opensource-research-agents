"""A concurrent use change cannot re-grant bytes from the same saved hit."""

from __future__ import annotations

import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.evidence.library import EvidenceError
from ora.mission.shared_materials import shared_hit_access
from ora.retrieval.providers import HTTPResult
from tests.mission.test_service import _search_spec, service


def test_same_hit_revocation_before_use_commit_blocks_new_grant(tmp_path, monkeypatch):
    def no_model(*_args, **_kwargs):
        raise AssertionError("This intake check must not invoke a model")

    host = service(tmp_path, monkeypatch, no_model)
    host.discovery_transport = lambda *_args, **_kwargs: HTTPResult(200, json.dumps({
        "hitCount": 1, "nextCursorMark": "*", "resultList": {"result": [{
            "source": "MED", "id": "123", "pmid": "123",
            "title": "Synthetic checked abstract",
            "abstractText": "Synthetic evidence from a retained original abstract. " * 7,
        }]},
    }).encode(), {})
    try:
        run = host.start(LiveStartRequest(
            request_id="same-hit-commit-revocation", question="Inspect synthetic evidence",
            agent_ids=["agent-synthesis"],
        ))
        found, _ = host._search(_search_spec(host, run.run_id, suffix="find"), {
            "route": "europe_pmc.search", "query": "synthetic", "limit": 1,
        })
        hit_id = found["hits"][0]["hit_id"]
        first, _ = host._inspect_hit(
            _search_spec(host, run.run_id, suffix="first"), {"hit_id": hit_id},
        )
        first_ref = {"kind": "source_copy", "id": first["source_id"],
                     "version": first["source_version"]}
        use = host.store.resolve("source_use.copy", f"{first_ref['id']}:{first_ref['version']}")
        hit = host.store.get("retrieval_hit", hit_id)
        second_spec = _search_spec(host, run.run_id, suffix="second")
        second_use_operation = f"mission-intake:{second_spec.work_id}-use"
        transact = host.store.transact
        interleavings = []

        def revoke(tx):
            current = tx.get("source_use", use["id"])
            return ref(tx.put("source_use", current["id"], {
                **current["body"], "policy_status": "revoked", "allowed_uses": [],
            }, expected_version=current["version"]))

        def interleave(operation_id, payload, fn, **kwargs):
            if operation_id == second_use_operation:
                # The service's earlier check succeeded. Commit another peer's
                # revocation after copy creation, immediately before the grant.
                assert len(host.store.records("source_copy")) == 2
                assert host.store.get("source_use", use["id"])["body"]["policy_status"] == "granted"
                interleavings.append(transact("concurrent-revoke", {}, revoke))
            return transact(operation_id, payload, fn, **kwargs)

        assert shared_hit_access(host.store, hit)
        monkeypatch.setattr(host.store, "transact", interleave)
        with pytest.raises(EvidenceError, match="same retrieval hit"):
            host._inspect_hit(second_spec, {"hit_id": hit_id})
        assert len(interleavings) == 1
        assert host.store.get("source_use", use["id"])["body"]["policy_status"] == "revoked"
        assert len(host.store.records("source_use")) == 1
        new_copy = next(item for item in host.store.records("source_copy")
                        if ref(item) != first_ref)
        assert host.store.resolve("source_use.copy", f"{new_copy['id']}:{new_copy['version']}") is None
        assert not shared_hit_access(host.store, hit)
        assert not host._has_inspectable_hit(host._control(run.run_id, "agent-synthesis"))
        assert not host.store.records("model_dispatch_attempt")
        assert host._control(run.run_id, "agent-synthesis")["body"]["source_refs"] == []

        # The failed use transaction left a checked copy but no source-use
        # alias. Restore the earlier exact grant through the supported
        # versioned policy update; the orphan remains saved and ungranted.
        monkeypatch.setattr(host.store, "transact", transact)

        def restore(tx):
            current = tx.get("source_use", use["id"])
            return ref(tx.put("source_use", current["id"], {
                **current["body"], "policy_status": "granted",
                "allowed_uses": ["discovery", "storage", "model_input"],
            }, expected_version=current["version"]))

        host.store.transact("restore-original-use", {}, restore,
                            mission_id=host._run(run.run_id)["body"]["mission_id"])
        assert host.store.resolve(
            "source_use.copy", f"{new_copy['id']}:{new_copy['version']}"
        ) is None
        assert shared_hit_access(host.store, hit)
        assert host._has_inspectable_hit(host._control(run.run_id, "agent-synthesis"))
        recovered, _ = host._inspect_hit(
            _search_spec(host, run.run_id, suffix="recovered"), {"hit_id": hit_id},
        )
        assert recovered["status"] == "inspected"
        assert recovered["source_id"] not in {first_ref["id"], new_copy["id"]}
        recovered_use = host.store.resolve(
            "source_use.copy", f"{recovered['source_id']}:{recovered['source_version']}"
        )
        assert recovered_use["body"]["policy_status"] == "granted"
        assert host.store.resolve(
            "source_use.copy", f"{new_copy['id']}:{new_copy['version']}"
        ) is None

        # A distinct independently retrieved hit can establish its own use,
        # even when the publication and returned bytes happen to be identical.
        later, _ = host._search(_search_spec(host, run.run_id, suffix="independent"), {
            "route": "europe_pmc.search", "query": "independent acquisition", "limit": 1,
        })
        later_id = later["hits"][0]["hit_id"]
        assert later_id != hit_id
        independent, _ = host._inspect_hit(
            _search_spec(host, run.run_id, suffix="independent-inspect"), {"hit_id": later_id},
        )
        assert independent["status"] == "inspected"
        independent_use = host.store.resolve(
            "source_use.copy", f"{independent['source_id']}:{independent['source_version']}",
        )
        assert independent_use["body"]["policy_status"] == "granted"
    finally:
        host.close()
