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
from pydantic.json_schema import SkipJsonSchema

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


class SearchWebInput(WireModel):
    """One explicitly chosen round; this is not a lifetime research budget."""

    mode: Literal["quick", "research"] = Field(description="Quick web lookup: one targeted information gap. Research web search: complementary questions, comparison or counterevidence.")
    information_need: str = Field(min_length=1, max_length=1000, description="Brief information need, not private reasoning.")
    queries: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(min_length=1, max_length=10)
    provider: Literal["exa", "tavily", "searxng"] = Field(default="exa", description="Optional agent selection for an identified research gap. Default Exa; quick uses the configured fallback.")
    limit: int = Field(default=10, ge=1, le=10)
    refresh: bool = Field(default=False, description="Reuse identical provider/query/options within this mission using a separate operation for each peer. Set true to request fresh retrieval. Different terms, providers, modes or filters remain separate.")

    @model_validator(mode="after")
    def distinct_queries(self):
        keys = [" ".join(query.split()).casefold() for query in self.queries]
        if not self.information_need.strip() or any(not key for key in keys):
            raise ValueError("Search needs a nonempty purpose and queries")
        if len(keys) != len(set(keys)) or (self.mode == "quick" and len(keys) != 1):
            raise ValueError("Quick uses one query; research uses up to ten distinct queries")
        if self.mode == "quick" and self.provider != "exa":
            raise ValueError("Quick starts with Exa; configured Tavily fallback is automatic")
        return self


class LegacySearchWebInput(SearchWebInput):
    refresh: bool = Field(default=False, description="Reuse an identical saved search in this peer task by default. Set true to intentionally start fresh retrieval; different queries or options remain separate searches.")


class ReadSearchResultsInput(WireModel):
    operation_id: Identifier
    cursor: int = Field(default=0, ge=0, description="Saved candidate sequence; never a provider pagination offset.")
    limit: int = Field(default=10, ge=1, le=20)
    wait: bool = Field(default=False, description="Wait for new saved results without model polling when none are available.")


class CancelSearchInput(WireModel):
    operation_id: Identifier


class ReadResearchActivityInput(WireModel):
    snapshot_id: Identifier | None = None
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=20)


class EvidenceSourceRef(WireModel):
    kind: Literal["source_copy"]
    id: Identifier
    version: Version


class EvidenceFindingRef(WireModel):
    kind: Literal["report"]
    id: Identifier
    version: Version


class RetainEvidenceInput(WireModel):
    source_refs: list[EvidenceSourceRef] = Field(min_length=1, max_length=10)
    relevance_note: str = Field(min_length=1, max_length=1000)
    finding_refs: list[EvidenceFindingRef] = Field(default_factory=list, max_length=10)


class SearchEvidenceLibraryInput(WireModel):
    query: str | None = Field(default=None, min_length=1, max_length=500)
    entry_id: Identifier | None = None
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def search_or_select(self):
        if ((self.query is None) == (self.entry_id is None)
                or self.query is not None and not self.query.strip()):
            raise ValueError("Provide a nonempty query or select one returned entry_id")
        if self.entry_id and self.cursor:
            raise ValueError("Selecting an entry does not use a page cursor")
        return self


class FetchSourceInput(WireModel):
    hit_id: Identifier
    refresh: bool = Field(default=False, description="Request a fresh source version instead of reusing compatible saved bytes.")


class PublicDocumentRef(WireModel):
    kind: Literal["public_document_fetch"]
    id: Identifier
    version: Version


class SourceAssetRef(WireModel):
    kind: Literal["source_asset"]
    id: Identifier
    version: Version


class ListSourceAssetsInput(WireModel):
    source_id: Identifier
    source_version: Version
    offset: int = Field(default=0, ge=0, description="Zero-based offset in saved figure candidates.")
    limit: int = Field(default=10, ge=1, le=20, description="Maximum figure candidates returned in one tool result.")


class FetchSourceAssetInput(WireModel):
    asset_ref: SourceAssetRef = Field(description="Exact figure asset returned by list_source_assets; no arbitrary URL is accepted.")


class RenderSourceInput(WireModel):
    ordinary_document_ref: PublicDocumentRef = Field(description="Exact kind/id/version of this task's saved ordinary public_document_fetch.")
    reason: str = Field(min_length=1, max_length=500, description="What JavaScript-dependent content is missing from ordinary extraction.")


class OCRRegion(WireModel):
    """Selected rectangle on the rendered PDF page or EXIF-oriented image."""

    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)
    width: float = Field(ge=0, le=1, allow_inf_nan=False)
    height: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def within_page(self):
        if (self.width <= 0 or self.height <= 0
                or self.x + self.width > 1 or self.y + self.height > 1):
            raise ValueError("OCR region must have positive size within the selected page")
        return self


class ReadDocumentOCRInput(WireModel):
    document_ref: PublicDocumentRef
    page_number: int = Field(ge=1, le=200, description="One selected PDF page, or 1 for a saved PNG/JPEG. Local OCR; no model API call.")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=3000, ge=1, le=4000, description="Maximum OCR characters returned; use next_request to read more.")
    region: OCRRegion | None = Field(default=None, description="Optional rectangle x/y/width/height in 0–1 fractions from top-left of the rendered PDF page or EXIF-oriented image. Use known coordinates to isolate text; omitted reads the page.")
    layout: Literal["auto", "block", "sparse"] = Field(default="auto", description="Auto: whole-page layout, or one block for a region. Block: one text block. Sparse: separated labels, without reliable reading order. OCR is unverified transcription, not figure interpretation.")


class InspectFigureInput(WireModel):
    document_ref: PublicDocumentRef
    page_number: int = Field(ge=1, le=200)
    question: str = Field(min_length=1, max_length=1000, description="Specific visual question about this page or figure. Returns an unverified model interpretation, not a checked source passage.")


class SearchOperationResult(WireModel):
    schema_version: Literal["ora.web_search.result.v1"] = "ora.web_search.result.v1"
    operation_ref: dict[str, JsonValue]
    status: Literal["pending", "completed", "failed", "cancelled", "uncertain"]
    available_results: list[dict[str, JsonValue]]
    continuation_cursor: int = Field(ge=0)
    has_more: bool = Field(default=False, description="More results are already saved after this cursor; pending work may still add results later.")
    pending: int = Field(ge=0)
    failed: int = Field(ge=0)
    wait_requested: bool = False
    reuse_status: Literal["new", "reused", "refreshed"] | None = Field(default=None, description="For a search start, whether it created an operation, reused saved work, or explicitly refreshed it. The operation_ref identifies that work.")


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
    access_state: Literal["abstract_only", "full_text", "locator_only", "supplied_text", "navigation_text", "document_text"]
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
    allow_vision: bool = Field(default=False, description="Explicitly permit auxiliary Groq Qwen figure-reading calls using this run's selected credential profile.")

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
    allow_vision: bool = Field(default=False, description="Explicitly permit auxiliary Groq Qwen figure-reading calls for this follow-up; permission is not inherited.")

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


class LiveBranchCancelRequest(WireModel):
    """Trusted local operator request for one exact saved mission task."""

    request_id: Identifier
    run_id: Identifier
    task_id: Identifier
    expected_task_version: Version
    reason: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]


class LiveBranchCancelReceipt(WireModel):
    """Durable acceptance; an in-flight remote effect may remain unresolved."""

    request_id: Identifier
    run_id: Identifier
    task_id: Identifier
    status: Literal["cancelled", "cancellation_requested"]
    reused: bool = False
    cancellation_ref: SavedRecordRef
    task_ref: SavedRecordRef

    @model_validator(mode="after")
    def references_match_target(self):
        if self.cancellation_ref.kind != "mission_branch_cancellation":
            raise ValueError("Cancellation receipt needs its saved cancellation record")
        if self.task_ref.kind != "board_proposal" or self.task_ref.id != self.task_id:
            raise ValueError("Cancellation receipt task reference must name the exact task")
        return self


class LiveRunCancelRequest(WireModel):
    """Trusted local operator request to stop one exact saved run."""

    request_id: Identifier
    run_id: Identifier
    expected_run_version: Version
    reason: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]


class LiveRunCancelReceipt(WireModel):
    """Durable stop receipt; an in-flight external effect may remain unresolved."""

    request_id: Identifier
    run_id: Identifier
    status: Literal["cancelled", "cancellation_requested"]
    reused: bool = False
    cancellation_ref: SavedRecordRef
    run_ref: SavedRecordRef

    @model_validator(mode="after")
    def references_match_target(self):
        if self.cancellation_ref.kind != "mission_run_cancellation":
            raise ValueError("Cancellation receipt needs its saved run cancellation")
        if self.run_ref.kind != "mission_run" or self.run_ref.id != self.run_id:
            raise ValueError("Cancellation receipt must name the exact run")
        return self


class LiveUserInputRequest(WireModel):
    """Authenticated local input for one exact open task and agent instance."""

    request_id: Identifier
    run_id: Identifier
    task_id: Identifier
    target_agent_id: Identifier
    expected_task_version: Version
    kind: Literal["direction", "unverified_pasted_evidence"]
    text: Annotated[str, Field(min_length=1, max_length=100000, pattern=r"\S")]


class LiveUserInputReceipt(WireModel):
    """Durable acceptance only; subsequent context records establish delivery."""

    request_id: Identifier
    run_id: Identifier
    task_id: Identifier
    target_agent_id: Identifier
    status: Literal["queued_for_next_boundary"] = "queued_for_next_boundary"
    reused: bool = False
    input_ref: SavedRecordRef
    task_ref: SavedRecordRef

    @model_validator(mode="after")
    def references_match_target(self):
        if self.input_ref.kind != "mission_user_input":
            raise ValueError("User-input receipt needs its saved input record")
        if self.task_ref.kind != "board_proposal" or self.task_ref.id != self.task_id:
            raise ValueError("User-input receipt must name the exact task")
        return self


class LiveRunSummary(WireModel):
    id: Identifier
    version: Version | None = None
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
    discovery_excerpt: str | None = Field(
        default=None, max_length=300,
        description="Bounded search-provider teaser for discovery only; not an inspected passage or a source-use grant.",
    )
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
        if self.discovery_excerpt is not None and (
            self.access_surface != "teaser" or self.origin != "original_source"
            or self.rights_status in {"restricted", "not_retained"}
        ):
            raise ValueError("Discovery excerpts require an unrestricted search teaser")
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


class PlanModel(BaseModel):
    """Strict snake-case tool input; host ownership is never caller supplied."""

    model_config = ConfigDict(extra="forbid", strict=True)


PlanText = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
PlanLocalId = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]


class PlanReference(PlanModel):
    """An exact existing research record; citation shape does not prove use."""

    kind: Literal[
        "board_proposal", "board_dissent", "board_outcome", "mail_request",
        "mail_answer", "mail_message", "source_copy", "source_passage",
        "source_read_window", "evidence_packet", "report",
    ]
    id: Annotated[str, Field(min_length=1, max_length=160, pattern=r"\S")]
    version: Annotated[int, Field(ge=1)]


class PlanStep(PlanModel):
    id: PlanLocalId
    description: PlanText
    status: Literal["todo", "in_progress", "done", "blocked", "deferred"]
    depends_on: list[PlanLocalId] = Field(default_factory=list, max_length=24)
    linked_refs: list[PlanReference] = Field(default_factory=list, max_length=24)


class RuntimePlan(PlanModel):
    """Bounded optional continuation aid, with no task-completion authority."""

    goal: Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")]
    completion_criteria: list[PlanText] = Field(min_length=1, max_length=24)
    uncertainties: list[PlanText] = Field(default_factory=list, max_length=24)
    status: Literal["active", "paused", "completed", "cancelled"]
    steps: list[PlanStep] = Field(default_factory=list, max_length=24)
    linked_refs: list[PlanReference] = Field(default_factory=list, max_length=64)
    revision_reason: PlanText

    @model_validator(mode="after")
    def valid_step_graph(self):
        steps = {step.id: step for step in self.steps}
        if len(steps) != len(self.steps):
            raise ValueError("Plan step IDs must be unique")
        visiting, visited = set(), set()

        def visit(step_id):
            if step_id in visiting:
                raise ValueError("Plan step dependencies must be acyclic")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in steps[step_id].depends_on:
                if dependency not in steps:
                    raise ValueError("Plan dependency names an unavailable local step")
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in steps:
            visit(step_id)
        return self


class LegacySavePlanInput(PlanModel):
    """Historical create-or-replace contract retained for pinned v1 calls."""

    model_config = ConfigDict(extra="forbid", strict=True, title="SavePlanInput")

    plan: RuntimePlan
    expected_version: Annotated[int, Field(ge=0)] = 0


class SavePlanInput(PlanModel):
    """Create a checkpoint; later changes belong to modify_plan."""

    plan: RuntimePlan
    expected_version: Literal[0] = 0


class PlanStepUpdate(PlanModel):
    id: PlanLocalId
    description: PlanText | SkipJsonSchema[None] = None
    status: Literal["todo", "in_progress", "done", "blocked", "deferred"] | SkipJsonSchema[None] = None
    depends_on: Annotated[list[PlanLocalId], Field(max_length=24)] | SkipJsonSchema[None] = None
    linked_refs: Annotated[list[PlanReference], Field(max_length=24)] | SkipJsonSchema[None] = None

    @model_validator(mode="after")
    def explicit_changes(self):
        fields = self.model_fields_set - {"id"}
        if not fields or any(getattr(self, name) is None for name in fields):
            raise ValueError("A step update needs non-null changed fields; use [] to clear a list")
        return self


class PlanChanges(PlanModel):
    goal: Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")] | SkipJsonSchema[None] = None
    completion_criteria: Annotated[list[PlanText], Field(min_length=1, max_length=24)] | SkipJsonSchema[None] = None
    uncertainties: Annotated[list[PlanText], Field(max_length=24)] | SkipJsonSchema[None] = None
    status: Literal["active", "paused", "completed", "cancelled"] | SkipJsonSchema[None] = None
    linked_refs: Annotated[list[PlanReference], Field(max_length=64)] | SkipJsonSchema[None] = None
    update_steps: list[PlanStepUpdate] = Field(default_factory=list, max_length=24)
    add_steps: list[PlanStep] = Field(default_factory=list, max_length=24)
    remove_step_ids: list[PlanLocalId] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def unambiguous_changes(self):
        fields = self.model_fields_set
        if not fields or any(getattr(self, name) is None for name in fields):
            raise ValueError("Plan changes need explicit non-null fields; use [] to clear a list")
        step_fields = {"update_steps", "add_steps", "remove_step_ids"}
        if not fields - step_fields and not any(getattr(self, name) for name in step_fields):
            raise ValueError("Plan changes cannot be empty")
        ids = [step.id for step in self.update_steps + self.add_steps] + self.remove_step_ids
        if len(ids) != len(set(ids)):
            raise ValueError("Each step ID may occur in only one modification")
        return self


class ModifyPlanInput(PlanModel):
    """Apply a targeted edit to an exact existing plan revision."""

    expected_version: Annotated[int, Field(ge=1)]
    revision_reason: PlanText
    changes: PlanChanges


class ReadPlanInput(PlanModel):
    version: Annotated[int, Field(ge=1)] | None = None
