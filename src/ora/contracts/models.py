"""Define the shared data shapes for requesting research work and inspecting results.

Commands, queries, events, receipts, jobs, and evidence references carry IDs,
versions, allowed states, and exact artifact or model details. Separate read-only
snapshot models describe what the sample Mac app displays: tasks, sources, claims,
reports, and diagnostics. These Python definitions reject unknown fields and are
the source for generated JSON rules and desktop TypeScript types. A valid record
shape does not authenticate its sender, persist a mission, or prove that a cited
passage supports a claim.
"""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator
from pydantic.alias_generators import to_camel

Identifier = Annotated[str, Field(min_length=1, max_length=160)]
Text = Annotated[str, Field(min_length=1)]
Version = Annotated[int, Field(ge=1)]
Latency = Literal["minimal", "medium", "long", "very_long"]
MissionStatus = Literal["active", "waiting", "completed", "cancelled"]
TaskStatus = Literal["queued", "running", "waiting", "completed", "failed", "cancelled", "deferred"]
AttemptStatus = Literal[
    "running", "waiting", "completed", "failed", "cancelled", "uncertain", "superseded"
]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)


class Actor(WireModel):
    id: Identifier
    kind: Literal["peer", "host", "user"]


class ArtifactRef(WireModel):
    id: Identifier
    version: Version
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class LatencyRecord(WireModel):
    category: Latency
    reason: Text
    elapsed_seconds: float = Field(default=0, ge=0)
    active_seconds: float = Field(default=0, ge=0)
    queue_seconds: float = Field(default=0, ge=0)
    provider_wait_seconds: float = Field(default=0, ge=0)
    peer_wait_seconds: float = Field(default=0, ge=0)
    retry_wait_seconds: float = Field(default=0, ge=0)
    first_useful_at: str | None = None
    last_progress_at: str | None = None
    next_action: Text


class WaitCondition(WireModel):
    kind: Literal["peer", "quota", "operation", "subscription"]
    reference_id: Identifier
    next_check_at: str | None = None
    wake_event: Text
    alternative_action: Text


class AttemptResources(WireModel):
    attempt_id: Identifier
    owner_token: Identifier
    generation: Version
    workspace: Text
    child_environment_names: list[str] = Field(default_factory=list)
    process_ids: list[int] = Field(default_factory=list)
    ports: list[int] = Field(default_factory=list)
    shared_access: Literal["read_only", "approved_transaction"] = "read_only"


class ModelSelection(WireModel):
    provider: Identifier
    model: Identifier
    reasoning_effort: str
    settings: dict[str, JsonValue] = Field(default_factory=dict)
    configuration_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    change_reason: str | None = None

    @model_validator(mode="after")
    def finite_settings(self):
        json.dumps(self.settings, allow_nan=False)
        return self

    @staticmethod
    def calculate_hash(provider: str, model: str, reasoning_effort: str, settings=None) -> str:
        value = {
            "provider": provider,
            "model": model,
            "reasoningEffort": reasoning_effort,
            "settings": settings or {},
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()


class JobRequest(WireModel):
    job_id: Identifier
    mission_id: Identifier
    task_id: Identifier
    attempt_id: Identifier
    operation_id: Identifier
    originating_model_call_id: Identifier
    tool_call_id: Identifier
    idempotency_key: Identifier
    owner_token: Identifier
    input_artifacts: list[ArtifactRef]
    expected_outputs: list[Text]
    intended_effects: list[Text]
    permitted_resources: list[Text]
    model_selection: ModelSelection
    request_timeout_seconds: float = Field(gt=0)
    lease_expires_at: Text
    mission_authorization_id: Identifier
    quota_wake_at: str | None = None


class OperationResult(WireModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"status": {"const": "uncertain"}},
                        "required": ["status"],
                    },
                    "then": {
                        "required": ["uncertaintySource"],
                        "properties": {"uncertaintySource": {"type": "string", "minLength": 1}},
                    },
                },
                {
                    "if": {"properties": {"status": {"const": "waiting"}}, "required": ["status"]},
                    "then": {
                        "required": ["nextCheck"],
                        "properties": {"nextCheck": {"type": "object"}},
                    },
                },
            ]
        }
    )
    operation_id: Identifier
    attempt_id: Identifier
    owner_token: Identifier
    status: Literal["running", "waiting", "completed", "failed", "cancelled", "uncertain"]
    result_artifacts: list[ArtifactRef] = Field(default_factory=list)
    progress: str
    next_check: WaitCondition | None = None
    uncertainty_source: str | None = None
    cancellation_requested: bool = False

    @model_validator(mode="after")
    def uncertain_has_source(self):
        if self.status == "uncertain" and not self.uncertainty_source:
            raise ValueError("an uncertain external outcome must identify the missing evidence")
        if self.status == "waiting" and self.next_check is None:
            raise ValueError("waiting requires a durable next-check/wake condition")
        return self


class EvidencePacket(WireModel):
    mission_id: Identifier | None = None
    id: Identifier
    version: Version
    kind: Literal["published_evidence", "checked_result"]
    artifact: ArtifactRef
    locators: list[Text] = Field(min_length=1)
    validation_record: Identifier
    access_surface: Literal["abstract", "full_text", "experiment_output"]
    summary: Text
    limitations: list[str] = Field(default_factory=list)


class ProposeTask(WireModel):
    kind: Literal["propose_task"]
    task_id: Identifier
    title: Text
    rationale: Text
    dependency_ids: list[Identifier] = Field(default_factory=list)
    latency: LatencyRecord


class Transition(WireModel):
    kind: Literal["transition"]
    entity_kind: Literal["mission", "branch", "task", "attempt", "report", "invitation", "request"]
    entity_id: Identifier
    target_state: Identifier
    reason: Text
    evidence: list[ArtifactRef] = Field(default_factory=list)
    terminal_authorization_id: str | None = None


class PeerMessage(WireModel):
    kind: Literal["peer_message"]
    message_id: Identifier
    recipient_id: Identifier
    message_type: Literal["request", "invitation", "response", "notification"]
    body: Text
    request_id: str | None = None
    in_reply_to: str | None = None
    evidence: list[ArtifactRef] = Field(default_factory=list)


class StartOperation(WireModel):
    kind: Literal["start_operation"]
    request: JobRequest


class FinishOperation(WireModel):
    kind: Literal["finish_operation"]
    result: OperationResult


Payload = Annotated[
    ProposeTask | Transition | PeerMessage | StartOperation | FinishOperation,
    Field(discriminator="kind"),
]


class Command(WireModel):
    schema_version: Literal["1.0"] = "1.0"
    command_id: Identifier
    actor: Actor
    mission_id: Identifier
    task_id: str | None = None
    expected_version: int = Field(ge=0)
    artifact_versions: list[ArtifactRef] = Field(default_factory=list)
    payload: Payload


class Query(WireModel):
    schema_version: Literal["1.0"] = "1.0"
    query_id: Identifier
    actor: Actor
    mission_id: Identifier
    task_id: str | None = None
    kind: Literal["snapshot", "events", "sources", "operation", "pending_requests"]
    after_cursor: int = Field(default=0, ge=0)
    operation_id: str | None = None


class Receipt(WireModel):
    command_id: Identifier
    status: Literal["accepted", "duplicate", "rejected", "deferred"]
    code: Identifier
    message: Text
    version: int = Field(ge=0)
    event_ids: list[Identifier] = Field(default_factory=list)
    repair: str | None = None


class Event(WireModel):
    schema_version: Literal["1.0"] = "1.0"
    id: Identifier
    sequence: int = Field(ge=1)
    actor: Actor
    mission_id: Identifier
    task_id: str | None = None
    command_id: Identifier
    entity_kind: Identifier
    entity_id: Identifier
    version: Version
    kind: Identifier
    state: Identifier
    reason: Text
    evidence: list[ArtifactRef] = Field(default_factory=list)


class Subscription(WireModel):
    id: Identifier
    actor: Actor
    mission_id: Identifier
    cursor: int = Field(ge=0)
    wake_kinds: list[Identifier]


# Read projection consumed by the desktop sample. Fields describe observations,
# not permission to execute. Runtime authority remains behind the service boundary.
class MissionView(WireModel):
    id: Identifier
    version: Version
    question: Text
    scope: str
    status: MissionStatus
    latency_class: Latency | None = None
    latency_basis: Text
    started_at: str
    report_artifact_id: str | None = None


class AgentView(WireModel):
    id: Identifier
    name: Text
    role: Text
    status: str


class TaskView(WireModel):
    id: Identifier
    display_number: Version | None = None
    version: Version | None = None
    parent_task_id: str | None = None
    target_artifact_id: str | None = None
    title: Text
    status: TaskStatus
    latency_class: Latency | None = None
    latency_basis: Text
    first_useful_at: str | None = None
    first_contribution_at: str | None = None
    active_or_waiting: str
    waiting_reason: str | None = None
    waiting_until: AwareDatetime | None = None
    waiting_operation_reference: "SavedRecordRef | None" = None
    attempt_ids: list[Identifier]


class ToolCallView(WireModel):
    name: Text
    provider: str | None = None
    status: str


class ReturnedReasoning(WireModel):
    availability: Literal["returned", "absent", "restricted", "truncated"]
    text: str | None = None


class AttemptView(WireModel):
    id: Identifier
    task_id: Identifier
    agent_id: Identifier
    status: AttemptStatus
    started_at: str
    ended_at: str | None = None
    tool_calls: list[ToolCallView]
    selected_model: str | None = None
    skill_versions: list[str]
    result_summary: str | None = None
    returned_reasoning: ReturnedReasoning


class MessageView(WireModel):
    id: Identifier
    request_id: str | None = None
    from_agent_id: Identifier
    to_agent_id: Identifier
    task_id: Identifier
    status: Literal["requested", "delivered", "acknowledged", "awaiting_review", "fulfilled"]
    summary: Text
    created_at: str
    updated_at: str | None = None
    source_id: str | None = None
    claim_id: str | None = None


class PassageView(WireModel):
    span_id: Identifier
    locator: Text
    text: Text


class SourceView(WireModel):
    id: Identifier
    copy_id: Identifier
    version: Version
    title: Text
    access_state: Literal["abstract_only", "full_text", "locator_only", "supplied_text"]
    retrieval_status: Text
    content_hash: str | None = None
    provider: str | None = None
    url: str | None = None
    passages: list[PassageView]


class Citation(WireModel):
    source_id: Identifier
    span_id: Identifier


class ClaimView(WireModel):
    id: Identifier
    version: Version
    text: Text
    disposition: Text
    supports: list[Citation]
    supersedes_version: int | None = None
    task_id: Identifier


class ArtifactView(WireModel):
    id: Identifier
    version: Version
    kind: Text
    title: Text
    body: Text
    citations: list[Citation]
    limitations: list[str]
    producer_attempt_id: Identifier | None = None


class TimelineView(WireModel):
    id: Identifier
    time: str
    kind: Text
    task_id: str | None = None
    agent_id: str | None = None
    summary: Text


class DiagnosticView(WireModel):
    id: Identifier
    task_id: str | None = None
    attempt_id: str | None = None
    kind: Text
    status: Text
    message: Text
    occurred_at: str


class SavedRecordRef(WireModel):
    kind: Text
    id: Identifier
    version: Version


class RecordInspection(WireModel):
    reference: SavedRecordRef
    label: Text
    data: dict


class SourceProvenanceView(WireModel):
    source_id: Identifier
    copy_reference: SavedRecordRef
    encounter_references: list[SavedRecordRef]
    origin: Literal["supplied", "retrieved", "reused"]
    from_snapshot_id: Identifier | None = None


class AttemptProvenanceView(WireModel):
    attempt_id: Identifier
    instance_reference: SavedRecordRef
    configuration_reference: SavedRecordRef | None = None
    call_references: list[SavedRecordRef] = Field(default_factory=list)
    input_references: list[SavedRecordRef] = Field(default_factory=list)
    accepted_output_references: list[SavedRecordRef] = Field(default_factory=list)
    explanation: Text


class ResearchStep(WireModel):
    """A readable account of a saved event, not private model reasoning."""

    id: Identifier
    sequence: int = Field(ge=1)
    recorded_at: Text
    actor_name: str | None = None
    action: Text
    summary: Text
    outcome: Text
    input_references: list[SavedRecordRef] = Field(default_factory=list)
    output_references: list[SavedRecordRef] = Field(default_factory=list)
    reason_summary: str | None = None
    next_action: str | None = None
    proof: Text


class SnapshotInspection(WireModel):
    """Backend-saved context for inspection, separate from reader-facing findings."""

    schema_version: Literal["1.0"] = "1.0"
    snapshot_reference: SavedRecordRef
    initial_source_ids: list[Identifier]
    sources: list[SourceProvenanceView]
    attempts: list[AttemptProvenanceView]
    records: list[RecordInspection]
    accepted_output_references: list[SavedRecordRef]
    followup_target: SavedRecordRef | None = None
    timing_basis: Literal["observed_local", "controlled_clock"]
    steps: list[ResearchStep] = Field(default_factory=list)


class ResearchSnapshot(WireModel):
    schema_version: Literal["1.0"] = "1.0"
    proof_kind: Literal[
        "synthetic_sample",
        "live_model_on_supplied_evidence",
        "offline_test",
        "live_mission_pending",
        "live_model_with_retrieval",
    ] = "synthetic_sample"
    parent_snapshot_id: Identifier | None = None
    input_question: str | None = None
    inspection: SnapshotInspection | None = None
    snapshot_id: Identifier
    sample_version: Text
    captured_at: str
    mission: MissionView
    agents: list[AgentView]
    tasks: list[TaskView]
    attempts: list[AttemptView]
    messages: list[MessageView]
    sources: list[SourceView]
    claims: list[ClaimView]
    artifacts: list[ArtifactView]
    events: list[TimelineView]
    diagnostics: list[DiagnosticView]


class LiveAgent(WireModel):
    id: Identifier
    name: Text
    role: Text
    skill_version: Text
    model: Text | None = None
    availability: Text | None = None
    unavailable_reason: str | None = None
    configuration_reference: SavedRecordRef | None = None
    role_template_id: Identifier | None = None


class LiveStartRequest(WireModel):
    request_id: Identifier
    question: Text
    scope: str = ""
    evidence_text: str = ""
    agent_ids: list[Identifier] = Field(min_length=1)
    required_agent_ids: list[Identifier] = Field(default_factory=list)
    credential_profile: Literal["primary", "backup"] | None = None

    @model_validator(mode="after")
    def required_agents_are_selected(self):
        if len(set(self.required_agent_ids)) != len(self.required_agent_ids) or set(
            self.required_agent_ids
        ) - set(self.agent_ids):
            raise ValueError("requiredAgentIds must be distinct starting agentIds")
        return self


class LiveFollowupRequest(WireModel):
    request_id: Identifier
    parent_run_id: Identifier
    parent_snapshot_id: Identifier
    target_id: Identifier
    target_version: Version
    question: Text
    scope: str = ""
    agent_ids: list[Identifier] = Field(min_length=1)
    required_agent_ids: list[Identifier] = Field(default_factory=list)
    credential_profile: Literal["primary", "backup"] | None = None

    @model_validator(mode="after")
    def required_agents_are_selected(self):
        if len(set(self.required_agent_ids)) != len(self.required_agent_ids) or set(
            self.required_agent_ids
        ) - set(self.agent_ids):
            raise ValueError("requiredAgentIds must be distinct starting agentIds")
        return self


class LiveReceipt(WireModel):
    request_id: Identifier
    run_id: Identifier
    task_id: Identifier
    status: Literal["queued"] = "queued"
    reused: bool = False


class LiveRunSummary(WireModel):
    id: Identifier
    question: Text
    status: Text
    parent_run_id: Identifier | None = None
    agent_ids: list[Identifier] = Field(default_factory=list)
    required_agent_ids: list[Identifier] | None = None
    participation_policy_version: Version | None = None
    continuing_task_ids: list[Identifier] | None = None
    requested_agent_names: dict[str, str] = Field(default_factory=dict)
    title: Text | None = None
    created_at: str | None = None
    last_progress_at: str | None = None
    completed_at: str | None = None
    waiting_reason: str | None = None
    workspace_label: str | None = None


class LiveState(WireModel):
    enabled: bool
    model: Text
    agents: list[LiveAgent]
    runs: list[LiveRunSummary]
    snapshot: ResearchSnapshot | None = None
    comparison_snapshot: ResearchSnapshot | None = None
    run_id: Identifier | None = None
    error: str | None = None
    workspace_id: Identifier | None = None
    workspace_label: str | None = None
    workflow_version: Text | None = None
    retrieval_routes: list[str] = Field(default_factory=list)


Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class BlobDescriptor(WireModel):
    sha256: Sha256
    size: int = Field(ge=0)


class RetrievalWindow(WireModel):
    offset: int | None = Field(default=None, ge=0)
    cursor: str | None = None
    limit: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def one_paging_mode(self):
        if self.offset is not None and self.cursor is not None:
            raise ValueError("offset and cursor cannot both be set")
        return self


class RetrievalContinuation(WireModel):
    action: Literal["continue", "stop", "switch_route"]
    reason: str = Field(min_length=1)


class RetrievalResultWindow(WireModel):
    returned_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    returned_offset: int | None = Field(default=None, ge=0)
    returned_cursor: str | None = None
    provider_total: int | None = Field(default=None, ge=0)  # None means unknown.
    known_unseen_count: int | None = Field(default=None, ge=0)
    more_available: Literal["yes", "no", "unknown"]
    next_window: RetrievalWindow | None = None
    provider_exposure_limit: int | None = Field(default=None, ge=0)
    provider_stop_reason: Literal[
        "exhausted", "more_available", "provider_limit", "quota_wait", "error", "unknown"
    ]
    continuation: RetrievalContinuation | None = None  # Peer choice, not provider status.
    window_errors: list[str] = Field(default_factory=list)


class RetrievalUsage(WireModel):
    unit: str = Field(min_length=1)  # Preserve provider-native unit names.
    amount: float = Field(ge=0)
    basis: Literal["observed", "documented"]


class RetrievalOmittedField(WireModel):
    """Explain a field omitted by the mapping without retaining its private value."""

    field: Text
    reason: Text


class RetrievalItem(WireModel):
    item_id: str = Field(min_length=1)  # Retrieval identity, never a study-family ID.
    rank: int = Field(gt=0)
    rank_scope: Literal["page", "global_offset", "provider_reported"]
    provider_item_id: str | None = None
    original_url: str | None = None  # Sanitized if token-bearing; see flag/hash.
    original_url_sha256: Sha256 | None = None
    url_redacted: bool
    title: str | None = None
    content: str | None = None  # Only retained if rights_status == permitted.
    content_sha256: Sha256 | None = None
    content_retained: bool
    content_unretained_reason: str | None = None
    access_surface: Literal[
        "metadata", "teaser", "abstract", "passage", "full_text", "provider_summary"
    ]
    origin: Literal["original_source", "provider_generated"]
    rights_status: Literal["unknown", "permitted", "restricted", "not_retained"]
    access_status: Literal["unknown", "available", "limited", "unavailable"]
    redacted_fields: list[str] = Field(default_factory=list)
    scientific_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    structured_content: dict[str, JsonValue] = Field(default_factory=dict)
    omitted_fields: list[RetrievalOmittedField] = Field(default_factory=list)

    @model_validator(mode="after")
    def finite_scientific_content(self):
        json.dumps([self.scientific_metadata, self.structured_content], allow_nan=False)
        return self


class RetrievalItemError(WireModel):
    position: int = Field(ge=0)
    code: Literal["invalid_item"]
    reason: str = Field(min_length=1)  # Controlled diagnostic, never raw payload.


class RetrievalPayloadRef(WireModel):
    blob: BlobDescriptor | None = None
    original_sha256: Sha256
    sanitized_sha256: Sha256
    transformation_version: str = Field(min_length=1)
    redacted_paths: list[str] = Field(default_factory=list)
    retained_projection: Literal["allowlisted_provider_fields", "mapped_scientific_fields"]
    original_retained: Literal[False] = False


class RetrievalRequest(WireModel):
    mission_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    delivery_id: str = Field(min_length=1)
    agent_instance_id: str | None = None
    route: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    index: str | None = None
    operation: Literal["search", "fetch"]
    query: str | None = None  # Exact after redaction; raw-query digest belongs in lineage.
    query_sha256: Sha256 | None = None
    filters: dict = Field(default_factory=dict)  # Sanitized before validation/persistence.
    requested_at: AwareDatetime  # Require timezone-aware in canonical validator.
    requested_window: RetrievalWindow
    resume_esearch_ref: SavedRecordRef | None = None


class RetrievalFailure(WireModel):
    category: Literal["provider_error", "quota_wait", "malformed_payload", "oversized_response"]
    code: Literal[
        "route_unavailable",
        "rate_limited",
        "unauthorized",
        "not_found",
        "timeout",
        "upstream_error",
        "unspecified",
    ]
    retry_after_seconds: float | None = Field(default=None, ge=0)


class RetrievalEnvelope(WireModel):
    schema_version: Literal["1.0"] = "1.0"
    mapping_version: Text = "legacy-allowlist-v1"
    request: RetrievalRequest
    retrieved_at: AwareDatetime  # Require timezone-aware in canonical validator.
    outcome: Literal[
        "empty", "success", "partial", "provider_failure", "item_failure", "malformed_payload",
        "oversized_response"
    ]
    failure: RetrievalFailure | None = None
    returned_window: RetrievalResultWindow
    items: list[RetrievalItem] = Field(default_factory=list)
    item_errors: list[RetrievalItemError] = Field(default_factory=list)
    usage: list[RetrievalUsage] = Field(default_factory=list)
    raw_payload_ref: RetrievalPayloadRef | None = None
    omitted_fields: list[RetrievalOmittedField] = Field(default_factory=list)

    @model_validator(mode="after")
    def item_counts(self):
        if self.returned_window.accepted_count != len(self.items):
            raise ValueError("accepted count must equal item list length")
        if self.returned_window.returned_count < len(self.items) + len(self.item_errors):
            raise ValueError("returned count cannot omit accepted or failed items")
        return self
