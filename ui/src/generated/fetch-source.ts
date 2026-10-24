/* Generated from contracts/generated/fetch_source.schema.json. Do not edit by hand. */
export type Hitid = string;
/**
 * Request a fresh source version instead of reusing compatible saved bytes.
 */
export type Refresh = boolean;

export interface FetchSourceInput {
  hitId: Hitid;
  refresh?: Refresh;
}
