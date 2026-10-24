/* Generated from contracts/generated/live_state.schema.json. Do not edit by hand. */
export type Availability = string | null;
export type Id = string;
export type Kind = string;
export type Version = number;
export type Id1 = string;
export type Model = string | null;
export type Name = string;
export type Role = string;
export type Roletemplateid = string | null;
export type Skillversion = string;
export type Unavailablereason = string | null;
export type Agents = LiveAgent[];
export type Id2 = string;
export type Name1 = string;
export type Role1 = string;
export type Status = string;
export type Agents1 = AgentView[];
export type Body = string;
export type Sourceid = string;
export type Spanid = string;
export type Citations = Citation[];
export type Id3 = string;
export type Kind1 = string;
export type Limitations = string[];
export type Producerattemptid = string | null;
export type Title = string;
export type Version1 = number;
export type Artifacts = ArtifactView[];
export type Agentid = string;
export type Endedat = string | null;
export type Id4 = string;
export type Resultsummary = string | null;
export type Availability1 = "returned" | "absent" | "restricted" | "truncated";
export type Text = string | null;
export type Selectedmodel = string | null;
export type Skillversions = string[];
export type Startedat = string;
export type Status1 = "running" | "waiting" | "completed" | "failed" | "cancelled" | "uncertain" | "superseded";
export type Taskid = string;
export type Name2 = string;
export type Provider = string | null;
export type Status2 = string;
export type Toolcalls = ToolCallView[];
export type Attempts = AttemptView[];
export type Capturedat = string;
export type Disposition = string;
export type Id5 = string;
export type Supersedesversion = number | null;
export type Supports = Citation[];
export type Taskid1 = string;
export type Text1 = string;
export type Version2 = number;
export type Claims = ClaimView[];
export type Attemptid = string | null;
export type Id6 = string;
export type Kind2 = string;
export type Message = string;
export type Occurredat = string;
export type Status3 = string;
export type Taskid2 = string | null;
export type Diagnostics = DiagnosticView[];
export type Agentid1 = string | null;
export type Id7 = string;
export type Kind3 = string;
export type Summary = string;
export type Taskid3 = string | null;
export type Time = string;
export type Events = TimelineView[];
export type Inputquestion = string | null;
export type Acceptedoutputreferences = SavedRecordRef[];
export type Acceptedoutputreferences1 = SavedRecordRef[];
export type Attemptid1 = string;
export type Callreferences = SavedRecordRef[];
export type Explanation = string;
export type Inputreferences = SavedRecordRef[];
export type Attempts1 = AttemptProvenanceView[];
export type Initialsourceids = string[];
export type Label = string;
export type Records = RecordInspection[];
export type Schemaversion = "1.0";
export type Encounterreferences = SavedRecordRef[];
export type Fromsnapshotid = string | null;
export type Origin = "supplied" | "retrieved" | "reused";
export type Sourceid1 = string;
export type Sources = SourceProvenanceView[];
export type Action = string;
export type Actorname = string | null;
export type Id8 = string;
export type Inputreferences1 = SavedRecordRef[];
export type Nextaction = string | null;
export type Outcome = string;
export type Outputreferences = SavedRecordRef[];
export type Proof = string;
export type Reasonsummary = string | null;
export type Recordedat = string;
export type Sequence = number;
export type Summary1 = string;
export type Steps = ResearchStep[];
export type Timingbasis = "observed_local" | "controlled_clock";
export type Claimid = string | null;
export type Createdat = string;
export type Fromagentid = string;
export type Id9 = string;
export type Requestid = string | null;
export type Sourceid2 = string | null;
export type Status4 = "requested" | "delivered" | "acknowledged" | "awaiting_review" | "fulfilled";
export type Summary2 = string;
export type Taskid4 = string;
export type Toagentid = string;
export type Updatedat = string | null;
export type Messages = MessageView[];
export type Id10 = string;
export type Latencybasis = string;
export type Latencyclass = ("minimal" | "medium" | "long" | "very_long") | null;
export type Question = string;
export type Reportartifactid = string | null;
export type Scope = string;
export type Startedat1 = string;
export type Status5 = "active" | "waiting" | "completed" | "cancelled";
export type Version3 = number;
export type Parentsnapshotid = string | null;
export type Proofkind =
  | "synthetic_sample"
  | "live_model_on_supplied_evidence"
  | "offline_test"
  | "live_mission_pending"
  | "live_model_with_retrieval";
export type Sampleversion = string;
export type Schemaversion1 = "1.0";
export type Snapshotid = string;
export type Accessstate =
  "abstract_only" | "full_text" | "locator_only" | "supplied_text" | "navigation_text" | "document_text";
export type Contenthash = string | null;
export type Copyid = string;
export type Id11 = string;
export type Locator = string;
export type Spanid1 = string;
export type Text2 = string;
export type Passages = PassageView[];
export type Provider1 = string | null;
export type Retrievalstatus = string;
export type Title1 = string;
export type Url = string | null;
export type Version4 = number;
export type Sources1 = SourceView[];
export type Activeorwaiting = string;
export type Attemptids = string[];
export type Displaynumber = number | null;
export type Firstcontributionat = string | null;
export type Firstusefulat = string | null;
export type Id12 = string;
export type Latencybasis1 = string;
export type Latencyclass1 = ("minimal" | "medium" | "long" | "very_long") | null;
export type Parenttaskid = string | null;
export type Status6 = "queued" | "running" | "waiting" | "completed" | "failed" | "cancelled" | "deferred";
export type Targetartifactid = string | null;
export type Title2 = string;
export type Version5 = number | null;
export type Waitingreason = string | null;
export type Waitinguntil = string | null;
export type Tasks = TaskView[];
export type Enabled = boolean;
export type Error = string | null;
export type Model1 = string;
export type Retrievalroutes = string[];
export type Runid = string | null;
export type Agentids = string[];
export type Completedat = string | null;
export type Continuingtaskids = string[] | null;
export type Createdat1 = string | null;
export type Id13 = string;
export type Lastprogressat = string | null;
export type Parentrunid = string | null;
export type Participationpolicyversion = number | null;
export type Question1 = string;
export type Requiredagentids = string[] | null;
export type Status7 = string;
export type Title3 = string | null;
export type Version6 = number | null;
export type Waitingreason1 = string | null;
export type Workspacelabel = string | null;
export type Runs = LiveRunSummary[];
export type Workflowversion = string | null;
export type Workspaceid = string | null;
export type Workspacelabel1 = string | null;

export interface LiveState {
  agents: Agents;
  comparisonSnapshot?: ResearchSnapshot | null;
  enabled: Enabled;
  error?: Error;
  model: Model1;
  retrievalRoutes?: Retrievalroutes;
  runId?: Runid;
  runs: Runs;
  snapshot?: ResearchSnapshot | null;
  workflowVersion?: Workflowversion;
  workspaceId?: Workspaceid;
  workspaceLabel?: Workspacelabel1;
}
export interface LiveAgent {
  availability?: Availability;
  configurationReference?: SavedRecordRef | null;
  id: Id1;
  model?: Model;
  name: Name;
  role: Role;
  roleTemplateId?: Roletemplateid;
  skillVersion: Skillversion;
  unavailableReason?: Unavailablereason;
}
export interface SavedRecordRef {
  id: Id;
  kind: Kind;
  version: Version;
}
export interface ResearchSnapshot {
  agents: Agents1;
  artifacts: Artifacts;
  attempts: Attempts;
  capturedAt: Capturedat;
  claims: Claims;
  diagnostics: Diagnostics;
  events: Events;
  inputQuestion?: Inputquestion;
  inspection?: SnapshotInspection | null;
  messages: Messages;
  mission: MissionView;
  parentSnapshotId?: Parentsnapshotid;
  proofKind?: Proofkind;
  sampleVersion: Sampleversion;
  schemaVersion?: Schemaversion1;
  snapshotId: Snapshotid;
  sources: Sources1;
  tasks: Tasks;
}
export interface AgentView {
  id: Id2;
  name: Name1;
  role: Role1;
  status: Status;
}
export interface ArtifactView {
  body: Body;
  citations: Citations;
  id: Id3;
  kind: Kind1;
  limitations: Limitations;
  producerAttemptId?: Producerattemptid;
  title: Title;
  version: Version1;
}
export interface Citation {
  sourceId: Sourceid;
  spanId: Spanid;
}
export interface AttemptView {
  agentId: Agentid;
  endedAt?: Endedat;
  id: Id4;
  resultSummary?: Resultsummary;
  returnedReasoning: ReturnedReasoning;
  selectedModel?: Selectedmodel;
  skillVersions: Skillversions;
  startedAt: Startedat;
  status: Status1;
  taskId: Taskid;
  toolCalls: Toolcalls;
}
export interface ReturnedReasoning {
  availability: Availability1;
  text?: Text;
}
export interface ToolCallView {
  name: Name2;
  provider?: Provider;
  status: Status2;
}
export interface ClaimView {
  disposition: Disposition;
  id: Id5;
  supersedesVersion?: Supersedesversion;
  supports: Supports;
  taskId: Taskid1;
  text: Text1;
  version: Version2;
}
export interface DiagnosticView {
  attemptId?: Attemptid;
  id: Id6;
  kind: Kind2;
  message: Message;
  occurredAt: Occurredat;
  status: Status3;
  taskId?: Taskid2;
}
export interface TimelineView {
  agentId?: Agentid1;
  id: Id7;
  kind: Kind3;
  summary: Summary;
  taskId?: Taskid3;
  time: Time;
}
/**
 * Backend-saved context for inspection, separate from reader-facing findings.
 */
export interface SnapshotInspection {
  acceptedOutputReferences: Acceptedoutputreferences;
  attempts: Attempts1;
  followupTarget?: SavedRecordRef | null;
  initialSourceIds: Initialsourceids;
  records: Records;
  schemaVersion?: Schemaversion;
  snapshotReference: SavedRecordRef;
  sources: Sources;
  steps?: Steps;
  timingBasis: Timingbasis;
}
export interface AttemptProvenanceView {
  acceptedOutputReferences?: Acceptedoutputreferences1;
  attemptId: Attemptid1;
  callReferences?: Callreferences;
  configurationReference?: SavedRecordRef | null;
  explanation: Explanation;
  inputReferences?: Inputreferences;
  instanceReference: SavedRecordRef;
}
export interface RecordInspection {
  data: Data;
  label: Label;
  reference: SavedRecordRef;
}
export interface Data {
  [k: string]: unknown;
}
export interface SourceProvenanceView {
  copyReference: SavedRecordRef;
  encounterReferences: Encounterreferences;
  fromSnapshotId?: Fromsnapshotid;
  origin: Origin;
  sourceId: Sourceid1;
}
/**
 * A readable account of a saved event, not private model reasoning.
 */
export interface ResearchStep {
  action: Action;
  actorName?: Actorname;
  id: Id8;
  inputReferences?: Inputreferences1;
  nextAction?: Nextaction;
  outcome: Outcome;
  outputReferences?: Outputreferences;
  proof: Proof;
  reasonSummary?: Reasonsummary;
  recordedAt: Recordedat;
  sequence: Sequence;
  summary: Summary1;
}
export interface MessageView {
  claimId?: Claimid;
  createdAt: Createdat;
  fromAgentId: Fromagentid;
  id: Id9;
  requestId?: Requestid;
  sourceId?: Sourceid2;
  status: Status4;
  summary: Summary2;
  taskId: Taskid4;
  toAgentId: Toagentid;
  updatedAt?: Updatedat;
}
export interface MissionView {
  id: Id10;
  latencyBasis: Latencybasis;
  latencyClass?: Latencyclass;
  question: Question;
  reportArtifactId?: Reportartifactid;
  scope: Scope;
  startedAt: Startedat1;
  status: Status5;
  version: Version3;
}
export interface SourceView {
  accessState: Accessstate;
  contentHash?: Contenthash;
  copyId: Copyid;
  id: Id11;
  passages: Passages;
  provider?: Provider1;
  retrievalStatus: Retrievalstatus;
  title: Title1;
  url?: Url;
  version: Version4;
}
export interface PassageView {
  locator: Locator;
  spanId: Spanid1;
  text: Text2;
}
export interface TaskView {
  activeOrWaiting: Activeorwaiting;
  attemptIds: Attemptids;
  displayNumber?: Displaynumber;
  firstContributionAt?: Firstcontributionat;
  firstUsefulAt?: Firstusefulat;
  id: Id12;
  latencyBasis: Latencybasis1;
  latencyClass?: Latencyclass1;
  parentTaskId?: Parenttaskid;
  status: Status6;
  targetArtifactId?: Targetartifactid;
  title: Title2;
  version?: Version5;
  waitingOperationReference?: SavedRecordRef | null;
  waitingReason?: Waitingreason;
  waitingUntil?: Waitinguntil;
}
export interface LiveRunSummary {
  agentIds?: Agentids;
  completedAt?: Completedat;
  continuingTaskIds?: Continuingtaskids;
  createdAt?: Createdat1;
  id: Id13;
  lastProgressAt?: Lastprogressat;
  parentRunId?: Parentrunid;
  participationPolicyVersion?: Participationpolicyversion;
  question: Question1;
  requestedAgentNames?: Requestedagentnames;
  requiredAgentIds?: Requiredagentids;
  status: Status7;
  title?: Title3;
  version?: Version6;
  waitingReason?: Waitingreason1;
  workspaceLabel?: Workspacelabel;
}
export interface Requestedagentnames {
  [k: string]: string;
}
