# Earlier supplied-evidence live extension

This guide describes the legacy runtime and its historical saved workspaces.
For the shared mission loop with retrieval, use [Stage 4 local research](stage4-local.md).
Select `--runtime legacy` explicitly when continuing the supplied-evidence extension;
the two storage formats are not silently converted into one another.

Atlas can submit a real model task and follow up on an exact saved answer. This
optional local extension uses Groq `openai/gpt-oss-120b` with high reasoning,
Sage for evidence synthesis and Mira for independent critique. The model chooses
when to read supplied evidence, request a peer check and publish an answer.
Publishing finishes that contribution so queued peers can work; a delivered peer
reply can start a further revision without closing the overall task prematurely.
The default sample app and offline checks still make no provider calls.

## Launch and inspect

After the [shared setup](setup.md), build the assets with `scripts/dev check`.
Provide `GROQ_API_KEY` through the process environment or the untracked root `.env`.
Then run:

```sh
scripts/dev live serve --runtime legacy --allow-model-calls --state-dir .local/live --run-budget-usd 0.08
```

Leave that process running. In a second terminal:

```sh
scripts/dev live open --state-dir .local/live
scripts/dev live status --state-dir .local/live
```

Build the native shell with `scripts/dev desktop` first for a Mac window; otherwise
the opener uses a browser. `open --browser` explicitly selects the browser. Opening
or inspecting the app does not submit a model call. Closing the viewer does not
stop the separate host process.

The navigation presents **Start or follow up**, **Current task**, then **Saved
runs**. Submit a question, scope, pasted source text and requested peers. A follow-up
selects an exact claim, task, source or artifact version from the current saved run.
A repeated unchanged submission returns its original receipt. Editing a request
creates a new identity. The original snapshot remains available in Compare. Execution timelines focus on the
current branch by default; include parent history explicitly to inspect earlier work
and gaps. Visual labels use whole minutes while Details retains precise timestamps.

The recorded Stage 2 review keeps **Start or follow up** and **Previous tasks**
visible. These views offer **Open live workspace**, which returns a desktop window
to the connection supplied by its live launcher, or a browser to its same local
host. A desktop window launched directly without that connection explains how to
reopen it using `scripts/dev live open --state-dir <saved-state-directory>`.
Recorded examples never submit a simulated request as though it were live work.

Research review shows the reader's answer. Activity, Communication, Evidence and
Details expose the named agents, peer requests/replies, exact saved passages,
model/tool inputs and outputs, timings, usage and failures. These are execution
records and concise explanations, not invented private model reasoning. The
provider is configured not to return reasoning text.

For the same records from a terminal or Codex, use the run ID shown in the app:

```sh
scripts/dev live status --state-dir .local/live --run-id RUN_ID
scripts/dev live status --state-dir .local/live --run-id RUN_ID --calls
```

Call records include submitted source text. Keep their exports private unless
reviewed for disclosure. Never copy `.local/live/connection.json` into a report:
it contains the local session credential.

## A small connection test

Use **Sage** as the requested peer.

**Question:** Using only the supplied screening log, state how many records were
screened and calculate the percentage included. Give a brief, source-linked answer.

**Scope:** Connection test using simulated counts. No web search. Keep the answer
under 90 words; distinguish the supplied counts from your calculation.

**Pasted evidence:** Sample screening log (simulated test data): 24 records were
identified. Four duplicate records were removed. The remaining 20 unique records
were screened. Five records met the inclusion criteria and 15 were excluded. These
are test counts, not findings from a published review.

The expected calculation is 5 / 20 = 25%. After completion, select **Follow up on
exact work**, keep the saved claim as the target, and enter:

**Follow-up:** Ask Mira to check the arithmetic in the saved answer. Then revise it
to distinguish the percentage included out of screened records from the percentage
included out of all identified records. Preserve the original counts and explain
both denominators.

**Scope:** Use the same saved screening log. No web search. Keep the revised answer
under 120 words. Ask for one focused peer check; do not repeat a resolved request.

The expected additional calculation is 5 / 24 = approximately 20.83%. Inspect the
actual peer exchange and the before/after answer; fluent text alone does not prove
that the peer checked it. This test checks connectivity and arithmetic, not research
quality or scientific acceptance.

## State, recovery and current boundaries

For readable database views, immutable call configurations, tool lifecycle records
and the explicit copy-tested migration, see [saved live provenance](live-provenance.md).
That inspector works without a running host. Historical missing values remain
unknown; the separate [Stage 2 inspector](state-inspection.md) covers recorded fixtures.

SQLite stores requests, immutable snapshots and call records in the selected private
state directory. A single host owns each directory. The API binds only to loopback,
authenticates local sessions and rejects cross-origin writes. It is not a remotely
exposed production service.

A known provider rate-limit rejection saves a waiting diagnostic and retries the
same conversation after the provider interval (60 seconds if none is provided).
An uncertain transport outcome is retained without automatic repetition. The
explicit test allowance reserves estimated call cost before dispatch; it is an
operational spending boundary, not a scientific completion rule or an exact bill.

Different runs may execute concurrently through four worker slots. Peers inside
one run currently execute serially. Quota waits occupy a worker slot. Completed
records survive host restart, but arbitrary mid-task restart and ten-hour autonomous
continuation are not qualified by this extension. Interrupted uncertain calls stay
visible rather than being silently repeated.

This extension uses pasted or already saved evidence; it does not discover fresh
web sources, retrieve articles, execute E2B jobs, or implement all nine roles.
Its small local store and loop are an integration reference for the staged runtime,
not a replacement for that runtime's stronger configuration, scheduling and recovery
contracts. A completed local task is not independently accepted scientific evidence.

## Saved report presentation

Research review puts the answer first, followed by expandable sources and related
peer exchanges. Common report formatting supports headings, paragraphs, lists,
tables, quotations, code and emphasis. HTML stays text, remote images are not
loaded, and only HTTP(S) document links are rendered as external links. This is a
bounded text renderer, not a complete CommonMark or mathematical-typesetting
engine. The original saved body is available in Report details.

Exact `sourceId::spanId` strings can open their saved passages through readable
citation controls. Older plain-text reports distinguish references found in the
answer from structurally saved citations; the viewer does not rewrite those
artifacts or treat reference resolution as proof of claim support. New ordinary
final-text answers preserve exact references only when they resolve to saved
passages read in that model conversation. Invalid or unread references remain
visible in the answer without becoming evidence links or blocking completion.

New reports identify their producing attempt explicitly. A unique identical-text
match in an older snapshot is labeled as a match. Related peer exchanges show
the exact request scope, reply and attempt link; they do not certify a review of
the whole report. Unknown authorship, ambiguous records and missing passages are
not guessed. Relevant saved limitations remain downstream; an empty metadata
field is described in Report details as not separately recorded, never as proof
that the research has no limitations.
