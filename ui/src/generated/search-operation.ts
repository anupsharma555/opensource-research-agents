/* Generated from contracts/generated/search_operation.schema.json. Do not edit by hand. */
export type JsonValue = unknown;
export type Availableresults = {
  [k: string]: JsonValue;
}[];
export type Continuationcursor = number;
export type Failed = number;
/**
 * More results are already saved after this cursor; pending work may still add results later.
 */
export type Hasmore = boolean;
export type Pending = number;
/**
 * For a search start, whether it created an operation, reused saved work, or explicitly refreshed it. The operation_ref identifies that work.
 */
export type Reusestatus = ("new" | "reused" | "refreshed") | null;
export type Schemaversion = "ora.web_search.result.v1";
export type Status = "pending" | "completed" | "failed" | "cancelled" | "uncertain";
export type Waitrequested = boolean;

export interface SearchOperationResult {
  availableResults: Availableresults;
  continuationCursor: Continuationcursor;
  failed: Failed;
  hasMore?: Hasmore;
  operationRef: Operationref;
  pending: Pending;
  reuseStatus?: Reusestatus;
  schemaVersion?: Schemaversion;
  status: Status;
  waitRequested?: Waitrequested;
}
export interface Operationref {
  [k: string]: JsonValue;
}
