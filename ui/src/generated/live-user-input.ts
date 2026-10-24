/* Generated from contracts/generated/live_user_input.schema.json. Do not edit by hand. */
export type Expectedtaskversion = number;
export type Kind = "direction" | "unverified_pasted_evidence";
export type Requestid = string;
export type Runid = string;
export type Targetagentid = string;
export type Taskid = string;
export type Text = string;

/**
 * Authenticated local input for one exact open task and agent instance.
 */
export interface LiveUserInputRequest {
  expectedTaskVersion: Expectedtaskversion;
  kind: Kind;
  requestId: Requestid;
  runId: Runid;
  targetAgentId: Targetagentid;
  taskId: Taskid;
  text: Text;
}
