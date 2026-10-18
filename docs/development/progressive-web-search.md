# Progressive web search (development setup)

This is an optional local Stage 4 search path for OpenSource Research Agents
(OSRA). It does not start a service during `scripts/dev setup` or `scripts/dev
check`. The provided Compose definition is isolated under `deploy/searxng/` and
binds only `127.0.0.1:18081`. Do not point it at a SearXNG service owned by
another project. The adapter requires an explicit `OSRA_SEARXNG_BASE_URL` and
rejects the known neighboring loopback ports 8080 and 18080.

## Local SearXNG preparation

The [Compose file](../../deploy/searxng/compose.yaml) uses the official SearXNG
container's port 8080 and mounts this project's small
[`settings.yml`](../../deploy/searxng/settings.yml). The settings enable JSON
search responses. [SearXNG's container guide](https://docs.searxng.org/admin/installation-docker)
documents that port and config location; its
[settings guide](https://docs.searxng.org/admin/settings/settings_search.html)
documents the JSON format. Review an exact official image digest for the intended
host architecture before starting; this repository deliberately supplies no
floating image tag or invented digest.

1. Create the ignored `.local/searxng.env` file with `OSRA_SEARXNG_IMAGE` set to
   the full reviewed image reference, such as
   `docker.io/searxng/searxng@sha256:<verified-digest>`, and
   `OSRA_SEARXNG_SECRET` set to a unique local secret. Keep the real values out
   of Git and command output. The container receives the secret through
   SearXNG's `SEARXNG_SECRET` override; the provided settings file contains only
   a placeholder. The [server settings guide](https://docs.searxng.org/admin/settings/settings_server.html)
   documents this override.
2. When an operator explicitly authorizes local service start, run from the
   repository root:

   ```sh
   docker compose --env-file .local/searxng.env -f deploy/searxng/compose.yaml up -d
   ```

3. Check the local JSON interface and stop the service when finished:

   ```sh
   curl --fail --silent --show-error 'http://127.0.0.1:18081/search?q=example&format=json'
   docker compose --env-file .local/searxng.env -f deploy/searxng/compose.yaml down
   ```

The JSON response check establishes that this local instance answers a request;
it does not qualify its result quality, outside network access, or an OSRA
mission. Docker or another compatible container engine must be installed and
running before these commands are used. In the repository's ignored root `.env`
or the mission host environment, set only
`OSRA_SEARXNG_BASE_URL=http://127.0.0.1:18081` for this adapter. Do not copy
the container secret into the mission configuration. OSRA does not load the
Compose env file. No service was started as part of this setup change.

## Search behavior and proof boundary

`search_web` starts a peer-selected round with an information need. `mode=quick`
accepts one query and uses Exa `fast`, with up to ten results. `mode=research`
accepts up to ten distinct complementary queries and uses Exa `auto`; Tavily
or a dedicated SearXNG route may be chosen for a research gap. Exa is the first
route. Quick mode can fall back to configured Tavily only after a known empty or
failed Exa result. An uncertain transport or rate wait is retained for review,
not blindly replayed on another provider. Groq browser search remains an
explicit model choice; it is not automatically duplicated. OpenAI search is excluded.
Apify is deferred until a concrete actor-specific need exists.

The agent chooses the search terms and mode. A repeated `search_web` call reuses
the same operation only when its ordered queries, information need, mode,
provider and result limit match exactly within the same run, peer and task.
Different terms or options remain separate searches; there is no fuzzy query
merging. `refresh=true` intentionally starts a fresh operation. Replaying that
same invocation still returns its original operation, including after a later
refresh. Pending, completed, empty, failed, uncertain and cancelled work remains
readable without silently launching another retrieval. Current source-use
checks still apply; saved results are not permission to reuse restricted text.

After a permitted search operation exists, `read_search_results` is attached
without a separate capability-selection turn. Eligible saved web candidates also
make `fetch_source` available. These attachments do not execute tools or select
which source to read. They remain task-scoped, survive restart and preserve the
separate Groq-native browsing turn.

The returned result is a saved operation, not a claim that all queries finished.
`read_search_results` pages completed candidate hits and can wait for progress
without model polling. `cancel_search` stops pending work while retaining
completed and late known results. Saved operations and per-query receipts allow
progress and restart inspection. Search excerpts are uninspected candidates;
the peer must fetch and read a permitted source copy before using a passage as
evidence. `render_source` is optional only after an ordinary public HTML fetch
leaves a stated content gap. Browserless rendering retains a separate copy and
does not bypass source permissions or login restrictions.

Model input uses compact discovery previews rather than repeating full provider
metadata. Completed search operations and candidate references remain visible
after a capability-selection turn. A preview identifies its saved result and
provides a `read_tool_result` request for permission-checked detail; reading that
result does not repeat the external search. Long items are readable in bounded,
hash-bound character windows, while `read_search_results` continues paging the
operation's available candidates. A candidate excerpt is still an uninspected
lead. Source passages retain their separate checked-window delivery rules.

Size fitting and minute pacing are separate. The model request packer counts
instructions, attached schemas, task context and result previews together and
reduces optional detail when the current allowance is tight. A minute wait can
restore capacity but cannot shrink an oversized request. The existing exclusive
provider-feedback probe remains available when only an estimate exceeds the
observed rate allowance; real provider refusals, configured ceilings and
source-use checks retain their own handling. Do not hardcode one observed Groq
limit as a universal retrieval-provider limit or claim acceptance from a local
token estimate.

These providers have different retrieval APIs but feed the same selected Groq
model context. Exa, Tavily and SearXNG use the progressive web-result projection;
PubMed, Crossref and Europe PMC return scholarly listings through
`search_scholarly`. Groq-native browsing has its own bounded saved-result reader.
Browserless produces a separately identified source copy whose passages use the
ordinary checked source-window path. Review listing size, detail continuation and
source permissions at these model-input boundaries, rather than copying a
model-token limit into each retrieval adapter. Merely configuring credentials
for a provider without an adapter does not add a tested path.

The host's shared `StateStore` capacity policy allows at most five concurrent
Exa searches and five Exa launches per second, six retrieval slots across
work, and one Browserless render at a time. Configured lower limits win, and
observed provider 429/503 responses supply retry pacing. These are admission
ceilings, not measured throughput or guaranteed provider capacity.

Offline review can inspect the focused tests with the pinned environment:

```sh
.venv/bin/python -m pytest -q tests/retrieval/test_web_search.py tests/mission/test_web_search_integration.py
scripts/dev check
```

Those checks exercise local contracts and fixtures; they do not prove a natural
model choice or live provider result. A later explicitly authorized live
evaluation should preserve the full capability catalog and natural planning
context, then record time to the first checked passage and supported answer,
source coverage, repeated queries, actual provider/API usage, and whether a
final answer is supported. Keep provider-backed qualification paused until its
separate Stage 4 prerequisites and fresh authorization are satisfied.

## Scholarly credentials and next-phase validation

`NCBI_API_KEY` is optional for existing PubMed routes. The adapter applies it to
every E-utilities HTTP leg, including resumed ESummary, and redacts it in receipts.
The local pool uses the documented no-key three-request/second allowance or the
keyed ten-request/second allowance; `OSRA_NCBI_REQUESTS_PER_SECOND` can lower it.
Separate processes using another state store or project still share any upstream
key allowance. Keeping a credential in `.env` does not verify it.

OpenAlex, Unpaywall, Zotero and Serper configuration does not enable an adapter.
Use the [next-phase validation plan](search-validation-plan.md) to qualify search
behavior and providers separately, beginning with focused offline and direct
provider checks before natural agent tasks. `scripts/validate_search.py` now
provides the bounded provider-only runner; `scripts/validate_search_agent.py`
provides the separately authorized natural DOE case on the full mission runtime.
