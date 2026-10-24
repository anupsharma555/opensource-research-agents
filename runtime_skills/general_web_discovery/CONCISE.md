# General web discovery

Honor no-external-search limits. Prefer permitted saved sources, known URLs and exact
DOI/PMID routes before broad search. `search_saved_passages` and
`search_evidence_library` make no web request. Library search returns metadata: select
one `entry_id`, then inspect its exact permitted copy with `read_source_window`.
Peer relevance notes are interpretations, not inspected evidence.

Request `search_web` with an information need. Choose the lane by information need:
**Quick web lookup** (`mode="quick"`) for one gap; **Research web search**
(`mode="research"`) for complementary questions or counterevidence (up to ten
queries per call, no lifetime cap). Consider `read_research_activity` before substantial rounds to see peer searches
and gaps; it is optional. Identical selected requests may share physical work while
peers keep separate views and interpretations. Distinct questions need distinct
queries; `refresh=true` requests new provider work. Read `read_search_results`;
`wait=true` avoids model polling. Cancel unneeded pending work.

Snippets and empty/404 results are diagnostics, not proof. Use `fetch_source`, then
`read_source_window` before citing. Repeat rounds from remaining gaps; preserve
versions, uncertainty and counterevidence. Explicitly request `browser_search` for
Groq browsing; `render_source` follows ordinary extraction.
