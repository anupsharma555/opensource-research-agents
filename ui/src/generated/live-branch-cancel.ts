/* Generated from contracts/generated/live_branch_cancel.schema.json. Do not edit by hand. */
export type Expectedtaskversion = number;
export type Reason = string;
export type Requestid = string;
export type Runid = string;
export type Taskid = string;

/**
 * Trusted local operator request for one exact saved mission task.
 */
export interface LiveBranchCancelRequest {
  expectedTaskVersion: Expectedtaskversion;
  reason: Reason;
  requestId: Requestid;
  runId: Runid;
  taskId: Taskid;
}
