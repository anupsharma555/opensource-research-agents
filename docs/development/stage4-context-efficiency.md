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

Packer v45 also bounds web and scholarly discovery listings. Their previews
retain exact operation/candidate handles and advertise a `read_tool_result`
detail request; the permission-filtered full listing stays pageable without
repeating discovery. Web operation navigation survives intervening capability
selections and later operation versions. Current source permissions and producing
source dependencies apply to both previews and detail pages. This is discovery
metadata, not checked source text; source-window delivery remains separate.

Packer v50 adds the same saved-result reader to `search_saved_passages` and
pages oversized immediate replies. Its latest query and exact read coordinates
remain visible even when the full exchange is archived. Previewed matches,
matches in the saved page and additional query matches are counted separately;
reading an existing result does not rerun the search. Both searched-copy and
producing-input source dependencies survive paging and are rechecked before
dispatch. These navigation snippets do not become checked claim evidence.

Packer v51 presents new OCR receipts with the exact selected text, source URL,
physical page, region/layout, unverified status and continuation. It retains
source-authored figure captions with their parent URL and truncation flag when
the image came from a discovered asset. Engine hashes, word-box diagnostics and
full image transforms remain in immutable OCR records instead of repeating in
model input. Current source and caption permissions are checked before this
projection; native checked passages and OCR transcriptions remain distinct.

Packer v52 also reduces peer-message bookkeeping under input pressure. Identical
next-action guidance for outstanding requests is shown once; each request keeps
its own status, exact reference and recipient. An unanswered request can retain
its question and a compact identity preview while its full record remains
available through `read_context_record`. Short requests retain the ordinary
view. This does not summarize peer answers, merge different questions, or turn
receipt of an answer reference into evidence that its prose was read.

The current question is included once when the task objective already contains
it, and the separate short scope survives packing. Longer previews retain an
explicit partial flag and exact saved-record reader. Selected image captions and
OCR/vision actions similarly survive capability changes, with current source-use
checks and truthful attached/request-first labels. See
[document readers](document-readers.md) for the image-specific correction when a
peer tries the text reader on a PNG/JPEG. These changes preserve useful next
actions; they do not require the peer to follow a prescribed reading sequence.

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

The developer instructions begin with shared `CALL NOW`, `REQUEST FIRST` and
`NEXT TURN` guidance and research rules. The exact peer identity follows this
shared prefix, before the role-specific catalog. Only attached function schemas
are callable now; a catalog-only local
tool requires a separate `request_capability` turn before use. Native browser
selection remains a distinct provider route. A verified unexposed-function error
offers this request-first route only while the exact local tool remains granted.
This makes the contract more visible; correct model selection still needs live
qualification.
`discover_capabilities` remains available for refresh and compatibility.
The catalog consumes input tokens and participates in the same complete-request
budget checks as instructions, selected sources and tool schemas.

Repeated input has two costs: the size of each request and the number of turns
that resend it. Preserve short purposes for every granted tool and essential
source-use rules, while keeping detailed optional guidance behind
`discover_capabilities`, `activate_skill` and `read_skill_resource`. A saved
reference is useful only when an exposed or requestable reader can resolve it.
Do not replace required instructions or attached argument schemas with an
unreadable link, or assume the model remembers an earlier request implicitly.

Keep three levels of tool guidance distinct:

1. The full catalog carries short names and purposes for discovery.
2. Attached schemas carry executable arguments and concise selection guidance.
   Under input pressure, unselected default scholarly, board and peer-reader
   schemas may be deferred through the same request-first mechanism used by
   optional tools. Explicit selections and active-skill tools remain attached.
3. Extended workflow guidance stays in optional skills and their pageable
   resources. Essential evidence, permissions, continuation and callability rules
   remain in the request. Fetching more guidance consumes context and can add a
   model turn, so a link is useful only for detail that is not needed every turn.

Admission estimator v17 attempts that schema deferral before an estimate-only
provider-feedback probe or source-free recovery. Packer v46 also removes a
duplicate selected passage preview only after proving the exact text and current
permissions match the separately delivered source packet. Public-document
coverage accepts the exact historical joined-block length only with verified
extraction lineage; new inspections record the sum of block lengths. Saved
receipts are unchanged. When a checked source window is selected under pressure,
repeated search lead cards and routine outcome pointers can be replaced by their
existing saved-result readers. Operation handles, continuation, current source
progress and the exact selected evidence remain available.

Packer v47 preserves a concise reading-position description in both ordinary and
compacted source-window replies. The description distinguishes physical PDF page
locators from extracted block counts and treats `next_request` as an available
reading action, not a requirement to scan every page. See
[selective reading](document-readers.md#selective-reading-and-progress) for the
supported saved-text-search route, completion guidance and proof limits.

Minute pacing and request size are separate controls: waiting replenishes a
rate window but does not make the next serialized request smaller. Compact
avoidable overhead first, then check the complete packet and its completion
reservation. Preserve provider feedback, real rejection repair, explicit run
ceilings and the configured output cap; never infer that an unseen response
consumed its full reservation.

Packer v48 pages older web lead cards through their exact saved-result reader
when a checked source is selected under pressure. It also removes verified
duplicate run references, equal source-status fields and redundant HTML fetch
aliases while retaining the source URL, document reference and factual reuse
feedback. Non-HTML reader advice remains intact. Confirmed size-repair packing
may compact these duplicates before the ordinary hard target is reached to leave
some headroom; it does not change the provider-feedback policy for an untried
forecast, the output cap, selected tools, or source permissions.

Compact catalog purposes and instruction wording retain every discoverable tool,
current callability and optional skill choice. The complete serialized request
is still the sizing authority. A passing saved-state replay qualifies only that
packet; a more informative passage or a later tool result can need a different
fit. See [source walkthroughs](source-quality-walkthroughs.md) for delivery and
answer-support checks.

Groq's [prompt caching](https://console.groq.com/docs/prompt-caching) supports
GPT-OSS and exact repeated prefixes, including instructions and tools. Keep
stable instructions before changing task/source data. Cache hits are not
guaranteed, and Groq credits cached tokens against rate limits after processing.
Do not assume a hit to admit an otherwise oversized cold request. The adapter
retains reported `prompt_tokens_details.cached_tokens`; missing values mean
unknown. Validate actual cache benefit separately from offline packing savings.

Variable peer identity no longer breaks the first shared instruction paragraph.
This reorders existing developer text without removing the role, scientific
guidance, source rules or capability catalog. Saved prepared requests retain
their exact bytes; new configuration snapshots record the changed layout.
Measure both same-peer and cross-peer prefix overlap, and retain tool-schema
comparisons separately from provider-confirmed cached tokens.

For new local-function requests, `ModelSession` validates the exact saved tool
set before presenting the attached core tools first, followed by the remaining
attached tools in name order. The canonical exposure manifest stays alphabetical;
no tool is added, kept attached, or granted solely for caching. Native browser
requests keep their separate tool path. Previously prepared requests are loaded
from their saved bytes, so a layout change does not rewrite pending requests.
Ordering may affect model choices and still requires live qualification.

When input pressure deferred default core tools, the next turn can retain that
smaller attachment instead of restoring unrelated schemas after one explicit
capability selection. `tool_attachment_policy.carry_forward_core_deferral`
requires the exact previous call and budget from the same actor, task, run and
configuration. Preparation rechecks the final configuration after source
selection; a provisional match does not carry into a changed source setup.
The saved budget records the tools actually deferred, so subsequent turns can
repeat this decision after reopening. Missing or mismatched records use the
ordinary attachment policy. Current grants, explicit selections, release,
schema pins and the full discovery catalog still apply; invitation and native
browser turns retain their separate paths. This changes newly prepared inputs,
not previously captured request bytes. Matched synthetic host tests measure
serialized input savings; provider cache hits and fewer live steps remain
separate evidence requirements.

For a currently readable answer to this task's own pending request, pressure
deferral keeps the existing `read_context_record` and `review_peer_answer`
schemas callable. The host checks exact requester/task/answer/report lineage
and current access, then intersects with the pinned tool grants. Explicit
release receipts suppress this convenience; a later explicit selection still
works normally. This exposes choices without reading, accepting or reviewing
an answer automatically. Closed requests, unrelated tasks, invitation reviews
and native browser turns do not acquire these tools through this path.

Reading an exact `mail_answer` can also return a `related_report_window` in the
same result when the caller is entitled to the linked report. Parent record
text, hash and offsets remain unchanged. Parent and report text share the
requested character limit, while each retains its own paging metadata; report
continuation resumes at the returned offset with the normal reader page size.
The report remains model-authored peer material, not inspected source evidence.
Preparation records its source dependencies and replay rechecks access before
delivering the nested text. A complete report projection must actually be in
the current model input before a test can credit complete answer delivery.
The result also carries the exact peer question within the existing bounded
question window; longer questions expose their exact reader without a clipped
excerpt. Offline path comparisons must derive choices from the current packet,
surface all injected transport exceptions, and account for each attempted turn.
Runtime recovery of a broken test chooser does not establish an efficient path.

When the current answer read carries an exact report page, packing can omit an
older direct read of that same page. It compares the report identity, version,
hash, text and navigation, ignoring only the two requested/effective limit
fields. A nonempty earlier assistant explanation prevents this consolidation.
The saved exchange remains recoverable, and current source access is rechecked.
Different pages and partial overlaps remain separate. A repeated direct report
read may receive a short observed-repeat hint only when it fits without removing
the requested text. This does not force the next action or prove that an earlier
result was seen. Tests live in `test_duplicate_report_read_compaction.py`;
serialized input savings alone do not establish fewer natural model steps.

An exact repeated `peer_request` can return a bounded observation of earlier
open requests with the same question, sender, recipient and task version.
This feedback is saved with the original message and remains stable on work
replay. It records observed versions and statuses, not a guarantee that those
requests remain open; concurrent requests may be missed. Both requests remain
distinct. No semantic similarity rule rejects or merges scientific questions,
and ordinary request results gain no extra feedback text.

## Public action purposes

Shared agent instructions ask for brief public explanations of consequential
skill choices and actions, including activation/release, tool selection, peer
requests, waits and changes of approach. Agents use a purpose/reason argument
when that tool supports it, or ordinary assistant content otherwise. After a
meaningful result they state what changed and the next action in the same
response or next normal turn. Routine unchanged steps do not need repeated
explanations. This guidance adds no separate model call or execution gate;
instruction delivery and explanation capture do not prove live compliance.
The same shared instructions ask peers to address an existing incoming request
before seeking another independent check. When help is needed first, the peer
states its provisional finding and specific gap. This is standing agent guidance,
not an extra instruction in evaluation questions or a host ban on early checks.

Mission `peer_request`, `request_capability` and `activate_skill` descriptors at
version 2 accept an optional short `purpose` alongside their ordinary arguments. It states the
agent's intended benefit in the same model response; no separate explanation
call is required. Missing, null or blank values provide no explanation. Reports
may quote nonempty purpose text as agent-stated intent, while checking actual
actions and results separately. It is not a record of private reasoning.

The purpose stays in the saved tool arguments and their normal replay. It does
not grant permission, change the requested peer/question or skill, or become
recipient message payload. Skill activation still uses the same package/version
checks and delivers the pinned skill instructions through its ordinary result.
Exact older version-1 descriptors remain executable through a
narrow compatibility check; their saved requests and schemas are not rewritten.
Cloud traces retain the metrics-only content boundary. Optional public text is
not automatically exported or treated as proof that an action was useful.

An incoming reply may be saved with `publish_result(answer_request_id=...)`
while that peer's own check is pending. This closes its current work, while
outstanding requests still govern run closure and can resume the peer for
review. Providing a public purpose does not replace the reply, its delivery,
the requester's read, or explicit review.

When reviewing token efficiency, separate provider-reported input/completion
usage from local tokenizer estimates of individual prompt components. Reasoning
is a completion subset; an output reservation is not consumption. A rejected
request without usage remains unknown. Measure complete serialized requests and
preserve the full capability catalog in before/after checks; a smaller prompt
alone does not prove fewer turns, lower total usage, or a supported final answer.

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
For `compacted_recovery`, the controller imposes an 8,000-token comparison
allowance with a 70% input target. This explicit evaluation bound cannot be
bypassed by the provider-rate estimate probe. The host may use the remaining
room up to the same hard allowance after an exact recovery read. The scorer
checks the saved pressure and final requests separately; a recovered passage
that never reached the final model input does not pass.

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
