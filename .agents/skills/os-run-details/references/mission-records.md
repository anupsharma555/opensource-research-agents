# Mission-runtime record map

Read only for a compatible local `ora.mission.MissionService` state directory.
Use the repository's pinned environment and current command help. `scripts/dev
mission status --state-dir DIR --run-id RUN_ID` and `scripts/dev mission calls
--state-dir DIR --run-id RUN_ID` are documented read-only views when the local
host connection is available. If unavailable, inspect the exact saved store via
`ora.state.StateStore(DIR, read_only=True)` or the documented read-only state
inspector; a failed connection does not prove the run stopped. Never instantiate
a writable host simply to read its state.

| Question | Saved authority / limit |
| --- | --- |
| What did the user ask? | `mission_run` request/profile; a child also pins `parent_run_id`, `parent_snapshot_id` and target. Read the original submission/runner plan if available, but reconcile it with the accepted run. |
| What reached the model? | Exact `model_request_intent` request blob plus `context_inclusion` and source-use decisions for each `agent_call`. Inspect captured packet content and source excerpts, not only source availability. The request intent proves preparation; pair it with dispatch/capture records before saying it was sent. |
| What happened? | `agent_attempt`, `agent_call`, `model_response_receipt`, exact `context_tool_result`, task/control history, peer messages and accepted report/snapshot links. `mission calls` uses `ora.mission.call_inspection.inspect_calls` to return a redacted call projection; inspect exact linked blobs only when needed. |
| What does a requested tool do? | Pair the saved tool name, schema and arguments with its matching handler in `ora.mission.service` or the delegated module. Explain expected ID/input type, local or external effects, and the observed result. If current code differs from the saved run, label the version uncertainty. |
| Did a provider request occur? | `model_dispatch_attempt` is a pre-provider marker. A captured response proves a returned result. A failed/uncertain request may lack usage. Reconcile with a runner's transport ledger when present; do not equate prepared, marked, captured and billed counts. |
| What is final and continuing? | Check `report` plus accepted output references in the exact snapshot and current `mission_task_control` phases; a saved report, task completion and whole-run status are different boundaries. |
| What did it cost and how long? | `model_response_receipt.usage` and exact call timestamps, plus runner attempts and saved waits. Cached input and reasoning completion are subsets of their parent totals. Missing or provider-unreported fields stay unknown. |

For a parent/update chain, inspect each run separately and then state what source
copies and accepted parent output were inherited, what later evidence was added,
and which model input actually contained it. Verify event timing against the
first child dispatch. The same source text may be delivered more than once;
distinguish retained meaning from recovery after lost context.

`os-qualify-run/references/mission-runtime.md` owns the detailed mission
inspection prerequisites and optional private receipt helper. Its receipt
counts metadata and selected small tool results, but does not read the full
model input or report body for this in-chat explanation. `os-groq-usage` owns
period-wide usage accounting. Neither helper should be mistaken for a complete
source-support or current-host proof.

## Report local and LangSmith traces

Use this when the selected run has observability records or the user asks about
tracing. Inspect locally with the pinned interpreter; the default reads saved
state without loading credentials or contacting LangSmith:

```sh
.venv/bin/python scripts/trace_run.py \
  --state-dir <selected-state-dir> --run-id <exact-run-id> --format markdown
```

Read [observability](../../../../docs/development/observability.md) for supported
projection/export commands and timing bases. A reporting request alone does not
authorize `--project`, `--export`, new model calls or cloud payload expansion.
Existing authorization to export the selected metrics remains valid.

Include a direct link to the exact LangSmith trace or final report when available,
and the matching local report. Summarize the displayed metrics-only **Output**;
it is a backend-generated diagnostic, not the agent's answer. Show the actual
agent answer separately in the report's final-output section. Do not upload it,
tool arguments, source passages or hidden reasoning to fill an empty cloud view.

Report these tool fields in a compact table or integrate them into numbered steps:

- Loop number and trusted selected tool name; name the selected capability for
  `request_capability` without counting that as the capability's execution.
- Verified execution and result outcome: completed, pending, rejected, failed
  or unknown. A finished search handler can return an operation still pending.
- New work, explicit reuse or unknown; do not infer reuse from a repeated name.
- Receipt-backed tool active time, separately from model HTTP time and whole-run
  elapsed time. Native tool duration requires an exact single-attempt interval;
  an instantaneous grouping marker is not run latency.
- Whether a matching tool message reached the **next captured request** for the
  same agent/task, and whether it reached a later request after an omission.
  Distinguish exact messages, shortened/restricted projections, no next captured
  request and prepared-only packets. Source information may also arrive through
  separate context packets: missing tool-message delivery does not establish
  missing source text. Inspect exact source inclusion separately when relevant.

Keep provider-reported cached-input counts and coverage separate from the local
exact-prefix reuse estimate. Missing cache fields are unknown, not zero; local
overlap is not a measured provider cache hit or a billing saving. Reasoning and
cached-input counts are subsets, not additions to usage.

Reconcile selected-run IDs, tool counts, token totals, timing bases and export
states against local records. An acknowledged send does not prove the dashboard
rendered it. If inspecting the website is requested, verify visible names and
Output using the browser; otherwise label the evidence as local projection or
export acknowledgment. Keep full identifiers and long tables in the private
technical companion, with a short readable trace summary in the main report.

## Report search activity

For retrieved PDFs and images, also apply the document-reading checks below.


Use this section when the selected run includes search, discovery or source
retrieval. Keep the details in the action sequence; a compact table can use
**Purpose / Actual queries and mode / Providers and reuse / Results and source use**.
Group repeated reads of one operation, retaining its query and attempt counts.

- **Agent choice:** read exact captured tool calls and arguments for
  `search_web`, `search_scholarly` or native browsing. Show the information need,
  actual query terms (including distinct complementary queries), selected mode,
  provider override and result limit when relevant. The model chooses terms and
  `quick`/`research`; the mode controls execution scope. Do not imply that the
  backend invented queries unless a saved transformation establishes it.
- **Executed route:** reconcile `web_search_operation`, `web_search_item`, saved
  deliveries and retrieval request/response receipts. Distinguish requested
  provider from actual providers, operational fallback, parallel requests and
  further agent-selected rounds. Name exact identifier or known-URL routes when
  used; show the selected identifier or URL instead of inventing query terms.
  For Groq-managed browsing, report only captured queries/actions; internal
  searches and their counts remain unknown when the provider does not expose them.
- **Reuse and freshness:** use the saved operation reference, `refresh` argument
  and `reuse_status` when present. Distinguish a new operation, reuse of pending or
  completed work, explicit refresh, and local `read_search_results` or
  `read_tool_result` calls. Older records lacking the marker require matching
  operation/dispatch evidence; do not infer reuse merely from similar wording.
- **Attempts and outcomes:** count actual HTTP attempts, retries and redirects
  from capture/transport records separately from search-tool calls, unique
  operations and queries. Distinguish returned candidates from unique sources,
  fetched copies, inspected passages and evidence actually delivered to the
  model. Preserve valid empty results, failures, waits and uncertain attempts as
  diagnostics; explain any subsequent route or query change.

Show only run-relevant queries, with narrow redactions for secrets or unrelated
private identifiers. Keep raw provider responses and source bodies private.
Provider-reported usage and latency may be included where captured; do not
manufacture token or time savings from reuse labels alone. A search result or
official-looking URL does not establish support for the final answer.

## Explain budget and admission stops

Read this section when the selected run stops or waits because of tokens. Start
with the user-visible outcome, then identify which limit actually bound the next
step. Avoid an unexplained “token limit” or “context overflow.”

| Limit | Meaning and evidence |
| --- | --- |
| Whole-test allowance | The user's authorized total across calls and retries. Read the controller ledger for confirmed usage, unknown-use holds and remaining allowance. Raising it does not raise provider rate limits or the model context window. |
| Provider rate or daily quota | Capacity over a time window, not the model's memory size. Use dated response headers or an explicit quota error and retain the selected profile/domain. Token-rate headers alone do not prove daily headroom. |
| Model context window | The model's supported input/output capacity. Use the saved configuration; do not substitute the token-rate number. |
| Local request admission | The runtime's rule for whether a complete request fits before dispatch. Inspect `mission_call_budget`, `mission_host_diagnostic`, task state and exact request previews. A local refusal is not a provider rejection. |

In the current mission runtime, `MissionService._schedule_model` can reserve the
previous observed completion as a prediction of the **next** completion. Explain
that this is a future-output estimate, not a second charge for the previous call.
Verify the saved values and implementation before applying this relationship:

```text
rate-derived input target = observed token-rate limit − predicted completion
remaining space, at most = input target − fixed instructions/tool definitions
```

This formula describes a conservative packing target, not an exact provider
rejection rule. Current preparation records `rate_admission_policy`: an
`estimate_pacing` attempt may fall back to a `provider_feedback_probe` that keeps
the required packet and unchanged output cap. Capacity then waits for a clear
window and admits one exclusive attempt. `confirmed_size_repair` follows an
actual captured size rejection. Historical records without this field can still
reflect the earlier estimate-only stop; do not reinterpret their evidence.

The model context window can impose a tighter target, and the packer can first
try a smaller soft target. The controller's whole-test guard may separately
reserve the configured maximum completion; do not confuse that maximum with the
runtime's prior-completion prediction or with actual billed/returned usage.
Report estimates as estimates, and compare them against a complete serialized
request including task state, evidence, feedback, tool schemas and history.
An attempted preview is not automatically the smallest possible request.

Explain the next action from the diagnosed cause:

- A request that fits the full rate allowance but lacks **current** capacity can
  wait for replenishment. Check the saved wake time and actual admission state.
- A request whose required content exceeds the runtime's full allowance after
  its output reservation will not be fixed by waiting alone under that policy.
  Inspect the saved policy: the current provider-feedback path can test that
  uncertain forecast after a capacity wait, while an actual size rejection still
  requires repair. Preserve task meaning, exact updates, necessary tools and
  evidence. Do not silently raise explicit budgets or change model settings.
- An exhausted whole-test allowance requires additional authorization; that
  authorization does not repair an oversized request or establish provider quota.

For the user, show one short calculation with values from the selected run:
“Of the allowed input, instructions and tools use this much, leaving this much
for the actual task; the required task needs more.” State what completed before
the stop, what remains saved, and what never reached the model. Preserve the
separation between a prepared request, a dispatch attempt and a captured provider
response. Verify exact payload delivery before saying new evidence was read.

## Report document reading

Name the method actually used: PDF/XML/plain-text extraction, local selected-page
OCR, or Groq Qwen figure interpretation. Pair `public_document_fetch` with the
exact `source_copy`, `intake_quality`, source-use decision and `source_read_window`.
A retained PDF or prepared window is not proof it reached a model.

For OCR, inspect `public_document_page_image` and `public_document_page_ocr`: report
page number, renderer/OCR engine, byte/text hashes, current permission and visible
window. OCR is unverified transcription, not an original checked passage.
For figures, follow `figure_request_intent` → `figure_dispatch_attempt` →
`figure_response_receipt`, then the exact `inspect_figure` result and later
GPT-OSS request. State the auxiliary model, saved profile, selected page, question,
interpretation, provider usage and unknown attempts. Do not count a vision call
as zero-cost local work or silently combine its model totals with GPT-OSS.
Run the `os-groq-usage` helper with `--model qwen/qwen3.8-27b` separately when
reporting that model's period totals. A pre-HTTP marker without response remains
uncertain, not proof of successful inference or zero consumption.

Distinguish extraction success, model receipt, checked passage and claim support.
Report OCR/vision uncertainty. A saved receipt does not authorize another call,
and a follow-up requires its own vision opt-in.
