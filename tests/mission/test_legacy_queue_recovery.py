"""A legacy over-limit prepared call must be fenced before a fresh model input."""

from __future__ import annotations

import math

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.models.token_estimate import TOKENIZER_BASIS, estimate_request_tokens
from tests.mission.test_service import answer, service

# High enough for the rebuilt full catalog, below the saved legacy request.
SYNTHETIC_OBSERVED_TOKEN_LIMIT = 10_000


def old_overlimit_call(tmp_path, monkeypatch):
    sent = []

    def transport(request, *, timeout):
        sent.append(request)
        return answer("Only synthetic input was used.", len(sent))

    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(
        request_id="legacy-overlimit-synthetic", question="Assess the supplied synthetic text",
        evidence_text="DOI: 10.1234/legacy. " + "A" * 30000,
        agent_ids=["agent-synthesis"],
    ))
    control = host._control(run.run_id, "agent-synthesis")
    source_refs = list(control["body"]["source_refs"])
    selected_passages = dict(control["body"].get("selected_passage_refs", {}))
    assert host._schedule_model(control)
    control = host._control(run.run_id, "agent-synthesis")
    work = host._work_for(control)
    spec = host.executor._spec_from(work)
    capacity_id = host.executor._capacity_id(spec)
    assert capacity_id in host.executor._prepare(control["body"]["mission_id"])
    capacity = host.store.get("capacity_operation", capacity_id)
    call_ref = control["body"]["call_ref"]
    intent = host.store.get(
        "model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}"
    )
    original_hash = intent["body"]["request_sha256"]
    assert intent["body"]["source_refs"] == source_refs
    assert (math.ceil(intent["body"]["blob"]["size"] / 4 * 1.25)
            > SYNTHETIC_OBSERVED_TOKEN_LIMIT)

    def old_estimate(tx):
        current = tx.get("capacity_operation", capacity_id)
        body = dict(current["body"], estimated_tokens=8192)
        body.pop("estimate_basis")
        tx.put("capacity_operation", capacity_id, body, expected_version=current["version"])
        return {}

    host.store.transact("synthetic-old-capacity", {}, old_estimate)
    assert len(host.capacity.reestimate_legacy_pending_models()) == 1
    capacity = host.capacity.get(capacity_id)
    assert capacity["estimated_tokens"] > SYNTHETIC_OBSERVED_TOKEN_LIMIT
    host.capacity.observe_headers(
        spec.quota_scopes[0], remaining_requests=100, remaining_tokens=100,
        token_limit=SYNTHETIC_OBSERVED_TOKEN_LIMIT, reset_after_tokens=60,
        observed_at=host.store.clock(),
    )
    status = host.capacity.legacy_overlimit_status(capacity_id, work["id"])
    assert status["status"] == "replan_allowed"
    assert status["per_request_limit"] == SYNTHETIC_OBSERVED_TOKEN_LIMIT
    assert sent == []
    return host, run, control, work, capacity_id, intent, original_hash, source_refs, selected_passages, sent


def test_legacy_overlimit_replans_without_mutating_saved_input(tmp_path, monkeypatch):
    (host, run, control, work, capacity_id, intent, original_hash,
     source_refs, selected_passages, sent) = old_overlimit_call(tmp_path, monkeypatch)
    try:
        assert host._replan_legacy_overlimit(control)
        current = host._control(run.run_id, "agent-synthesis")
        assert host.capacity.get(capacity_id)["phase"] == "canceled"
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "superseded"
        assert host.store.get("model_request_intent", intent["id"])["body"][
            "request_sha256"
        ] == original_hash
        assert current["body"]["phase"] == "ready_model"
        assert current["body"]["source_refs"] == source_refs
        assert current["body"]["selected_passage_refs"] == selected_passages
        assert current["body"]["input_budget"]["observed_limit_tokens"] == (
            SYNTHETIC_OBSERVED_TOKEN_LIMIT
        )
        assert host._schedule_model(current)
        new_call_ref = host._control(run.run_id, "agent-synthesis")["body"]["call_ref"]
        assert new_call_ref != control["body"]["call_ref"]
        new_intent = host.store.get(
            "model_request_intent",
            f"model-intent:{new_call_ref['id']}:{new_call_ref['version']}",
        )
        request_bytes = host.store.read_blob(new_intent["body"]["blob"]["sha256"])
        estimate = estimate_request_tokens(request_bytes)
        assert estimate.basis == TOKENIZER_BASIS
        assert estimate.tokens <= SYNTHETIC_OBSERVED_TOKEN_LIMIT
        assert new_intent["body"]["request_token_estimate"] == {
            "tokens": estimate.tokens,
            "basis": estimate.basis,
            "asset_sha256": estimate.asset_sha256,
            "fallback_reason": estimate.fallback_reason,
        }
        assert new_intent["body"]["request_sha256"] != original_hash
        assert new_intent["body"]["source_refs"] == source_refs
        decisions = new_intent["body"]["source_decisions"]
        assert len(decisions) == len(source_refs) == 1
        assert decisions[0]["copy_ref"] == source_refs[0]
        assert decisions[0]["allowed_for_model"] is True
        assert decisions[0]["use"] == "permitted"
        assert decisions[0]["unknown_policy"] == "reject"
        new_budget = host.store.get("mission_call_budget", new_call_ref["id"])["body"]
        assert new_budget["request_stage_token_estimate"]["estimated_demand_tokens"] <= (
            SYNTHETIC_OBSERVED_TOKEN_LIMIT
        )
        assert new_budget["source_turn_mode"] == "all_allowed_sources_fit"
        assert sent == []
    finally:
        host.close()


def test_uncertain_legacy_dispatch_is_not_replanned(tmp_path, monkeypatch):
    host, run, control, work, capacity_id, intent, _digest, _sources, _selected, sent = (
        old_overlimit_call(tmp_path, monkeypatch)
    )
    try:
        host.store.transact(
            "synthetic-uncertain-dispatch", {},
            lambda tx: tx.put(
                "model_dispatch_attempt", f"model-dispatch:{intent['id']}",
                {"mission_id": control["body"]["mission_id"],
                 "intent_ref": ref(intent), "status": "uncertain"},
            ),
        )
        status = host.capacity.legacy_overlimit_status(capacity_id, work["id"])
        assert status["status"] == "unsafe"
        assert status["reason"] == "external_effect_uncertain"
        host._replan_legacy_overlimit(control)
        assert host.capacity.get(capacity_id)["phase"] in {"queued", "waiting"}
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "pending"
        assert len(host.store.records("model_request_intent")) == 1
        current = host._control(run.run_id, "agent-synthesis")["body"]
        assert current["phase"] == "waiting"
        assert "uncertain" in str(current["waiting_reason"]).lower()
        assert sent == []
    finally:
        host.close()


@pytest.mark.parametrize("intervening_effect", ["claim", "dispatch_marker"])
def test_legacy_replan_rechecks_a_late_claim_or_dispatch(
    tmp_path, monkeypatch, intervening_effect
):
    host, run, control, work, capacity_id, intent, old_hash, _sources, _selected, sent = (
        old_overlimit_call(tmp_path, monkeypatch)
    )
    try:
        original_preview = host.capacity.legacy_overlimit_status
        inserted = False

        def preview_then_intervene(cap_id, work_id):
            nonlocal inserted
            status = original_preview(cap_id, work_id)
            if not inserted and status["status"] == "replan_allowed":
                inserted = True
                if intervening_effect == "claim":
                    def claim(tx):
                        current = tx.get("capacity_operation", capacity_id)
                        tx.put("capacity_operation", capacity_id, {
                            **current["body"], "phase": "admitted", "owner": "other-host",
                            "reservation": True, "local_active": True,
                        }, expected_version=current["version"])
                        return {}

                    host.store.transact("synthetic-late-claim", {}, claim)
                else:
                    host.store.transact(
                        "synthetic-late-dispatch-marker", {},
                        lambda tx: tx.put(
                            "model_dispatch_attempt", f"model-dispatch:{intent['id']}",
                            {"mission_id": control["body"]["mission_id"],
                             "intent_ref": ref(intent), "status": "uncertain"},
                        ),
                    )
            return status

        monkeypatch.setattr(host.capacity, "legacy_overlimit_status", preview_then_intervene)
        assert host._replan_legacy_overlimit(control) is False
        assert inserted
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "pending"
        assert host.store.get("model_request_intent", intent["id"])["body"][
            "request_sha256"
        ] == old_hash
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == (
            "pending_model"
        )
        assert len(host.store.records("model_request_intent")) == 1
        assert host.capacity.get(capacity_id)["phase"] != "canceled"
        assert sent == []
    finally:
        host.close()


def test_host_drive_fences_legacy_overlimit_before_dispatch(tmp_path, monkeypatch):
    host, run, _control, work, capacity_id, intent, old_hash, sources, selected, sent = (
        old_overlimit_call(tmp_path, monkeypatch)
    )
    try:
        changed, _wake = host._drive_run(host._run(run.run_id))
        assert changed
        capacity = host.capacity.get(capacity_id)
        assert capacity["phase"] == "canceled"
        assert capacity["diagnostic"] == "legacy_request_replanned_before_dispatch"
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "superseded"
        assert host.store.get("model_request_intent", intent["id"])["body"][
            "request_sha256"
        ] == old_hash
        current = host._control(run.run_id, "agent-synthesis")["body"]
        assert current["phase"] == "ready_model"
        assert current["source_refs"] == sources
        assert current["selected_passage_refs"] == selected
        assert sent == []
    finally:
        host.close()


def test_uncertain_legacy_review_survives_a_later_higher_ceiling(tmp_path, monkeypatch):
    host, run, control, work, capacity_id, intent, _digest, _sources, _selected, sent = (
        old_overlimit_call(tmp_path, monkeypatch)
    )
    try:
        host.store.transact(
            "synthetic-persisting-uncertain-dispatch", {},
            lambda tx: tx.put(
                "model_dispatch_attempt", f"model-dispatch:{intent['id']}",
                {"mission_id": control["body"]["mission_id"],
                 "intent_ref": ref(intent), "status": "uncertain"},
            ),
        )
        assert host._replan_legacy_overlimit(control)
        waiting = host._control(run.run_id, "agent-synthesis")
        assert waiting["body"]["phase"] == "waiting"
        new_ceiling = host.capacity.get(capacity_id)["estimated_tokens"] + 1000
        host.capacity.observe_headers(
            host.executor._spec_from(work).quota_scopes[0],
            remaining_requests=100, remaining_tokens=new_ceiling,
            token_limit=new_ceiling, reset_after_tokens=60,
            observed_at=host.store.clock() + 1,
        )
        status = host.capacity.legacy_overlimit_status(capacity_id, work["id"])
        assert status["status"] == "ordinary_capacity_path"
        host._replan_legacy_overlimit(waiting)
        current = host._control(run.run_id, "agent-synthesis")["body"]
        assert current["phase"] == "waiting"
        assert current["legacy_capacity_review"]["reason"] == "external_effect_uncertain"
        assert len(host.store.records("model_request_intent")) == 1
        assert sent == []
    finally:
        host.close()


def test_operator_credential_hold_precedes_legacy_replan(tmp_path, monkeypatch):
    host, run, _control, work, capacity_id, intent, old_hash, _sources, _selected, sent = (
        old_overlimit_call(tmp_path, monkeypatch)
    )
    try:
        monkeypatch.setattr(host.credential_profiles, "is_enabled", lambda _profile: False)
        host._drive_run(host._run(run.run_id))
        current = host._control(run.run_id, "agent-synthesis")["body"]
        assert current["phase"] == "waiting"
        assert current["operator_credential_hold"]["phase"] == "pending_model"
        assert host.capacity.get(capacity_id)["phase"] in {"queued", "waiting"}
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "pending"
        assert host.store.get("model_request_intent", intent["id"])["body"][
            "request_sha256"
        ] == old_hash
        assert sent == []
    finally:
        host.close()


def recoverable_legacy_review(host, run, control, work, capacity_id, intent):
    """Create then resolve a local blob fault without changing the saved intent."""
    blob_path = host.store.blobs.path(intent["body"]["blob"]["sha256"])
    original = host.store.read_blob(intent["body"]["blob"]["sha256"])
    blob_path.write_bytes(b"corrupt synthetic fixture")
    assert host._replan_legacy_overlimit(control)
    waiting = host._control(run.run_id, "agent-synthesis")
    assert waiting["body"]["legacy_capacity_review"]["reason"] == (
        "intent_blob_unverifiable"
    )
    blob_path.write_bytes(original)
    larger_limit = host.capacity.get(capacity_id)["estimated_tokens"] + 1000
    host.capacity.observe_headers(
        host.executor._spec_from(work).quota_scopes[0],
        remaining_requests=100, remaining_tokens=larger_limit,
        token_limit=larger_limit, reset_after_tokens=60,
        observed_at=host.store.clock() + 1,
    )
    assert host.capacity.legacy_overlimit_status(capacity_id, work["id"])[
        "status"
    ] == "ordinary_capacity_path"
    return waiting


def test_resolved_legacy_review_clears_when_work_is_unclaimed(tmp_path, monkeypatch):
    host, run, control, work, capacity_id, intent, old_hash, _sources, _selected, sent = (
        old_overlimit_call(tmp_path, monkeypatch)
    )
    try:
        waiting = recoverable_legacy_review(
            host, run, control, work, capacity_id, intent
        )
        assert host._replan_legacy_overlimit(waiting)
        current = host._control(run.run_id, "agent-synthesis")["body"]
        assert current["phase"] == "pending_model"
        assert current["legacy_capacity_review"] is None
        assert host.capacity.get(capacity_id)["phase"] in {"queued", "waiting"}
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "pending"
        assert host.store.get("model_request_intent", intent["id"])["body"][
            "request_sha256"
        ] == old_hash
        assert sent == []
    finally:
        host.close()


@pytest.mark.parametrize("intervening_effect", ["claim", "dispatch_marker"])
def test_legacy_review_clear_rechecks_late_claim_or_dispatch(
    tmp_path, monkeypatch, intervening_effect
):
    host, run, control, work, capacity_id, intent, old_hash, _sources, _selected, sent = (
        old_overlimit_call(tmp_path, monkeypatch)
    )
    try:
        waiting = recoverable_legacy_review(
            host, run, control, work, capacity_id, intent
        )
        original_transact = host.store.transact
        inserted = False

        def intervene_before_clear(operation_id, request, action, *args, **kwargs):
            nonlocal inserted
            if operation_id.startswith("mission-legacy-review-clear:") and not inserted:
                inserted = True
                if intervening_effect == "claim":
                    def claim(tx):
                        current = tx.get("capacity_operation", capacity_id)
                        tx.put("capacity_operation", capacity_id, {
                            **current["body"], "phase": "admitted", "owner": "other-host",
                            "reservation": True, "local_active": True,
                        }, expected_version=current["version"])
                        return {}

                    original_transact("synthetic-clear-race-claim", {}, claim)
                else:
                    original_transact(
                        "synthetic-clear-race-marker", {},
                        lambda tx: tx.put(
                            "model_dispatch_attempt", f"model-dispatch:{intent['id']}",
                            {"mission_id": control["body"]["mission_id"],
                             "intent_ref": ref(intent), "status": "uncertain"},
                        ),
                    )
            return original_transact(operation_id, request, action, *args, **kwargs)

        monkeypatch.setattr(host.store, "transact", intervene_before_clear)
        assert host._replan_legacy_overlimit(waiting) is False
        assert inserted
        current = host._control(run.run_id, "agent-synthesis")["body"]
        assert current["phase"] == "waiting"
        assert current["legacy_capacity_review"]["reason"] == (
            "intent_blob_unverifiable"
        )
        assert host.store.get("executor_work", work["id"])["body"]["phase"] == "pending"
        assert host.store.get("model_request_intent", intent["id"])["body"][
            "request_sha256"
        ] == old_hash
        assert len(host.store.records("model_request_intent")) == 1
        assert sent == []
    finally:
        host.close()
