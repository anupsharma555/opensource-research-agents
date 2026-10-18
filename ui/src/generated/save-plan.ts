/* Generated from contracts/generated/save_plan.schema.json. Do not edit by hand. */
export type ExpectedVersion = 0;
/**
 * @minItems 1
 * @maxItems 24
 */
export type CompletionCriteria = [string, ...string[]];
export type Goal = string;
export type Id = string;
export type Kind =
  | "board_proposal"
  | "board_dissent"
  | "board_outcome"
  | "mail_request"
  | "mail_answer"
  | "mail_message"
  | "source_copy"
  | "source_passage"
  | "source_read_window"
  | "evidence_packet"
  | "report";
export type Version = number;
/**
 * @maxItems 64
 */
export type LinkedRefs = PlanReference[];
export type RevisionReason = string;
export type Status = "active" | "paused" | "completed" | "cancelled";
/**
 * @maxItems 24
 */
export type DependsOn = string[];
export type Description = string;
export type Id1 = string;
/**
 * @maxItems 24
 */
export type LinkedRefs1 = PlanReference[];
export type Status1 = "todo" | "in_progress" | "done" | "blocked" | "deferred";
/**
 * @maxItems 24
 */
export type Steps = PlanStep[];
/**
 * @maxItems 24
 */
export type Uncertainties = string[];

/**
 * Create a checkpoint; later changes belong to modify_plan.
 */
export interface SavePlanInput {
  expected_version?: ExpectedVersion;
  plan: RuntimePlan;
}
/**
 * Bounded optional continuation aid, with no task-completion authority.
 */
export interface RuntimePlan {
  completion_criteria: CompletionCriteria;
  goal: Goal;
  linked_refs?: LinkedRefs;
  revision_reason: RevisionReason;
  status: Status;
  steps?: Steps;
  uncertainties?: Uncertainties;
}
/**
 * An exact existing research record; citation shape does not prove use.
 */
export interface PlanReference {
  id: Id;
  kind: Kind;
  version: Version;
}
export interface PlanStep {
  depends_on?: DependsOn;
  description: Description;
  id: Id1;
  linked_refs?: LinkedRefs1;
  status: Status1;
}
