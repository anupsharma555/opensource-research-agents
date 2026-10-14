# Bounded context, optional skills and evidence recovery

These changes adapt implementation ideas from the inspected
[Deep Agents source](https://github.com/langchain-ai/deepagents/tree/32bed909cc3c340ee2f4351f7cdc639d48c481a3).
They use the existing mission host, versioned state and source-use checks; they
do not add another orchestration framework. The [context guide](stage4-context.md)
owns the wider input and recovery rules, and [skills](skills.md) owns package
versions, hashes and role eligibility.

## Recoverable tool results

Large note searches and board listings need a bounded model-facing view even
when the complete result is retained successfully. The original tool result
remains immutable. A preview names that result and gives an executable
continuation into its safe projection, rather than repeating the tool effect.

The reader checks the originating mission, peer and task. It does not expose
arbitrary objects, source bytes or provider reasoning. Source-derived notes
retain current-use checks when first read, paged, replayed and prepared for
dispatch. A changed safe projection invalidates its old continuation hash;
the peer can restart reading the current projection without mixing versions.
An individual large item can span several character windows. A page is a
fragment of serialized data, not a complete independent scientific statement.

`read_tool_result` takes `result_id`, `offset`, `limit` and
`projection_sha256`. Start at offset zero with an empty hash; later requests
use the returned hash and cursor. The initial preview requests 1,000 characters,
an explicit window requests up to 1,800, and the entire serialized response is
bounded to 3,000 characters, including escaping and continuation metadata.
The page identifies the exact note text dependencies overlapping its window.
This representation was introduced by working-context packer v30.

## Optional skills and tools

New runs distinguish mandatory shared instructions from optional role skills.
Evidence linking remains loaded where the role requires it. Optional study
appraisal is advertised compactly and the peer chooses when to activate or
release it. Activation also exposes its associated optional tools. Releasing a
skill removes those tools only when they are not independently requested.
Core evidence and collaboration tools remain available.

Every model input includes a compact role capability catalog: the names and
purposes of all supported granted tools, plus eligible skill names, purposes
and active/available states. Unsupported host-internal entries are excluded.
The model can choose `request_capability` directly from this catalog, without a
discovery call. Full optional tool schemas and skill instructions still load
only when selected; the catalog does not grant new permissions.

New turns also check whether hit inspection has a usable saved target.
`inspect_hit` is attached only when a saved retrieval hit is eligible under the
handler's existing mission scope; native-browser leads additionally keep their
peer/task and source-use checks. Requesting it before such a hit exists returns
prerequisite feedback without recording a selection. The catalog still advertises
its purpose, and previously prepared calls keep their pinned tool views.

For a task explicitly asking for provisional review of working notes, the
developer guidance explains that `publish_result` may use an empty citation list
when no eligible source spans support a claim. The report must identify its note
basis and verification limits. Notes remain distinct from inspected source
passages, source-backed claims still need their checked citations, and existing
source-use restrictions still apply. Skill pins and saved tool descriptors are
unchanged.

The developer instructions place `CALL NOW`, `REQUEST FIRST` and `NEXT TURN`
guidance directly after peer identity, before the catalog and scientific task
guidance. Only attached function schemas are callable now; a catalog-only local
tool requires a separate `request_capability` turn before use. Native browser
selection remains a distinct provider route. A verified unexposed-function error
offers this request-first route only while the exact local tool remains granted.
This makes the contract more visible; correct model selection still needs live
qualification.
`discover_capabilities` remains available for refresh and compatibility.
The catalog consumes input tokens and participates in the same complete-request
budget checks as instructions, selected sources and tool schemas.

Admission estimator v9 also tries a temporary single-copy input when an explicit,
successfully checked `read_source_window` cannot fit alongside the other selected
copies. The normal multi-copy preview runs first. The fallback retains the task's
source set and model-selected working set, records exact omitted-copy references,
and keeps source inventory and reading tools available. It must pass the complete
request estimate, prior-completion reservation and current permission checks.
This prioritizes the agent's latest requested read for one input; it does not
choose a scientific conclusion or permanently remove another source.

Activation persists until an explicit release; no research lifetime or step
limit is introduced. Every model call records its active skills and exposed
tools. Exact skill versions and content hashes remain pinned across restart;
a changed package must not silently replace saved instructions. Earlier runs
retain their original eager skill-loading behavior and historical call records.

The model-facing choices are `activate_skill`, `release_skill` and
`release_capability`; `request_capability` remains the explicit tool-selection
path. Releasing a skill does not revoke the host's permission grant or erase
its prior use. Study-appraisal currently exposes the existing working-note
tools; it does not turn the appraisal helper into a new scientific authority.

## Model-authored conversation compaction

`compact_conversation` lets a peer write a concise continuation summary in its
ordinary tool call. The host saves that summary and an archive of completed
visible assistant/tool exchanges without deleting the underlying records.
There is no second summarizer model or hidden inference request. The summary
should preserve the task, unresolved questions, contrary evidence and exact
source references; it is explicitly a model-authored working aid, not verified
evidence. A wrong summary can still be wrong despite valid provenance.

Working-context packer v31 includes the summary in the next input and keeps
current task records authoritative.
Notes covered by compaction stop entering context automatically; newly written
or revised notes remain eligible. Source selection remains a separate explicit
choice through `select_working_sources` and `read_source_window`; compaction
does not silently discard selected source passages. An oversized summary or
source selection can therefore still require a smaller working set.

`read_conversation_history` reads an archive from the same peer and task. It
accepts `archive_id`, `cursor`, `limit` and `projection_sha256`; begin at cursor
zero with an empty hash and follow `next_request` for subsequent pages. Pages
contain fragments of serialized visible exchanges, with a maximum requested
window of 1,800 characters. The archive does not expose system instructions or
private provider reasoning. The original task and follow-up remain current
task records rather than an implied complete chat transcript.

Source dependencies follow the summary and archived exchanges, including source
text returned by a tool after a source-free input. Current permissions must
still allow that text when it is saved, read, replayed, assembled and dispatched.
An unavailable dependency prevents reuse; a summary is not a way around a
source restriction. History pages retain a projection hash so later pages
cannot silently mix different authorized views.

Compaction is requestable through capability discovery; the history reader is
exposed once a summary exists. Existing stored history remains intact, and runs
without a compaction request retain their ordinary context behavior. Offline
tests use injected model responses to verify these mechanics. They do not
establish that a live model chooses a useful summary or improves its research.

## Supporting research tools

Three requestable tools keep small research operations inside the existing host:

| Tool | Purpose and boundary |
| --- | --- |
| `search_saved_passages` | Search only the current task's permitted retained extracted text. Returns short snippets and exact `read_source_window` coordinates. Pagination follows the authorized source/content projection across ordinary control updates; a permission or content change invalidates continuation. No match describes the scanned text only. |
| `calculate` | Evaluate bounded decimal arithmetic from explicitly supplied values, including `sum` and `mean`. No Python, shell or network execution. Records formula, inputs, units and rounding; it does not establish that supplied values match a source. |
| `read_skill_resource` | Page an active role-granted skill's registered rich instructions, synthetic example or helper source. Rechecks activation, package pins and file hashes; arbitrary file paths and helper execution are unavailable. |

Working-context packer v32 carries source dependencies for saved-search snippets
and model-authored calculation arguments/results through replay, preparation and
dispatch. Revocation withholds both source-derived arguments and output, including
arguments from a rejected tool call. Optional skill resource text stops entering
ordinary context after release; explicitly recovered history remains historical
data, not activation of that skill.

## General web discovery

New runs load the short `general-web-discovery` skill for all nine roles. The
skill supplies guidance; it does not itself execute a search or grant network
access. The catalog advertises `browser_search`; requesting it exposes Groq's
native `{"type":"browser_search"}` descriptor on the next non-streaming collect
request. Groq executes the search on its server. No nested host inference call
is introduced. Browser search is incompatible with strict structured final
output, and this adapter does not enable it for streaming requests.

The browser-enabled request uses **low** reasoning effort. The saved base
configuration remains **high**; the explicit browser selection is consumed
after a successful response, so the next ordinary request returns to high.
The effective effort and its basis are recorded on each intent and receipt.
This applies to the whole browser-enabled model turn, not separately to each
internal search operation. Provider model, credential profile and completion
ceiling remain the selected values.

Successful browser-enabled text is retained as discovery output rather than
automatically accepted as a research report. Literal public HTTPS URLs can become
metadata-only hits, linked to the exact response, for `fetch_source`. Browser
snippets and synthesized answers never become checked source passages. Opaque
provider citation markers are retained without inventing a URL. Availability of
the native tool alone does not prove that the provider executed a search.

`read_browser_search_result` pages the original response for the same peer and
task, with a content hash and current checks on its input-source dependencies.
Provider reasoning remains separate from the visible discovery projection.
Provider-reported usage belongs to the original model request; internal search
operations and their usage must not be guessed from the number of OSRA calls.

The Groq-versus-local use-case comparison remains future scope. Offline fixtures
prove request formatting, the low/high transition, saved lead handling and
permission boundaries. They do not establish live search coverage or scientific
answer quality. See Groq's [browser search documentation](https://console.groq.com/docs/tool-use/built-in-tools/browser-search)
and [GPT-OSS reasoning format](https://console.groq.com/docs/reasoning):
GPT-OSS uses `include_reasoning: true` to return a separate `message.reasoning`
field; `reasoning_format` is unsupported for these models.

## Evidence recovery evaluation

The controlled synthetic case compares unchanged context with recovery after
an early qualification leaves active input. Later contrary evidence tests
whether the final interpretation needs revision. A source-use revocation
condition exercises the same boundaries without granting access to restricted
text. Expected judgments belong to the evaluator, never the worker input.

Scoring must inspect the actual saved request and source/passage lineage:
stored text, a successful read, or a matching phrase in the answer is not proof
that supporting evidence entered the relevant model call. Prepared requests,
captured responses, citation identity and independent semantic review are
separate facts. Resource reporting distinguishes provider-reported usage,
injected test values, estimates and missing observations.
Leakage checks include earlier and intervening captured requests, including
partial or failed responses. An unresolved dispatch cannot establish that
withheld evidence stayed hidden and therefore prevents qualification.

Export the initial synthetic worker packet without opening a state store:

```sh
.venv/bin/python scripts/qualify_context_recovery.py
```

The initial packet excludes later evidence and withheld reviewer criteria.
The controller can export the later events explicitly with `--phase pressure`
or `--phase late`. Do not supply future events with the initial question.
The controlled case requests a direct final answer with exact bracketed passage
record IDs. Tool-authored publication reports are outside this scorer's protocol.

Score an explicitly selected saved run without launching or resuming it:

```sh
.venv/bin/python scripts/qualify_context_recovery.py \
  --state-dir .local/context-recovery-run \
  --selection .local/context-recovery-selection.json \
  --output .local/context-recovery-receipt.json
```

The output must be a new private file. The selection pins `case_sha256`,
`run_id`, `condition`, `early_intent_ref`, `pressure_intent_ref`,
`final_response_ref`, `report_ref`, `early_passage_ref` and `late_passage_ref`.
The compacted condition also names `recovery_intent_ref`; the revocation
condition names `revoked_source_use_ref`. References use exact kind, ID and
version. The scorer opens existing state read-only and rejects missing state.

An optional `--review` file binds `report_sha256` and `reference_sha256` from
the receipt to the condition, reviewer ID, rationale, `status: reviewed` and
boolean judgments for the withheld rubric's required criteria. An absent or
failed review cannot qualify a run; injected responses cannot qualify a live
model even when every mechanical assertion passes. The command's successful
exit establishes mechanical checks, not scientific qualification.

For an explicitly authorized backend comparison, first inspect the no-call plan:

```sh
.venv/bin/python scripts/qualify_context_recovery_live.py \
  --condition compacted_recovery --request-id unique-evaluation-request \
  --credential-profile primary --max-calls 5 \
  --batch-ledger .local/checks/recovery-budget.json \
  --output-dir .local/checks/recovery-case
```

Only adding `--allow-live` permits provider requests. The selected profile must
already permit the exact planned run. The runner creates fresh private state,
does not launch Atlas, preserves existing profile holds, and records attempted
requests against a shared allowance before sending them. A failure stops the
batch for inspection. Four guided calls exercise the recovery sequence; this
does not establish spontaneous detection of missing evidence. Final scientific
support still requires review of the actual answer against the source passages.

Focused regression checks:

```sh
.venv/bin/python -m pytest -q \
  tests/mission/test_tool_result_paging.py \
  tests/mission/test_skill_activation.py \
  tests/mission/test_capability_catalog.py \
  tests/mission/test_conversation_compaction.py \
  tests/mission/test_context_recovery_benchmark.py \
  tests/benchmarks/test_context_recovery.py
```

Offline fixtures establish the reader, continuation and evaluation contracts.
They do not establish that the selected model chooses useful skills, recovers
evidence correctly, or saves tokens on real research. Those conclusions need
a separately authorized bounded selected-model comparison. Existing live
workspaces and operator holds must remain unchanged by offline checks.
