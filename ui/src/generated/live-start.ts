/* Generated from contracts/generated/live_start.schema.json. Do not edit by hand. */
/**
 * @minItems 1
 */
export type Agentids = [string, ...string[]];
/**
 * Explicitly permit auxiliary Groq Qwen figure-reading calls using this run's selected credential profile.
 */
export type Allowvision = boolean;
export type Credentialprofile = ("primary" | "backup") | null;
export type Evidencetext = string;
export type Question = string;
export type Requestid = string;
export type Requiredagentids = string[];
export type Scope = string;

export interface LiveStartRequest {
  agentIds: Agentids;
  allowVision?: Allowvision;
  credentialProfile?: Credentialprofile;
  evidenceText?: Evidencetext;
  question: Question;
  requestId: Requestid;
  requiredAgentIds?: Requiredagentids;
  scope?: Scope;
}
