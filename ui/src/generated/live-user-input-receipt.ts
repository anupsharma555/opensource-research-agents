/* Generated from contracts/generated/live_user_input_receipt.schema.json. Do not edit by hand. */
export type Id = string;
export type Kind = string;
export type Version = number;
export type Requestid = string;
export type Reused = boolean;
export type Runid = string;
export type Status = "queued_for_next_boundary";
export type Targetagentid = string;
export type Taskid = string;

/**
 * Durable acceptance only; subsequent context records establish delivery.
 */
export interface LiveUserInputReceipt {
  inputRef: SavedRecordRef;
  requestId: Requestid;
  reused?: Reused;
  runId: Runid;
  status?: Status;
  targetAgentId: Targetagentid;
  taskId: Taskid;
  taskRef: SavedRecordRef;
}
export interface SavedRecordRef {
  id: Id;
  kind: Kind;
  version: Version;
}
