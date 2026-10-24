# Local-first run traces

OSRA's saved SQLite records remain the authority for a research run. The trace
command reads those records locally, shows token and context coverage, and can
optionally prepare metadata-only OpenTelemetry spans. Cloud export is a separate,
explicit operation, or an opt-in background observer on newly started live runs.
Research runs do not require LangSmith or its dashboard.

## Local input reuse across steps

`ora.observability.prefix_reuse.analyze_local_prefix_reuse` is the shared local
calculator. `build_context_metrics` joins exact saved request bytes to their
dispatch and response records before calling it. A cut-off response still has
measurable input; missing or uncertain records remain comparison barriers.
Prepared-only requests do not count. No model call, instruction, or prompt token
is added by this measurement.

The run-level structural reuse percentage sums unchanged opening message tokens
and identical whole tool definitions, then divides by measured message and tool
tokens. Both quantities use the same local tokenizer. Each agent/task/provider/
model/credential-profile scope starts with a zero-reuse baseline included in the
denominator. A run with only baselines has no repeat-request percentage. Missing,
unsupported, and overlapping comparisons are excluded with explicit coverage;
they are never silently counted as zero. Text after the first difference and
partially changed tool schemas are outside this conservative measure. It is not
provider cache use, billed input, an avoided inference, or a billing discount.

The existing follow-up message-prefix percentage remains available as a separate,
narrower metric: it excludes first requests and tool definitions. Compare runs
only with the same measurement basis and comparable scope/coverage. An average
of step percentages is not a run percentage; use summed token numerators and
denominators. Cross-run summaries may compare these values without pooling
different credentials or treating a previous run as a warm provider cache.

The calculator returns content-free per-request rows plus aggregate counts under
`local_prefix_reuse`. The enabled runtime observer saves them in the existing
`observability_context_snapshot` record, versioned by its metrics hash. LangSmith
receives aggregate counts and percentages only, separately from provider cache
telemetry. When tracing is disabled, the original requests remain saved but no
observer snapshot is created; the read-only trace command reconstructs the same
metrics. Save its JSON/Markdown as private report artifacts for those runs.
`os-run-details` requires those links and scope/coverage in each full run report.

```sh
.venv/bin/python scripts/trace_run.py \
  --state-dir .local/research --run-id <saved-run-id> --format json
```

This default command does not read credentials, change the run, or export traces.
The Markdown view includes the per-request comparison table. New results derived
from historical records should be labeled recomputed, with the analysis time;
previously exported cloud summaries are not rewritten by a local report.

## Token measurement fidelity

New model receipts separate the local serialized-request estimate, any numeric
request demand in a matching provider token-rate error, and actual input usage
reported after inference. A 413/429 admission count is not a billed input count;
missing usage remains unknown. The local trace renders these measurements
side by side. Metrics-only model spans expose admission demand and limit as
`osra.admission.requested_tokens` / `osra.admission.limit_tokens`, separate from
`gen_ai.usage.*`. The explicit packing reserve is
`osra.estimated.rate_input_headroom_tokens`. Error messages and organization
identifiers stay private; existing receipts and sent exports are not rewritten.

The shared scheduler admits one initial model request when token capacity is
unknown for its quota domain. Saved provider feedback then governs normal
concurrency. A completed attempt without token headers releases this startup
gate rather than serializing all future work. Unknown external outcomes retain
their existing reconciliation boundary. Limits and reset headers remain
provider observations; see [Groq rate-limit semantics](https://console.groq.com/docs/rate-limits).

## Readable comparisons of saved API cases

`scripts/report_api_runs.py` builds a local HTML comparison, a Markdown report,
and machine-readable metrics from explicitly selected validation directories.
It does not load credentials, start a model, retrieve sources, or export traces.
Each card links to the existing full `RESULT.md` input-to-output report. Keep the
manifest and generated output under ignored `.local/`; they contain private
case references even though the metric fields are sanitized.

```json
{
  "schema": "ora.run_report_manifest.v1",
  "cases": [
    {"case_dir": "../search-validation/example-case", "kind": "natural_agent"}
  ]
}
```

Paths resolve relative to the manifest. Optional `cloud_url` links must point to
LangSmith. The two recognized direct diagnostic formats use `api_diagnostic`;
the synthetic repeat diagnostic also requires an explicit `continuation_dir`.
The loader checks saved request/response hashes, usage totals, and the evaluation
ledger before rendering. An unrecognized diagnostic is rejected rather than
silently presented as a natural agent run.

```sh
.venv/bin/python scripts/report_api_runs.py \
  --manifest .local/reporting/manifest.json --output-dir .local/reporting
```

Cache status is visible even when its percentage cannot be calculated: **Not
reported by Groq** means the captured response omitted the field; **reported
zero** means the provider explicitly reported zero. Failed attempts with unknown
usage are separate. Counts include continuations once, and a failed request plus
one successful response does not establish a successful repeated-request pair.

LangSmith summary Output contains a deterministic metrics/status explanation,
not the model's answer. The outbound guard rejects arbitrary Output text. It
preserves the metrics-only policy while making cache coverage readable without
searching technical attributes. Existing sent spans remain immutable; a new
summary is a reporting update, not another model call.

Read timing by its basis. A model-call span measures its observed request
duration. An operation marker or grouping anchor can be instantaneous; its
`0.00s` is not whole-run latency. A closed evaluation's separate final report
uses the saved controller duration, with its absolute end derived from the saved
run start. Direct diagnostic reports use the first ledger start through the last
HTTP end; a continued diagnostic's window includes the gap between processes.
The local comparison also shows summed measured HTTP durations separately.
Neither a between-call gap nor the sum of concurrent request durations should be
reported as model generation latency. Existing sent anchors are not rewritten.

From the repository root, inspect one run without loading `.env`, changing state,
or contacting a service:

```sh
.venv/bin/python scripts/trace_run.py \
  --state-dir .local/research --run-id <saved-run-id> --format markdown
```

Omit `--format markdown` for JSON. Both views include the saved run status,
model and figure token coverage, context-inclusion counts, integrity diagnostics,
saved request-stage estimates, and local span states (`pending`, `sending`,
`sent`, `uncertain`). They exclude prompts, source bodies, raw provider errors,
credential values, and private URLs. Cloud trace names may contain a short,
redacted hint from the user question, a date/time in America/New_York (EDT/EST),
and a stable short run identifier. Full questions and model answers stay local.
A prepared inclusion or retained discovery
hit does not establish that the model used evidence correctly.

In Markdown, model requests with a matching dispatch marker or captured response
appear separately from prepared-only and unmatched call records. A dispatch marker
is a saved pre-transport observation; only a separate transport ledger can count
HTTP attempts. An unmatched call manifest has no saved request intent and is not
evidence of another prepared request. The JSON `loops` array retains all records
for diagnostics; this presentation grouping does not change token totals.

## Tools selected in each loop

The local trace report includes a tool table joined to exact saved model,
executor and result records. It shows the loop, selected tool, actual execution,
outcome, new/reused/unknown work, measured active time, and result delivery.
Capability selection is listed as `request_capability → selected_tool`; it does
not count as execution of the selected tool. An asynchronous search can have a
completed handler while the search operation is still pending.

Tool-message delivery to the next captured model request and any later recovery
are reported separately. Source text and references may also arrive in separate
context packets, so a missing tool message does not mean that source information
was absent.
When the next captured request already contains the matching result, the Markdown
later-delivery cell is a dash instead of repeating that same observation. A later
recovery after an omitted next request remains visible.
Delivery distinguishes the exact saved tool message from a shortened or
permission-filtered host projection. A prepared packet or pre-transport dispatch
marker alone proves neither HTTP delivery nor model receipt. A source reference
does not establish delivery of the full passage, and delivery does not establish
scientific use. An absent timing or reuse receipt remains unknown.

The optional runtime observer projects finalized tool observations as named
LangSmith tool spans under the existing run anchor. Each span has readable
metrics-only Output and loop metadata; tool arguments, result bodies, source
text and prompts stay local. Native span duration uses a validated single
capacity-attempt start/end interval. Executor active time is a separate metric;
unmeasured intervals are explicitly selection observations rather than inferred
execution durations. Model HTTP time and whole-run elapsed time retain their
own bases. Later evidence creates a versioned observation without resending an
acknowledged or uncertain span or duplicating token counters.

This observation path adds no model calls, prompt labels or prompt tokens. Local
inspection works without LangSmith or its credentials.

To prepare local trace spans for that exact run, without network access:

```sh
.venv/bin/python scripts/trace_run.py \
  --state-dir .local/research --run-id <saved-run-id> --project \
  --max-spans 100 --format markdown
```

Projection uses the saved mission and run IDs, pages text-model receipts,
figure-model receipts, coordination traces and tool activity, and adds a run anchor.
The default cap is 100 new spans per invocation (minimum 4); tool projections use
only the remaining span allowance. If it reports `complete: false`, rerun
with its `next_after_receipt_id`, `next_after_figure_receipt_id` and
`next_after_trace_id` and `next_after_tool_id` values as `--after-receipt-id`,
`--after-figure-receipt-id`, `--after-trace-id` and `--after-tool-id`. `END` means that stream is already
exhausted. The default 200-page scan bound can be adjusted with `--max-pages`
(up to 1,000); a partial run reports continuation cursors. Projection is
idempotent: existing spans, including those already sent or marked uncertain,
are not reset or duplicated. It never invokes a research model or retrieval
provider.

To enable **explicit** export, first install the optional pinned packages:

```sh
scripts/dev setup --observability
```

Place only your own values in the ignored repository `.env` (or in the process
environment):

```dotenv
OSRA_TRACE_EXPORT_ENABLED=true
OSRA_TRACE_OTLP_TRACES_ENDPOINT=https://api.smith.langchain.com/otel/v1/traces
OSRA_TRACE_LANGSMITH_PROJECT=<dedicated-trace-project>
LANGSMITH_API_KEY=<tracing-only-api-key>
```

The CLI reads only these four names for `--export`; process-environment values
override `.env`. The local `.env` parser accepts simple `KEY=value`, quoted
values, and `export KEY=value`, without shell interpolation. The endpoint must
be an allowlisted LangSmith HTTPS OTLP traces endpoint. A tracing-only key can
upload without broader project-read access; an API project-list request may
return HTTP 403 for that key even when trace upload is authorized. Neither
`--project` nor the default inspection reads these settings.

After projection, send only pending spans from the specified mission and run:

```sh
.venv/bin/python scripts/trace_run.py \
  --state-dir .local/research --run-id <saved-run-id> --export \
  --max-spans 100 --format markdown
```

`--project` and `--export` are deliberately separate. Export records each
acknowledged send as `sent`; an interrupted or unconfirmed send becomes
`uncertain` and is **not retried automatically**. A local `sent` receipt records
the OTLP transport acknowledgement, not proof that a hosted dashboard displayed
the trace. If the command reaches its page bound, pass `next_after_id` back as
`--after-export-id` to continue. The command reports status directly from local
state, so routine inspection does not need website access. Offline checks never
export; the live host exports only with the separate runtime opt-in below.

The local report also groups uncertain spans by saved, sanitized `error_code`
and HTTP status, when present. Older uncertain receipts retain their saved
reason, such as `acknowledgement_unknown`, under `legacy_without_error_code`.
Unknown or malformed values are counted without displaying raw exception text;
the HTTP-status list is capped and includes an overflow count.

Provider-reported input and output tokens are separate totals. Reasoning tokens
are a subset of output, and cached-input tokens are a subset of input. The
provider boundary saves a compact `cache_observation` before optional fields are
normalized: `reported_hit`, `reported_zero`, `not_reported`, `malformed`, or
`unavailable`. Only valid reported values contribute to cache totals. Text,
streaming and auxiliary figure responses share this classification; the legacy
Groq provider retains it too. Interrupted streams are unavailable.
The accompanying `field_presence` diagnostic distinguishes absent, null or
malformed details from an absent, null or invalid cached-token value. It contains
only a classification; provider text and request content are not exported.

An absent cache field means **unknown**, not zero. Older normalized-only receipts
retain usable reported cache values, but missing values become `legacy_uncertain`:
they cannot prove whether Groq omitted the field or earlier validation dropped it.
The displayed cache fraction uses only calls reporting both cached and input
tokens. It includes complete/partial/unavailable coverage, reported hit and zero
counts, call coverage, and the covered share of reported input tokens. For
example, a 60% cached share on one reporting call is not a 60% whole-run saving.
Unknown token totals remain null, and no billed savings are inferred.

The additional `cache_fraction_confirmed` divides known cached input by **all
reported input** in the run. With partial cache coverage it is a confirmed
minimum, not the actual whole-run hit rate; with no cache counts it remains
unknown. Its input-reporting-call denominator excludes prepared-only requests.
Local and LangSmith summaries expose this measure alongside the narrower
reporting-call fraction and coverage, so the two percentages remain interpretable.

## Local prompt reuse when provider cache counts are absent

The backend also measures **local prompt reuse** from verified saved requests.
Within the same run, actor, task, provider, model and credential profile, it
compares a successful request with the preceding comparable successful request.
The predecessor must have finished before the later request was dispatched.
Concurrent, failed, unsupported or unverifiable requests cannot establish a
comparable pair. Restarted observers reconstruct the measure from durable records.

The measure counts the common leading token IDs of canonical message JSON using
the pinned local tokenizer. The displayed estimate is summed repeated leading
message tokens divided by summed message tokens in the compared follow-up calls.
First calls are outside that denominator. Tool definitions are compared
separately, because the provider's internal placement of tools in the model
prompt is not observable. Identical tool definitions do not prove every other
request setting remained identical. No prompt text or token IDs are exported.

This is a structural overlap measure, **not a substitute provider cache hit
rate**. It cannot establish server-side prompt rendering, cache residency,
minimum cacheable size, evictions, latency savings, or billing discounts. It also
cannot detect a hit created by an earlier run outside the comparison scope.
Estimated local tokens must never be divided by provider-billed input tokens.
Missing saved request bodies remain unavailable even if their hashes match.

Local reports and LangSmith summary Output present provider cache reporting and
local prompt reuse on separate lines. Only custom `osra.local_reuse.*` attributes
carry this estimate; `gen_ai.usage.cache_read.input_tokens` remains reserved for
provider-reported cache reads. Projection never invokes a model or changes its
request. Changed diagnostic summaries receive new deterministic report IDs;
historical sent or uncertain spans are not overwritten or replayed.

Other options have different meanings. An application response cache could
measure real local hits and avoided HTTP requests, but would change tool-using
research execution and needs separate replay/freshness design. Timing differences,
rate-limit header deltas and aggregate invoice discounts cannot reliably recover
per-call cached tokens. Groq's separate Responses API documents
`input_tokens_details.cached_tokens`; switching endpoint does not recover missing
Chat Completions evidence or guarantee future reporting. See the
[Groq API reference](https://console.groq.com/docs/api-reference),
[Groq prompt-caching guide](https://console.groq.com/docs/prompt-caching), and
[LangSmith's provider-cache mapping](https://docs.langchain.com/langsmith/trace-with-opentelemetry).

Failed or unresolved calls may have unknown usage. Request-stage counts are local size estimates, not
provider billing. Figure-model usage has its own bucket and must not be silently
added to the text-model total. The Markdown view also lists each saved model and
figure call's usage and cache observation without exposing its prompt or tool results.

This command is also useful during a long local run: repeat the read-only form
to inspect newly saved receipts. The command has no dependency on a LangSmith UI
session.

## Observe new live runs automatically

Add the separate opt-in alongside the export settings:

```dotenv
OSRA_TRACE_RUNTIME_ENABLED=true
```

The live CLI and natural-task validation runner pass this explicit configuration
to the existing mission service. An independent background worker registers newly
created runs, projects saved model/coordination records and exports bounded pages.
Existing unrelated runs are not enrolled. On restart, only previously bound runs
for the same configured destination resume observation. Direct service constructors
and offline tests do not load the environment or enable this worker implicitly.

The observer saves local context metric snapshots and versioned aggregate summary
spans as saved records change. It does not change prompts, choose research tools,
or call a model. Monitoring failures do not reject research actions. Host shutdown
makes a bounded attempt to drain final metadata; remaining pending or uncertain
records stay local for inspection. Uncertain sends are not automatically replayed.

Core metrics are input/output usage, reasoning as an output subset, cached input
and reporting coverage, model/figure call counts, errors, largest reported input,
time through the last saved activity, and exact-reference reuse/integrity. Local
request-stage estimates remain separate from provider-reported usage. A run's
cache percentage is summed cached input divided by summed input from matching
reporting calls; input/output and reasoning percentages use analogous matching
call sets. Partial coverage never becomes a whole-run efficiency claim.

Aggregate summary spans use custom `osra.summary.*` fields. They do not repeat
standard model token counters, which avoids counting per-call usage twice.
The root span groups the run and carries its readable identity; its zero duration
is not a run-latency measurement. Summaries carry the saved elapsed-time measure.

Retrieval progress reports saved logical attempts and their outcomes, scoped by
the requesting actor's run. These are not complete HTTP-attempt counts: a PubMed
search may make two HTTP requests, while a crash can precede a normalized receipt.
The HTTP aggregate is explicitly unavailable from these records. Bounded live
evaluation reports reconcile the separate transport allowance ledger when one
exists. Retrieval-only changes refresh snapshots and summaries; figure receipts
also receive separate `osra.figure.call` spans with their reported usage.

The natural-task evaluation runner saves an `evaluation_observation` before
closing its host: stop reason, HTTP attempt counts, elapsed time and a hash of
the allowance ledger snapshot. Its update is projected into the run summary.
The mission's own status is preserved: a capped evaluation may leave an active
mission without an active host. This record describes the controller's stop
decision and attempt accounting, not final billing for callbacks still settling.
An exact saved ledger can also be reconciled after a closed evaluation; that
adds a diagnostic record without replaying work or rewriting the original run.

The local trace report owns export-health counts and safe error groups. These
are local acknowledgement states, not evidence of dashboard visibility. Summary
spans do not include their own changing export counters, avoiding a feedback loop.

## Package choices

Keep the existing pinned `tiktoken`, `opentelemetry-sdk` and
`opentelemetry-exporter-otlp-proto-http` packages for local overlap measurement
and metadata-only LangSmith export. LangSmith accepts standard OpenTelemetry
from a custom application; no LangChain or LangGraph migration is required.
SQLite receipts remain the authoritative run ledger. Rebuilding OTel counters
from those receipts without a durable delivery cursor would risk double counting
on restart. LangSmith's documented `/otel/v1/traces` route must not be treated as
an undocumented metrics-ingestion endpoint.

| Optional package | Useful when | Current decision |
| --- | --- | --- |
| `langsmith` | Programmatic trace queries, datasets or evaluation experiments | Defer until a concrete read/evaluation workflow needs it and an appropriately scoped key is available; the current tracing-only key does not authorize reading traces. |
| `openinference-instrumentation-groq` / `openinference-instrumentation-openai` | Instrumenting their corresponding provider SDKs | OSRA's direct `urllib` path is not one of those SDKs. Keep the explicit metadata allowlist; adopting an SDK would need separate retry and privacy validation. |
| `litellm` | A deliberate multi-provider gateway, routing or response cache | Defer; response replay, retries and routing change execution semantics and do not recover unreported provider-prefix cache counts. |
| `diskcache` | Persistent application-level memoization | Defer for model answers; source freshness, permissions, side effects and replay need explicit design. StateStore already preserves measurement evidence. |

Groq also documents Enterprise-only Prometheus prompt-cache hit/miss rates at
model/project scope over five-minute windows. If account access is available,
those can support aggregate operational monitoring through an ordinary HTTP
client; they cannot fill an individual run's missing cached-token field. No
account eligibility or endpoint access is assumed here.

Sources: [LangSmith OpenTelemetry](https://docs.langchain.com/langsmith/trace-with-opentelemetry),
[LangSmith API keys](https://docs.langchain.com/langsmith/create-account-api-key),
[OpenInference Groq](https://arize-ai.github.io/openinference/python/instrumentation/openinference-instrumentation-groq/),
[LiteLLM response caching](https://docs.litellm.ai/docs/proxy/caching),
[DiskCache](https://grantjenks.com/docs/diskcache/), and
[Groq Prometheus metrics](https://console.groq.com/docs/prometheus-metrics).

## Protocol references

The exporter follows [LangSmith OpenTelemetry attribute mapping](https://docs.langchain.com/langsmith/trace-with-opentelemetry). OSRA uses a named OTLP tracing project; the LangSmith SDK agent-addressing variables are not used by this exporter. OpenTelemetry packages are optional and do not introduce an agent framework.

[Groq's caching guide](https://console.groq.com/docs/prompt-caching) describes
automatic prefix caching and reported `usage.prompt_tokens_details.cached_tokens`.
This is API-server metadata, never a number requested from a model's answer.
Groq's response types make prompt-token details optional. An omitted field must
not be converted into a reported zero or estimated from repeated text. Matching
request bytes, instructions or tool schemas demonstrate reuse potential; only
provider-reported cached tokens establish an actual provider cache hit.
