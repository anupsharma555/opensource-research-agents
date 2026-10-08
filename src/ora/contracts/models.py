"""Define the fields and allowed values for research records shared across components.

Python validates these records, and generators turn the definitions into JSON rules
and desktop TypeScript types. The definitions describe an unreleased interface;
an actor ID inside a record does not authenticate its sender.
"""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

Identifier = Annotated[str, Field(min_length=1, max_length=160)]
Text = Annotated[str, Field(min_length=1)]
Version = Annotated[int, Field(ge=1)]
Latency = Literal["minimal", "medium", "long", "very_long"]
MissionStatus = Literal["active", "waiting", "completed", "cancelled"]
TaskStatus = Literal["queued", "running", "waiting", "completed", "failed", "cancelled"]
AttemptStatus = Literal["running", "waiting", "completed", "failed", "cancelled", "uncertain"]


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
    settings: dict[str, str | int | float | bool] = Field(default_factory=dict)
    configuration_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    change_reason: str | None = None

    @staticmethod
    def calculate_hash(provider: str, model: str, reasoning_effort: str, settings=None) -> str:
        value = {
            "provider": provider,
            "model": model,
            "reasoningEffort": reasoning_effort,
            "settings": settings or {},
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
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
    latency_class: Latency
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
    parent_task_id: str | None = None
    target_artifact_id: str | None = None
    title: Text
    status: TaskStatus
    latency_class: Latency
    latency_basis: Text
    first_useful_at: str | None = None
    active_or_waiting: str
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
    status: Literal["requested", "delivered", "acknowledged", "fulfilled"]
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
    access_state: Literal["abstract_only", "full_text", "locator_only"]
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


class ResearchSnapshot(WireModel):
    schema_version: Literal["1.0"] = "1.0"
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
