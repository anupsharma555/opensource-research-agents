/* Generated from contracts/generated/live_branch_cancel_receipt.schema.json. Do not edit by hand. */
export type Id = string;
export type Kind = string;
export type Version = number;
export type Requestid = string;
export type Reused = boolean;
export type Runid = string;
export type Status = "cancelled" | "cancellation_requested";
export type Taskid = string;

/**
 * Durable acceptance; an in-flight remote effect may remain unresolved.
 */
export interface LiveBranchCancelReceipt {
  cancellationRef: SavedRecordRef;
  requestId: Requestid;
  reused?: Reused;
  runId: Runid;
  status: Status;
  taskId: Taskid;
  taskRef: SavedRecordRef;
}
export interface SavedRecordRef {
  id: Id;
  kind: Kind;
  version: Version;
}
