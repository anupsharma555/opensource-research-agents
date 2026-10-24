# Stage 4 local mission service

`ora.mission.MissionService` connects the saved agent registry, proposal board,
mailbox, capacity ledger, concurrent executor, selected-model session, discovery
routes, intake, and Atlas snapshot store. It is a local research service, not a
production deployment or an endurance result. The service accepts the shared
`LiveStartRequest` and `LiveFollowupRequest` contracts and returns a saved
`LiveReceipt` before model work finishes. Status reads return the latest saved
`ResearchSnapshot`; reading status does not dispatch work.

## Durable turn boundary

One executor work item covers one selected-model call or one tool action. An
accepted `continue_task` outcome saves its artifact, receipt, and trace while
leaving the scientific proposal open. The next call has a new attempt, call,
intent, and capacity operation. Model permits therefore end before a tool or
peer wait. Work submission and its matching task-control transition commit in
one transaction before dispatch is notified, so a callback cannot observe an
unpublished control. A final response or `publish_result` can complete that peer's task;
mission closure is a separate transaction checking accepted contributions,
explicitly required tasks, peer obligations, and open optional controls. An empty ready
queue is a wait, not completion.

For `navigate_venue`, submission also saves a task-scoped local operation
reference with the original model response, tool-call ID and arguments hash,
attempt, configuration and executor work. This happens before the existing
background callback starts. The exact record reader distinguishes prepared
work, callback dispatch, the venue fetch's saved dispatch marker (which alone
does not prove HTTP began), its retained
result, executor acceptance and an uncertain dispatch. A held fetch needs no
model polling; independent work continues through the same executor. Only the
accepted result becomes a final model-visible tool result, once, with the
operation reference. No partial artifact is claimed when the fetch supplied
none. An interrupted fetch remains uncertain rather than being replayed as a
new HTTP request. This local identity does not imply an upstream asynchronous
job protocol or a native progress view.

A locally rejected peer request remains an unresolved action on its task even
after the original tool exchange is omitted from later prompts. A subsequent
direct answer is retained as a provisional contribution. The peer can obtain and
review a response, or record a later reasoned scope disposition explaining why
that review will not be used; sending an invitation alone does not resolve it.
This applies to attempted peer actions, not requirements inferred from prose.
Already completed historical runs are not rewritten by this continuation rule.

An accepted peer request returns its exact saved reference and the recipient's
task-control reference. The requester may continue independent work or choose
`wait_for_dependencies`. That explicit wait follows routine progress on the
same recipient task without asking the model to poll for a new control version.
An answer, a changed request, or a recipient finishing without an answer returns
the requester to review; cross-run targets remain invalid. This is a local wait
on the model's chosen dependency, not a requirement that other peers stop.

Direct answers can retain explicit bracketed passage identifiers as navigable
citations when they match an unambiguous checked passage in that exact producing
input and its current source-use policy permits it. Unknown, unselected, or
ambiguous identifiers remain unlinked. These links establish traceability, not
scientific support for the surrounding text; they add no conversion-only model
call. Structured `publish_result` remains the explicit publication tool.

Old queued calls whose recovered reservation exceeds a recorded full-request
token ceiling are replanned only if their exact work and intent remain unclaimed
and undispatched. A transaction preserves the old input and records its
replacement before preparing a fresh call under that ceiling. A temporary lack
of remaining tokens still waits normally; uncertain dispatch or corrupt lineage
stays a visible local repair condition without replaying the old call.

Peers can propose, revise, or defer a board task and invite another supported
role through saved tool turns. Every optional proposal has its own task control,
call IDs, and attempt history, even when its actor owns a requested task. An
invitation opens a separate recipient-owned review turn. Only the recipient
can accept, decline, or counter; accepted directed work carries the exact
invitation into later claims. The starting participant list remains the user's
original selection. New runs treat `requiredAgentIds` as an explicit subset;
historical runs without participation policy version 2 retain their original
all-starting-required rule. Any participating peer may signal requested-scope
completion through `propose_scope_completion` or `publish_result`. Omitted
optional controls continue by default. Exact agent-authored defer decisions
cannot cancel work in flight. Without a scope signal, direct completion waits
for optional work and pending peer obligations. Continued optional work and
later peer replies remain runnable after requested scope completion.

Closure records the last committed mission event sequence in the same
transaction. A subsequently opened peer request receives an exact saved
disposition: continue under its already continued optional scope, or require a
new scoped follow-up. This records why later work is eligible without rewriting
the completed snapshot. Older runs lacking the cutoff retain their existing
behavior; the host does not invent an opening order for them.

The saved run profile and call fix the selected Groq model, endpoint, output
settings, saved reasoning effort, tool descriptors, role instructions, skill
versions, and exact input references.
When a source or registered tool view changes, a new configuration points to
its predecessor and records the reason; model and reasoning settings stay
pinned. Current inbox/request records and saved tool-turn results are assembled
before each model call. An inclusion receipt proves which exact records entered
the request; it does not imply that the model used them scientifically.

Retrieved text enters a model request only through a checked source copy,
passage references, and the current source-use policy. Source text and tool or
peer content are data under the saved role instructions. Search results expose
metadata and hit IDs; `inspect_hit` or the bounded public HTTPS fetch checks
actual bytes, article identity, and copy-specific use before a passage is
offered as model input. A discovery hit's abstract depth or permission does not
grant use of different fetched full-text bytes. Unknown or
restricted use returns a local diagnostic. Direct, cache, and peer source routes
use the same model boundary.

Inspection initially previews a bounded substantive block with exact offsets
and a section outline while retaining the exact source copy.
`read_source_window` gives a peer a checked, paged passage and
cursor from that copy. The next model call selects the requested window's
passage references; earlier windows remain citable through saved exact
references. The tool bounds each response, not the number of windows a peer
may choose. A peer can explicitly request up to 16,000 characters from one
extracted block, while `inspect_hit` initially previews its first 1,900
characters. Smaller requests and exact continuation cursors remain available.
Saving a larger window does not prove its text entered the next model input;
all permitted saved copies enter a model turn only when the complete request
fits the observed input allowance. Otherwise the service sends a source-free
inventory with exact reading positions so the peer can choose copies or smaller
windows; it does not rank away scientific sources. A metadata-only hit reports
which configured routes can return source text without treating metadata as
support.
Venue catalog entries and peer-proposed URLs are navigation leads. A peer may
open an exact entry with `navigate_venue`; the saved page, redirects and links
do not become a checked scientific source. Report citations keep exact passage
references and record whether the cited passage entered the producing call or
was saved from an earlier inspection.

## Tool inputs and continued reading

Tool descriptions and schemas are saved with each prepared model call. The
mission handler validates against that exact schema before running the action.
Current search tools accept the documented `pubmed` and `crossref` aliases as
search routes; an exact identifier lookup uses `pubmed.pmid`, `pubmed.abstract`
or `crossref.doi`. The two PubMed routes distinguish ESummary bibliographic
metadata from the available EFetch published abstract. A positive integer PMID is converted to its decimal text without
changing the identifier. A route name belongs in `search_scholarly.route`, not
in the function name.
An absent abstract remains metadata-only and can prompt another source route.
The original abstract and a same-title correction must retain their distinct
PMIDs/DOIs. An explicit NLM truncation marker remains a partial original during
intake and reading-coverage review; it is not silently treated as complete text.

Europe PMC pages use an opaque cursor; PubMed and Crossref search pages use an
offset. Search results supply `next_request` when another page is available, so
a peer can reuse the provider's continuation. The host does not guess a cursor
from an offset or choose a different scientific search route.

`inspect_hit` returns the exact source ID and version. A larger positive source
window request returns at most 16,000 characters from one block per call,
reports the requested and returned sizes, and supplies a reusable
`next_request` for the same copy. A full reread from offset zero can use
`selection_mode=replace` to avoid including the initial prefix twice.
Context pages similarly report their requested and effective page sizes.
Neither page bound limits the number of reads or changes the retained source.
A model call sees only the context assembled for that call; a separate request
does not automatically remember an earlier request. Exact retained text, reading
cursors, task notes and prior findings provide continuity. A peer can reread an
adjacent passage or overlapping window when an argument crosses the boundary.
Whole-text conclusions remain provisional while relevant sections are unread;
supported passage-level facts and unrelated parallel work can still progress.
A missing or out-of-scope reference remains rejected, with a next lookup step;
known local validation failures state that no external action occurred. Failures
whose external effects cannot be established keep that uncertainty visible.

New calls on older participation policies expose the compatible publication
schema. A previously prepared call may still contain the newer inline
`scope_completion` field. Its supplied decisions are checked using the saved
policy: legacy runs retain their requested-participant authority and complete
optional-work coverage requirement. The result identifies the applied policy;
the saved run and original request are not migrated or rewritten.

## Recovery and peer work

The local supervisor scans the shared store on startup, and the executor
rechecks eligible work and due capacity waits. A recorded 429 waits for the
provider retry time, then uses a fresh call/intent ID; replaying the original
captured response would not retry the provider. A lost response from a saved
Groq local-function call can be fenced and superseded by a fresh call after a
saved backoff. Its remote response and usage stay unknown, its quota charge
remains conservative, and any late tool suggestion is never executed. Uncertain
side-effecting tool work still requires reconciliation. A saved accepted segment
or report is not
run again merely because the process stopped before updating its read control.
Each newly dispatched executor work item saves its actual custom-asyncio runtime,
capacity generation, attempt identity and configured renewable lease before its
callback runs. It records that no executor-wide callback timeout or SDK step limit
applies. The metadata transaction rechecks the current capacity lease and board
claim before allowing the callback. A submitted item has no dispatch metadata
yet; historical items without the field remain unknown. The provider's
per-request HTTP timeout is a separate
setting, not a limit on the executor segment or scientific task. If the runtime
record cannot be saved after admission, no callback runs and the attempt is
reconciled as a local no-effect failure.
Repeated identical quota waits leave the task-control version unchanged.
Accepted reports clear operational wait fields. An undispatched preparation
replaced by a later call is recorded as superseded at the replacement time.
Older inactive attempts retain their original versions and unknown end times;
the refreshed snapshot labels them superseded when no current control names
their call, while accepted reports remain completed.
An observed 413 can lower the next call's input target while retaining the
selected model and reasoning. A known provider tool-name rejection becomes a
saved, model-visible correction and a fresh call. The rejected response and
original request remain inspectable.
The same recovery applies to verified optional-null schema rejections. Tool
fields accept null only where the existing handler already treats it as omission,
including search identifiers and the report's optional peer-request target.
Historical repair checks the exact captured provider error against the exposed
tool, its saved schema and a known compatible field. Required values and unrelated
invalid values remain validated. Other unexplained HTTP 400 responses keep their
unknown cause.

Fresh provider capacity headers now inform admission before the next model
request. Remaining requests and tokens retain their separate reset clocks;
local reservations share that headroom, and a later explicit Retry-After remains
in force across subsequent header observations and restart. Request estimates
use the saved serialized request and prior observed completion, with no assumed
cache hit. They remain estimates: other account consumers and unexpectedly long
output can still cause a refusal. The selected model, reasoning effort and output
allowance stay pinned. Known account input limits also inform context packing
before another oversized request is sent.
An observed token-rate limit is a pacing signal, not proof that the next
estimated input plus predicted completion will be rejected. If normal packing
cannot fit that forecast, preparation preserves the required current packet
and permits a provider-feedback attempt within the model context window. It
does not lower the configured output cap. The exact forecast remains recorded
in `mission_call_budget`, including `rate_admission_policy`.

Runtime request assembly also records `request_stage_token_estimate` in that
budget record before work is enqueued. It measures cumulative canonical variants
using the final request's pinned estimate basis: envelope, pinned instructions,
attached schemas, source packets, and each context or tool message. Marginal
counts sum to the final input estimate; they are not separate usage charges.
The receipt binds the final intent and request hash, active input target,
observed provider limit, prior-completion reservation and configured output cap.
The complete final estimate drives packing and the submitted capacity demand;
staged accounting is not a post-run substitute for admission.
When a complete preview fails local admission, its preparation diagnostic saves
`preview_stage_token_estimate`, including the candidate hash, stage totals and
`complete_request_not_dispatched` status. This describes an unsent preview, not
a saved final intent or provider usage. If assembly never produced a complete
preview, the host records the failure without inventing component counts.

These records can be inspected while a request is queued or in flight. The
shared capacity ledger separately tracks reserved, dispatched and settled work
within its StateStore and quota domain. Request drafts consume no provider
capacity. Before dispatch, concurrent workers share one atomic admission check;
after a wait they recheck current headroom. Provider-reported usage is reconciled
after capture, and a failed call without usage retains its reservation as unknown.
Local tokenization remains an estimate of provider processing, not an exact
provider token count. Auxiliary vision retains its existing image-plus-text and
output estimate, tied to its own exact saved request; it does not tokenize
base64 image bytes as text or use the main request's staged tokenizer trace.

If a request fits the observed full window but current shared headroom is too
small, the host waits for capacity. A single oversized request needs repacking:
remove redundant overhead, keep exact evidence windows and retain executable
routes to deferred material. A confirmed size rejection requires a newly
serialized smaller request under the tightened target; waiting alone cannot
repair identical bytes. Further research can proceed through multiple paced
requests while preserving saved work. The provider's observed limit remains
authoritative; the illustrative 8,000-token window is not a universal constant.
A tightened repair target belongs to its captured rejection. Once a different,
smaller replacement has a verified successful response, subsequent requests
return to observed-limit pacing and normal provider-feedback admission. The
original rejection and successful-replacement references remain available;
already-held tasks can resolve this from saved evidence. Newer matching rate
headers inform packing, and a fresh size rejection establishes a new repair
target. Successful repair must not leave a permanent smaller limit on later
tool selections.
[Groq's rate-limit documentation](https://console.groq.com/docs/rate-limits)
describes organization-level limits and response headers; activity outside this
runtime's ledger is known only through available provider feedback.

For a prepared model request whose forecast exceeds an observed full window,
the capacity ledger reserves that window for one exclusive attempt after local
charges and applicable provider waits clear. It retains the original estimate
and reconciles actual usage without clipping it. This exception requires saved
model-input estimate metadata; old unverifiable work retains its repair path.
Configured local ceilings and evaluation call/token allowances remain hard
bounds. A real provider size rejection keeps the existing smaller-request repair;
a rate refusal keeps its saved retry time. Partial tool arguments never execute,
and length-cutoff recovery preserves only eligible visible draft text.
When a token-headroom reset time passes, the provider's reported token ceiling
still guides combined local reservations and recent charges. It is not treated
as permission for several individually valid calls to exceed the shared window.
Older quota records can recover that ceiling from their exact saved response
header; no account limit is hardcoded. Other consumers of the provider account
remain outside this local ledger.

Among eligible queued operations, resource sharing uses recent charged tokens
and outstanding reservations within the configured quota window. Historical
lifetime call counts do not keep an older mission behind newer work after
capacity refills. Existing review priority remains; equal recent allocations
use queue age. This orders access to shared resources without selecting the
agents' scientific actions.

On startup, queued historical reservations with the old 8,192-token placeholder
are recalculated from their exact saved request and executor lineage. This updates
admission metadata only; active or uncertain calls, request bytes and accepted
results remain unchanged. Validated route-as-function errors receive specific
model-visible guidance; an unspecified tool-generation failure keeps an unknown
cause. Provider-reported cache and reasoning token details, when present, are
retained as subsets of the input/output totals rather than added again.

Peer requests and answers have individual recipient/requester states. A late
request or answer can reopen the affected peer task, while unaffected peers
continue. The requesting peer reviews a saved answer explicitly. Peers can save
a rough latency category without a deadline, or wait on exact unresolved peer
and proposal dependencies. A changed dependency, explicit wait cycle, or
stranded wait returns the affected peer to model-directed work. An empty saved
dependency list is inspected too: when no work, timer, search wait or operator
hold can wake that control, it receives the exact wait diagnosis once. The
transaction rechecks the control version and run cancellation before releasing
the wait. It never chooses the scientific continuation. Offline integration
tests in `tests/mission/test_stranded_wait_resume.py` verify delivery in the next
serialized model input, restart, a healthy sibling and concurrent holds or
cancellation. Test fixtures that deliberately pause a peer must use a real saved
hold or future timer, rather than accidentally constructing this stranded state.
A peer may also
choose a positive review interval for its latency expectation. When that interval
elapses, the host saves one neutral, task-scoped diagnostic for the exact
expectation version with the current work phase and wait. It does not change the
task or call the model; the peer can inspect it on a later normal turn. Older
category-only expectations retain their behavior, and known capacity waits are
identified as waits rather than scientific nonprogress. The notice stops entering
later model inputs only after an exact included request has a captured successful
model response. That receipt proves delivery through the saved call, not that the
peer agreed with the notice or revised its work. Failed or merely prepared calls
and omitted notices leave it available for a later normal turn.

Follow-up creates a child run against an exact accepted parent snapshot and
output reference. The parent's saved snapshot is unchanged. Each state
directory has a durable workspace ID for reconnecting to that workspace.

## Verification and limits

Focused offline tests use injected provider transports and synthetic source
packets. They exercise direct completion, multiple model/tool turns, checked
search-to-citation and source-window paths, optional agent work and scope
disposition, malformed-tool isolation, peer answer review, interruption after
board acceptance, and provider repair:

```sh
.venv/bin/python -m pytest tests/mission tests/coordination -q
```

The full offline check passed 718 Python and 124 UI tests on 2026-10-09,
including 20 shared JavaScript contract cases, lint, generated-artifact checks
and the frontend build. Tool-interface regression cases also execute returned
continuations, reject real out-of-scope fixture records before dispatch, and
exercise legacy publication without changing its saved completion policy.
These tests do not prove live
retrieval, scientific accuracy, hours-long
operation, a durable external process supervisor, or remote deferred-tool
reconciliation. Live qualification keeps its own exact calls, source versions,
observations, and proof boundaries. Current context sizing is an estimate
against the selected endpoint's documented window. An observed HTTP 413 with
token-rate headers records a tighter account input target, retains the rejected
call, and prepares a fresh call with exact omission references and unchanged
model settings. Older completed tool turns become references while the current
unfinished tool pair stays intact. This is a per-call input repair, not a cap on
research turns. Source over-limit recovery uses an indexed turn so the peer can
choose a smaller working set. A fixed required-context overflow still needs a
scoped repair.

Additional offline integration cases cover changes while work is in progress:

- `tests/mission/test_inflight_context_integration.py` delivers a new source-copy
  version and peer update during a model call. The dispatched input stays fixed;
  after restart, the next input exposes the durable update without duplicating
  the message or completed tool result. This does not establish scientific uptake.
- `tests/mission/test_scope_completion_integration.py` inserts required peer work
  between the preliminary completion check and its transaction. It also checks
  recorded follow-up needs after completion, optional work that continues, and
  cancellation that cannot silently resume. This covers controlled interleavings,
  not every concurrent task or wake-event race.
- `tests/mission/test_evidence_instruction_boundary.py` uses the shared
  [source-instruction fixture](../../benchmarks/stage4/fixtures/README.md) through
  supplied and retrieved evidence. Source text retains its facts and locators
  without gaining authority to change the model or grant tools. Recovery choices
  are scripted, so actual model resistance requires separate qualification.

Two focused integration checks distinguish a recorded error from a demonstrated
repair. `tests/mission/test_tool_name_repair.py` retains a route-as-function 400
across restart, then executes the corrected scholarly tool through an injected
provider. `tests/mission/test_oversize_recovery.py` starts with a source-heavy 413,
checks the smaller exact replacement request, and confirms that a peer-selected
narrower passage enters a later model input. Both use offline transports and
preserve the original source and selected model settings. They do not establish
live account headroom or scientific correctness.

When a saved preparation has no model intent yet, restart may prepare that same
call again from a newer task-control revision. Its source-inventory operation
identity includes that revision, so changed context does not collide with the
older saved operation. A repeated operation ID with different inputs becomes
a visible local repair wait; ordinary concurrent-version conflicts remain retries.
The prior inventory, attempt and budget versions stay available for inspection.

`inspect_hit` accepts a saved search-result ID. If a caller supplies one of its
own retained source-copy IDs, the rejection gives the exact permitted source
reader arguments instead of requiring another search. The example explicitly
replaces the next input's selected window; the agent may choose another range or
append mode. Saved communication is inspected through the context page and record
readers. Unknown or out-of-scope IDs receive the same generic feedback without
revealing other tasks' records.
