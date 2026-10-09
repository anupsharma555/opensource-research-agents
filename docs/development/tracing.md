# Inspectable agent activity

`ora.coordination.traces` saves what the host observed: who acted, which work and
configuration were used, the exact input and output records, and what happened.
These records explain execution. A valid trace does not establish that a research
claim is correct or that a model performed an unobserved action.

## Records and boundaries

`TraceContext` identifies a mission, run, registered agent, task, attempt and
operation. Model events can name an exact call-manifest version and parent
operation. A configuration must belong to that actor; a call must match its task,
attempt and configuration. Cross-run actors and unavailable versions are rejected
at that operation. Out-of-order children remain visible with unresolved ancestry
until their parent arrives. Cycles and conflicting ancestry are labeled, not
converted into successful spans.

`append_tx` lets an accepted contribution, required peer message, completion
receipt and trace commit in the same transaction. It performs no model call.
`append` is available for an independent host observation. Event identities are
idempotent: the same event can be replayed, while changed content under an old
identity produces a specific conflict. The accepted executor path supplies the
actual output record. A callback result is labeled as such; it is not described
as observed provider work.

Capability observations distinguish assigned, available, loaded, exposed,
invoked, attempted, successful, failed and unavailable. Merely listing a skill
or preparing a call does not say that it ran. Human explanations come from
recorded actions, evidence, alternatives and outcomes. Agent/group/run summaries
are explicitly derived explanations with links to their inputs.

Returned model reasoning has a separate restricted record and file. The adapter
records returned, absent, truncated, unsupported or unknown according to the
actual response. An interrupted response without reasoning is unknown; no text
is invented to fill the gap. First-pass independent critics cannot read this
material. `read_reasoning` requires an explicit diagnostic grant from a trusted
host caller. This library boundary is not an authentication service or operating
system isolation; a future service must authenticate the caller before providing
the grant. Messages, source text and model output cannot grant it.

General trace prose removes common inline credentials, contact addresses and
embedded web links; exact source records remain the place to inspect references.
Timing accepts numeric durations and known timing labels rather than arbitrary
provider payloads. General snapshot fields use an allowlist. Private prompts,
unrestricted callback/message payloads and returned reasoning are excluded.
Operational IDs and exact references are retained in these private local review
exports, which are not public reports or an anonymization service. Unknown record
kinds are explicitly unavailable in the diagnostic projection until reviewed.

## Timing and usage

Capacity receipts distinguish queue, quota, provider-capacity, retry and active
time using the store's clock. Model receipts also retain observed call timing.
Peer requests separately record creation, delivery, answer and resolution times,
expected latency and last useful progress. `progress` reads these records with
the first accepted artifact and the next actions on open proposals. A completed
operation's elapsed time stops at completion; an unanswered peer wait remains
open. Unknown external effects are not assigned invented completion times.

Durations from overlapping tasks are not added together as mission latency.
Expected categories describe work and are not scientific deadlines. An accepted
artifact timestamp establishes useful mechanical progress, not scientific
adequacy. A heartbeat alone does not establish useful progress.

Usage is grouped by scope, native unit and observation basis. Each total links
its constituent receipts. Tokens, requests, credits, dollars and sandbox units
remain distinct; cached-token subsets are not added to provider total tokens.
The caller must supply an account/endpoint scope without credentials. A local
ledger cannot establish total account headroom or actual billed dollars.

## Read without executing

After creating the [offline Stage 3 fixture](stage3-inspection.md), substitute the
printed state directory below:

```sh
.venv/bin/python -m ora.coordination.trace_inspect --state-dir .local/stage3/demo-<id>/state --mission-id stage3-synthetic-mission --view events --limit 100
.venv/bin/python -m ora.coordination.trace_inspect --state-dir .local/stage3/demo-<id>/state --mission-id stage3-synthetic-mission --view progress
.venv/bin/python -m ora.coordination.trace_inspect --state-dir .local/stage3/demo-<id>/state --mission-id stage3-synthetic-mission --view usage
```

These commands open the store read-only. Trace pages have a saved-event cursor,
`hasMore`, missing references and a completeness label. A page can contain fewer
trace steps than events because the event stream also contains other activity.
Advance to `nextCursor`, not the number of displayed steps. Ancestry lookup reads
only the requested ancestors, rather than loading every trace body for each page.
This bounds returned pages; it is not a guarantee about indexed lookup cost for
every future workload. The saved snapshot projection uses the same readable-step
function for Atlas and Codex. It remains a finite snapshot, not a streaming host.

`scripts/measure_stage3_diagnostics.py` exercises a disconnected viewer by making
no export calls during writes, then reading from a reopened read-only store.
The retained private receipt covers 100 and 1,000 traces, exact reference replay,
parent links, page timings, and unchanged SQL row fingerprints. All steps were
recovered with no missing references. This is a local durable-event-backlog test;
it does not measure a real browser outage, production memory, or long-run uptime.
Optional Python allocation measurement is explicitly distinct from process RSS.

Run the focused trace/inspection tests or `scripts/dev check`. Provider failures,
cutoffs and backup/recovery are also exercised in `tests/models`; no model
credential is required for the default checks.
