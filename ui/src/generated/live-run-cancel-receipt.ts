/* Generated from contracts/generated/live_run_cancel_receipt.schema.json. Do not edit by hand. */
export type Id = string;
export type Kind = string;
export type Version = number;
export type Requestid = string;
export type Reused = boolean;
export type Runid = string;
export type Status = "cancelled" | "cancellation_requested";

/**
 * Durable stop receipt; an in-flight external effect may remain unresolved.
 */
export interface LiveRunCancelReceipt {
  cancellationRef: SavedRecordRef;
  requestId: Requestid;
  reused?: Reused;
  runId: Runid;
  runRef: SavedRecordRef;
  status: Status;
}
export interface SavedRecordRef {
  id: Id;
  kind: Kind;
  version: Version;
}
