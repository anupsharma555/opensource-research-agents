/* Generated from contracts/generated/search_web.schema.json. Do not edit by hand. */
/**
 * Brief information need, not private reasoning.
 */
export type Informationneed = string;
export type Limit = number;
/**
 * Quick web lookup: one targeted information gap. Research web search: complementary questions, comparison or counterevidence.
 */
export type Mode = "quick" | "research";
/**
 * Optional agent selection for an identified research gap. Default Exa; quick uses the configured fallback.
 */
export type Provider = "exa" | "tavily" | "searxng";
/**
 * @minItems 1
 * @maxItems 10
 */
export type Queries =
  | [string]
  | [string, string]
  | [string, string, string]
  | [string, string, string, string]
  | [string, string, string, string, string]
  | [string, string, string, string, string, string]
  | [string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string, string];
/**
 * Reuse identical provider/query/options within this mission using a separate operation for each peer. Set true to request fresh retrieval. Different terms, providers, modes or filters remain separate.
 */
export type Refresh = boolean;

/**
 * One explicitly chosen round; this is not a lifetime research budget.
 */
export interface SearchWebInput {
  informationNeed: Informationneed;
  limit?: Limit;
  mode: Mode;
  provider?: Provider;
  queries: Queries;
  refresh?: Refresh;
}
