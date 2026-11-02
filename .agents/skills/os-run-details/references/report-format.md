# Full input-to-output report format

Use when creating or reformatting a saved `report.md`, `RESULT.md` or batch report. This is a readable account
of the whole run, not a replacement for the actual answer or the local trace.
[SKILL.md](../SKILL.md) owns the workflow; [mission records](mission-records.md)
owns evidence lookup. This guide owns presentation and field meanings.

## Reading order

Immediately below the title, place **Report completed:** followed by the date,
local time and explicit timezone of this finalized report revision, for example
`October 23, 2026 · 9:58 a.m. EDT (UTC−04:00)`. While unfinished, label it
**Report updated (draft):** instead. Refresh the timestamp when revising the report;
do not infer an old completion time from a run timestamp or present a revision
time as the mission's completion time. Preserve the prior report when revising it.

1. **Summary:** one short paragraph stating the practical task and material,
   the behavior being tested, and what actually happened. Follow-ups briefly
   identify what remained to be checked. Ordinary research reports omit an
   invented testing objective. Status labels supplement this account, not replace it.
2. **Run overview:** grouped labeled lines for identity and timing.
3. **Task and input verification:** exact question/scope, then **Input coverage**,
   **Execution**, and **Skills and review**, in that order.
4. **Numbered steps:** each step has a descriptive heading and short **Input**,
   **Action**, **Result**, **Change** lines where those distinctions add information.
   For a rejection, the Result names the rejecting component, the concrete reason,
   and the effect. For example: "OSRA's backend rejected the reply before saving
   it because the agent targeted its own outgoing question. Its peer had already
   answered; the pending action was to review that answer." Keep technical error
   codes in supporting detail when they do not help explain the result.
5. **Actual output and assessment:** identify who contributed each part, show the
   complete answer unchanged, then separate the evaluator's assessment.
6. **Reasoning summary, diagnostics, usage and traces:** explain important choices
   and failures in the main report, followed by accounting and evidence links.

Place the linked technical index after the summary and at the end. Keep IDs,
hashes, complete technical tool envelopes, source windows and long inventories in
the companion, with a return link. Preserve current report links and timestamped
originals. In a batch summary, compare outcomes briefly and link each full report.

## Agent exchanges in numbered steps

For a peer request, reply or review, include **From → To** and the **Exact request**,
**Exact reply** or **Exact feedback** as a readable quotation within that step.
Use captured ordinary message/tool-argument text, not reconstructed reasoning.
Keep this substantive exchange in the main report rather than only in raw JSON
or the technical companion; readers should see what was asked and answered.
Include a **Reason stated by the agent** when ordinary assistant content or an
explicit `reason`, `purpose` or information-need field provides one. Otherwise
say **Not provided**; keep any useful post-run interpretation separately labeled.
Do not present a question's topic as proof of why the agent chose to ask it then.

Preserve changed wording when distinguishing a repeated request from a new one.
Quote a long draft once in the main report and link back to that step from its
assessment. Keep tool envelopes and identifiers in the companion unless needed
to understand the message. State missing capture or necessary redaction explicitly.
Continue distinguishing saved requests, recipient model delivery, replies and
review; a quoted outgoing request does not prove the recipient received its text.
Omit these exchange fields for steps without an agent exchange. Do not include
private instructions or Codex's internal reasoning.

For a combined tool such as `peer_turn`, keep one inference step and one selected
tool count, then list its reply/report, review and follow-up outcomes separately.
Use the saved operation results and acceptance records: a valid reply may persist
when an independent review fails, while a saved revision may remain provisional
because a follow-up was rejected. Do not turn a mixed result into whole-step
success, or count its local operations as additional model HTTP calls.

## Agent contributions in the output section

State **Final combined answer: Produced / Not produced / Unknown** before showing
individual contributions. For multiple agents, use a compact contribution table
or a heading such as **Author's reply to Recipient — Subject**. Before each full
reply, give **Author**, **Recipient / requester**, **Question addressed**,
**Contribution**, **Delivery** and **Review status** where relevant. Link back to
the producing step instead of repeating its full action history. Keep the actual
reply wording intact and display it once; link to it from other steps or sections.

Use concrete status text: **Saved by OSRA; not yet reviewed by the requester**,
rather than an unexplained **accepted reply**. Distinguish full/partial delivery,
an explicit review disposition and incorporation into a final answer. Name the
author of a combined answer and which peer contributions it demonstrably used;
do not infer equal contribution or agreement from participation or delivery.
If there is only one author, omit unnecessary recipient/collaboration fields.

Place **Assessment by: evaluator / independent reviewer** after the outputs.
Identify the contribution being assessed and keep that assessment separate from
agent-authored content. Missing authorship or review evidence stays Unknown;
never invent a contribution or mark a saved artifact as scientifically endorsed.

## Provider reasoning in each step

Every inference step in a private run report has a **Reasoning trace** label (or
retains an existing **Provider-emitted reasoning** capture label) and a
direct link to a private, human-readable per-step `reasoning_trace.md`. Use only
the exact reasoning field saved from that step's research-model
response or its linked existing trace. Include intermediate tool-selection calls,
peer calls and auxiliary model calls; match by exact run, agent, call/attempt and
receipt references, not merely by nearby timestamps. A local tool-only step has
no separate model reasoning and should link to its producing inference step.

- **Capture:** Available, Truncated, Unavailable or Unknown, with a concise reason.
- **Fidelity:** Exact provider text, unredacted legacy saved text, or previously
  redacted saved text. Never describe a redacted legacy capture as an unmodified
  original; it cannot be restored by inference or reconstruction.
- **Reasoning tokens:** Provider-reported count or Unknown; a completion subset.
- **Reasoning trace:** Append the step's Markdown link directly after its brief
  capture description, for example, `**Reasoning trace:** Returned prefix;
  truncated at the output limit. [Step 2 reasoning](.../reasoning_trace.md)`.
  Keep a short public purpose or verified semantic finding in the main action
  account; file presence and token counts alone are not semantic review. Embed
  the complete text inline only if the user explicitly asks for it.
- **Trace files:** The per-step Markdown must contain all saved text for that
  step, safely escaped so HTML, images and link syntax from provider output
  cannot fetch remote content. Formatting may add headings or quote markers,
  but must not paraphrase, omit or invent emitted text. Link its byte-exact
  `reasoning_trace.txt` when text exists and its JSON metadata. If unavailable,
  show the status/reason and JSON link rather than a blank or fabricated text
  block. Keep all three files private.

At the end of the report, include **Full provider reasoning:** a direct link to
one combined `reasoning_trace.md`, ordered by inference step and labeled with
agent, provider/model, timestamp and capture status. Keep exact per-step text and
structured JSON accessible from it. Do not hide the step text only in a technical
index. Preserve an earlier report copy before adding these sections.

Use the automatic local artifacts when present. For a saved run, the repository's
`scripts/dev reasoning-export --state-dir <state> --run-id <id>` creates a private
export from existing records without inference. Verify the command and returned
paths before linking. If an older export lacks per-step Markdown, create each
private reading file from its matching saved JSON using the exporter's safe
escaping convention; verify exact step identity, text parity and local links.
Never fill a missing field from token counts or another call. An absent receipt
is not a zero-token or successful step;
keep unsuccessful/uncertain attempts visible with their missing-capture reason.
A local dispatch marker can precede a transport refusal or local budget stop;
count it separately unless a transport receipt or HTTP ledger establishes that
the request was attempted. The number of exported steps is not an HTTP count.

The provider trace is recorded model output, not proof that its explanation is
faithful, that its tools executed, or that its scientific claims are correct.
Keep **Reason stated by the agent** for public `purpose`/`reason` arguments and
**Post-run interpretation** for reviewer analysis as separate fields. This
reporting rule never authorizes capture of Codex's own private reasoning, new
model calls, changing provider settings, or uploading raw reasoning to LangSmith.

## Checklist outcomes

For a run linked to acceptance checks, include **Issue / exact checkbox label /
result / evidence / tracker state** in the main report. Evaluate the full stated
threshold, including required variants, rather than inferring closure from a
successful HTTP response or accepted answer. Keep already-completed items separate
from items newly completed by this run. Use Completed, Partial, Failed or
Unexercised for verification; record tracker state as checked, still open or not
verified. If evidence supports closure but no authorized update was saved, say
**Eligible; tracker still open**. Read back every checkbox update before reporting
it checked. The chat handoff names newly completed issue/check labels, or explicitly
states **None newly completed**, followed by the most consequential remaining gap.

## Labeled fields

Use `- **Label:** value` lines in small relevant groups, not one giant checklist.
Add a short explanation underneath only for an exception or interpretation. Use
tables for comparisons and per-call numbers. Omit unrelated optional categories;
do not omit an expected capability merely because it failed or was untested.
For an exception, prefer a brief explanation on the same line: what happened,
why (when verified), and what it means. For example, "Report text: first page
delivered; later omitted to fit newer results. Full text remains saved." Keep
longer diagnostic analysis separately, without making the field opaque alone.

**Identity and outcome**

- **Type:** single-agent control, peer collaboration, retrieval-only, follow-up,
  or another accurate description of the case design.
- **Evidence mode:** live model on retained evidence, live retrieval with model
  execution, replay, or injected fixture. Registered peers do not prove collaboration.
- **Outcome:** the useful result or reason for stopping. **Saved mission status**
  is a separate field when it differs from the evaluation outcome.
- **Provider**, **Primary model/settings**, **Credential profile**, **Agents**, and
  **Parent run** where applicable. Add **Vision model** and other auxiliary models
  when relevant, with their exact saved identifiers, provider/profile and separate
  attempts/usage. A reasoning model and a vision model can both appear in one run
  (for example, GPT-OSS and Qwen). Use “Not used” when configured but never called;
  do not substitute current defaults for recorded models. For several models,
  a compact role/model/provider/profile/calls table is clearer than one mixed label.
  Use Primary/Backup API-key profile labels,
  never key values or the misleading provider name “Backup Groq.”

**Timing and closure**

- **Started**, **Mission completed**, **Evaluation stopped**, and **Host shutdown**:
  use available evidence and the displayed timezone. For an unfinished mission,
  completion is “Not completed”; never substitute its observation end time.
- **Total elapsed time:** include in the overview using readable minutes/seconds
  and explicit endpoints, such as first HTTP dispatch to last HTTP response.
  Label a follow-up duration as the selected continuation, not the whole mission.
  Use Unknown if either timestamp is unavailable; never invent completion.
- **Model HTTP time**, **Tool active time**, and **Provider wait**: report separately
  when measured. Elapsed time can include preparation and pacing; do not label
  the remainder as provider wait without evidence. Overlapping intervals are not additive.

**Input coverage**

When expected material is absent, explain the saved reason briefly: initial
paging, later budget omission, permission restriction or unavailable capture.
Distinguish data still saved locally from text actually sent on that call. If the
cause is inferred rather than recorded, label that uncertainty and link the evidence.

- **Request payloads captured:** count saved exact request bodies matched to
  observed HTTP attempts, such as “Yes — 2/2 sent requests.” Record any additional
  prepared-only packets separately. This is not a response-capture count.
- **Task/scope/source present:** inspect the saved sent packets and report coverage,
  such as “Yes — full question, scope and supplied passage in 2/2 sent requests.”
  Separate these fields if coverage differs. Source availability, a pointer or an
  inclusion receipt alone does not prove full text delivery or use.
- **Starting material:** a concise source identity/link and retained/fresh status.
- **Web providers used:** actual provider routes observed, with calls by provider
  when recorded; configured availability does not count as use. Distinguish
  search providers, page fetchers/renderers and model-provider browsing.
- **Web sources:** available counts of distinct discovered, fetched/inspected,
  and cited sources, labeled by stage. Count distinct source URLs/documents after
  recorded deduplication, not queries, snippets, passages or copies of the same
  document. Show retained sources supplied separately from new retrieval. If a
  provider hides its internal searches or identities are insufficient, use Unknown
  or a labeled partial count. Include queries/routes in the numbered steps when used.

**Execution**

- **Model inference HTTP attempts:** observed requests to reasoning/vision models,
  including retries; name their providers. **Successful inference HTTP responses:**
  2xx responses. **Failed inference HTTP responses:**
  HTTP errors. **Unknown HTTP outcome:** dispatched attempts without a confirmed
  response. These categories reconcile to attempts; prepared records do not add calls.
- **Search/source retrieval HTTP attempts:** separate from inference, with actual
  search/fetch providers. **Telemetry export HTTP attempts:** a separate category;
  if the saved ledger does not count them, say so. Acknowledged export records are
  not an HTTP-attempt count. Never label a model-only subtotal “total HTTP calls.”
  State incomplete transport coverage.
  When a supplied-evidence case says “no web research” or “no external actions,”
  preserve the original wording and clarify which research/tool actions were
  restricted versus separately authorized model inference and tracing. Do not
  describe a hosted model call as offline or treat it as a web-search request.
- **Tool outcomes:** distinguish successful transport from accepted, rejected,
  pending or failed tool work and from answer quality.

**Skills and review**

- **Runtime skills loaded:** count distinct skill/version instruction packages
  actually included, identifying automatically loaded packages and per-agent
  differences when relevant. **Automatic-loading basis:** inspect the run's pinned
  role/catalog defaults and saved configuration; do not infer loading from a tool
  name or from an instruction package being available. When asserting delivery,
  verify the instruction content in sent packets. Loading guidance alone does not
  execute its tools or imply compliance with it;
  **Optional skills activated:** observed activation. Discoverable is not loaded,
  and loaded is not proof of productive use. Keep Codex reporting skills separate.
- **On-demand skill activation attempts:** explicit activation requests, listing
  skill names and counts when present. **Successful on-demand activations:**
  activations confirmed by saved tool outcomes; distinguish failed/unknown attempts.
  Use these precise labels when the user asks for “skills called” and successful
  skill calls. A preloaded instruction package
  is not an activation call, and an ordinary tool call is not automatically a
  skill call. For no attempts, report 0 attempts and 0 successful on-demand
  activations while retaining the separate loaded-skill count. Report automatic one-time guidance
  separately when present. Activation success does not establish answer quality.
- **In-run peer review:** state requested/performed/read/considered separately when
  relevant, with counts or a short basis. A registered peer, delivered message,
  saved answer and requester uptake are different observations.
- **Independent post-run review:** whether a separate reviewer assessed the saved
  run, who or what kind of reviewer, and its scope. Do not count it as runtime
  peer participation or skill validation. Unknown reviewer independence stays unknown.
- **Evaluation conditions:** allowances, prescribed choices or natural choices,
  and withheld review criteria. Label these as controller metadata, not worker input.

**Usage, traces and diagnostics**

- **Reasoning summary:** when explaining agent decisions, give an evidence-based
  account in the main report of what information/actions were available, what was
  selected, plausible explanations and alternatives, and why the result followed.
  Label this as post-run analysis, distinguish observed causes from inferred
  motives, and state unknowns. Do not substitute private internal chain-of-thought
  or invent deliberation. Keep reasoning-token counts separate from this narrative.
  Avoid repeating the same explanation in both the steps and diagnostics.
- Report input, completion and total tokens with coverage. Reasoning is a
  completion subset; cached input is an input subset. Failed-call usage can remain
  unknown despite known usage on successful calls. Reservations are not charges.
  Put identifiers/outcomes first, then **Input | Reasoning (subset) | Completion |
  Total**, with Total at the far right. State **Total = Input + Completion**;
  the reasoning column is already included in Completion. Keep other metrics
  outside those columns so they do not displace Total or suggest double-counting.

### Local input reuse and provider cache

- **Run-wide confirmed cache share:** sum of provider-reported cached input divided
  by all provider-reported input across sent calls. When only some calls report
  cache, label this a minimum confirmed share, not the actual whole-run hit rate;
  omitted fields remain unknown. State incomplete input-usage coverage if present.
  **Cache reporting coverage:** calls with valid cache counts / sent responses and
  their share of reported input. Optionally show the cached percentage within that
  reporting subset as a secondary metric, with its narrower denominator explicit.
  If no cache counts are reported, use Unknown rather than presenting 0% as a hit
  rate. Keep the local input-reuse estimate separate.
- **Local input reuse:** report the saved local structural-input reuse percentage
  as repeated structural input tokens / measured structural input tokens, with
  both token counts and the measurement scope. It compares exact captured
  request messages and tool definitions only within the same run, actor, task,
  provider, model and credential profile. Include a first request in each scope
  as a zero-reuse baseline in the measured denominator. Give the count of
  measured calls and the count skipped for overlap or unavailable request/token
  capture; preserve the saved coverage status `complete`, `partial`,
  `baseline_only` or `unavailable`. If there is no measured follow-up pair,
  display the percentage as Unknown even if a baseline request exists.
  Do not use the narrower follow-up message-prefix percentage as the full-run
  metric. Link the per-call comparison rows in the private technical companion
  or saved trace and label each numbered inference step with its own measured
  share or its specific baseline/skipped/unavailable status. This is local
  tokenized structure overlap, not a provider-confirmed cache hit, charged-token
  reduction or latency saving. Provider cached-input coverage remains its own
  separate field.
- **Changed tool-list diagnostic:** when available and useful, separately report
  how many exact individual tool definitions survived a list change and their
  share of separately tokenized current definitions. This ignores order and
  explains continuity that the stricter whole-list metric does not credit.
  Do not add these token counts to the primary reuse numerator or compare them
  with its whole-array denominator: tokenization across boundaries is not additive.
  Keep this a local diagnostic, not a provider cache hit. Missing/ambiguous tool
  identities and invalid call comparisons remain unavailable, not zero overlap.

### Trace links

- **Local trace:** link. **LangSmith Output:** Yes/No/Unknown, with a short basis
  such as “Yes — metrics-only output saved; export acknowledged.” This identifies
  the trace summary, not the model answer. A local payload with no export must
  say “Local only”; it does not establish website availability.
  **LangSmith export:** acknowledged/pending/uncertain/disabled
  with counts when available. **Website inspected:** Yes/No/Unknown. Include an
  exact verified trace URL when available; do not invent one to fill the field.
- **Observed issue**, **Evidence**, **Cause**, **What worked**, **Next check**:
  use only the relevant labels. Qualify inferred causes. Date later repairs so a
  failed historical run does not appear to have succeeded after a code change.

## Status meanings and examples

Use **Yes** for a verified fact, **No** for verified absence, **Partial** for mixed
coverage (give a denominator), **Unknown** when records cannot establish the fact,
and **Not applicable** when the case does not require it. “Not tested” names a
missing evaluation, not a pass. A required but unperformed peer review is “No,”
not “Not applicable.” Prefer dates or counts over a boolean where they carry the fact.

Synthetic formatting examples:

- **Model inference HTTP attempts:** 2 — model provider
- **Successful inference HTTP responses:** 2
- **Search/source retrieval HTTP attempts:** 0
- **Tool outcomes:** One rejected citation; one accepted report
- **Request payloads captured:** Yes — 2/2 sent requests
- **Task and scope present:** Yes — verified in both sent requests
- **In-run peer review:** Not requested

For a failed peer case, use “Requested: Yes; performed: No; read/considered: Not
reached.” Show the saved mission as active if that is what remains, and the
evaluation host as closed only if shutdown evidence establishes it. Keep failed
usage unknown rather than reporting the reservation or zero as measured usage.

## Final readability check

Check each factual field against the selected saved evidence, preserve the exact
task and answer, and verify technical links. Remove paragraphs that merely repeat
fields. Keep explanations that connect actions to outcomes, uncertainty or source
support. Avoid dense four-part steps on a single line. A shorter report is useful
only if the complete input-to-output account remains understandable and inspectable.
