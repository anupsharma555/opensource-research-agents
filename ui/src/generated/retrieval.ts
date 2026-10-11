/* Generated from contracts/generated/retrieval.schema.json. Do not edit by hand. */
export type Category = "provider_error" | "quota_wait" | "malformed_payload" | "oversized_response";
export type Code =
  "route_unavailable" | "rate_limited" | "unauthorized" | "not_found" | "timeout" | "upstream_error" | "unspecified";
export type Retryafterseconds = number | null;
export type Code1 = "invalid_item";
export type Position = number;
export type Reason = string;
export type Itemerrors = RetrievalItemError[];
export type Accessstatus = "unknown" | "available" | "limited" | "unavailable";
export type Accesssurface = "metadata" | "teaser" | "abstract" | "passage" | "full_text" | "provider_summary";
export type Content = string | null;
export type Contentretained = boolean;
export type Contentsha256 = string | null;
export type Contentunretainedreason = string | null;
export type Itemid = string;
export type Field = string;
export type Reason1 = string;
export type Omittedfields = RetrievalOmittedField[];
export type Origin = "original_source" | "provider_generated";
export type Originalurl = string | null;
export type Originalurlsha256 = string | null;
export type Provideritemid = string | null;
export type Rank = number;
export type Rankscope = "page" | "global_offset" | "provider_reported";
export type Redactedfields = string[];
export type Rightsstatus = "unknown" | "permitted" | "restricted" | "not_retained";
export type JsonValue = unknown;
export type Title = string | null;
export type Urlredacted = boolean;
export type Items = RetrievalItem[];
export type Mappingversion = string;
export type Omittedfields1 = RetrievalOmittedField[];
export type Outcome =
  "empty" | "success" | "partial" | "provider_failure" | "item_failure" | "malformed_payload" | "oversized_response";
export type Sha256 = string;
export type Size = number;
export type Originalretained = false;
export type Originalsha256 = string;
export type Redactedpaths = string[];
export type Retainedprojection = "allowlisted_provider_fields" | "mapped_scientific_fields";
export type Sanitizedsha256 = string;
export type Transformationversion = string;
export type Agentinstanceid = string | null;
export type Attemptid = string;
export type Deliveryid = string;
export type Index = string | null;
export type Missionid = string;
export type Operation = "search" | "fetch";
export type Provider = string;
export type Query = string | null;
export type Querysha256 = string | null;
export type Requestid = string;
export type Requestedat = string;
export type Cursor = string | null;
export type Limit = number | null;
export type Offset = number | null;
export type Id = string;
export type Kind = string;
export type Version = number;
export type Route = string;
export type Taskid = string;
export type Retrievedat = string;
export type Acceptedcount = number;
export type Action = "continue" | "stop" | "switch_route";
export type Reason2 = string;
export type Knownunseencount = number | null;
export type Moreavailable = "yes" | "no" | "unknown";
export type Providerexposurelimit = number | null;
export type Providerstopreason = "exhausted" | "more_available" | "provider_limit" | "quota_wait" | "error" | "unknown";
export type Providertotal = number | null;
export type Returnedcount = number;
export type Returnedcursor = string | null;
export type Returnedoffset = number | null;
export type Windowerrors = string[];
export type Schemaversion = "1.0";
export type Amount = number;
export type Basis = "observed" | "documented";
export type Unit = string;
export type Usage = RetrievalUsage[];

export interface RetrievalEnvelope {
  failure?: RetrievalFailure | null;
  itemErrors?: Itemerrors;
  items?: Items;
  mappingVersion?: Mappingversion;
  omittedFields?: Omittedfields1;
  outcome: Outcome;
  rawPayloadRef?: RetrievalPayloadRef | null;
  request: RetrievalRequest;
  retrievedAt: Retrievedat;
  returnedWindow: RetrievalResultWindow;
  schemaVersion?: Schemaversion;
  usage?: Usage;
}
export interface RetrievalFailure {
  category: Category;
  code: Code;
  retryAfterSeconds?: Retryafterseconds;
}
export interface RetrievalItemError {
  code: Code1;
  position: Position;
  reason: Reason;
}
export interface RetrievalItem {
  accessStatus: Accessstatus;
  accessSurface: Accesssurface;
  content?: Content;
  contentRetained: Contentretained;
  contentSha256?: Contentsha256;
  contentUnretainedReason?: Contentunretainedreason;
  itemId: Itemid;
  omittedFields?: Omittedfields;
  origin: Origin;
  originalUrl?: Originalurl;
  originalUrlSha256?: Originalurlsha256;
  providerItemId?: Provideritemid;
  rank: Rank;
  rankScope: Rankscope;
  redactedFields?: Redactedfields;
  rightsStatus: Rightsstatus;
  scientificMetadata?: Scientificmetadata;
  structuredContent?: Structuredcontent;
  title?: Title;
  urlRedacted: Urlredacted;
}
/**
 * Explain a field omitted by the mapping without retaining its private value.
 */
export interface RetrievalOmittedField {
  field: Field;
  reason: Reason1;
}
export interface Scientificmetadata {
  [k: string]: JsonValue;
}
export interface Structuredcontent {
  [k: string]: JsonValue;
}
export interface RetrievalPayloadRef {
  blob?: BlobDescriptor | null;
  originalRetained?: Originalretained;
  originalSha256: Originalsha256;
  redactedPaths?: Redactedpaths;
  retainedProjection: Retainedprojection;
  sanitizedSha256: Sanitizedsha256;
  transformationVersion: Transformationversion;
}
export interface BlobDescriptor {
  sha256: Sha256;
  size: Size;
}
export interface RetrievalRequest {
  agentInstanceId?: Agentinstanceid;
  attemptId: Attemptid;
  deliveryId: Deliveryid;
  filters?: Filters;
  index?: Index;
  missionId: Missionid;
  operation: Operation;
  provider: Provider;
  query?: Query;
  querySha256?: Querysha256;
  requestId: Requestid;
  requestedAt: Requestedat;
  requestedWindow: RetrievalWindow;
  resumeEsearchRef?: SavedRecordRef | null;
  route: Route;
  taskId: Taskid;
}
export interface Filters {
  [k: string]: unknown;
}
export interface RetrievalWindow {
  cursor?: Cursor;
  limit?: Limit;
  offset?: Offset;
}
export interface SavedRecordRef {
  id: Id;
  kind: Kind;
  version: Version;
}
export interface RetrievalResultWindow {
  acceptedCount: Acceptedcount;
  continuation?: RetrievalContinuation | null;
  knownUnseenCount?: Knownunseencount;
  moreAvailable: Moreavailable;
  nextWindow?: RetrievalWindow | null;
  providerExposureLimit?: Providerexposurelimit;
  providerStopReason: Providerstopreason;
  providerTotal?: Providertotal;
  returnedCount: Returnedcount;
  returnedCursor?: Returnedcursor;
  returnedOffset?: Returnedoffset;
  windowErrors?: Windowerrors;
}
export interface RetrievalContinuation {
  action: Action;
  reason: Reason2;
}
export interface RetrievalUsage {
  amount: Amount;
  basis: Basis;
  unit: Unit;
}
