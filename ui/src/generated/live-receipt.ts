/* Generated from contracts/generated/live_receipt.schema.json. Do not edit by hand. */
export type Requestid = string;
export type Reused = boolean;
export type Runid = string;
export type Status = "queued";
export type Taskid = string;

export interface LiveReceipt {
  requestId: Requestid;
  reused?: Reused;
  runId: Runid;
  status?: Status;
  taskId: Taskid;
}
