"""Score exact saved context-recovery evidence without dispatching or judging prose.

The worker case and withheld semantic rubric are separate files. This evaluator
checks captured request lineage, passage delivery, omission and citation links;
scientific correctness remains an explicit, hash-bound reviewer assessment.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from ora.coordination.board import ref
from ora.state import StateStore
from ora.state.store import canonical


class RecoveryEvaluationError(ValueError):
    """A selection does not identify verifiable saved evaluation evidence."""


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load_case(root: Path) -> dict:
    """Load controller staging data; never give this full definition to a model."""
    case = json.loads((root / "benchmarks/context_recovery/case.json").read_text())
    if case.get("schema") != "ora.context_recovery_case.v1":
        raise RecoveryEvaluationError("Unsupported recovery case")
    return case


def make_worker_packet(case: dict, phase: str = "initial") -> dict:
    """Reveal only the explicitly selected event, without future evidence or rubric."""
    if phase not in {"initial", "pressure", "late"}:
        raise RecoveryEvaluationError("Unknown case staging phase")
    text = (case["early"] if phase == "initial" else case["late"] if phase == "late"
            else case["pressure_unit"] * case["pressure_repetitions"])
    return {"case_id": case["id"], "case_version": case["version"],
            "phase": phase, "question": case["question"], "evidence_text": text}


def _records(store: StateStore, kind: str) -> list[dict]:
    result, cursor = [], ""
    while page := store.records(kind, limit=1000, after_id=cursor):
        result.extend(page)
        cursor = page[-1]["id"]
    return result


def _get(store: StateStore, pointer: dict, kind: str) -> dict:
    if (not isinstance(pointer, dict) or set(pointer) != {"kind", "id", "version"}
            or pointer.get("kind") != kind or type(pointer.get("version")) is not int):
        raise RecoveryEvaluationError(f"An exact {kind} reference is required")
    saved = store.get(**pointer)
    if saved is None:
        raise RecoveryEvaluationError(f"Selected {kind} is missing")
    return saved


def _blob(store: StateStore, record: dict) -> dict:
    blob = record["body"]["blob"]
    raw = store.read_blob(blob["sha256"])
    if hashlib.sha256(raw).hexdigest() != blob["sha256"]:
        raise RecoveryEvaluationError("Saved blob checksum differs")
    return json.loads(raw)


def _request(store: StateStore, pointer: dict) -> tuple[dict, dict]:
    intent = _get(store, pointer, "model_request_intent")
    packet = _blob(store, intent)
    if digest(packet) != intent["body"]["request_sha256"]:
        raise RecoveryEvaluationError("Prepared request checksum differs")
    return intent, packet


def _packets(request: dict) -> list[dict]:
    packets = []
    for message in request.get("messages", []):
        try:
            parsed = json.loads(message.get("content") or "null")
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            packets.extend(parsed.get("source_packets", []))
    return packets


def _delivered(store: StateStore, intent: dict, request: dict, passage: dict) -> bool:
    pointer, body = ref(passage), passage["body"]
    copy = _get(store, body["copy_ref"], "source_copy")
    text = body["text"]
    if body.get("inspection_state") != "checked":
        return False
    decision = any(
        item.get("copy_ref") == ref(copy) and item.get("use") == "permitted"
        and item.get("allowed_for_model") is True
        and pointer in item.get("passage_refs", [])
        for item in intent["body"].get("source_decisions", [])
    )
    return decision and any(
        packet.get("id") == copy["id"] and packet.get("version") == copy["version"]
        and packet.get("copy_sha256") == copy["body"]["content_sha256"]
        and pointer in packet.get("passage_refs", []) and text in packet.get("text", "")
        and hashlib.sha256(packet["text"].encode()).hexdigest() == packet.get("sha256")
        for packet in _packets(request)
    )


def evaluate_recovery(
    store: StateStore, root: Path, selection: dict, *, review: dict | None = None,
) -> dict:
    """Evaluate a selected saved run; missing evidence fails closed, never launches work.

    Selection names run/condition, early and pressure intent refs, final response
    and report refs, and exact early/late passage refs. Compacted runs additionally
    name recovery_intent_ref. Revocation names the exact revoked_source_use_ref.
    Checkpoints must belong to one ordered task lineage. A reviewed result still
    says whether its model transport was live, injected, or unknown.
    """
    case = load_case(root)
    if selection.get("case_sha256") != digest(case):
        raise RecoveryEvaluationError("Selection must pin the public case checksum")
    condition = selection.get("condition")
    if condition not in case["conditions"]:
        raise RecoveryEvaluationError("Unknown context-recovery condition")
    run = store.get("mission_run", selection.get("run_id", ""))
    if run is None:
        raise RecoveryEvaluationError("Selected mission run is missing")
    mission_id = run["body"]["mission_id"]
    early = _get(store, selection["early_passage_ref"], "source_passage")
    late = _get(store, selection["late_passage_ref"], "source_passage")
    if early["body"]["text"] != case["early"] or late["body"]["text"] != case["late"]:
        raise RecoveryEvaluationError("Selected passages differ from the versioned case")
    final = _get(store, selection["final_response_ref"], "model_response_receipt")
    report = _get(store, selection["report_ref"], "report")
    named = {name: _request(store, selection[name + "_intent_ref"])
             for name in ("early", "pressure")}
    if condition == "compacted_recovery":
        named["recovery"] = _request(store, selection["recovery_intent_ref"])
    named["final"] = _request(store, final["body"]["intent_ref"])
    task_id = named["early"][0]["body"]["task_id"]
    for record in [*(pair[0] for pair in named.values()), final, report]:
        if record["body"].get("mission_id") != mission_id:
            raise RecoveryEvaluationError("Evidence belongs to another mission")
        if record["kind"] != "report" and record["body"].get("task_id") != task_id:
            raise RecoveryEvaluationError("Checkpoints belong to different tasks")
    intents = {digest(item["body"]["call_ref"]): item
               for item in _records(store, "model_request_intent")
               if item["body"].get("mission_id") == mission_id}

    inclusions = {digest(item["body"]["intent_ref"]): item
                  for item in _records(store, "context_inclusion")
                  if item["body"].get("mission_id") == mission_id}
    # Source changes intentionally reset parent_call_ref. Context event watermarks
    # retain saved task chronology across that reset and a service restart.
    ordered = [inclusions.get(digest(ref(pair[0]))) for pair in named.values()]
    lineage = all(item is not None for item in ordered) and all(
        left["body"]["event_through"] < right["body"]["event_through"]
        for left, right in zip(ordered, ordered[1:])
    )
    receipts = [item for item in _records(store, "model_response_receipt")
                if item["body"].get("mission_id") == mission_id]

    def matching_receipts(intent: dict) -> list[dict]:
        return [item for item in receipts
                if item["body"].get("intent_ref") == ref(intent)
                and item["body"].get("request_sha256") == intent["body"]["request_sha256"]
                and item["body"].get("call_ref") == intent["body"]["call_ref"]]

    def captured_successfully(intent: dict) -> bool:
        return any(item["body"].get("status") in {"complete", "tool_requested"}
                   for item in matching_receipts(intent))

    early_time = datetime.fromisoformat(named["early"][0]["created_at"])
    pressure_time = datetime.fromisoformat(named["pressure"][0]["created_at"])
    final_time = datetime.fromisoformat(named["final"][0]["created_at"])
    scoped_intents = {
        key: intent for key, intent in intents.items()
        if intent["body"].get("task_id") == task_id
        and datetime.fromisoformat(intent["created_at"]) <= final_time
    }
    # Partial and error receipts may already have consumed their input. Audit
    # them conservatively, including task history preceding the selected early
    # checkpoint; selecting a later checkpoint cannot erase contamination.
    captured_task = [(intent, _request(store, ref(intent))[1])
                     for intent in scoped_intents.values() if matching_receipts(intent)]
    captured_interval = [(intent, packet) for intent, packet in captured_task
                         if datetime.fromisoformat(intent["created_at"]) >= early_time]
    uncertain_dispatches = [
        marker for marker in _records(store, "model_dispatch_attempt")
        if (intent := scoped_intents.get(digest(marker["body"].get("call_ref"))))
        and not matching_receipts(intent)
    ]

    final_intent, final_request = named["final"]
    final_content = _blob(store, final).get("content")
    report_text = report["body"].get("body")
    provenance = report["body"].get("citation_input_provenance", [])

    def cited(passage: dict) -> bool:
        return any(item.get("passage_ref") == ref(passage)
                   and item.get("copy_ref") == passage["body"]["copy_ref"]
                   and item.get("producing_intent_ref") == ref(final_intent)
                   and item.get("input_status") == "exact_passage_in_producing_call"
                   for item in provenance)

    # Selected report must be the actual captured direct answer, not a supplied
    # answer string or a detached report with plausibly shaped citation metadata.
    checks = {
        "ordered_task_lineage": lineage,
        "same_selected_model_and_reasoning": len({
            (pair[1].get("model"), pair[1].get("reasoning_effort")) for pair in named.values()
        }) == 1,
        "captured_checkpoint_responses": all(
            captured_successfully(pair[0]) for pair in named.values()
        ),
        "no_uncertain_dispatch_gaps": not uncertain_dispatches,
        "final_response_complete": final["body"].get("status") == "complete",
        "final_response_lineage": (
            final["body"].get("request_sha256") == final_intent["body"]["request_sha256"]
            and final["body"].get("call_ref") == final_intent["body"]["call_ref"]
            and final["body"].get("attempt_id") == final_intent["body"]["attempt_id"]
        ),
        "report_producer_lineage": report["body"].get("producer_attempt_ref") == {
            "kind": "agent_attempt", "id": final_intent["body"]["attempt_id"], "version": 1,
        },
        "report_matches_captured_answer": bool(final_content) and report_text == final_content,
        "early_passage_supplied": _delivered(store, *named["early"], early),
        "late_evidence_withheld_until_after_pressure": all(
            case["late"] not in canonical(packet) for intent, packet in captured_task
            if datetime.fromisoformat(intent["created_at"]) <= pressure_time
        ),
        "late_passage_in_final_input": _delivered(store, final_intent, final_request, late),
        "late_citation_lineage": cited(late),
    }
    pressure_text = canonical(named["pressure"][1])
    pressure_reads = [item for item in _records(store, "context_tool_result")
                      if any(receipt["body"].get("intent_ref") == ref(named["early"][0])
                             and item["body"].get("response_ref") == ref(receipt)
                             for receipt in receipts)]
    checks["intervening_pressure_read_saved"] = any(
        case["pressure_unit"] in _blob(store, item).get("content", "")
        and len(_blob(store, item).get("content", "")) > 10000
        for item in pressure_reads
    )
    if condition == "unchanged_context":
        checks["early_retained_at_pressure_checkpoint"] = case["early"] in pressure_text
    elif condition == "compacted_recovery":
        pressure_intent = named["pressure"][0]
        budget = store.get("mission_call_budget", pressure_intent["body"]["call_ref"]["id"])
        checks["early_omitted_at_pressure_checkpoint"] = case["early"] not in pressure_text
        checks["recorded_source_compaction"] = bool(
            budget and budget["body"].get("source_turn_mode") == "source_free_recovery"
        )
        checks["exact_early_passage_recovered"] = _delivered(store, *named["recovery"], early)
        recovery_receipts = [item for item in receipts
                             if item["body"].get("intent_ref") == ref(pressure_intent)]
        reader_calls = [call for item in recovery_receipts
                        for call in _blob(store, item).get("tool_calls", [])
                        if call.get("function", {}).get("name") == "read_source_window"]
        checks["model_requested_recovery_reader"] = any(
            json.loads(call["function"]["arguments"]).get("source_id")
            == early["body"]["copy_ref"]["id"] for call in reader_calls
        )
        saved_reads = [item for item in _records(store, "context_tool_result")
                       if item["body"].get("response_ref") in [ref(r) for r in recovery_receipts]
                       and item["body"].get("tool_call_id") in [c["id"] for c in reader_calls]]
        checks["reader_saved_exact_passage"] = any(
            ref(early) in json.loads(_blob(store, item)["content"]).get("passage_refs", [])
            for item in saved_reads
        )
    else:
        revoked = _get(store, selection["revoked_source_use_ref"], "source_use")
        checks["source_use_revoked_before_pressure"] = (
            revoked["created_at"] < named["pressure"][0]["created_at"]
        )
        checks["source_use_revoked"] = (
            revoked["body"].get("copy_ref") == early["body"]["copy_ref"]
            and "model_input" not in revoked["body"].get("allowed_uses", [])
            and revoked["body"].get("policy_status") == "revoked"
        )
        denied_responses = [ref(item) for item in receipts
                            if item["body"].get("intent_ref") == ref(named["pressure"][0])]
        checks["revoked_reader_rejected"] = any(
            item["body"].get("response_ref") in denied_responses
            and (payload := _blob(store, item)).get("name") == "read_source_window"
            and json.loads(payload["content"]).get("status") == "rejected"
            for item in _records(store, "context_tool_result")
        )
        checks["revoked_passage_absent_after_pressure"] = all(
            case["early"] not in canonical(packet) for intent, packet in captured_task
            if datetime.fromisoformat(intent["created_at"])
            >= datetime.fromisoformat(revoked["created_at"])
        ) and case["early"] not in (final_content or "")
    if condition != "revoked_source":
        checks["early_passage_in_final_input"] = _delivered(store, final_intent, final_request, early)
        checks["early_citation_lineage"] = cited(early)

    reference = json.loads((root / "benchmarks/context_recovery/withheld/reference.json").read_text())
    if (reference.get("case_id"), reference.get("case_version")) != (case["id"], case["version"]):
        raise RecoveryEvaluationError("Withheld reference belongs to another case version")
    review_bound = bool(review and review.get("report_sha256") == digest(report["body"])
                        and review.get("reference_sha256") == digest(reference)
                        and review.get("condition") == condition
                        and review.get("reviewer_id") and review.get("rationale"))
    reviewed = bool(review_bound and review.get("status") == "reviewed"
                    and all(review.get("judgments", {}).get(key) is True
                            for key in reference["review_required"]))
    execution = run["body"].get("profile", {}).get("executionMode", "unknown")
    usage = [item["body"].get("usage") for item in receipts]
    actual = usage if execution == "live" else None
    injected = usage if execution == "offline_test" else None
    budgets = [item["body"].get("exact_complete_request_estimate_tokens")
               for item in _records(store, "mission_call_budget")
               if item["id"] in {intent["body"]["call_ref"]["id"] for intent in intents.values()}]
    return {
        "schema": "ora.context_recovery_evaluation.v1", "case_id": case["id"],
        "case_version": case["version"], "case_sha256": digest(case),
        "reference_sha256": digest(reference), "run_id": run["id"], "condition": condition,
        "execution_mode": execution, "checks": checks,
        "mechanical_pass": all(checks.values()),
        "semantic_review": "reviewed_pass" if reviewed else "pending_or_not_passed",
        "semantic_review_bound_to_report": review_bound,
        "qualified": all(checks.values()) and reviewed and execution == "live",
        "response_protocol": case["response_protocol"],
        "proof_boundary": "Direct-answer protocol only. Injected transport tests mechanisms only; prepared inputs are not captured responses; citation linkage is not scientific support.",
        "report_sha256": digest(report["body"]),
        "audited_captured_interval_requests": len(captured_interval),
        "audited_captured_task_requests": len(captured_task),
        "uncertain_dispatch_count": len(uncertain_dispatches),
        "captured_task_statuses": [item["body"].get("status", "unknown")
                                   for intent, _ in captured_task
                                   for item in matching_receipts(intent)],
        "selected_request_sha256": {name: pair[0]["body"]["request_sha256"]
                                    for name, pair in named.items()},
        "configuration_sha256": sorted({pair[0]["body"]["configuration_sha256"]
                                        for pair in named.values()}),
        "usage": {"actual_provider_usage": actual, "injected_fixture_usage": injected,
                  "unknown_usage_responses": sum(item is None for item in usage),
                  "estimated_input_tokens": budgets, "currency_cost": None},
        "identity": {"evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                     "saved_host_source_identity": "not_linked_by_selected_records"},
    }
