/* Generated from contracts/generated/read_search_results.schema.json. Do not edit by hand. */
/**
 * Saved candidate sequence; never a provider pagination offset.
 */
export type Cursor = number;
export type Limit = number;
export type Operationid = string;
/**
 * Wait for new saved results without model polling when none are available.
 */
export type Wait = boolean;

export interface ReadSearchResultsInput {
  cursor?: Cursor;
  limit?: Limit;
  operationId: Operationid;
  wait?: Wait;
}
