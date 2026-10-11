/* Generated from contracts/generated/live_followup.schema.json. Do not edit by hand. */
/**
 * @minItems 1
 */
export type Agentids = [string, ...string[]];
export type Credentialprofile = ("primary" | "backup") | null;
export type Parentrunid = string;
export type Parentsnapshotid = string;
export type Question = string;
export type Requestid = string;
export type Requiredagentids = string[];
export type Scope = string;
export type Targetid = string;
export type Targetversion = number;

export interface LiveFollowupRequest {
  agentIds: Agentids;
  credentialProfile?: Credentialprofile;
  parentRunId: Parentrunid;
  parentSnapshotId: Parentsnapshotid;
  question: Question;
  requestId: Requestid;
  requiredAgentIds?: Requiredagentids;
  scope?: Scope;
  targetId: Targetid;
  targetVersion: Targetversion;
}
