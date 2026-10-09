# Research Atlas: interfaces for long-running agent work

Reviewed 2026-10-08. This is a focused review of eight primary resources, not a
systematic review or a benchmark of the products. Official documentation and
selected repository files were read; upstream applications were not installed or
executed. Living documentation and `main` links can change. Existing Atlas code,
saved run records and user feedback supplied the comparison baseline.

## Design conclusion

Atlas should help a reader answer three questions in order: **What is the current
research result? Who did what to produce it? What exact record supports that
account?** A conversation view, an execution timeline and a call inspector serve
different purposes. They should link to the same saved identities rather than
reconstructing one another from prose.

For long runs, also distinguish the viewer's connection, the run's saved progress,
and an agent's actual execution state. A refreshed window does not establish that
a worker is making progress. A completed model turn does not establish scientific
acceptance. These are design conclusions for Atlas, informed by the resources
below, rather than experimentally proven benefits of this implementation.

## Eight resources and what they contribute

| Resource | Verified feature or finding | Implication for Atlas | Evidence boundary |
| --- | --- | --- | --- |
| [AgentGUI paper and interface](https://arxiv.org/html/2607.26300v1) | Separates activity, elapsed-time overview, call debugging, console and artifacts. A counterbalanced study of eight participants reported faster information lookup than its Hermes Dashboard baseline. | Keep readable outputs, activity and technical records at different detail levels; test whether users can find an actor, source, wait and changed answer. | July 28, 2026 preprint; small, narrow study. It does not establish an optimal research UI or general multi-agent reliability. |
| [LangSmith: viewing traces](https://docs.langchain.com/langsmith/view-traces) | Conversation/trajectory, turn summaries and detailed run inspection remain linked. Subagent actions and tool groups lead to their exact records. | Distinguish assignment from response and preserve conversation context when opening a call. | Product documentation; not an independent usability trial. |
| [Langfuse: sessions](https://langfuse.com/docs/observability/features/sessions) | Groups separate traces with a shared session identity and supports review of the multi-turn interaction. | Preserve mission → start/follow-up run → attempt → call lineage; offer parent history without mixing elapsed gaps into the current branch by default. | Session grouping requires correct instrumentation. It cannot infer missing relationships. |
| [Phoenix: tracing tutorial](https://arize.com/docs/phoenix/tracing/tutorial) | Inspects model, retrieval and tool operations through linked traces and their inputs, outputs and timings. | An agent's readable contribution should open its actual call and supporting source records. | Observability data does not prove that a cited passage supports a scientific claim. |
| [Temporal: Web UI](https://docs.temporal.io/web-ui) | Exposes execution history, relationships and pending activities alongside run status and timing. | Separate current state from historical waits/retries, and show the reason for a wait only when saved. | A durable-workflow analogy. Atlas has not acquired Temporal's replay or recovery guarantees by imitating its UI. |
| [OpenHands: architecture](https://github.com/OpenHands/OpenHands/blob/main/docs/architecture.md) | Separates the frontend from the agent server and the execution environment. Its conversation UI includes work-specific panes. | Keep the app a client of authoritative records; show source, task or execution panes when those capabilities have evidence to display. | Coding-agent interface, not a validated scientific research UI. Repository inspection used mutable `main`. |
| [Mastra Studio: observability](https://mastra.ai/docs/studio/observability) | Separates metrics, traces and logs; supports chosen trace columns, exact trace export and linked log navigation. Metrics depend on a supporting store. | Keep raw trace export and operational measures accessible without inserting them into the reader's findings. | Do not fabricate cost, latency distributions or aggregate coverage from incomplete records. |
| [AG-UI: subagent attribution](https://docs.ag-ui.com/concepts/subagents) | Distinguishes agent definitions from individual invocations and carries producer attribution through messages and tool events. It does not schedule agents. | Use saved agent, attempt, message and call IDs for labels and links. Requesting a tool and producing its result can have different authors. | A protocol reference, not a proposed dependency migration. Version compatibility and actual backend event support must be qualified separately. |

The [AgentGUI repository](https://github.com/eth-medical-ai-lab/agent-gui) adds a
concrete local React/FastAPI example. Its office metaphor and automated manager
are optional product choices, not requirements for Atlas. Atlas should retain
peer scientific decisions and its explicitly selected model configuration.

## Priorities, ordered by project fit

### 1. Make authorship and contribution traceable

Use a stable visual badge for each named agent. Show the assignment's author,
recipient and time separately from the response's author and time. A resumed
answer should link to the peer reply it actually received. Never determine the
speaker because an answer happens to mention a name.

Atlas now has backend trace links joining a call's saved input to its peer request,
delivered reply and uniquely identified source attempt. The projection identifies
the snapshot used for those joins and preserves the raw call. Ambiguous references
remain explicit. This aligns with AG-UI attribution and LangSmith's linked detail
pattern; it does not require either framework.

**Check:** use different requester/responder names, repeated peer attempts, an
unrelated name in answer text and an ambiguous result. The UI and API export must
agree, including when they cannot identify one originating attempt.

### 2. Separate current work from mission history

The current follow-up timeline should show its actual attempts, including a peer
check and a resumed contribution. Earlier parent work belongs behind an explicit
history control. When history is included, label gaps without claiming that an
agent was active or stalled throughout them. Request containers without attempts
must not receive fabricated bars.

This is Atlas's adaptation of session-aware review and event-history navigation.
The current implementation uses exact parent-attempt/event identities and saved
timestamps. Whole-minute labels simplify the visual while precise times remain
in Details. A completed attempt's span may include provider waiting; it is not a
measurement of active model computation alone.

**Check:** an earlier parent separated by a long gap must not shrink a short
follow-up into an unreadable line. A history toggle must retain the original times.

### 3. Keep the connection clock separate from the progress clock

Expose the viewer's last successful state read separately from the last saved run
progress. A failed refresh retains the previously saved result and names the
connection problem. A provider quota wait is a run condition, not a disconnected
viewer. Do not add a liveness claim, percentage complete or estimated finish time
without corresponding backend evidence.

This distinction is an Atlas design inference from the separate run, worker and
pending-work concepts in Temporal and OpenHands. It is selected for the current
UI update because the existing polling and saved timestamps already support it.

**Check:** a successful refresh of an unchanged snapshot advances only the viewer
check time; an unsuccessful or superseded response does not rewrite either fact.

### 4. Make longer histories searchable without losing context

Add agent and text filters to Activity, alongside its existing record-category
filters. A peer message belongs in either participant's filter, while the sender
and recipient remain visibly distinct. Show the number of matching saved records
and an understandable empty state. Filters never change execution.

This adapts trace navigation from LangSmith and Langfuse. Further zoom, pagination
and windowed rendering should be introduced against representative long histories,
not assumed necessary because a file or timeline is long. The
[Langfuse timeline update](https://langfuse.com/changelog/2026-08-28-responsive-timeline)
provides a useful later reference for fit, zoom and selected-step navigation.

**Check:** filtering for the receiving peer keeps the actual request/reply context;
searching a missing term does not imply missing research data or cancel a run.

### 5. Keep outputs readable and diagnostics progressively available

Use concise field titles: Attempt, Assignment, Recorded actions and Saved response.
Keep exact text under those titles rather than generating a new story in the
renderer. Put raw payloads and technical identities behind expandable details.
Saved step explanations, where the backend supplies them, should link to exact
input/output versions and state whether work was prepared or actually executed.
Absent explanations remain absent; private model reasoning is not required.

The current local example still has a limitation: a plain assistant answer may
contain a citation in its text without populated structured citation fields.
Traceability must not silently turn that text into a validated evidence link.
Report qualification should exercise prose, formal citations and limitations
separately before claiming a fully integrated scientific output.

### 6. Add richer operational controls only with backend support

A per-agent status roster, reliable wait/cost rollups, source-route coverage,
versioned artifacts and durable annotations would help larger missions. Their
inputs must first distinguish current attempts from inherited history, preserve
retry identities, and specify what was measured. The present call inspector is
useful evidence, but a collection of successful calls is not complete billing or
worker-health telemetry.

Use [AG-UI state snapshots and deltas](https://docs.ag-ui.com/concepts/state) as a
reference for later reconnection. Atlas would need ordered durable events, cursor
gap handling and a recovery contract before claiming resumable live streaming.
Retain polling until that transport is actually implemented and tested.

## Changes selected for this update

* Named agent badges and distinct assignment/response labels, grounded in saved IDs.
* Current-branch attempt timelines, explicit parent history and whole-minute labels.
* Readable call cards with peer-reply links, exact output paths and expandable raw JSON.
* Activity agent/text filters and separate viewer-check versus run-progress times.
* A saved-step disclosure for snapshots that actually contain step records.

The first three primarily address direct user feedback; the external review
supports their direction. The filter and freshness distinction are additional
scoped findings from this review. No tracing vendor, model provider, runtime
framework or sandbox is installed by this UI work.

## What to defer and how to evaluate the result

Defer a decorative office, a mandatory central manager, automatic model switching,
and scientific approval gates. Do not copy telemetry that Atlas cannot supply.
Keep persistent annotations, event replay, multi-hour worker health and deep
resource aggregation in their backend implementation scope.

Use the same saved parent/follow-up records for a short human review: identify who
asked the question, who answered it, which evidence was used, what changed and why
a wait occurred. Record confusing answers and revise the UI. Do not treat passing
component tests or the small AgentGUI study as proof of Atlas's usability or
long-duration research quality. No additional model run is needed for this review.

## Source-inspection notes

Supporting frontend examples were inspected at mutable `main`:
[OpenHands conversation tabs](https://github.com/OpenHands/OpenHands/blob/main/frontend/src/components/features/conversation/conversation-tabs/conversation-tabs.tsx)
and [Mastra latency view](https://github.com/mastra-ai/mastra/blob/main/packages/playground-ui/src/domains/metrics/components/latency-card-view.tsx).
No pinned upstream build was executed, no upstream code was copied, and no
performance comparison against those products was run. The resource table records
observed patterns; the prioritized decisions are the project's adaptation.
