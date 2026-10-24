# Selected Groq model adapter

`ora.models.GroqSelectedAdapter` prepares one traceable Chat Completions
request and decodes one response. It does not schedule a mission, execute a tool,
retry, switch models, or decide that evidence collection is finished. The host
handles scheduling and tool dispatch, while research agents choose further
evidence work and scientific completion. The host must persist each call
manifest, restricted returned reasoning, and outcome under the corresponding
attempt.

The selected endpoint is `https://api.groq.com/openai/v1/chat/completions` with
`openai/gpt-oss-120b`. New research runs default to `reasoning_effort=medium`;
both phases send the exact saved effort and `include_reasoning=true`, without
`reasoning_format`. Existing high-effort configurations remain supported and are
not rewritten on replay or continuation. Follow-ups inherit their parent's saved
effort. A collection request
provides local function definitions, `tool_choice=auto`, and
`parallel_tool_calls=false`. One response may request one tool. Collection may
use SSE streaming with `stream_options.include_usage=true`; the adapter assembles
the entire response and exposes a tool request only after a terminal `[DONE]`
event, a finish reason, observed usage and complete JSON arguments. The host returns
its result with the exact `tool_call_id`, preserving the ordered assistant/tool
messages, applicable tools and instructions on every dependent model request.
There is no adapter-imposed number of turns. A separate final request uses
`response_format.type=json_schema` with `strict=true`, no tools, and no streaming.
That formatting request does not confer scientific acceptance or prevent later
evidence work.

The per-call `CallConfiguration` pins the saved role/configuration ID and hash,
instructions, endpoint, model, reasoning settings, stream mode, and explicit
output budget. `prepare(...)` adds exact source ID/version/hash references, the
source-use decision and route, plus call/attempt/parent IDs and task category.
`PreparedCall.manifest()` records all effective nonsecret settings and the exact
request digest. The host should link this to its durable agent configuration
snapshot and reject a changed snapshot; the adapter rejects a mutated prepared
body immediately before dispatch. A changed output budget requires a new
configuration snapshot and a linked new call/attempt, rather than a hidden
retry.

`SourceUse` is the adapter's temporary typed input boundary while the broader
source-use contract is integrated. It accepts exact source copies with
`use=permitted`, `allowed_for_model=true`, text matching its SHA-256, and a
recorded unknown-policy decision. Unknown copies may pass only ID/version/hash
when `unknown_policy=allow_metadata_only`, `allowed_for_model=false`, and no
text is supplied. Restricted or explicitly denied copies fail before credential
lookup for direct, cache and peer routes alike.
The caller must pass *every* referenced source copy through this boundary; a
later host integration must enforce that invariant from its exact StateStore
refs. The adapter does not infer rights from a citation or search hit and cannot
detect an undeclared source embedded in arbitrary caller-authored text.

`ModelOutcome` reports `complete`, `tool_requested`, `partial`, or `failed`.
Only `tool_requested` exposes `executable_tool_calls`. Malformed or incomplete
arguments, a `length` cutoff, wrong returned model, malformed final JSON, or
transport failure never becomes a runnable tool action or accepted final answer.
An interrupted non-streaming JSON response exposes exact partial bytes only in
the restricted outcome field, with a digest and no executable tool call.
Returned `message.reasoning` remains a separate restricted value. A parsed
complete response with no reasoning is `absent`; returned text on a cut-off
response is `truncated`. A cutoff with no returned text, an interrupted stream
with no reasoning text, or a transport/unparsed failure is `unknown`, because
none of those proves absence. Known unsupported settings fail preflight with
`reasoning_status=unsupported` on the rejection; an HTTP
400 without a specific supportedness signal stays `unknown`. No private
reasoning is invented or required to explain a decision.
The outcome includes response and
provider request IDs when present, observed usage and provider timing fields,
allowlisted rate headers, wall time, and typed failure/Retry-After fields. The
host alone owns rate waits and retries. Provider `completion_tokens` can include
reasoning plus visible output; high effort does not reserve enough space for a
visible final answer.
The local final-response validator intentionally accepts a small strict JSON
Schema subset: closed objects, required fields, arrays, scalar types, enums and
nullable scalar fields. Unqualified validation keywords fail preflight rather
than being silently ignored.

The final strict-output phase rejects streaming. An interrupted collection
stream is a marked partial outcome, with exact bytes confined to a restricted
field and no executable tool call. `length` cutoff fixtures cover incomplete
tool and final phases.
Provider queue, prompt and completion times are recorded only when returned by
Groq; tool execution and host wait durations must be measured separately by the
host. Neither a slow call nor a cutoff imposes a scientific deadline.

## Saved-call integration boundary

`ora.models.ModelSession` is a host-invoked bridge for one model turn. It loads
an exact `agent_call` version and its `agent_configuration` from the shared
StateStore, checks the configuration hash and copied call fields, and derives
provider, endpoint, model, instructions, reasoning settings, output budget,
stream mode, selected local function tools and final schema from that saved
selection. The saved settings must explicitly name `max_completion_tokens`,
`stream`, and `unknown_source_policy` (`reject` or `allow_metadata_only`).
Unexpected model settings fail instead of being silently dropped. A final phase
also needs a saved strict `response_format` schema. Collection and final
requests may use separate exact call manifests linked to the same configuration.

For each `source_copy` in the call's saved `input_refs`, the host passes one
typed `SourceSelection` with its exact record reference, route, use state, and
model-input decision. The bridge checks the saved record-body hash, retained
blob checksum, text type and mission ownership. Explicit saved prohibitions such as
`allow_model_input=false`, restricted rights, or a restrictive copy-use record
override a caller's allow decision. An unknown copy sends ID/version/checksum
only when the saved policy allows metadata; it never sends unapproved text.
An explicit saved `use_record.use=unknown` cannot be promoted to permitted by
the caller. SourceLibrary's `rights_basis` is an explanation, not a structured
model-use grant; the host must make and save that decision from its own rights
authority.
All pinned source copies need decisions, so a cache or peer route cannot omit a
denied copy while keeping the same saved call. The host must supply these
decisions from its own authority, not from peer prose. Arbitrary source bytes
hidden in a user-authored message remain outside what this bridge can identify.

`prepare` writes a private exact request blob and an immutable request-intent
record with the request digest, saved call/configuration refs, source refs and
use decisions. `invoke_capture(intent_ref)` is a callback for an already
admitted host worker: it loads and verifies that intent, atomically records one
attempted external dispatch, sends one request, and saves one response receipt.
A second caller seeing that marker without a receipt receives
`DispatchUncertain`; the host must reconcile the possible external effect before
any new attempt. On capture, that marker advances to `captured` with an exact
receipt reference in the same transaction. `capture(intent_ref, outcome)` can instead attach a
response obtained by a host-controlled transport. The receipt points to a
private content/tool payload blob and to `restricted_reasoning`; its public
TraceJournal event contains only diagnostic status, usage, timing and exact
lineage. The restricted reasoning record, response receipt and public trace
commit together. Request and result payloads use StateStore's top-level `blob`
field so its normal backup/restore copies their bytes, including the separate
reasoning blob. If a response is malformed, its raw partial bytes are not duplicated
into the general outcome blob, because they may contain reasoning; the saved
digest and marked failure preserve the observable boundary. The separate live
qualification recipe retains exact raw provider bytes under ignored `.local`.

A newer source-copy or call-manifest head does not silently replace a frozen
input. Exact older versions remain available for a valid continuation. A
current explicit model-use revocation blocks *future dispatch* of a prepared
intent; an in-flight response can still be captured against its original
versions. The dispatch-marker transaction rechecks the exact source record
hashes and explicit current-use restrictions before recording the attempt.
That commit is the dispatch authorization boundary: a revocation committed
before it prevents the call; one arriving afterward cannot unsend the request.
The check reads saved records without loading source blobs under the write lock.
The bridge does not claim a worker lease, reserve provider capacity,
retry, execute tools, or make a mission-level scientific decision. The host
still owns admission and uncertain-effect reconciliation; a new request requires
an explicit linked attempt rather than reusing a marked intent. An already
captured intent returns its saved receipt only when the exact reference and
request digest match.

## Private provider-reasoning files

For new captured model responses, the host saves the provider-returned reasoning
for each inference step as an exact UTF-8 text artifact alongside a JSON metadata
file under the private state directory. These files are separate from the
redacted `restricted_reasoning` record used by ordinary inspection. A step whose
response contains no returned reasoning gets JSON metadata with its recorded
status; no text is inferred or synthesized. Truncated returned reasoning remains
labelled `truncated` and is only the returned prefix. Provider reasoning describes
the model's returned text, not verified scientific support for its answer.

To export every saved inference step for an existing mission run, including
dispatches without a captured response, use an exact run ID and state directory:

```sh
scripts/dev reasoning-export --state-dir .local/research --run-id RUN_ID
```

This opens SQLite read-only. It does not start the host, read `.env`, invoke the
model, or send anything to LangSmith. The default export root is
`STATE_DIR/reasoning/`; `--output-dir LOCAL_DIRECTORY` selects another local
filesystem destination. Existing symbolic-link destinations are rejected. The
command prints only statuses, counts, and paths. It writes a combined
`reasoning_trace.md` for human inspection, plus `reasoning_trace.json` and
`reasoning_trace.txt` under the selected run directory. Each saved step has its own
`reasoning_trace.json` and a text file only when returned text is saved. The
per-receipt UTF-8 text file preserves the saved bytes; the Markdown file adds
formatting for reading and must not be used for byte-level comparisons. Read
these files locally from the private destination when authorized to inspect
returned reasoning. Authorized private operator run reports can embed the matching
step text and link the combined Markdown, as defined by `os-run-details`. Keep it
out of model prompts, public reports, tracked examples, and cloud trace exports.

The JSON includes the saved text when present and identifies the exact run and
response receipt, agent, provider, model, timestamp, reasoning status, and
capture fidelity. Treat it as equally private as the text files.
`provider_exact` marks text captured before redaction; `saved_unredacted` and
`saved_redacted` describe historical records. An export cannot recover words that
an older capture already redacted. `unavailable` means there is no saved text to
reconstruct. `absent`, `unknown`, `unsupported`, `not_requested`, and
`not_captured` remain distinct from returned
text. `receipt_count` counts captured responses, while `dispatch_only_count`
counts local dispatch markers with no captured response; a marker alone does not
establish an HTTP attempt. `exported_count` counts their
combined metadata steps. The CLI's `complete`, `partial`, and `no_receipts`
labels describe export coverage, not research quality or provider success.

## Verification and live protocol recipe

Run the focused offline fixtures first:

```sh
.venv/bin/python -m pytest -q -p no:cacheprovider tests/models
.venv/bin/ruff check src/ora/models tests/models scripts/qualify_stage3_model.py
```

With the repository's existing `GROQ_API_KEY` environment or untracked root
`.env` credential route, explicitly run:

```sh
.venv/bin/python scripts/qualify_stage3_model.py
```

The script makes at most four serial requests on the selected model, each with
at most 8,192 completion tokens: first synthetic tool, result-dependent second
tool, collection answer, then strict final JSON. It makes no automatic retry.
The maximum output allowance is 32,768 tokens across this run; actual token
usage and price must come from saved provider receipts/account data, rather than
assuming that allowance is spent. Exact nonsecret requests, raw responses,
returned reasoning, usage, manifests, and a sanitized summary are saved with
restricted file permissions under ignored `.local/stage3/model/<run>/`. Only a
small status line is printed. Inspect the private record before claiming a
successful live protocol sequence; a provider refusal, wrong tool choice or
cutoff is a useful bounded qualification result, not a reason to switch models.
For one additional collection stream check, run
`.venv/bin/python scripts/qualify_stage3_model.py --stream-smoke`. It makes at
most one request and saves the raw SSE and restricted assembled outcome privately.

For later scientific qualification, use fixed versioned packets for the nine research roles and
temporary specialist, pin this model/configuration and real admission limits,
save exact source refs and token/cost receipts, then assess evidence use,
counterevidence, corrections, and final scientific outputs independently.
This adapter's fixtures and synthetic live marker check do not qualify role
quality, account capacity, a production mission, or endurance.

The provisional role-qualification handoff is `ora.stage3.role_qualification.v1`. Each row
must name a frozen benchmark packet, exact source versions and hashes, role
configuration/skill versions, permitted tools, expected evidence, and a bounded
live allowance before calling the provider. Start from
`benchmarks/v1/manifest.json` and its three synthetic scenarios; neither the
packet set nor this table is a completed role evaluation.

| Role ID | Fixed-packet observation to score |
| --- | --- |
| `literature_acquisition` | Source identity, access depth, and a justified next query or stop. |
| `evidence_preparation_synthesis` | Exact supporting and opposing passages linked to each claim. |
| `independent_methods_reviewer_critic` | Independent appraisal, limitations, and substantive counterevidence. |
| `questions_hypotheses_ranking` | Competing hypotheses with explicit evidence-based ranking. |
| `experimental_design` | Testable design, assumptions, and failure conditions. |
| `data_management` | Source/version lineage, rights, and missing-data handling. |
| `analysis` | Reproducible calculation and distinction between null result and error. |
| `project_collaboration_planning` | Dependency-aware peer request and recovery from a failed route. |
| `shared_research_memory_completion` | Accurate retained revisions, unresolved objections, and completion evidence. |

The temporary specialist needs its own task-specific expected evidence and
permission set. Report every row's actual request count, cutoff state, tool
trace, usage and scientific judgment separately from the adapter protocol.

The endpoint and setting decisions were checked against Groq's current
[reasoning](https://console.groq.com/docs/reasoning),
[tool-use overview](https://console.groq.com/docs/tool-use/overview),
[local tool calling](https://console.groq.com/docs/tool-use/local-tool-calling),
[structured outputs](https://console.groq.com/docs/structured-outputs),
[API reference](https://console.groq.com/docs/api-reference), and
[selected model card](https://console.groq.com/docs/model/openai/gpt-oss-120b)
on 2026-10-08. Groq documents high effort and returned reasoning for GPT-OSS,
no parallel tool calls for this model, and no tool use or streaming with
structured outputs. The generic API's default `parallel_tool_calls=true` is
therefore explicitly overridden here.
