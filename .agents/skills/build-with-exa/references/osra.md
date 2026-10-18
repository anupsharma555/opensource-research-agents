# Build Exa integrations in OSRA

Read this when modifying Exa-backed search in this repository. Use the upstream
[search](search.md) and [contents](contents.md) references for API shapes; read
other endpoint references only when the requested work needs them.

## Reuse the existing integration

- `src/ora/retrieval/providers.py` owns route descriptions, request construction
  and response projection; `discovery.py` owns saved retrieval and recovery.
  `src/ora/mission/tool_catalog.py` and `service.py` expose and dispatch the tools.
  Paths here are relative to the repository root. Read
  [discovery design](../../../../docs/development/stage4-discovery.md) before edits.
- Direct `exa.search` uses Search for leads; `exa.contents` fetches an explicitly
  selected URL or ID. Groq-native `browser_search` is a separate provider-managed
  model turn, not a call using `EXA_API_KEY`. Keep their receipts and usage distinct.
- Reuse existing raw HTTP transport, injected-test seams and shared accounting.
  No SDK installation is needed for the existing adapter. If an SDK becomes
  necessary, follow the repository's pinned dependency workflow instead of
  running the generic latest-version installation examples above.

## Preserve deliberate research choices

The upstream recommended search uses `contents.highlights: true`. OSRA currently
uses metadata-first search followed by selected text extraction. Treat these as
explicit alternative designs: adding highlights changes returned content,
normalization and possibly cost; test that change rather than silently enabling
it while installing this skill. Explicit result counts and text bounds are
product/resource choices, not provider defaults. Task-defined date windows are
not cache-freshness controls. Do not invent pagination.

Use Search/Contents for evidence supplied to OSRA's selected research model.
Exa Answer, generated summaries, structured synthesis, Agent and Monitors are
separate capabilities; do not introduce them merely to answer a web-search task.
Preserve leads versus inspected copies, original text versus generated summaries,
exact source/version/locators, current permissions and successful sibling results.
A skill gives guidance; executable tools perform searches and evidence intake.
Runtime guidance already lives in `runtime_skills/general_web_discovery/`; do not
load this developer package into research-model prompts or duplicate its API manual there.

## Credentials and verification

The existing backend reads `EXA_API_KEY` from its environment or this repository's
ignored `.env`. Preserve the user's configured value; report presence only, never
the value. A team-management service key is not needed for Search/Contents.
Do not change credentials in other projects. Skill installation is not a live
search authorization; retain existing holds and bounded-test requirements.

For adapter changes, start with `.venv/bin/python -m pytest -q
 tests/retrieval/test_discovery.py` (one shell line), then affected mission tests.
Use injected responses for request shape, empty/partial results, identity,
clipping, freshness, retry and secret-redaction checks. Validate actual model use
separately under an authorized live allowance; do not claim it from offline passes.
