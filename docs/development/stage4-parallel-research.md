# Parallel research and retained evidence

Research peers choose complementary questions and decide when another round is
useful. The host shares identical retrieval work, keeps each peer's source-use
checks separate, and makes current activity visible without repeating full plans.
The retained evidence library supports deliberate reuse in later missions.

This is local-runtime infrastructure. Injected offline transports can verify
coordination and exact model inputs; they do not establish natural agent choices,
provider throughput, supported-answer quality or long-running endurance.

Technical index: [activity](#activity-and-communication),
[shared search](#shared-search), [source reuse](#source-reuse),
[library](#evidence-library), [capacity](#capacity),
[compatibility](#compatibility), [verification](#verification).

## Activity and communication

`read_research_activity` returns a caller-scoped, paged snapshot of other peers in
the same mission/run: objectives, recent queries and providers, progress, checked
source references, plan gaps and next actions. Snapshots preserve page positions;
source-dependent text is rechecked when read. Operational progress does not imply
that another peer has established a scientific finding.

The next relevant model input receives at most four changed entries and 512
estimated tokens, including the digest's references and continuation. The complete
serialized request is still counted by the existing request-sizing pipeline.
Only entries included in an accepted model response are marked seen. The host
does not treat preparing an input or a failed provider response as delivery.

Substantive checked evidence, newly recorded plan gaps and responsibility changes
produce coalesced reference-only notices for active peers. These can refresh a
queued model input before dispatch. They do not broadcast HTTP progress, copy
private reasoning, reopen completed tasks, or override an explicit dependency
wait. The checked activity digest supplies details on the next useful turn.

The runtime web-discovery skill recommends checking activity before a substantial
new round. It remains optional guidance. Existing directed peer requests remain
the way to negotiate responsibility or ask another peer for a specific result.

## Shared search

`search_web`, `read_search_results` and `cancel_search` remain the agent interface.
Quick mode starts with one Exa fast query and the existing configured operational
fallback. Research mode accepts up to ten complementary queries per round.

The shared index keys each physical provider request by mission, provider,
query/options and compatible source-use context. It does not key the whole query
array or a peer's differently worded information need. Matching requests join a
pending job or reuse a completed result. Distinct terms, providers, filters or
modes remain separate; no semantic similarity model silently merges them.

Each subscriber keeps its own operation, delivery, hit records and discovery
provenance. A physical job is owned independently of its first subscriber.
Cancelling one peer detaches it. Cancelling the last subscriber can stop queued
work, while already completed results remain saved. A later subscriber can start
a new generation after a proven no-dispatch cancellation; an uncertain external
attempt is never automatically replayed.

Each operation preserves the round's information need, queries, outcomes and
references. Plans/proposal revisions preserve agent-authored changes in direction;
retained entries identify selected evidence. Further rounds target remaining gaps
or changed information, with no additional lifetime query limit. `refresh=true`
explicitly requests fresh work.

## Source reuse

Ordinary public retrieval uses an atomic URL/representation claim within a mission.
Meaningful query parameters are retained. A verified redirect can supply an alias;
refresh updates an alias only if it still refers to the prior matching fetch.
An independent refresh of the destination is preserved.

Parsed content is reusable by content hash, parser version and extraction options.
Ordinary HTML, rendered HTML, PDF/text and OCR remain separate representations.
Per-peer adoption creates a distinct document/source view and repeats normal intake
and model-input permission checks. Shared bytes never create a shared permission
grant. Every hit and discovery route remains traceable through its local view.

`fetch_source` accepts explicit `refresh`; changed bytes produce a fresh source
copy. A local parser interruption can retry under a fenced lease because it has
no external effect. An uncertain HTTP dispatch cannot. Pending consumers wait in
host state and can inspect completed bytes without repeating the download.

Browserless remains an explicit route after ordinary extraction. OCR remains an
unverified derived transcription, and a vision answer remains a model-generated
interpretation. Neither becomes a checked source passage merely through caching.

## Evidence library

`retain_evidence` accepts exact checked, retained source-copy versions selected by
the current task and a brief relevance note. Optional finding references preserve
the peer's interpretation separately. Retention is curation, not certification of
a finding or permission to upload to another service.

`search_evidence_library(query=...)` returns bounded metadata. The peer selects a
returned `entry_id` using the same tool, then reads its exact source reference
through `read_source_window`. Search does not automatically insert source text.
Selection and model dispatch both recheck source identity, version and current
use. BHEB-derived copies also require their current external corpus permission;
an unmounted archive cannot be bypassed by the library.

Entries live in the existing SQLite store and refer to existing content-addressed
files. A disposable term index narrows candidates before full metadata/permission
inspection. `rebuild_evidence_library_index(host)` rebuilds the index from entry
heads; a missing index is rebuilt on demand. A corrupt or stale candidate cannot
authorize source delivery. No new database, embeddings service, corpus migration
or BHEB upload is introduced.

Retrieval timestamps, exact source versions and curation timestamps let the agent
assess age. Negative leads remain diagnostics; agents retain selected useful
evidence rather than automatically promoting every search result.

## Capacity

All peers share the existing retrieval pool: six slots, at most five concurrent
Exa requests, five Exa launches per second, and one Browserless render. These
are host defaults, not per-peer grants or promises about upstream account limits.
Search dispatch remains separate from Groq generation capacity.

Tavily admission applies both its existing second window and a minute window:
`OSRA_TAVILY_REQUESTS_PER_MINUTE` defaults to 100 and permits lower values;
`OSRA_TAVILY_WINDOW_SECONDS` defaults to 60 and permits longer windows up to 3600.
Existing lower capacity policies win. Provider retry waits remain durable.

Provider documentation checked 2026-10-22: Exa limits apply across a team's keys;
Tavily documents 100 requests/minute for development and 1,000 for production
search keys. Separate project keys do not prove independent upstream capacity.
See [Exa billing/limits](https://exa.ai/docs/admin/billing) and
[Tavily rate limits](https://docs.tavily.com/documentation/rate-limits).
The host conservatively retains the approved 100/minute default.

## Compatibility

New missions and follow-ups pin `parallel_research_version=1`. Runs without this
marker retain their prior tool catalog and exact-search/fetch behavior. New tools
are discoverable before attachment; actual callable schemas remain controlled by
the existing capability mechanism.

Canonical inputs live in `ora.contracts.models`. Generate JSON schemas and UI
types with `scripts/dev generate`. Runtime web discovery is version 3; saved
version-1 and version-2 packages are preserved for historical pins. The Codex Exa
development manual is not injected into research-agent prompts.

## Verification

Focused offline suites exercise 2/4-peer host runs and an 8-peer queue, duplicate
joins versus distinct requests, progressive results, cancellation and uncertain
attempts, source revocation, parser recovery, source refresh, and explicit
cross-mission library selection. Inspect the actual serialized request for
callable tools, bounded activity, and exact checked passage delivery.

Use `.venv/bin/python -m pytest` with the shared-search/fetch, research-activity,
library, notice, permission and parallel-integration test files. Tests use injected
transports and synthetic sources. Review adjacent legacy tests separately from
new behavior. Full provider qualification requires its own live authorization.

The later live comparison should use matched one-, two- and four-peer tasks and
measure first checked passage, supported-answer latency, coverage, counterevidence,
duplicate HTTP/extraction work, context overhead and actual usage. Scripted tool
choices establish plumbing only. Unattended monitoring is outside this increment.
