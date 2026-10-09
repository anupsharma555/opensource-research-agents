/* Generated from contracts/generated/live_followup.schema.json. Do not edit by hand. */
/**
 * @minItems 1
 */
export type Agentids = [string, ...string[]];
export type Parentrunid = string;
export type Parentsnapshotid = string;
export type Question = string;
export type Requestid = string;
export type Scope = string;
export type Targetid = string;
export type Targetversion = number;

export interface LiveFollowupRequest {
  agentIds: Agentids;
  parentRunId: Parentrunid;
  parentSnapshotId: Parentsnapshotid;
  question: Question;
  requestId: Requestid;
  scope?: Scope;
  targetId: Targetid;
  targetVersion: Targetversion;
}
