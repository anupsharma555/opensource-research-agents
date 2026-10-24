/* Generated from contracts/generated/read_research_activity.schema.json. Do not edit by hand. */
export type Cursor = number;
export type Limit = number;
export type Snapshotid = string | null;

export interface ReadResearchActivityInput {
  cursor?: Cursor;
  limit?: Limit;
  snapshotId?: Snapshotid;
}
