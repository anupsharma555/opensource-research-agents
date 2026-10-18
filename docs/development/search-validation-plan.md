# Progressive search validation plan

This is a plan for qualifying the local Stage 4 search workflow, not a report of
live results. Run one case at a time, inspect the first unexpected mismatch, and
fix or replay it offline before spending another provider or model allowance. A
404, empty result, or failed search is a saved observation for the research peer
to work around within the case allowance, not an automatic run failure. Keep the
default mission capability catalog, discovery/activation steps, and normal
`MissionService` path in natural-behavior tests. Evaluation ceilings limit a
test batch; they do not become permanent research limits.

## Two independent coverage maps

Search behavior and provider reachability answer different questions. A working
provider call does not establish good query choice, checked evidence, or a
supported answer. A useful answer through another valid route does not prove
that an unselected route was exercised.

| Search behavior | Trigger and observable check | First proof lane |
| --- | --- | --- |
| Quick web lookup | One targeted question selects one query; Exa `fast` returns a saved operation and candidate hits. A known empty/failed Exa attempt may use configured Tavily, then a bounded reformulation if neither helps; uncertain transport does not silently switch. | Offline transport fixture, then natural quick case |
| Research web search | A broader question motivates parallel complementary queries, Exa `auto`, progressive results, and a targeted gap follow-up only when useful. Check coverage and repetition, not query count alone. | Offline operation tests, then natural research case |
| Scholarly search | A question suited to indexed literature leads to a suitable PubMed, Europe PMC, or Crossref route; cursor/offset and date/identifier behavior match the selected route. | Existing route tests, then natural case |
| Exact known-source lookup | A PMID, DOI, or known URL is retrieved without an unnecessary topic search. A 404 leaves the failed attempt in history while the peer may use another relevant hit. Inspect source identity, permissions, and retained copy. | Focused route/intake tests |
| Results, cancellation, and restart | `read_search_results` pages saved candidates, wait avoids model polling, `cancel_search` retains completed work, and restart preserves exact operation and per-query receipts without blind replay. | Offline fault/replay tests |
| Saved-source search | Search only already retained, currently permitted material. A matching saved passage should not start a provider request; revoked material must be withheld. | Local source and context tests |
| Source checking | A selected hit leads to a permitted fetched copy and `read_source_window`; a candidate excerpt alone is not citable support. Check exact passage and contrary evidence. | Offline source-use tests, then saved live records |
| Optional rendering | Only after an ordinary public HTML fetch leaves a stated gap, `render_source` may produce a separate Browserless copy. No login or rights bypass. | Offline rendering tests, then selected direct case |
| Groq browser search | A model may explicitly choose provider-managed browser discovery when useful; verify selected tool and returned leads separately from fetched, checked text. | Natural model case with server-tool usage marked unknown until observed |

| Provider or service | Current readiness for this plan | Separate first check |
| --- | --- | --- |
| Exa Search/Contents | Implemented optional adapter; requires selected credential. | One direct search; selected Contents or ordinary fetch is a separately counted source check. |
| Tavily | Implemented optional web-search adapter; only configured quick fallback on known Exa failure/empty, or an explicit research gap. | One direct query; then a synthetic fallback fixture before any natural fallback claim. |
| Dedicated OSRA SearXNG | Optional adapter and isolated loopback setup; service start and endpoint qualification remain operator actions. | Confirm local JSON endpoint, then one direct OSRA query. Do not use another project's service. |
| Groq browser search | Implemented provider-managed model tool, not a standalone no-LLM search endpoint. | Natural model case; count Groq model requests and any reported server-tool usage distinctly. |
| PubMed E-utilities | Implemented ESearch/ESummary and exact PMID/abstract routes; optional NCBI key support is implemented; live key validity remains unverified. | Direct ESearch plus ESummary (two HTTP legs), or one exact EFetch; check keyed/unkeyed pacing separately. |
| Crossref | Implemented bibliographic and exact DOI routes. | One direct search or DOI lookup. |
| Europe PMC | Implemented scholarly search with cursor paging. | One direct page; a continuation is a separate request. |
| Browserless | Optional renderer, not a discovery index. | One render only after a separately verified ordinary fetch; count both HTTP attempts. |
| OpenAlex, Unpaywall, Zotero, Serper | Credential fields may be stored, but these are **not** OSRA search adapters in this plan. A key is not route readiness. | First specify a concrete caller and contract: OpenAlex for scholarly discovery, Unpaywall for access resolution, Zotero for library read/import, and Serper as an optional search comparison. Library mutation needs its own explicit scope; no direct test yet. |
| arXiv, PsyArXiv, medRxiv | Future source routes; no implemented provider case is implied. | Define an adapter and lawful text path before adding cases. |

OpenAI search is excluded. Apify is deferred. Typesafe, Gemini, and other
unrelated credential names do not enter this search-provider matrix.

The agent chooses among a small, visible set of differentiated lanes:

| Visible lane | Selection | When useful |
| --- | --- | --- |
| Saved sources | `search_saved_passages` | Evidence already retained and permitted |
| Scholarly lookup | `search_scholarly` | Publication databases and exact DOI/PMID routes |
| Quick web lookup | `search_web(mode="quick")` | One focused gap; defined operational backup |
| Research web search | `search_web(mode="research")` | Complementary queries in parallel and targeted follow-up |
| Groq browsing | `browser_search` | Explicit provider-managed linked-page exploration |

Choose by information need, existing evidence and expected value, not keyword rules
or task age. The catalog exposes labels and concise purposes before attachment;
selected schemas provide details. The backend enforces the chosen lane, concurrency,
permissions and operational fallback. It does not choose the scientific inquiry.
Provider overrides remain optional agent choices within research search. Reading,
fetching, rendering and cancellation are supporting actions, not additional search
provider menus. A known URL or identifier can take its existing exact retrieval
path without broad discovery. Natural-choice tests retain the full real catalog.

## Recovery acceptance cases

Keep negative attempts as durable metadata with their exact route, status, and
time. They must not become fabricated evidence or disappear from the receipt.
The peer may choose an alternate hit, a targeted reformulation, or earlier
checked material; the host enforces the case allowance and source-use rules.

| Trigger | Acceptable continuation | Evidence to check |
| --- | --- | --- |
| One selected article returns 404 | Read a different relevant candidate or already retained permitted source; keep the failed article visible. | Failed fetch receipt, alternate source identity, checked passage, and supported output. |
| Both quick providers return no useful results | Reformulate the information need once within the allowance, or state the unresolved gap. Do not cycle through every provider. | Both saved search outcomes, changed query purpose, attempt count, and result or honest limit. |
| Research round misses one comparison dimension | Use completed hits, then ask a targeted follow-up for the missing dimension if needed. | Coverage by dimension, non-repeated query, checked passage, and final support. |
| Provider returns a rate wait | Preserve the pending operation until its saved retry time; if the test window ends, classify it as pending rather than scientific failure. | Retry-After, wait state, dispatch count, and later continuation if authorized. |
| External dispatch has uncertain outcome | Keep the attempt uncertain and do not blindly replay it or hide it behind another provider. | Dispatch marker, missing or captured response, and explicit next action. |

Routine route failures and retries belong in saved diagnostics. The final
research answer should mention only failures or limits that materially affect
its evidence, coverage, or conclusion. A recovered 404 need not dominate an
otherwise supported answer.

## Progressive gates and proposed allowance

**Tier 0: offline.** Reuse the existing focused retrieval, mission, and contract
checks first. Add only a missing transport, redaction, rate, interruption, or
source-use fixture needed by a specific failure. Do not rerun the full suite for
every case. Confirm the runner's no-call default and refusal to dispatch after
its aggregate ceiling with injected transports.

Check continuation handles in the actual serialized request after capability
selection: the reader needs its operation reference, and the fetcher needs its
selected hit. A scripted model remembering those IDs in Python variables does
not prove that the real model receives them. Include representative five-result
payloads when checking request size; tiny fixture replies can hide projection
overhead. Preserve full saved provenance while keeping the model-facing view
bounded and navigable.

**Tier 1: direct provider, no research LLM.** Select one configured provider at
a time. Use one query, at most three returned candidates, and normally one or
two HTTP attempts per case; PubMed ESearch plus ESummary counts as two. A
selected source fetch, Contents call, or Browserless prerequisite/render is a
separate HTTP attempt and must fit the stated case allowance. Propose a 20–30
second request timeout and 120-second case window. A saved `Retry-After` pauses
the pending attempt; if it extends past the window, save the wait and end this
observation as pending, not failed. Never replay an uncertain dispatch blindly.
Groq browser search starts at Tier 2 because it is a model-managed tool.

**Tier 2: natural quick and research cases.** Run distinct natural-language
tasks for quick lookup and complementary research, without forcing a tool or
reducing the catalog. Propose separate ceilings: quick lookup up to eight model requests and 40,000
planned tokens; research up to twelve model requests and 60,000 planned tokens.
Both permit up to eight transport-level retrieval attempts including fallbacks
and retries, and five minutes of active case time. The offline complete-catalog
quick path currently needs eight turns to discover, search, read, fetch and answer;
a four-call ceiling would measure premature stopping rather than successful use.
Do not spend the whole ceiling by default. Admit each model request against the
remaining whole-case allowance and record actual provider-reported tokens separately. These are fresh
evaluation ceilings to review before a live case, not authorization and not
expected consumption. If the task succeeds through a valid alternative, record
task quality and mark the targeted capability **not exercised**. A scripted
tool-use probe, if needed after a failure, stays a separately labeled mechanism
test.

**Tier 3: intermediate real-world question.** After direct and natural basics,
use one new, non-REEACT research question that benefits from
counterevidence, for example whether reflective roofs reduce nighttime neighborhood
air temperature or mainly daytime roof surface temperature. Specify audience, measurement period,
source-use constraints, and useful output without naming tools or expected
conclusions. Propose at most twelve model requests, 60,000 planned model tokens,
twelve transport-level retrieval attempts, and ten minutes of active time for
one case. Inspect source support and missing evidence before considering a
second case; one success cannot establish generalization. A later comparison
must hold the task, selected model, settings, and evidence as constant as
possible before claiming an efficiency or quality improvement.

All proposed allowances need fresh user authorization before provider-backed
execution. They are scalar ceilings for one selected case, not permission to
launch a ten-provider batch. Check existing holds and concurrent work first;
preserve the selected model, credential profile, and saved state. Do not silently
switch accounts or add a provider because another fails.

## Reusable runner and records

`scripts/validate_search.py` implements the first provider-only batch with a
versioned urban-trees case selection. Its default only prints the plan:

```sh
.venv/bin/python scripts/validate_search.py
.venv/bin/python scripts/validate_search.py --provider pubmed
```

After explicit authorization, `--live --batch-id <approved-batch-id>` runs the
selected cases. `--case` is repeatable; `--provider` selects one provider.
`--max-http-attempts` can lower the default eight-attempt ceiling. It uses only
this checkout's `.env` credentials, never inherited keys from another project.
Searches request at most three candidates, Exa Contents at most 2,000 characters,
and no model or generated-answer call is available. Cases run serially with
30-second requests, two-minute case windows and a ten-minute batch deadline.

The counter lives in `.local/search-validation/batches/<approved-batch-id>.json`,
independent of `--state-dir`. The state directory, selection, credentials and code
are pinned; changing them on restart is rejected. Every request, redirect and
retry is charged before dispatch. One known transient retry is allowed per case
within the same allowance. Never use a new batch ID to reset an existing approved
allowance. A new batch requires its own authorization.

Saved JSON summaries are under `.local/search-validation/reports/`; the selected
state directory contains normal discovery receipts. Captures retain response
hashes, sizes, status and numeric provider usage, without bypassing source-text
retention rules. Empty searches and HTTP 404 diagnostics may continue independent
cases; 404 does not establish scientific absence. Authentication errors, malformed
responses, missing capture and uncertain transport stop new dispatch. Exa Contents
item errors are reported as unavailable source extraction, not a valid empty search.

`scripts/validate_search_agent.py` uses the real mission host and full normal
catalog. Its default prints a DOE cool-roof task and limits; `--start-request`
accepts a private natural task with a unique request ID, scope, agent IDs and
explicit Primary or Backup profile. `--live` uses a fresh private runtime snapshot
and enables only the selected profile and run without changing original holds.
An exact completed-parent follow-up can instead use the paired follow-up options.
The default case ceiling is
eight model HTTP attempts, 55,000 planned tokens and four retrieval HTTP attempts,
including redirects. Token admission uses settled provider usage plus outstanding
reservations and the next request's full input/output reservation; unused output
capacity is released when actual usage arrives. Provider-unreported usage stays
unknown. Private records preserve the worker task, withheld review criteria,
source snapshot, budget, loaded code identity and final observation separately.

When the user explicitly authorizes observing natural resource use without an
aggregate ceiling, pair `--no-aggregate-limits --until-complete`. This records null
model-call, retrieval-call, token and observation-deadline limits; it does not
substitute arbitrarily large numbers. Do not combine this mode with numeric
limits. Without `--live`, it remains a no-call preview. The ledger still records
every HTTP attempt and provider-reported usage, retains unknown consumption, and
cannot be reopened under a different accounting mode to reset history.

Provider pacing, context limits, current source permissions and per-response
completion settings still apply. Normal minute waits remain scheduled; explicit
daily-quota refusals and long waits with unknown scope stop the case with distinct
diagnostics. Model errors, missing usage/capture and uncertain transport also stop
for inspection. A known retrieval 404 remains available to the runtime for an
alternative route. The runner never switches credentials automatically. A new
case on another profile or a repaired rerun needs existing authorization covering
that action, with its lineage and changed conditions recorded. Completion or a
classified stop closes the private host and disables its profiles while retaining
the saved mission and original workspace configuration.

The controller intercepts each HTTP exchange while retaining the adapters and
source fetcher's existing checks. It keeps a deny guard if shutdown cannot prove
that worker callbacks have stopped. Keep Groq provider-managed server-tool calls
**unknown until reported**, separately from client HTTP retrieval attempts; the
client ceiling cannot measure or cap hidden provider-internal browser operations.
Each model-backed batch needs its own authorization; the command is not an
automatic continuation of the provider-only allowance.

Keep three private surfaces separate under ignored `.local/`: the worker's
natural task and currently available evidence; the controller's condition,
event timing, model/profile, allowance, and stop rules; and a withheld rubric
covering acceptable support, counterevidence, and unacceptable claims. Verify
the worker export contains no future event or rubric. Each case receipt should
name the trigger, actual selected tools, attempted/executed requests, response
and usage evidence, first checked passage, supported-answer latency, coverage,
repeated work, source permissions, and unresolved gaps. Separate request
markers, captured responses, checked passages, accepted reports, and semantic
review. A successful HTTP request or valid citation shape is not a supported
scientific answer. Stop at the first unexpected harness or contract mismatch,
diagnose it with saved records, and prefer an offline correction before another
live call. A saved negative search outcome can continue through the bounded
recovery cases above.

Use [`os-eval-testing`](../../.agents/skills/os-eval-testing/SKILL.md) to design
cases, [`os-qualify-run`](../../.agents/skills/os-qualify-run/SKILL.md) to review
saved outcomes, and [`os-run-details`](../../.agents/skills/os-run-details/SKILL.md)
to explain exact actions and limits. These are Codex development skills, not
instructions loaded into research peers. No provider or model calls are part of
writing this plan.
