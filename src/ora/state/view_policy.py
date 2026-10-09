"""Choose saved diagnostic fields that may enter a research inspection view.

Domain records can contain private instructions or provider-returned reasoning.
The default projection exposes provenance and useful diagnostics through an
allowlist, then applies the same credential redaction used by retrieval receipts.
It does not grant access to raw storage or execute referenced content.
"""

from ora.retrieval.redact import redact

GLOBAL_KINDS = {"publication", "source_copy", "source_passage", "source_encounter", "study_family", "identity_review"}
FIELDS = {
    "agent_instance": {"mission_id", "run_id", "role_id", "display_name", "kind", "lane_id", "predecessor_id"},
    "agent_configuration": {"instance_id", "mission_id", "task_scope", "provider", "endpoint", "model_id", "settings", "reasoning_settings", "instructions_sha256", "skills", "catalog_snapshot", "catalog_sha256", "permitted_tools", "grants", "input_refs", "qualification", "configuration_sha256", "previous_configuration_id", "change_reason"},
    "agent_call": {"call_id", "configuration_id", "mission_id", "task_id", "attempt_id", "instance_id", "provider", "endpoint", "model_id", "settings", "reasoning_settings", "configuration_sha256", "instructions_sha256", "skills", "loaded_skill_ids", "exposed_skill_ids", "permitted_tools", "exposed_tools", "input_refs"},
    "retrieval_delivery": {"envelope", "delivery_id", "blob"},
    "retrieval_attempt": {"request", "outcome", "delivery_refs", "last_retrieved_at", "usage_by_delivery"},
    "retrieval_hit": {"mission_id", "task_id", "request_id", "attempt_id", "agent_instance_id", "provider", "route", "index", "query", "retrieved_at", "source", "raw_payload_ref", "delivery_ref", "mapping_version"},
    "publication": {"report_kind", "titles", "verified_identifiers", "verification", "accepted_copy"},
    "source_copy": {"publication", "encounter", "blob", "content_sha256", "mime_type", "source_version", "access_surface", "retrieved_at", "rights_basis", "retain_bytes", "reference", "passage_ids", "inspection_state", "validation_record", "scientific_metadata", "structured_content", "omitted_fields", "mapping_version"},
    "capability_invocation": {"mission_id", "call_id", "manifest_version", "tool_id", "arguments_sha256", "status", "result_sha256", "reason"},
    "skill_invocation": {"mission_id", "call_id", "manifest_version", "skill_id", "skill_version", "helper_sha256", "input_ref", "status", "result_sha256"},
    "agent_activity": {"mission_id", "instance_id", "target_kind", "target_id", "target_version", "task_id", "attempt_id", "call_id"},
    "agent_handoff": {"schema", "mission_id", "predecessor_ref", "successor_ref", "task_ref", "context_refs", "task_control_changed"},
    "evidence_packet": {"id", "version", "mission_id", "kind", "artifact", "locators", "validation_record", "access_surface", "limitations"},
    "source_passage": {"span_id", "locator", "text", "start", "end", "copy_ref"},
    "source_encounter": {"publication", "retrieval_hit", "status", "access_surface", "retrieved_at", "route", "actor"},
    "claim": {"id", "version", "text", "disposition", "supports", "supersedesVersion", "taskId", "mission_id", "proof_kind"},
    "report": {"id", "version", "kind", "title", "body", "citations", "limitations", "mission_id", "proof_kind"},
    "task": {"id", "parentTaskId", "targetArtifactId", "title", "status", "latencyClass", "latencyBasis", "firstUsefulAt", "activeOrWaiting", "attemptIds", "mission_id", "proof_kind"},
    "attempt": {"id", "taskId", "agentId", "status", "startedAt", "endedAt", "toolCalls", "selectedModel", "skillVersions", "resultSummary", "mission_id", "proof_kind"},
    "message": {"id", "requestId", "fromAgentId", "toAgentId", "taskId", "status", "summary", "createdAt", "updatedAt", "sourceId", "claimId", "mission_id", "proof_kind"},
    "coordination_trace": {"schema", "mission_id", "run_id", "task_id", "attempt_id", "instance_id", "operation_id", "turn_id", "call_id", "call_version", "parent_operation_id", "configuration_ref", "category", "status", "action", "summary", "reason_summary", "next_action", "input_refs", "output_refs", "relationships", "capability_state", "timing", "timing_fields_omitted", "proof", "usage_refs", "redacted_paths"},
    "trace_summary": {"mission_id", "kind", "level", "summary", "source_refs", "coverage"},
    "peer_update_uptake": {"schema", "mission_id", "actor_ref", "update_ref", "disposition", "statement", "evidence_refs", "effect_ref", "created_at", "scientific_status"},
    "contribution_review_view": {"mission_id", "run_id", "purpose", "proposal_context", "summary_views", "late_peer_ref", "source_refs", "proof"},
    "trace_usage": {"mission_id", "receipt_id", "unit", "amount", "basis", "scope"},
    "restricted_reasoning": {"schema", "mission_id", "status", "redacted", "origin", "access"},
    "executor_work": {"spec", "phase", "result_ref", "receipt_ref", "created_at"},
    "capacity_operation": {"mission_id", "run_id", "task_id", "attempt_id", "call_id", "kind", "lane", "phase", "submitted_at", "queued_at", "admitted_at", "queue_wait_seconds", "provider_wait_seconds", "quota_wait_seconds", "retry_wait_seconds", "active_seconds", "wait_started_at", "wait_reason", "wake_at", "last_progress_at", "last_heartbeat_at", "generation", "reservation", "local_active", "external_active", "cancel_requested", "diagnostic", "output_ref"},
    "executor_artifact": {"mission_id", "work_id", "actor_id", "configuration_ref", "input_refs", "output_refs", "handler_id", "handler_version", "result_sha256", "proof"},
    "executor_receipt": {"mission_id", "work_id", "artifact_ref", "capacity_operation_id", "used_tokens", "cached_tokens", "result_sha256", "handler_version", "timing", "peer_wait"},
    "executor_unaccepted_result": {"mission_id", "work_id", "proposal_ref", "configuration_ref", "input_refs", "handler_id", "handler_version", "capacity_operation_id", "capacity_generation", "reason_code", "external_effect", "result_sha256", "observed_receipt_ref", "used_tokens", "cached_tokens", "capacity_reconciled", "created_at"},
    "board_proposal": {"mission_id", "actor_ref", "objective", "expected_artifact", "next_action", "prerequisite_refs", "depends_on", "branch_of", "status", "outcome_ref"},
    "board_claim": {"mission_id", "proposal_ref", "owner_ref", "lease", "invitation_ref"},
    "board_outcome": {"mission_id", "proposal_ref", "actor_ref", "claim_generation", "result_refs"},
    "board_invitation": {"mission_id", "proposal_ref", "sender_ref", "recipient_ref", "objective", "expected_artifact", "prerequisite_refs", "status", "counter", "decision_at"},
    "board_dissent": {"mission_id", "proposal_ref", "actor_ref", "critique", "evidence_refs"},
    "mail_message": {"mission_id", "duplicate_detection_id", "sender_ref", "recipient_ref", "correlation_id", "task_ref", "evidence_refs", "request_id", "created_at", "delivered_at", "acknowledged_at"},
    "mail_outbox": {"mission_id", "message_ref", "recipient_ref", "status", "updated_at"},
    "mail_request": {"mission_id", "requester_id", "recipient_id", "question", "expected_artifact", "cause_ref", "message_ref", "status", "answer_refs", "accepted_answer_ref", "replacement_request_ref", "reviewer_id", "review", "expected_latency", "last_useful_progress_at", "created_at", "delivered_at", "answered_at", "resolved_at"},
    "mail_answer": {"mission_id", "request_id", "responder_id", "artifact_ref", "partial", "created_at"},
    "stage3_fixture_input": {"mission_id", "question", "uncertainty", "proof"},
    "stage3_fixture_proof": {"mission_id", "run_id", "intervals_monotonic_ns", "independent_overlap_observed", "dependent_started_before_slow_finished", "provider_calls", "proof"},
    "model_request_intent": {"schema", "mission_id", "task_id", "attempt_id", "instance_id", "call_ref", "configuration_ref", "configuration_sha256", "request_sha256", "phase", "parent_call_ref", "source_refs", "evidence_refs", "source_decisions"},
    "model_response_receipt": {"schema", "mission_id", "task_id", "attempt_id", "call_ref", "configuration_ref", "intent_ref", "request_sha256", "response_sha256", "response_id", "provider_request_id", "returned_model", "status", "finish_reason", "reasoning_status", "partial_response_sha256", "usage", "rate_headers", "timings", "error_kind", "http_status", "retry_after_seconds"},
    "model_dispatch_attempt": {"mission_id", "intent_ref", "status", "receipt_ref", "request_sha256"},
}


def record_mission(record):
    body = record["body"]
    return body.get("mission_id") or body.get("request", {}).get("mission_id") or body.get("envelope", {}).get("request", {}).get("missionId")


def visible_in_mission(record, mission_id):
    return record["kind"] in GLOBAL_KINDS or record_mission(record) == mission_id


def project_record(record):
    fields = FIELDS.get(record["kind"])
    if fields is None:
        return {"availability": "not_projected", "explanation": "This record kind has no diagnostic-view policy yet"}
    data = {key: value for key, value in record["body"].items() if key in fields}
    if "skills" in data:
        data["skills"] = {key: {name: value for name, value in skill.items() if name != "instructions"} for key, skill in data["skills"].items()}
    if "instructions" in record["body"]:
        data["instructionText"] = "Stored privately; the exact instruction hash is shown"
    if "returnedReasoning" in record["body"]:
        availability = record["body"]["returnedReasoning"].get("availability", "absent")
        data["returnedReasoning"] = {"availability": "absent" if availability == "absent" else "restricted"}
    if record["kind"] in {"executor_artifact", "mail_message"}:
        data["payloadAccess"] = "Saved result content is separate from this diagnostic overview"
    if record["kind"] == "executor_unaccepted_result":
        data["payloadAccess"] = "Raw result content remains outside this diagnostic overview"
        explanations = {
            "known": "An execution outcome was recorded, but its result was not accepted into the task.",
            "known_rejection": "The provider rejected the request; this is a known rejection, not an uncertain completed call.",
            "no_request": "No provider request was sent; execution stopped before dispatch.",
        }
        data["explanation"] = explanations.get(
            data.get("external_effect"),
            "A callback result was retained, but the external outcome is not verified and the task did not accept it."
        )
    safe, paths = redact(data)
    if paths:
        safe["redactedPaths"] = paths
    return safe
