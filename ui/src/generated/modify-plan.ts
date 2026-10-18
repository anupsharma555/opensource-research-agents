/* Generated from contracts/generated/modify_plan.schema.json. Do not edit by hand. */
/**
 * @maxItems 24
 */
export type DependsOn = string[];
export type Description = string;
export type Id = string;
export type Id1 = string;
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
 * @maxItems 24
 */
export type LinkedRefs = PlanReference[];
export type Status = "todo" | "in_progress" | "done" | "blocked" | "deferred";
/**
 * @maxItems 24
 */
export type AddSteps = PlanStep[];
/**
 * @minItems 1
 * @maxItems 24
 */
export type CompletionCriteria = [string, ...string[]];
export type Goal = string;
/**
 * @maxItems 64
 */
export type LinkedRefs1 = PlanReference[];
/**
 * @maxItems 24
 */
export type RemoveStepIds = string[];
export type Status1 = "active" | "paused" | "completed" | "cancelled";
/**
 * @maxItems 24
 */
export type Uncertainties = string[];
/**
 * @maxItems 24
 */
export type DependsOn1 = string[];
export type Description1 = string;
export type Id2 = string;
/**
 * @maxItems 24
 */
export type LinkedRefs2 = PlanReference[];
export type Status2 = "todo" | "in_progress" | "done" | "blocked" | "deferred";
/**
 * @maxItems 24
 */
export type UpdateSteps = PlanStepUpdate[];
export type ExpectedVersion = number;
export type RevisionReason = string;

/**
 * Apply a targeted edit to an exact existing plan revision.
 */
export interface ModifyPlanInput {
  changes: PlanChanges;
  expected_version: ExpectedVersion;
  revision_reason: RevisionReason;
}
export interface PlanChanges {
  add_steps?: AddSteps;
  completion_criteria?: CompletionCriteria;
  goal?: Goal;
  linked_refs?: LinkedRefs1;
  remove_step_ids?: RemoveStepIds;
  status?: Status1;
  uncertainties?: Uncertainties;
  update_steps?: UpdateSteps;
}
export interface PlanStep {
  depends_on?: DependsOn;
  description: Description;
  id: Id;
  linked_refs?: LinkedRefs;
  status: Status;
}
/**
 * An exact existing research record; citation shape does not prove use.
 */
export interface PlanReference {
  id: Id1;
  kind: Kind;
  version: Version;
}
export interface PlanStepUpdate {
  depends_on?: DependsOn1;
  description?: Description1;
  id: Id2;
  linked_refs?: LinkedRefs2;
  status?: Status2;
}
