---
name: os-run-details
description: Explain an exact OpenSource Research Agents run or parent/follow-up sequence from saved evidence, in chat or a requested private report. Use for readable summaries, numbered steps, actual output, diagnostics and usage; this does not start a new evaluation.
metadata:
  version: 0.1.20
---

# Explain a saved research run

Give the user one readable, end-to-end account of the **selected** run. Default to
read-only inspection. Do not start or resume a host, submit or retry work, alter
holds, change profiles, or invoke providers to fill a gap. Do not open Atlas
unless the user asks for interface evidence. This is a Codex development skill;
research agents do not load it.

When the user asks to save or revise reports, write the report artifacts while
keeping runtime state, original receipts, profiles and holds unchanged. Keep
existing report links working and preserve prior versions when reorganizing them.

First identify the exact state directory, run ID, parent/follow-up chain and
observation time. Establish whether records are live-provider, injected, replayed
or synthetic. Read current `AGENTS.md`, `README.md` and the relevant runtime
documentation before interpreting records. For the mission runtime, read
[mission record map](references/mission-records.md); use the selected runtime's
documented read-only path if its records differ. Do not broaden to other private
workspaces merely because they are available.

Make the report understandable without prior knowledge of the runtime. Define
unfamiliar terms briefly where they first appear, using a plain-language
explanation followed by the technical term. Explain distinctions that matter to
the selected run, such as a saved search result (search hit), a retained copy of
source material (source copy), a specific cited segment (passage/span), and records linking
evidence to its origin and use (provenance). Explain status labels by their actual
meaning; for example, host acceptance does not establish scientific correctness.
Avoid a separate exhaustive glossary or repeating definitions already given.

Immediately below every saved report's title, show **Report completed:** with
the current revision's completion date, time and timezone. Use **Report updated
(draft):** until finalized. This timestamps the report, not mission completion;
keep run timing separate. Apply this to `report.md`, `RESULT.md` and batch reports.

Lead the summary with one short, self-contained paragraph: what the task was about, what
the test was checking (for evaluations), and what actually happened. Name the
practical subject and supplied material before agent actions or status labels.
Include the decisive outcome and stopping reason without implying an unobserved
step succeeded. Put timestamps, run IDs and reconstruction notes after this summary.

For an evaluation run, explain why the case was chosen, what capability or
uncertainty it targets briefly in the opening summary; keep detailed staged
conditions and evaluation design in separate run metadata.
Name the concrete behaviors being checked and pair each
with the actual action, request, revision or output that supports the result;
distinguish demonstrated behavior from unexercised or inconclusive checks.
Explain what the observed outcome establishes and what it
leaves untested. This framing belongs in the user's report, not in a reconstructed
claim that the worker saw the controller plan or withheld review. For ordinary
research runs, explain the requested outcome without inventing an evaluation.

Reconstruct the sequence from the original request through accepted output:

1. **Input.** Show the saved user question and scope, supplied material or source
   identities, chosen agents, model/settings and credential *profile label*. For
   follow-ups, identify the exact parent snapshot and accepted target. Check the
   captured request for what evidence and task text actually reached each model
   call. A source in the library or a prepared intent alone does not prove delivery.
   Separate worker-facing task/evidence from controller conditions and withheld
   review. Summarize repeated system/developer scaffolding and tool definitions;
   never expose secrets or private instructions.
2. **Actions.** Walk through each meaningful model response, tool choice, saved
   tool result, local rejection/error, peer exchange and accepted artifact in time
   order. Group repetitive reads while preserving counts and decisive arguments,
   results and source references. Distinguish a controller stopping before a
   tool executes from a saved host/tool rejection; do not infer an external effect
   from a tool name alone. Say what the agent visibly chose and what that choice
   accomplished. Explain apparent rationale as an inference from its
   actions or final text, labeled as such; do not invent deliberation. Keep
   Codex's private reasoning and instructions out of reports. For reasoning
   explicitly emitted by OSRA's research provider, use the separate captured
   artifacts and the step-level format below; never reconstruct missing text.
   For each distinct requested tool, include its plain-language purpose, the kind
   of input it expects, and what it actually added in this run. Distinguish new
   source content from citation/provenance metadata, saving an output, and rereading
   information already in context. Explain whether it reads or writes local state
   or can contact an external service, using the actual handler and saved request
   rather than the tool name. When relevant, explain whether the step was needed,
   redundant, or the wrong operation, and whether direct reading or a direct final
   answer was supported. Do not assume a local tool execution adds another model
   API call. A compact **Tool / What it does / What happened here** table can keep
   these explanations within the action sequence without repeating every call.
   For searches, report the actual query terms, stated information need and chosen
   mode/route. Distinguish agent choices from backend provider selection, fallback
   and query transformations. Show whether work was new, reused or explicitly
   refreshed, actual retrieval attempts, results returned and what was inspected.
   Read [search activity](references/mission-records.md#report-search-activity) for
   the saved-record map and compact reporting format. Missing capture stays unknown;
   do not infer hidden provider-managed queries or report a saved-result read as a
   new external search. If no search occurred, say so briefly when relevant.
   For documents and figures, distinguish selectable-text extraction, local OCR
   and auxiliary vision. Report selected pages, actual models/profile, separate
   attempts/usage and whether derived text reached a subsequent reasoning call.
   OCR transcription and generated visual interpretation are not checked citation
   passages; follow [document reading](references/mission-records.md#report-document-reading).
3. **Output and support.** Put the actual accepted final answer in the requested
   report surface; when the user asks for full output in chat, reproduce it there.
   Preserve its substantive wording and citations,
   with narrow redactions for credentials or unrelated private identifiers. If
   long, give the full requested text in parts rather than replacing it with an
   artifact link. Identify draft, direct model text, published report and accepted
   output separately. For each peer contribution, identify its author, intended
   recipient, question addressed, delivery and review status before the text.
   Say whether a combined final answer exists; saving a reply is not peer approval.
   Follow [contribution labels](references/report-format.md#agent-contributions-in-the-output-section)
   to separate the agents' output from the evaluator's assessment.
   Check consequential claims against the exact permitted
   passages and counterevidence; label scientific assessment separately from
   execution status. A citation pointer alone is not support.
4. **Usage, timing and state.** Count every attempted provider request, including
   failures and uncertain dispatches, separately from captured responses and
   usage-bearing responses. A local dispatch marker alone does not establish an
   HTTP attempt: reconcile it with transport receipts and the test's HTTP ledger,
   and label locally withheld or unconfirmed dispatches separately.
   Report provider-reported input, output and total
   tokens by call and total when available; reasoning and cached tokens are
   subsets, not additions. State unknown usage as unknown. Separate configured
   maxima, estimated usage and provider totals. Give saved start/end times,
   elapsed/wait intervals only where timestamps support them, current run/task
   state, continuing work, model/profile selections, and any observed hold or
   restoration. A completed report may coexist with continuing work.
   When tokens stop progress, name the binding limit: whole-test allowance,
   provider rate or daily quota, model context window, or local request-admission
   policy. Read the [budget and admission explanation](references/mission-records.md#explain-budget-and-admission-stops)
   for the mission runtime's evidence and arithmetic. Explain what space remains
   for useful input, whether the request reached the provider, and whether waiting,
   a larger test allowance or a smaller request would address the observed cause.
   Keep provider measurements, local estimates and future-output reservations
   distinct; do not describe a rate-derived local stop as the model's context limit.

## Report organization

For the full **input-to-output report**, read
[report format and field definitions](references/report-format.md). Use grouped
**Label: value** lines for routine facts and short prose for interpretation.
State a fact once in its relevant group; do not repeat the same metadata in a
paragraph. Preserve the original task, meaningful actions, complete actual answer
and evidence assessment. Shorten the format for a narrow chat question without
dropping requested facts.

Make labeled fields understandable on their own. For Partial, missing, rejected
or failed results, append one short explanation of the recorded reason and its
practical effect. Do not leave the reader dependent on a later diagnostic paragraph
to understand the line; keep the fuller evidence and uncertainty there.

1. **Summary:** one short paragraph answering **What was this about? What was
   being tested? What happened?** Include meaningful actions and the stopping
   reason. A status label or recovery description alone is not a summary.
2. **Run overview:** labeled type, evidence mode, outcome, primary model and any
   vision/auxiliary models actually used, provider/API-key profile, agents and
   timing. Include **Total elapsed time** with its measured start/end boundary
   (for example, first HTTP dispatch to last HTTP response). For a continuation,
   label the selected segment; do not present it as the full mission duration.
   Keep active HTTP time separate, and use Unknown when timestamps are missing.
3. **Task and input verification:** quote the exact question and additional scope.
   Use coverage fields for saved request capture and verified task/source inclusion;
   preparation alone is not delivery. Include actual web providers and available
   distinct-source counts, separating new retrieval from retained material. Place
   an **Execution** group between **Input coverage** and **Skills and review**:
   count model inference HTTP attempts and successful, failed or unknown responses
   separately from search/source retrieval HTTP attempts, telemetry export HTTP
   attempts and local tool outcomes. Export-record counts do not establish telemetry
   HTTP-attempt counts. List tools in chronological step order with each outcome;
   keep optional aggregate counts separate. Include calls returning no tool choice
   so their place in the sequence remains clear. Then count automatically loaded skills separately from
   on-demand activation attempts and successes; zero activation calls does not mean
   no skills were loaded. List peer-review stages separately from independent
   post-run review. Keep evaluator conditions distinct from worker input.
4. **Numbered steps:** give each meaningful action a plain-language title, then
   **Input sent → Action → Returned result → What changed**. Preserve call counts,
   decisive arguments, queries and reading locations when grouping repetitions.
   For peer exchanges, show sender → recipient and the actual question, reply or
   feedback in the step; a tool name or topic paraphrase is not the exchange.
   Follow the [agent exchange format](references/report-format.md#agent-exchanges-in-numbered-steps).
   Distinguish returned-but-saved results from the shortened projection actually
   sent next. A pending result must not imply later arrivals were already present.
   Give each inference step a **Reasoning trace** or existing
   **Provider-emitted reasoning** capture label with capture status,
   fidelity, reported reasoning-token count and a direct link to its private,
   human-readable `reasoning_trace.md`. Place the link directly after the step's
   capture description. Keep the main report's action purpose or a concise
   reviewed finding separate from the provider trace. Include full trace text
   inline only when the user explicitly requests it; the linked per-step file
   must then still remain available with exact text/JSON. End the report with one
   combined human-readable `reasoning_trace.md`. Read the
   [provider reasoning format](references/report-format.md#provider-reasoning-in-each-step)
   for unavailable captures, intermediate calls and formatting boundaries.
   When exact request capture permits it, include a short **Local input reuse**
   line for each model call and link its measured comparison row in the private
   trace or technical companion. Name the prior comparable call and distinguish
   a first-call baseline, skipped overlapping call, or unavailable comparison
   from measured reuse. Do not turn shared prompt text into a provider cache hit.
5. **Final output and support:** include the complete requested answer unchanged,
   with its evidence assessment separate. If none exists, say why; tool calls are
   not an omitted prose answer. A provider-only test may intentionally have none.
6. **Diagnostics:** briefly state the observed problem, decisive evidence, likely
   cause with uncertainty, what worked, and remaining repair or validation. Keep
   rejected actions understandable: name who rejected them (provider, OSRA
   backend, or evaluation controller), explain the concrete mistake or unmet
   condition in plain language, and state what was or was not saved or executed.
   Identify the supported next action when known; do not substitute an opaque
   status or field name for the explanation.
   independent failures separate. Explain which earlier fix actually worked and
   its proof limit; cached fetches or deduplicated text do not prove useful progress.
   Do not infer the model's interpretation from an action alone or propose a new
   repair as though it has already been implemented or tested.
7. **Usage and stopping point:** include per-call accounting, totals, waits,
   binding limits, current saved state and unresolved unknowns as described above.
   Order token columns Input → Reasoning (subset) → Completion → Total, with
   Total rightmost; reasoning is included in completion, not added again.
   Report the measured **Local input reuse** percentage for the selected run,
   its numerator/denominator and call coverage, or an explicit unavailable
   status. Use the saved run metadata and exact captured-request comparison;
   state the same-run/actor/task/provider/model/profile scope and the skipped
   calls. Keep this local estimate separate from provider-reported cached input
   and its own reporting coverage. Read the [metric definition](references/report-format.md#local-input-reuse-and-provider-cache).
   When saving a run report, keep a private machine-readable local-reuse
   artifact with the run ID, method, coverage and per-call comparison rows, and
   link its readable per-step table. Recompute from saved captures without
   mutating historical runtime records or claiming a new provider test.
8. **LangSmith and local trace view:** when tracing is available, include the
   exact run's LangSmith link and a readable summary of its metrics-only Output,
   including per-loop tool activity. Keep this beside the local trace link so
   the report remains usable without the website. Include a LangSmith Output
   Yes/No/Unknown field with its saved/exported basis. State whether cloud content
   was visibly inspected, export was only acknowledged, or export remains pending,
   uncertain or disabled. If unavailable, say so briefly; reporting does not
   authorize a new export or research call. Read the [trace reporting guide](references/mission-records.md#report-local-and-langsmith-traces)
   for timing, cache coverage and next-request delivery distinctions.

For saved report files, keep large technical records in a **linked companion**
(for example, `RESULT-TECHNICAL.md`) rather than appending them to the readable
report. Link it near the report navigation and at the end. Give the companion a
short technical index and a return link. Put full technical tool envelopes, exact
source windows, candidate inventories, hashes and receipt IDs there; retain the
ordinary agent-to-agent message text in the main numbered steps. Keep the
main report self-contained with numbered steps, substantive findings, actual
answer and usage. Do not replace a requested answer with a link or raw JSON.

Before delivery, verify both directions of local links, sequential steps and
attempt counts, totals against receipts, exact accepted output, and the distinction
between source availability and actual model delivery. Review the summary and
diagnostics for readability, unsupported causality and stale repair claims. Keep
the local input-reuse percentage, denominator and per-call link consistent with
the saved metric, without filling unavailable calls with zero. Keep
field explanations only where needed; use Partial, Unknown or Not applicable
instead of forcing every finding into Yes/No. Preserve timestamped prior report
copies when reformatting, and verify that detail moved to the companion remains
reachable. Keep raw request packets, source bodies and private identifiers out of
tracked files. Provider-emitted reasoning belongs only in the user's authorized
private report/export destination, separate from answers and tool records. Do
not copy it into cloud telemetry, runtime peer context or tracked examples.
Never reproduce Codex conversation logs or private instructions in these exports.
Reporting alone does not justify another API run or a full test suite.

Use `$os-qualify-run` when the user also wants a deeper execution or scientific
qualification, and `$os-groq-usage` for broader account-period accounting. Read
those skills before applying them. Their helpers may write private receipts, so
do not run them merely to produce this read-only chat snapshot. Missing capture,
unsupported record shapes and unavailable host state remain explicit limits.
