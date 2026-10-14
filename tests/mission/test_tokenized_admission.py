"""Exact tokenizer estimates survive host admission, saved state and dispatch checks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.models import ModelSession, SessionError
from ora.models.token_estimate import (
    LEGACY_BASIS,
    TOKENIZER_BASIS,
    TOKENIZER_BASIS_V1,
    estimate_request_tokens,
)
from ora.state import StateStore
from tests.mission.test_service import ROOT, drain, service, tool


def test_tokenized_complete_request_retains_source_and_reserved_completion(tmp_path, monkeypatch):
    requests = []
    host = None
    evidence = ("Synthetic comparison reports uncertainty and needs independent review. " * 130).strip()

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        requests.append(packet)
        assert evidence in "\n".join(item.get("content") or "" for item in packet["messages"])
        assert packet["reasoning_effort"] == "high"
        assert packet["max_completion_tokens"] == 8192
        if len(requests) == 1:
            return tool("request_capability", {"tool_id": "calculate"}, 1)
        assert len(requests) == 2
        copy = host.store.records("source_copy")[0]
        return tool("publish_result", {
            "title": "Synthetic admission check", "text": "The comparison remains uncertain.",
            "citations": [{"source_id": copy["id"], "span_id": "supplied-1"}],
            "limitations": ["Offline synthetic evidence"],
        }, 2)

    host = service(tmp_path, monkeypatch, transport)
    monkeypatch.setattr(host, "_effective_input_budget", lambda _control: {
        "observed_limit_tokens": 8000, "target_fraction": 0.70,
    })
    try:
        run = host.start(LiveStartRequest(
            request_id="tokenized-admission", question="Assess the supplied comparison.",
            evidence_text=evidence, agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=10)
        assert len(requests) == 2
        assert host.state(run.run_id).snapshot.mission.status == "completed"
        intents = host.store.records("model_request_intent")
        assert len(intents) == 2
        reservations = []
        saw_legacy_overflow = False
        for intent in intents:
            body = intent["body"]
            raw = host.store.read_blob(body["blob"]["sha256"])
            estimate = estimate_request_tokens(raw)
            assert estimate.basis == TOKENIZER_BASIS
            assert body["request_token_estimate"] == asdict(estimate)
            assert body["source_refs"]
            assert hashlib.sha256(raw).hexdigest() == body["request_sha256"]
            budget = host.store.get("mission_call_budget", body["call_ref"]["id"])["body"]
            reserve = budget["prior_completion_reservation"]
            reservations.append(reserve)
            assert budget["request_token_estimate"] == asdict(estimate)
            assert budget["estimated_tokens_with_completion_reservation"] == estimate.tokens + reserve
            assert estimate.tokens + reserve <= 8000
            saw_legacy_overflow |= estimate_request_tokens(raw, basis=LEGACY_BASIS).tokens > 8000
            capacity = next(item["body"] for item in host.store.records("capacity_operation")
                            if item["body"].get("call_id") == body["call_ref"]["id"]
                            and item["body"]["kind"] == "model")
            assert capacity["estimated_tokens"] == estimate.tokens + reserve
            assert capacity["estimate_basis"] == TOKENIZER_BASIS
            assert capacity["request_token_estimate"]["request_sha256"] == body["request_sha256"]
            assert ModelSession(host.store, ROOT).load(ref(intent), for_dispatch=True).intent_ref == ref(intent)
        assert sorted(reservations) == [0, 6]
        assert saw_legacy_overflow

        first = intents[0]
        first_raw = host.store.read_blob(first["body"]["blob"]["sha256"])
        current = estimate_request_tokens(first_raw, basis=TOKENIZER_BASIS)
        old = estimate_request_tokens(first_raw, basis=TOKENIZER_BASIS_V1)
        assert estimate_request_tokens(first_raw) == current
        assert old.tokens == current.tokens + 128
        assert old.asset_sha256 == current.asset_sha256
        ModelSession._check_token_estimate(
            first_raw, asdict(estimate_request_tokens(first_raw, basis=LEGACY_BASIS))
        )
        with pytest.raises(SessionError, match="invalid"):
            ModelSession._check_token_estimate(
                first_raw, {**asdict(old), "basis": "unknown-estimator"}
            )

        def save_old_basis(tx):
            good = tx.put("model_request_intent", "model-intent:old-token-basis", {
                **first["body"], "request_token_estimate": asdict(old),
            })
            bad = tx.put("model_request_intent", "model-intent:tampered-old-token-basis", {
                **first["body"], "request_token_estimate": {
                    **asdict(old), "tokens": old.tokens - 1,
                },
            })
            return {"old_ref": ref(good), "tampered_ref": ref(bad)}

        saved_refs = host.store.transact(
            "save-old-token-basis-replay", {}, save_old_basis,
            mission_id=first["body"]["mission_id"],
        )
        old_ref, tampered_ref = saved_refs["old_ref"], saved_refs["tampered_ref"]

        # A saved new estimate cannot silently become a byte estimate on restart.
        import ora.models.token_estimate as estimator
        with monkeypatch.context() as patch:
            patch.setattr(estimator, "ASSET_PATH", tmp_path / "missing-vocabulary")
            with pytest.raises(SessionError, match="cannot be reproduced"):
                ModelSession(host.store, ROOT).load(ref(first), for_dispatch=True)
            with pytest.raises(SessionError, match="cannot be reproduced"):
                ModelSession(host.store, ROOT).load(old_ref, for_dispatch=True)
        assert host.store.get(**ref(intents[0])) == intents[0]
    finally:
        host.close()

    reopened = StateStore(tmp_path)
    assert ModelSession(reopened, ROOT).load(old_ref, for_dispatch=True).intent_ref == old_ref
    with pytest.raises(SessionError, match="differs from exact bytes"):
        ModelSession(reopened, ROOT).load(tampered_ref, for_dispatch=True)
