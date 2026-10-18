"""Receipt accounting stays stable after restart and keeps missing usage unknown."""

from __future__ import annotations

import hashlib
import json

from ora.observability import build_context_metrics
from ora.observability.metrics import read_cache_observation
from ora.state import StateStore
from ora.state.store import canonical


def _put(store: StateStore, kind: str, id: str, body: dict) -> dict:
    return store.transact(
        f"fixture:{kind}:{id}", {}, lambda tx: tx.put(kind, id, body),
    )


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _request(store: StateStore, *, text: str = "synthetic") -> tuple[dict, str]:
    body = {"messages": [{"role": "system", "content": text}], "tools": []}
    raw = canonical(body).encode()
    return store.put_blob(raw), hashlib.sha256(raw).hexdigest()


def test_saved_usage_cache_coverage_reuse_and_restart(tmp_path):
    store = StateStore(tmp_path)
    _put(store, "mission_run", "run-a", {"mission_id": "mission-a"})
    _put(store, "agent_instance", "actor-a", {"mission_id": "mission-a", "run_id": "run-a"})
    source = _put(store, "source_copy", "copy-a", {"mission_id": "mission-a"})

    for index, usage in enumerate((
        {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
         "prompt_tokens_details": {"cached_tokens": 0},
         "completion_tokens_details": {"reasoning_tokens": 3}},
        {"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220,
         "prompt_tokens_details": {"cached_tokens": 100}},
        None,
    ), start=1):
        call = _put(store, "agent_call", f"call-{index}", {
            "mission_id": "mission-a", "instance_id": "actor-a", "task_id": "task-a",
        })
        blob, digest = _request(store)
        intent = _put(store, "model_request_intent", f"model-intent:call-{index}:1", {
            "mission_id": "mission-a", "call_ref": _ref(call),
            "request_sha256": digest, "blob": blob,
            "source_decisions": [{"route": "cache", "copy_ref": _ref(source)}],
        })
        _put(store, "context_inclusion", f"context:{intent['id']}", {
            "mission_id": "mission-a", "intent_ref": _ref(intent),
            "request_sha256": digest, "included_refs": [_ref(source)],
        })
        _put(store, "model_dispatch_attempt", f"model-dispatch:{intent['id']}", {
            "mission_id": "mission-a", "intent_ref": _ref(intent),
            "request_sha256": digest,
        })
        if usage is not None:
            _put(store, "model_response_receipt", f"model-response:{intent['id']}", {
                "mission_id": "mission-a", "intent_ref": _ref(intent),
                "request_sha256": digest, "status": "complete", "usage": usage,
            })
        if index == 1:
            _put(store, "mission_call_budget", call["id"], {
                "mission_id": "mission-a",
                "request_stage_token_estimate": {
                    "intent_ref": _ref(intent), "request_sha256": digest,
                    "final_input_tokens": 12, "basis": "test", "request_bytes": 42,
                    "stages": [
                        {"kind": "working_context", "marginal_tokens": 3},
                        {"kind": "working_context", "marginal_tokens": 4},
                    ],
                },
            })

    figure_blob, figure_hash = _request(store, text="synthetic figure")
    figure = _put(store, "figure_request_intent", "figure-intent-a", {
        "mission_id": "mission-a", "run_id": "run-a", "task_id": "task-a",
        "request_sha256": figure_hash, "request_blob": figure_blob,
        "token_estimate": 20,
    })
    _put(store, "figure_dispatch_attempt", f"figure-dispatch:{figure['id']}", {
        "mission_id": "mission-a", "intent_ref": _ref(figure),
        "request_sha256": figure_hash,
    })
    _put(store, "figure_response_receipt", f"figure-response:{figure['id']}", {
        "mission_id": "mission-a", "intent_ref": _ref(figure),
        "request_sha256": figure_hash, "status": "generated_unverified",
        "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
    })

    result = build_context_metrics(StateStore(tmp_path, read_only=True), "run-a")
    assert result == build_context_metrics(StateStore(tmp_path, read_only=True), "run-a")
    assert result["model"]["counts"] == {"captured": 2, "unresolved_after_dispatch": 1}
    assert result["model"]["usage"]["cache_fraction"] == {
        "basis": "provider_reported_cache_read_over_matching_reported_input",
        "value": 100 / 300, "cached_tokens": 100, "input_tokens": 300,
        "covered_calls": 2, "total_calls": 3, "covered_input_tokens": 300,
        "total_calls_basis": "saved_request_intents_in_run",
        "all_reported_input_tokens": 300,
        "coverage_status": "partial", "eligible_input_coverage_fraction": 1.0,
        "reported_hit_calls": 1, "reported_miss_calls": 1,
    }
    assert result["model"]["usage"]["cache_read"]["unknown_calls"] == 1
    run_usage = result["run_usage"]
    assert run_usage["prompt_tokens"]["tokens"] == 350
    assert run_usage["completion_tokens"]["tokens"] == 35
    assert run_usage["token_mix"]["input_fraction"] == 350 / 385
    assert run_usage["token_mix"]["output_fraction"] == 35 / 385
    assert run_usage["token_mix"]["covered_calls"] == 3
    assert run_usage["cache_fraction"]["value"] == 100 / 300
    assert run_usage["cache_fraction"]["covered_calls"] == 2
    assert run_usage["cache_fraction"]["total_calls"] == 4
    assert run_usage["reasoning_fraction"]["value"] == 3 / 10
    assert run_usage["reasoning_fraction"]["covered_calls"] == 1
    assert result["run_activity"]["model_call_count"] == 3
    assert result["run_activity"]["model_dispatch_recorded_count"] == 3
    assert result["run_activity"]["model_captured_response_count"] == 2
    assert result["run_activity"]["prepared_only_count"] == 0
    assert result["calls"][0]["usage"]["cache_read"] == {"status": "reported", "value": 0}
    assert result["calls"][0]["cache_observation"] == {
        "status": "reported_zero", "cached_tokens": 0,
    }
    assert result["calls"][2]["usage"]["cache_read"] == {"status": "unknown"}
    assert result["model"]["stage_estimate"]["marginal_tokens_by_kind"] == {"working_context": 7}
    assert result["context"]["reused_exact_ref_occurrences"] == 2
    assert result["figure"]["usage"]["prompt_tokens"]["tokens"] == 50
    assert result["figure"]["usage"]["cache_fraction"]["covered_calls"] == 0
    assert result["figure"]["usage"]["cache_read"]["tokens"] is None
    assert result["figure"]["usage"]["cache_read"]["legacy_uncertain_calls"] == 1
    assert result["figure"]["usage"]["cache_fraction"]["coverage_status"] == "unavailable"
    assert result["retrieval"]["recorded_attempt_count"] == 0
    assert result["retrieval"]["http_attempt_count"] is None
    assert "calls" not in build_context_metrics(store, "run-a", include_calls=False)
    serialized = json.dumps(result)
    assert "synthetic figure" not in serialized
    assert "synthetic\"" not in serialized


def test_missing_inclusion_ref_and_request_hash_are_diagnostics(tmp_path):
    store = StateStore(tmp_path)
    _put(store, "mission_run", "run-a", {"mission_id": "mission-a"})
    _put(store, "agent_instance", "actor-a", {"mission_id": "mission-a", "run_id": "run-a"})
    call = _put(store, "agent_call", "call-a", {
        "mission_id": "mission-a", "instance_id": "actor-a", "task_id": "task-a",
    })
    blob, _digest = _request(store)
    intent = _put(store, "model_request_intent", "model-intent:call-a:1", {
        "mission_id": "mission-a", "call_ref": _ref(call),
        "request_sha256": "0" * 64, "blob": blob,
    })
    _put(store, "context_inclusion", f"context:{intent['id']}", {
        "mission_id": "mission-a", "intent_ref": _ref(intent),
        "request_sha256": "0" * 64,
        "included_refs": [{"kind": "source_copy", "id": "missing", "version": 1}],
    })
    result = build_context_metrics(StateStore(tmp_path, read_only=True), "run-a")
    assert result["integrity"]["request_hash_mismatches"] == 1
    assert result["context"]["missing_exact_refs"] == 1
    assert result["model"]["counts"] == {"prepared_only": 1}
    assert result["run_activity"]["model_call_count"] == 1
    assert result["run_activity"]["model_dispatch_recorded_count"] == 0
    assert result["run_activity"]["model_captured_response_count"] == 0
    assert result["run_activity"]["prepared_only_count"] == 1
    assert result["run_usage"]["token_mix"]["input_fraction"] is None
    assert result["run_usage"]["cache_fraction"]["value"] is None
    assert result["run_usage"]["cache_read"]["tokens"] is None
    assert result["run_usage"]["peak_reported_input_tokens"] is None

    store.transact("advance-call-head", {}, lambda tx: tx.put(
        "agent_call", "call-a", {**call["body"], "revision": 2},
        expected_version=1,
    ))
    advanced = build_context_metrics(StateStore(tmp_path, read_only=True), "run-a")
    assert advanced["model"]["counts"] == {
        "manifest_without_intent": 1, "prepared_only": 1,
    }
    assert advanced["calls"][0]["intent_id"] == intent["id"]


def test_provider_cache_observation_distinguishes_zero_missing_invalid_and_legacy(tmp_path):
    store = StateStore(tmp_path)
    _put(store, "mission_run", "run-a", {"mission_id": "mission-a"})
    _put(store, "agent_instance", "actor-a", {"mission_id": "mission-a", "run_id": "run-a"})
    observations = (
        {"status": "reported_zero", "cached_tokens": 0},
        {"status": "reported_hit", "cached_tokens": 40},
        {"status": "not_reported"},
        {"status": "malformed"},
        None,
    )
    for index, observation in enumerate(observations):
        call = _put(store, "agent_call", f"call-{index}", {
            "mission_id": "mission-a", "instance_id": "actor-a", "task_id": "task-a",
        })
        blob, digest = _request(store)
        intent = _put(store, "model_request_intent", f"intent-{index}", {
            "mission_id": "mission-a", "call_ref": _ref(call),
            "request_sha256": digest, "blob": blob,
        })
        receipt = {
            "mission_id": "mission-a", "intent_ref": _ref(intent),
            "request_sha256": digest, "status": "complete",
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        }
        if observation is not None:
            receipt["cache_observation"] = observation
        _put(store, "model_response_receipt", f"model-response:{intent['id']}", receipt)
    metrics = build_context_metrics(StateStore(tmp_path, read_only=True), "run-a")
    cache = metrics["run_usage"]["cache_fraction"]
    assert cache["value"] == 40 / 200
    assert cache["coverage_status"] == "partial"
    assert cache["reported_hit_calls"] == 1
    assert cache["reported_miss_calls"] == 1
    assert cache["eligible_input_coverage_fraction"] == 200 / 500
    assert metrics["run_usage"]["cache_read"] == {
        "tokens": 40, "reported_calls": 2, "unknown_calls": 0,
        "not_reported_calls": 1, "invalid_calls": 1, "legacy_uncertain_calls": 1,
    }
    assert [call["cache_observation"]["status"] for call in metrics["calls"]] == [
        "reported_zero", "reported_hit", "not_reported", "malformed", "legacy_uncertain",
    ]
    assert [call["usage"]["cache_read"]["status"] for call in metrics["calls"]] == [
        "reported", "reported", "not_reported", "invalid", "legacy_uncertain",
    ]
    assert read_cache_observation({"cache_observation": {"status": ["bad"]}}) == {
        "status": "malformed",
    }
    assert read_cache_observation({
        "usage": {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 5}},
    }) == {"status": "reported_hit", "cached_tokens": 5}
    assert read_cache_observation({
        "usage": {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 11}},
    }) == {"status": "malformed"}
    assert read_cache_observation({
        "usage": {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 5}},
        "cache_observation": {"status": "reported_hit", "cached_tokens": 6},
    }) == {"status": "malformed"}


def test_retrieval_attempts_are_scoped_to_run_actor_and_count_current_ids(tmp_path):
    store = StateStore(tmp_path)
    _put(store, "mission_run", "run-a", {"mission_id": "mission-a"})
    _put(store, "mission_run", "run-b", {"mission_id": "mission-a"})
    _put(store, "agent_instance", "actor-a", {"mission_id": "mission-a", "run_id": "run-a"})
    _put(store, "agent_instance", "actor-b", {"mission_id": "mission-a", "run_id": "run-b"})
    for label, mission, actor, outcome in (
        ("a", "mission-a", "actor-a", "success"),
        ("b", "mission-a", "actor-b", "provider_failure"),
        ("c", "mission-b", "actor-a", "partial"),
        ("d", "mission-a", None, "empty"),
    ):
        _put(store, "retrieval_attempt", f"attempt-{label}", {
            "request": {"mission_id": mission, "agent_instance_id": actor},
            "outcome": outcome,
            "delivery_refs": [{"kind": "retrieval_delivery", "id": f"delivery-{label}",
                               "version": 1}],
        })
    first = store.get("retrieval_attempt", "attempt-a")
    store.transact("advance-retrieval-a", {}, lambda tx: tx.put(
        "retrieval_attempt", "attempt-a", {**first["body"], "outcome": "partial",
                                           "delivery_refs": [*first["body"]["delivery_refs"],
                                                             {"kind": "retrieval_delivery",
                                                              "id": "delivery-a2", "version": 1}]},
        expected_version=1,
    ))
    summary = build_context_metrics(StateStore(tmp_path, read_only=True), "run-a")["retrieval"]
    assert summary["recorded_attempt_count"] == 1
    assert summary["outcome_counts"] == {"partial": 1}
    assert summary["recorded_delivery_count"] == 2
    assert summary["http_attempt_count"] is None
    latest_attempt = store.get("retrieval_attempt", "attempt-a")
    activity = build_context_metrics(StateStore(tmp_path, read_only=True), "run-a")[
        "run_activity"
    ]
    assert activity["last_observed_at"] == latest_attempt["created_at"]
    assert activity["observed_elapsed_seconds"] is not None


def test_local_prefix_proxy_uses_captured_same_actor_requests_only(tmp_path):
    store = StateStore(tmp_path)
    _put(store, "mission_run", "run-a", {"mission_id": "mission-a"})
    for actor in ("actor-a", "actor-b"):
        _put(store, "agent_instance", actor, {
            "mission_id": "mission-a", "run_id": "run-a",
        })
    tools = [{"type": "function", "function": {
        "name": "synthetic_tool", "description": "Synthetic example",
        "parameters": {"type": "object", "properties": {}},
    }}]
    for index, actor in enumerate(("actor-a", "actor-a", "actor-b")):
        call = _put(store, "agent_call", f"call-{index}", {
            "mission_id": "mission-a", "instance_id": actor, "task_id": "task-a",
            "attempt_id": f"attempt-{index}",
            "provider": "groq", "model_id": "openai/gpt-oss-120b",
        })
        raw = canonical({
            "model": "openai/gpt-oss-120b", "tools": tools,
            "messages": [
                {"role": "developer", "content": "Shared synthetic guidance"},
                {"role": "user", "content": f"Synthetic turn {index}"},
            ],
        }).encode()
        digest = hashlib.sha256(raw).hexdigest()
        intent = _put(store, "model_request_intent", f"intent-{index}", {
            "mission_id": "mission-a", "call_ref": _ref(call),
            "task_id": "task-a", "instance_id": actor,
            "attempt_id": f"attempt-{index}",
            "credential_profile": "synthetic_profile",
            "request_sha256": digest, "blob": store.put_blob(raw),
        })
        marker = _put(store, "model_dispatch_attempt", f"model-dispatch:{intent['id']}", {
            "mission_id": "mission-a", "intent_ref": _ref(intent),
            "call_ref": _ref(call), "credential_profile": "synthetic_profile",
            "request_sha256": digest, "status": "attempted_external_effect",
        })
        receipt = _put(store, "model_response_receipt", f"model-response:{intent['id']}", {
            "mission_id": "mission-a", "intent_ref": _ref(intent), "call_ref": _ref(call),
            "task_id": "task-a", "attempt_id": f"attempt-{index}",
            "request_sha256": digest, "status": "complete",
            "credential_profile": "synthetic_profile",
            "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                      "total_tokens": 110},
            "cache_observation": {"status": "not_reported"},
        })
        store.transact(f"capture-marker-{index}", {}, lambda tx: tx.put(
            "model_dispatch_attempt", marker["id"],
            {**marker["body"], "status": "captured"}, expected_version=1,
        ))
        assert store.get("model_dispatch_attempt", marker["id"])["created_at"] >= receipt[
            "created_at"]
    result = build_context_metrics(StateStore(tmp_path, read_only=True), "run-a")
    local = result["local_prefix_reuse"]
    assert local["status"] == "measured"
    assert local["eligible_requests"] == 3
    assert local["eligible_pairs"] == local["measured_pairs"] == 1
    assert local["stable_tool_schema_pairs"] == 1
    assert local["estimated_repeated_message_tokens"] > 0
    assert 0 < local["estimated_message_prefix_token_fraction"] < 1
    assert local["eligible_request_coverage_fraction"] == 1
    assert result["run_usage"]["cache_fraction"]["value"] is None
    assert "Shared synthetic guidance" not in json.dumps(result)

    class MissingFirstMarker(StateStore):
        def get(self, kind, id, version=None):
            if kind == "model_dispatch_attempt" and version == 1:
                return None
            return super().get(kind, id, version)

    missing = build_context_metrics(MissingFirstMarker(tmp_path, read_only=True), "run-a")
    assert missing["run_activity"]["model_captured_response_count"] == 3
    assert missing["local_prefix_reuse"]["status"] == "request_unavailable"
    assert missing["local_prefix_reuse"]["request_unavailable_calls"] == 3

    second = store.get("model_response_receipt", "model-response:intent-1")
    store.transact("mismatch-receipt-profile", {}, lambda tx: tx.put(
        "model_response_receipt", second["id"],
        {**second["body"], "credential_profile": "different_profile"},
        expected_version=1,
    ))
    mismatched = build_context_metrics(StateStore(tmp_path, read_only=True), "run-a")
    assert mismatched["local_prefix_reuse"]["eligible_requests"] == 2
    assert mismatched["local_prefix_reuse"]["request_unavailable_calls"] == 1
    assert mismatched["local_prefix_reuse"]["measured_pairs"] == 0
