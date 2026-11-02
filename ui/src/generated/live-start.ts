/* Generated from contracts/generated/live_start.schema.json. Do not edit by hand. */
/**
 * @minItems 1
 */
export type Agentids = [string, ...string[]];
export type Agentmodels = {
  [k: string]: LiveAgentModelChoice;
} | null;
/**
 * Optional per-alias model choice for a new research run.
 */
export type LiveAgentModelChoice = LiveAgentModelChoice1;
export type Maxcompletiontokens = number | null;
export type Model = "openai/gpt-oss-120b" | "qwen/qwen3.8-27b";
export type Reasoningeffort = ("low" | "medium" | "high") | null;
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
  agentModels?: Agentmodels;
  allowVision?: Allowvision;
  credentialProfile?: Credentialprofile;
  evidenceText?: Evidencetext;
  question: Question;
  requestId: Requestid;
  requiredAgentIds?: Requiredagentids;
  scope?: Scope;
}
export interface LiveAgentModelChoice1 {
  maxCompletionTokens?: Maxcompletiontokens;
  model: Model;
  reasoningEffort?: Reasoningeffort;
}
