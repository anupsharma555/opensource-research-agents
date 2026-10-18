"""Sanitized, receipt-based context and usage metrics for one saved run.

This module never dispatches work or treats a prepared input as model uptake.
Provider usage remains unknown when a dispatch lacks a captured response.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from typing import Any

from ora.observability.evaluation import read_evaluation_observation
from ora.state import StateError, StateStore
from ora.state.store import canonical, unpack

from .prefix_reuse import analyze_local_prefix_reuse

_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
_DETAIL_FIELDS = (
    ("reasoning", "completion_tokens_details", "reasoning_tokens"),
)
_CACHE_OBSERVATION_STATUSES = frozenset({
    "reported_hit", "reported_zero", "not_reported", "malformed", "unavailable",
})


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _ref_key(pointer: Any) -> tuple[str, str, int] | None:
    if (not isinstance(pointer, dict) or set(pointer) != {"kind", "id", "version"}
            or not isinstance(pointer["kind"], str)
            or not isinstance(pointer["id"], str)
            or type(pointer["version"]) is not int):
        return None
    return pointer["kind"], pointer["id"], pointer["version"]


def _scoped_heads(store: StateStore, kind: str, mission_id: str) -> list[dict]:
    """Decode only current records in one mission, without reading private blobs."""
    db = store._connect()
    try:
        return [unpack(row) for row in db.execute(
            "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) "
            "WHERE r.kind=? AND json_extract(r.body,'$.mission_id')=? ORDER BY r.id",
            (kind, mission_id),
        )]
    finally:
        db.close()


def _retrieval_attempts(store: StateStore, mission_id: str, actors: set[str]) -> list[dict]:
    """Current logical attempts whose saved request names an actor in this run."""
    db = store._connect()
    try:
        rows = [unpack(row) for row in db.execute(
            "SELECT r.* FROM records r JOIN heads h USING(kind,id,version) "
            "WHERE r.kind='retrieval_attempt' "
            "AND json_extract(r.body,'$.request.mission_id')=? "
            "ORDER BY r.id", (mission_id,),
        )]
        return [item for item in rows if isinstance(item["body"].get("request"), dict)
                and item["body"]["request"].get("agent_instance_id") in actors]
    finally:
        db.close()


def _reported(value: Any, *, unresolved: bool, present: bool = True) -> dict:
    if type(value) is int and value >= 0:
        return {"status": "reported", "value": value}
    if present and value is not None:
        return {"status": "invalid"}
    return {"status": "unknown" if unresolved else "not_reported"}


def read_cache_observation(receipt_body: dict | None) -> dict:
    """Read only the provider-boundary cache classification from a saved receipt.

    Older receipts cannot establish whether the provider omitted a field or
    the local normalizer omitted it. Never reclassify that absence as a miss.
    """
    if receipt_body is None:
        return {"status": "unavailable"}
    usage = receipt_body.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    prompt = usage.get("prompt_tokens")
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    normalized_present = "cached_tokens" in details
    normalized = details.get("cached_tokens")
    normalized_valid = (type(normalized) is int and normalized >= 0
                        and not (type(prompt) is int and prompt >= 0 and normalized > prompt))
    if "cache_observation" not in receipt_body:
        if not normalized_present:
            return {"status": "legacy_uncertain"}
        if not normalized_valid:
            return {"status": "malformed"}
        return {"status": "reported_hit" if normalized else "reported_zero",
                "cached_tokens": normalized}
    observed = receipt_body["cache_observation"]
    if (not isinstance(observed, dict) or not isinstance(observed.get("status"), str)
            or observed["status"] not in _CACHE_OBSERVATION_STATUSES):
        return {"status": "malformed"}
    status = observed["status"]
    amount = observed.get("cached_tokens")
    if status in {"reported_hit", "reported_zero"}:
        if (type(amount) is not int or amount < 0
                or (status == "reported_hit") != (amount > 0)
                or type(prompt) is int and prompt >= 0 and amount > prompt
                or normalized_present and (not normalized_valid or normalized != amount)):
            return {"status": "malformed"}
        return {"status": status, "cached_tokens": amount}
    if "cached_tokens" in observed:
        return {"status": "malformed"}
    if status == "not_reported" and normalized_present:
        return {"status": "malformed"}
    return {"status": status}


def _usage(body: dict | None, *, unresolved: bool) -> dict:
    saved = body.get("usage") if body else None
    saved = saved if isinstance(saved, dict) else {}
    result = {key: _reported(saved.get(key), unresolved=unresolved,
                             present=key in saved) for key in _USAGE_FIELDS}
    for label, section, field in _DETAIL_FIELDS:
        detail = saved.get(section)
        detail = detail if isinstance(detail, dict) else {}
        result[label] = _reported(detail.get(field), unresolved=unresolved,
                                  present=field in detail)
    observed = read_cache_observation(body)
    if observed["status"] in {"reported_hit", "reported_zero"}:
        result["cache_read"] = {"status": "reported", "value": observed["cached_tokens"]}
    else:
        result["cache_read"] = {"status": {
            "not_reported": "not_reported", "malformed": "invalid",
            "unavailable": "unknown", "legacy_uncertain": "legacy_uncertain",
        }[observed["status"]]}
    return result


def _usage_total(calls: list[dict]) -> dict:
    totals: dict[str, dict] = {}
    for field in (*_USAGE_FIELDS, "cache_read", "reasoning"):
        reported = [call["usage"][field]["value"] for call in calls
                    if call["usage"][field]["status"] == "reported"]
        totals[field] = {"tokens": sum(reported) if reported else None,
                         "reported_calls": len(reported),
                         "unknown_calls": sum(call["usage"][field]["status"] == "unknown"
                                              for call in calls),
                         "not_reported_calls": sum(call["usage"][field]["status"] == "not_reported"
                                                   for call in calls),
                         "invalid_calls": sum(call["usage"][field]["status"] == "invalid"
                                              for call in calls),
                         "legacy_uncertain_calls": sum(
                             call["usage"][field]["status"] == "legacy_uncertain"
                             for call in calls),
                         }
    peaks = [call["usage"]["prompt_tokens"]["value"] for call in calls
             if call["usage"]["prompt_tokens"]["status"] == "reported"]
    totals["peak_reported_input_tokens"] = max(peaks) if peaks else None
    eligible = [call for call in calls if all(
        call["usage"][key]["status"] == "reported"
        for key in ("prompt_tokens", "cache_read")
    )]
    input_tokens = sum(call["usage"]["prompt_tokens"]["value"] for call in eligible)
    cached_tokens = sum(call["usage"]["cache_read"]["value"] for call in eligible)
    all_reported_input = totals["prompt_tokens"]["tokens"]
    coverage = ("complete" if calls and len(eligible) == len(calls)
                else "partial" if eligible else "unavailable")
    totals["cache_fraction"] = {
        "basis": "provider_reported_cache_read_over_matching_reported_input",
        "value": cached_tokens / input_tokens if input_tokens else None,
        "cached_tokens": cached_tokens if eligible else None,
        "input_tokens": input_tokens if eligible else None,
        "coverage_status": coverage,
        "reported_hit_calls": sum(call["usage"]["cache_read"]["value"] > 0
                                  for call in eligible),
        "reported_miss_calls": sum(call["usage"]["cache_read"]["value"] == 0
                                   for call in eligible),
        "covered_calls": len(eligible), "total_calls": len(calls),
        "total_calls_basis": "saved_request_intents_in_run",
        "covered_input_tokens": input_tokens,
        "all_reported_input_tokens": all_reported_input,
        "eligible_input_coverage_fraction": (
            input_tokens / all_reported_input if all_reported_input else None
        ),
    }
    paired = [call for call in calls if all(
        call["usage"][key]["status"] == "reported"
        for key in ("prompt_tokens", "completion_tokens")
    )]
    paired_input = sum(call["usage"]["prompt_tokens"]["value"] for call in paired)
    paired_output = sum(call["usage"]["completion_tokens"]["value"] for call in paired)
    paired_total = paired_input + paired_output
    totals["token_mix"] = {
        "basis": "provider_reported_input_and_output_on_matching_calls",
        "input_fraction": paired_input / paired_total if paired_total else None,
        "output_fraction": paired_output / paired_total if paired_total else None,
        "input_tokens": paired_input, "output_tokens": paired_output,
        "covered_calls": len(paired), "total_calls": len(calls),
        "total_calls_basis": "saved_request_intents_in_run",
    }
    reasoning_eligible = [call for call in calls if all(
        call["usage"][key]["status"] == "reported"
        for key in ("completion_tokens", "reasoning")
    )]
    paired_reasoning = sum(call["usage"]["reasoning"]["value"]
                           for call in reasoning_eligible)
    paired_completion = sum(call["usage"]["completion_tokens"]["value"]
                            for call in reasoning_eligible)
    totals["reasoning_fraction"] = {
        "basis": "provider_reported_reasoning_subset_of_matching_output",
        "value": paired_reasoning / paired_completion if paired_completion else None,
        "reasoning_tokens": paired_reasoning, "output_tokens": paired_completion,
        "covered_calls": len(reasoning_eligible), "total_calls": len(calls),
        "total_calls_basis": "saved_request_intents_in_run",
    }
    return totals


def _retrieval_summary(attempts: list[dict]) -> dict:
    """Count logical recorded attempts, never provider HTTP requests."""
    outcomes = Counter(item["body"].get("outcome", "unknown") for item in attempts)
    deliveries = sum(len(item["body"].get("delivery_refs", []))
                     for item in attempts
                     if isinstance(item["body"].get("delivery_refs"), list))
    return {
        "basis": "current_retrieval_attempt_heads_with_exact_run_actor",
        "recorded_attempt_count": len(attempts),
        "outcome_counts": dict(sorted(outcomes.items())),
        "recorded_delivery_count": deliveries,
        "http_attempt_count": None,
        "http_attempt_status": "unavailable_from_retrieval_attempt_receipts",
    }


def _request_diagnostic(store: StateStore, intent: dict) -> dict:
    body = intent["body"]
    descriptor: dict[str, Any] = {"status": "unavailable"}
    blob = body.get("blob") or body.get("request_blob")
    if not isinstance(blob, dict) or not isinstance(blob.get("sha256"), str):
        return descriptor
    try:
        raw = store.read_blob(blob["sha256"])
        digest = hashlib.sha256(raw).hexdigest()
        if digest != body.get("request_sha256"):
            return {"status": "hash_mismatch"}
        request = json.loads(raw)
        messages = request.get("messages") if isinstance(request, dict) else None
        tools = request.get("tools", []) if isinstance(request, dict) else None
        if not isinstance(messages, list) or not messages or not isinstance(tools, list):
            return {"status": "invalid_shape"}
        if intent["kind"] == "figure_request_intent":
            return {"status": "verified", "request_bytes": len(raw)}
        system = canonical(messages[0]).encode()
        tool_bytes = canonical(tools).encode()
        descriptor = {
            "status": "verified", "request_bytes": len(raw),
            "instruction_sha256": hashlib.sha256(system).hexdigest(),
            "tool_manifest_sha256": hashlib.sha256(tool_bytes).hexdigest(),
            "tool_schema_count": len(tools),
        }
    except (StateError, UnicodeError, ValueError, TypeError, KeyError):
        pass
    return descriptor


def _stage_budget(body: dict | None, intent: dict | None) -> dict | None:
    if body is None:
        return None
    stage = body.get("request_stage_token_estimate")
    if isinstance(stage, dict) and intent is not None and (
        stage.get("intent_ref") == _ref(intent)
        and stage.get("request_sha256") == intent["body"].get("request_sha256")
        and type(stage.get("final_input_tokens")) is int
        and isinstance(stage.get("stages"), list)
    ):
        return {
            "status": "saved_exact_request_estimate", "basis": stage.get("basis"),
            "final_input_tokens": stage["final_input_tokens"],
            "request_bytes": stage.get("request_bytes"),
            "stages": [
                {"kind": item["kind"], "marginal_tokens": item["marginal_tokens"]}
                for item in stage["stages"]
                if isinstance(item, dict) and item.get("kind") in {
                    "request_envelope", "pinned_instructions", "attached_tool_schemas",
                    "source_packet", "tool_outcome", "assistant_tool_call",
                    "working_context", "context_or_skill_message",
                }
                and type(item.get("marginal_tokens")) is int
            ],
        }
    preview = body.get("exact_complete_request_estimate_tokens")
    if type(preview) is int and preview >= 0:
        return {"status": "saved_preview_estimate", "final_input_tokens": preview}
    return None


def _inclusion(store: StateStore, intent: dict, record: dict | None) -> dict:
    result = {"status": "missing", "included_ref_count": 0,
              "missing_exact_ref_count": 0, "included_ref_kinds": {}}
    if record is None:
        return result
    body = record["body"]
    if (body.get("intent_ref") != _ref(intent)
            or body.get("request_sha256") != intent["body"].get("request_sha256")):
        return {**result, "status": "lineage_mismatch"}
    refs = body.get("included_refs")
    if not isinstance(refs, list):
        return {**result, "status": "invalid_refs"}
    missing = 0
    kinds: Counter[str] = Counter()
    for pointer in refs:
        key = _ref_key(pointer)
        if key is None:
            missing += 1
            continue
        kinds[key[0]] += 1
        if store.get(*key) is None:
            missing += 1
    return {"status": "verified_receipt", "included_ref_count": len(refs),
            "missing_exact_ref_count": missing, "included_ref_kinds": dict(sorted(kinds.items()))}


def build_context_metrics(
    store: StateStore, run_id: str, *, include_calls: bool = True,
) -> dict:
    """Read sanitized per-loop and run totals from saved current record heads.

    The result is JSON-safe. It contains no prompt, query, URL, source text,
    credential, or model-authored response. An inclusion means bytes were in a
    prepared request; it does not establish scientific use or support.
    """
    run = store.get("mission_run", run_id)
    if run is None:
        raise StateError("Unknown mission run")
    mission_id = run["body"].get("mission_id")
    if not isinstance(mission_id, str):
        raise StateError("Saved run has no mission identity")
    actors = {item["id"] for item in _scoped_heads(store, "agent_instance", mission_id)
              if item["body"].get("run_id") == run_id}
    manifest_by_ref = {
        _ref_key(_ref(item)): item
        for item in _scoped_heads(store, "agent_call", mission_id)
        if item["body"].get("instance_id") in actors
    }
    intents = []
    for item in _scoped_heads(store, "model_request_intent", mission_id):
        key = _ref_key(item["body"].get("call_ref"))
        if key is None or key[0] != "agent_call":
            continue
        call = store.get(*key)
        if call is None or call["body"].get("instance_id") not in actors:
            continue
        intents.append(item)
        manifest_by_ref[key] = call
    manifests = list(manifest_by_ref.values())
    intent_by_call = {_ref_key(item["body"]["call_ref"]): item for item in intents}
    model_calls = []
    prefix_candidates = []
    captured_terminal_model_calls = 0
    included_refs: Counter[tuple[str, str, int]] = Counter()
    source_routes: Counter[str] = Counter()
    previous_prefix: dict[str, tuple[str, str]] = {}
    for call in sorted(manifests, key=lambda item: (item["created_at"], item["id"])):
        intent = intent_by_call.get(_ref_key(_ref(call)))
        budget = store.get("mission_call_budget", call["id"])
        if intent is None:
            model_calls.append({
                "kind": "model", "call_id": call["id"], "task_id": call["body"].get("task_id"),
                "status": "manifest_without_intent", "request_sha256": None,
                "intent_id": None, "dispatch_recorded": False, "response_status": None,
                "usage": _usage(None, unresolved=False),
                "cache_observation": read_cache_observation(None),
                "stage_estimate": _stage_budget(budget["body"] if budget else None, None),
                "request": {"status": "not_prepared"},
                "inclusion": {"status": "not_prepared", "included_ref_count": 0,
                              "missing_exact_ref_count": 0, "included_ref_kinds": {}},
            })
            continue
        marker = store.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}")
        first_marker = store.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}", 1)
        receipt = store.get("model_response_receipt", f"model-response:{intent['id']}")
        inclusion = store.get("context_inclusion", f"context:{intent['id']}")
        response_body = receipt["body"] if receipt else None
        valid_receipt = bool(response_body and response_body.get("intent_ref") == _ref(intent)
                             and response_body.get("request_sha256") == intent["body"].get("request_sha256"))
        valid_marker = bool(marker and marker["body"].get("intent_ref") == _ref(intent)
                            and marker["body"].get("request_sha256") == intent["body"].get("request_sha256"))
        valid_first_marker = bool(
            first_marker and first_marker["body"].get("intent_ref") == _ref(intent)
            and first_marker["body"].get("request_sha256") == intent["body"].get("request_sha256")
            and first_marker["body"].get("status") == "attempted_external_effect"
        )
        status = ("captured" if valid_receipt else "receipt_lineage_mismatch" if receipt
                  else "unresolved_after_dispatch" if valid_marker else "dispatch_lineage_mismatch" if marker
                  else "prepared_only")
        request = _request_diagnostic(store, intent)
        actor = call["body"].get("instance_id")
        if (valid_receipt and valid_marker
                and response_body.get("status") in {"complete", "tool_requested"}):
            captured_terminal_model_calls += 1
            intent_body = intent["body"]
            profile = intent_body.get("credential_profile")
            scope_matches = bool(
                valid_first_marker and isinstance(profile, str) and profile
                and profile == first_marker["body"].get("credential_profile")
                == marker["body"].get("credential_profile")
                == response_body.get("credential_profile")
                and intent_body.get("mission_id") == call["body"].get("mission_id")
                == response_body.get("mission_id")
                and intent_body.get("task_id") == call["body"].get("task_id")
                == response_body.get("task_id")
                and intent_body.get("instance_id") == actor
                and intent_body.get("attempt_id") == call["body"].get("attempt_id")
                == response_body.get("attempt_id")
                and intent_body.get("call_ref") == _ref(call)
                == first_marker["body"].get("call_ref")
                == marker["body"].get("call_ref")
                == response_body.get("call_ref")
                and intent_body.get("configuration_ref")
                == response_body.get("configuration_ref")
                and intent_body.get("configuration_sha256")
                == call["body"].get("configuration_sha256")
            )
            if scope_matches:
                descriptor = intent_body.get("blob") or intent_body.get("request_blob")
                try:
                    raw = (store.read_blob(descriptor["sha256"])
                           if request["status"] == "verified" and isinstance(descriptor, dict)
                           and isinstance(descriptor.get("sha256"), str) else None)
                except StateError:
                    raw = None
                scope = {
                    "run_id": run_id, "actor_id": actor,
                    "task_id": call["body"].get("task_id"),
                    "provider": call["body"].get("provider"),
                    "model_id": call["body"].get("model_id"),
                    "credential_profile": profile,
                    "call_id": call["id"],
                    "dispatch_created_at": first_marker["created_at"],
                    "response_created_at": receipt["created_at"],
                }
                if raw is not None and all(isinstance(value, str) and value
                                           for value in scope.values()):
                    prefix_candidates.append({
                        **scope, "request_bytes": raw,
                        "request_sha256": intent_body.get("request_sha256"),
                    })
        if request["status"] == "verified":
            prefix = (request["instruction_sha256"], request["tool_manifest_sha256"])
            previous = previous_prefix.get(actor)
            request["prefix_change_from_prior_call"] = (
                {"instruction_changed": previous[0] != prefix[0],
                 "tool_manifest_changed": previous[1] != prefix[1]}
                if previous else None
            )
            previous_prefix[actor] = prefix
        inclusion_info = _inclusion(store, intent, inclusion)
        if inclusion_info["status"] == "verified_receipt" and request["status"] != "verified":
            inclusion_info["status"] = "request_unverified"
        if inclusion_info["status"] == "verified_receipt":
            for pointer in inclusion["body"]["included_refs"]:
                key = _ref_key(pointer)
                if key is not None:
                    included_refs[key] += 1
        for item in intent["body"].get("source_decisions", []):
            if isinstance(item, dict) and item.get("route") in {"direct", "cache", "peer"}:
                source_routes[item["route"]] += 1
        model_calls.append({
            "kind": "model", "call_id": call["id"], "task_id": call["body"].get("task_id"),
            "status": status, "intent_id": intent["id"],
            "request_sha256": intent["body"].get("request_sha256"),
            "dispatch_recorded": valid_marker, "response_status": response_body.get("status") if valid_receipt else None,
            "usage": _usage(response_body if valid_receipt else None,
                            unresolved=bool(
                                valid_marker and not valid_receipt
                                or valid_receipt and response_body.get("status") not in {
                                    "complete", "tool_requested",
                                }
                            )),
            "cache_observation": read_cache_observation(response_body if valid_receipt else None),
            "stage_estimate": _stage_budget(budget["body"] if budget else None, intent),
            "request": request, "inclusion": inclusion_info,
        })
    figure_calls = []
    for intent in _scoped_heads(store, "figure_request_intent", mission_id):
        if intent["body"].get("run_id") != run_id:
            continue
        marker = store.get("figure_dispatch_attempt", f"figure-dispatch:{intent['id']}")
        receipt = store.get("figure_response_receipt", f"figure-response:{intent['id']}")
        valid_marker = bool(marker and marker["body"].get("intent_ref") == _ref(intent)
                            and marker["body"].get("request_sha256") == intent["body"].get("request_sha256"))
        valid_receipt = bool(receipt and receipt["body"].get("intent_ref") == _ref(intent)
                             and receipt["body"].get("request_sha256") == intent["body"].get("request_sha256"))
        status = ("captured" if valid_receipt else "receipt_lineage_mismatch" if receipt
                  else "unresolved_after_dispatch" if valid_marker else "dispatch_lineage_mismatch" if marker
                  else "prepared_only")
        figure_calls.append({
            "kind": "figure", "intent_id": intent["id"], "task_id": intent["body"].get("task_id"),
            "status": status, "request_sha256": intent["body"].get("request_sha256"),
            "dispatch_recorded": valid_marker,
            "response_status": receipt["body"].get("status") if valid_receipt else None,
            "usage": _usage(receipt["body"] if valid_receipt else None,
                            unresolved=bool(
                                valid_marker and not valid_receipt
                                or valid_receipt and receipt["body"].get("status") != "generated_unverified"
                            )),
            "cache_observation": read_cache_observation(
                receipt["body"] if valid_receipt else None),
            "token_estimate": {
                key: intent["body"]["token_estimate"][key]
                for key in ("input_tokens", "reserved_tokens")
                if isinstance(intent["body"].get("token_estimate"), dict)
                and type(intent["body"]["token_estimate"].get(key)) is int
            },
            "request": _request_diagnostic(store, intent),
        })
    def summarize(calls: list[dict]) -> dict:
        return {
            "counts": dict(sorted(Counter(call["status"] for call in calls).items())),
            "dispatch_recorded": sum(call["dispatch_recorded"] for call in calls),
            "usage": _usage_total([call for call in calls
                                   if call.get("intent_id") is not None]),
        }
    stages: Counter[str] = Counter()
    stage_calls = 0
    estimated_peaks = []
    for call in model_calls:
        stage = call.get("stage_estimate")
        if stage and stage["status"] == "saved_exact_request_estimate":
            stage_calls += 1
            estimated_peaks.append(stage["final_input_tokens"])
            for item in stage["stages"]:
                stages[item["kind"]] += item["marginal_tokens"]
    run_calls = [*(call for call in model_calls if call.get("intent_id") is not None),
                 *figure_calls]
    local_prefix_reuse = analyze_local_prefix_reuse(prefix_candidates)
    if captured_terminal_model_calls and not local_prefix_reuse["eligible_requests"]:
        local_prefix_reuse["status"] = "request_unavailable"
    local_prefix_reuse["captured_terminal_model_calls"] = captured_terminal_model_calls
    local_prefix_reuse["request_unavailable_calls"] = max(
        0, captured_terminal_model_calls - local_prefix_reuse["eligible_requests"])
    local_prefix_reuse["eligible_request_coverage_fraction"] = (
        local_prefix_reuse["eligible_requests"] / captured_terminal_model_calls
        if captured_terminal_model_calls else None)
    local_prefix_reuse["measured_pair_coverage_fraction"] = (
        local_prefix_reuse["measured_pairs"] / local_prefix_reuse["eligible_pairs"]
        if local_prefix_reuse["eligible_pairs"] else None)
    retrieval_attempts = _retrieval_attempts(store, mission_id, actors)
    first_run = store.get("mission_run", run_id, 1)
    observed_at = [run["created_at"]]
    if first_run:
        observed_at.append(first_run["created_at"])
    observed_at.extend(item["created_at"] for item in retrieval_attempts)
    for call in run_calls:
        receipt_kind = ("model_response_receipt" if call["kind"] == "model"
                        else "figure_response_receipt")
        receipt_id = (("model-response:" if call["kind"] == "model" else "figure-response:")
                      + call["intent_id"])
        receipt = store.get(receipt_kind, receipt_id)
        if receipt and call["status"] == "captured":
            observed_at.append(receipt["created_at"])
    try:
        start = datetime.fromisoformat(first_run["created_at"] if first_run else run["created_at"])
        last_observed = max(datetime.fromisoformat(item) for item in observed_at)
        elapsed = max(0.0, (last_observed - start).total_seconds())
        last_observed_at = last_observed.isoformat()
    except (TypeError, ValueError):
        elapsed = None
        last_observed_at = None
    result = {
        "schema": "ora.context_metrics.v1", "basis": "saved_read_only_receipts",
        "run_id": run_id, "mission_id": mission_id,
        "model": {**summarize(model_calls), "stage_estimate": {
            "basis": "saved_marginal_token_estimate_not_provider_usage",
            "covered_calls": stage_calls,
            "peak_input_tokens": max(estimated_peaks) if estimated_peaks else None,
            "marginal_tokens_by_kind": dict(sorted(stages.items()))}},
        "figure": summarize(figure_calls),
        "run_usage": _usage_total(run_calls),
        "local_prefix_reuse": local_prefix_reuse,
        "evaluation": read_evaluation_observation(store, run_id),
        "retrieval": _retrieval_summary(retrieval_attempts),
        "run_activity": {
            "basis": "saved_run_response_and_retrieval_attempt_timestamps",
            "call_count": len(run_calls),
            "model_call_count": sum(call["kind"] == "model" for call in run_calls),
            "model_call_count_basis": "saved_request_intents_not_http_attempts",
            "model_dispatch_recorded_count": sum(
                call["kind"] == "model" and call["dispatch_recorded"] for call in run_calls
            ),
            "model_captured_response_count": sum(
                call["kind"] == "model" and call["status"] == "captured"
                for call in run_calls
            ),
            "prepared_only_count": sum(call["status"] == "prepared_only"
                                       for call in run_calls),
            "figure_call_count": sum(call["kind"] == "figure" for call in run_calls),
            "captured_call_count": sum(call["status"] == "captured" for call in run_calls),
            "error_or_unresolved_count": sum(
                call["status"] in {"receipt_lineage_mismatch", "dispatch_lineage_mismatch",
                                   "unresolved_after_dispatch"}
                or call["status"] == "captured"
                and call["response_status"] not in {"complete", "tool_requested",
                                                    "generated_unverified"}
                for call in run_calls
            ),
            "observed_elapsed_seconds": elapsed,
            "last_observed_at": last_observed_at,
        },
        "context": {
            "basis": "prepared_inclusion_receipts_not_scientific_uptake",
            "verified_inclusion_receipts": sum(call["inclusion"]["status"] == "verified_receipt"
                                               for call in model_calls),
            "included_ref_occurrences": sum(included_refs.values()),
            "distinct_exact_refs": len(included_refs),
            "reused_exact_ref_occurrences": sum(count - 1 for count in included_refs.values()),
            "included_ref_occurrences_by_kind": dict(sorted(Counter({
                kind: sum(count for (ref_kind, _id, _version), count in included_refs.items()
                          if ref_kind == kind)
                for kind in {key[0] for key in included_refs}
            }).items())),
            "reused_exact_ref_occurrences_by_kind": dict(sorted(Counter({
                kind: sum(count - 1 for (ref_kind, _id, _version), count in included_refs.items()
                          if ref_kind == kind)
                for kind in {key[0] for key in included_refs}
            }).items())),
            "missing_exact_refs": sum(call["inclusion"]["missing_exact_ref_count"]
                                      for call in model_calls),
            "source_routes_in_prepared_intents": dict(sorted(source_routes.items())),
        },
        "integrity": {
            "request_hash_mismatches": sum(call["request"]["status"] == "hash_mismatch"
                                           for call in (*model_calls, *figure_calls)),
            "receipt_lineage_mismatches": sum(call["status"] == "receipt_lineage_mismatch"
                                              for call in (*model_calls, *figure_calls)),
            "dispatch_lineage_mismatches": sum(call["status"] == "dispatch_lineage_mismatch"
                                               for call in (*model_calls, *figure_calls)),
            "inclusion_lineage_mismatches": sum(call["inclusion"]["status"] == "lineage_mismatch"
                                               for call in model_calls),
        },
    }
    if include_calls:
        result["calls"] = model_calls + figure_calls
    return result
