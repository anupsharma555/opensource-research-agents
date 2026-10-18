---
name: os-run-details
description: Explain an exact OpenSource Research Agents run or parent/follow-up sequence from saved evidence, in chat or a requested private report. Use for readable summaries, numbered steps, actual output, diagnostics and usage; this does not start a new evaluation.
metadata:
  version: 0.1.8
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

Lead with a readable summary of the practical task, the agent's main actions,
the actual outcome and why it completed or stopped. Include key measured usage
when useful. Put timestamps, run IDs and reconstruction notes after this summary.

For an evaluation run, explain why the case was chosen, what capability or
uncertainty it targets, and any staged condition in separate run metadata.
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
   actions or final text, labeled as such; do not invent private reasoning or
   reveal hidden chain-of-thought, scratchpads or restricted provider reasoning.
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
   output separately. Check consequential claims against the exact permitted
   passages and counterevidence; label scientific assessment separately from
   execution status. A citation pointer alone is not support.
4. **Usage, timing and state.** Count every attempted provider request, including
   failures and uncertain dispatches, separately from captured responses and
   usage-bearing responses. Report provider-reported input, output and total
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

Use this reading order for a full saved-run report; shorten it for a narrow chat
question without dropping the requested facts:

1. **Summary:** one or two readable paragraphs covering the task, meaningful
   actions, outcome and reason for stopping. A status code alone is not a summary.
2. **Original task input:** quote the exact question and any additional scope
   instruction. Label scope as input, not editorial summary; verify whether its
   wording reached the model rather than inferring delivery from saved settings.
3. **Run metadata:** separate provider, model and API-key/credential profile
   (for example, Groq with the Backup API-key profile). Include active runtime
   skills, allowances, dates/timezone, parent lineage and material available at
   launch. Place evaluator conditions here: whether choices were scripted and
   which review criteria were withheld. Do not present those as worker input.
4. **Numbered steps:** give each meaningful action a plain-language title, then
   **Input sent → Action → Returned result → What changed**. Preserve call counts,
   decisive arguments, queries and reading locations when grouping repetitions.
   Distinguish returned-but-saved results from the shortened projection actually
   sent next. A pending result must not imply later arrivals were already present.
5. **Final output and support:** include the complete requested answer unchanged,
   with its evidence assessment separate. If none exists, say why; tool calls are
   not an omitted prose answer. A provider-only test may intentionally have none.
6. **Diagnostics:** briefly state the observed problem, decisive evidence, likely
   cause with uncertainty, what worked, and remaining repair or validation. Keep
   independent failures separate. Explain which earlier fix actually worked and
   its proof limit; cached fetches or deduplicated text do not prove useful progress.
   Do not infer the model's interpretation from an action alone or propose a new
   repair as though it has already been implemented or tested.
7. **Usage and stopping point:** include per-call accounting, totals, waits,
   binding limits, current saved state and unresolved unknowns as described above.
8. **LangSmith and local trace view:** when tracing is available, include the
   exact run's LangSmith link and a readable summary of its metrics-only Output,
   including per-loop tool activity. Keep this beside the local trace link so
   the report remains usable without the website. State whether cloud content
   was visibly inspected, export was only acknowledged, or export remains pending,
   uncertain or disabled. If unavailable, say so briefly; reporting does not
   authorize a new export or research call. Read the [trace reporting guide](references/mission-records.md#report-local-and-langsmith-traces)
   for timing, cache coverage and next-request delivery distinctions.

For saved report files, keep large technical records in a **linked companion**
(for example, `RESULT-TECHNICAL.md`) rather than appending them to the readable
report. Link it near the report navigation and at the end. Give the companion a
short technical index and a return link. Put full tool arguments/replies, exact
source windows, candidate inventories, hashes and receipt IDs there; keep the
main report self-contained with numbered steps, substantive findings, actual
answer and usage. Do not replace a requested answer with a link or raw JSON.

Before delivery, verify both directions of local links, sequential steps and
attempt counts, totals against receipts, exact accepted output, and the distinction
between source availability and actual model delivery. Review the summary and
diagnostics for readability, unsupported causality and stale repair claims. Keep
raw request packets, source bodies and private identifiers out of tracked files;
never reproduce credentials, private instructions or hidden reasoning in either
artifact. Reporting alone does not justify another API run or a full test suite.

Use `$os-qualify-run` when the user also wants a deeper execution or scientific
qualification, and `$os-groq-usage` for broader account-period accounting. Read
those skills before applying them. Their helpers may write private receipts, so
do not run them merely to produce this read-only chat snapshot. Missing capture,
unsupported record shapes and unavailable host state remain explicit limits.
