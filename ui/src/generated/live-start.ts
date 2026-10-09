/* Generated from contracts/generated/live_start.schema.json. Do not edit by hand. */
/**
 * @minItems 1
 */
export type Agentids = [string, ...string[]];
export type Evidencetext = string;
export type Question = string;
export type Requestid = string;
export type Scope = string;

export interface LiveStartRequest {
  agentIds: Agentids;
  evidenceText: Evidencetext;
  question: Question;
  requestId: Requestid;
  scope?: Scope;
}
