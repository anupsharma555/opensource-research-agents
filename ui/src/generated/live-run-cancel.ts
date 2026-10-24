/* Generated from contracts/generated/live_run_cancel.schema.json. Do not edit by hand. */
export type Expectedrunversion = number;
export type Reason = string;
export type Requestid = string;
export type Runid = string;

/**
 * Trusted local operator request to stop one exact saved run.
 */
export interface LiveRunCancelRequest {
  expectedRunVersion: Expectedrunversion;
  reason: Reason;
  requestId: Requestid;
  runId: Runid;
}
