# Web discovery choices

Use saved permitted sources, known URLs and exact DOI/PMID routes before broad discovery.
`search_saved_passages` searches retained material; `search_scholarly` selects a publication
index or exact identifier route. Neither is the general web search tool.

Request `search_web` when broader web evidence helps. Choose from task meaning and missing
evidence, not keywords or task age. State the information need without private reasoning.

- **Quick web lookup**: `mode="quick"`, one targeted query, Exa fast, up to ten results.
  Example: locate a trial's publication page when its exact identifier is unknown.
- **Research web search**: `mode="research"`, up to ten distinct complementary queries per
  round, Exa auto, at most five concurrent Exa requests. Example: trials, long-term outcomes,
  economic outcomes and contradictory findings. More rounds may address unresolved gaps.
- **Read search results**: `read_search_results` with the saved operation ID and cursor.
  It makes no new search. `wait=true` waits for progress without model polling.
- **Cancel search**: `cancel_search` stops unnecessary pending work and keeps completed results.

Quick narrows scope, not evidence quality. Research does not call every provider. Exa is
primary; configured Tavily handles operational failures or empty quick results. Explicit
Tavily or dedicated OSRA SearXNG research queries can address a gap. If economic outcomes
are missing, add that focused query rather than repeating every provider.

Groq `browser_search` is an explicitly selected provider-managed browsing turn. It is not
an automatic duplicate search. `render_source` uses optional Browserless only after an
ordinary `fetch_source` extraction leaves a stated JavaScript-dependent gap.

Highlights and provider snippets are candidates, not inspected evidence or summaries.
Use returned hit IDs with `fetch_source`, then `read_source_window` for exact permitted
passages before source-backed claims. Read or reuse checked copies rather than refetching.
Preserve uncertainty, counterevidence, versions and all discovery routes. Respect search
restrictions and run authorization; no source availability or permission is implied.

## Selecting scope and further work

A single missing publication URL usually needs quick lookup. A comparison across several
evidence dimensions benefits from research queries that ask different questions. Ten is
a round size, not a lifetime limit. Decide relevance, sufficiency and the next question
from returned evidence, constraints and remaining authorization. Mechanical fallbacks
handle provider availability; they cannot judge scientific completeness.

Each finished query becomes readable independently while siblings run. Keep the
operation reference and continuation cursor. Read existing progress before repeating
work. Known completed results survive restart/cancellation; an uncertain dispatch
requires reconciliation rather than blind retries. Source discovery routes and query
purposes remain available even when several queries find the same URL.

Search candidates, retained original/rendered copies, inspected passages and generated
interpretations have different evidentiary roles. A rendered copy has its own identity.
Do not infer a paper, version, study family or claim from a matching title alone.

## Negative attempts and useful continuation

A missing article or empty response is a local observation, not a conclusion that
no relevant evidence exists. Preserve the query, provider, exact source, time and
outcome in saved metadata. A known failed or empty quick search may use its configured
backup; rate waits and uncertain external attempts follow their recovery rules.

After an inaccessible source, consider another saved hit, an existing checked copy,
an exact publication identifier, an alternate public version or a refined query.
Do not fabricate alternate URLs or identifiers. In a research round, retain useful
siblings and address only the remaining information gap. Choose relevance and further
work from evidence; do not mechanically repeat every route. Cancel unneeded work.

Provider details are optional advanced controls. Most tasks choose quick versus
research and describe the information need. Keep ordinary failures in diagnostics;
state access or coverage limitations in the final answer when they affect support.
Never convert persistent failure into an unsupported claim or an endless retry loop.
