# General web discovery

Honor no-external-search limits. Prefer permitted saved material (`search_saved_passages`),
known URLs, or exact DOI/PMID routes (`search_scholarly`) before broader discovery.

Request `search_web` and state the information need:
- **Quick web lookup** (`mode="quick"`): one targeted gap; Exa fast with configured backup.
- **Research web search** (`mode="research"`): complementary questions or counterevidence;
  up to ten queries per round, Exa auto, at most five concurrent Exa requests.

Quick narrows scope, not evidence standards. Research does not call every provider.
Choose the lane by information need; provider overrides are optional. Read `read_search_results`; `wait=true` avoids model
polling. `cancel_search` preserves completed work. Address gaps with targeted follow-up.

Empty searches and 404s are diagnostics, not proof of absence or reasons to stop independent
work. Consider another hit, an exact identifier, or a revised query within authorization.

Explicitly request `browser_search` for Groq browsing; `render_source` follows ordinary
extraction. Candidates are not inspected evidence: use `fetch_source`, then
`read_source_window` before citing. Report only material evidence limitations.
