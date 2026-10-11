# Stage 4 working context and within-run notes

`ora.mission.context.WorkingContext` prepares one selected peer's next model input
from current saved records. It reads the current board proposal, the call's pinned
configuration, exact source-copy references, incoming messages, requester-owned
outgoing requests and their current answer/review dispositions,
new shared critiques/outcomes/uptake statements, and any explicitly selected
evidence, contribution, note or summary references.
An incoming request remains visible to its named recipient even when its task
reference names the sender's different proposal. The recipient sees the exact
originating message and current request state; requester-side task scoping stays
with the sender's proposal.
The run question, scope and settings, source copies, working notes, reports,
exact `model_request_intent` bytes and `context_inclusion` omissions share the
same `StateStore` and immutable object store. A saved input is not automatically
resent to the next model call: each intent has its own selected context, and
its exact inclusion binds the prepared bytes. A dispatch marker alone does not
prove provider execution; captured responses are tracked separately. There is
no second input database or automatic full-history replay.
The saved task and current source-use record take precedence over an older summary.
An old summary is labeled stale when one of its declared input versions changed.
It remains an optional aid, never an authority for a task decision.
Working-note text is also rechecked against exact `input_refs` before it enters
context or a note-reading result. Direct source copies, passages, windows and
same-task note ancestry resolve to current source use; incomplete ancestry
produces a text-free diagnostic. The saved historical note remains intact.
Prepared inputs carry those exact copy dependencies into the saved intent. The
model session checks them again inside the dispatch-marker transaction, so a
later source-use change blocks only the affected call before provider dispatch.

Call `assemble(call_ref, task_ref, ...)` before `ModelSession.prepare`. Pass the
returned `pack.messages` to the model bridge and the same exact source-copy
selection through its `SourceSelection` inputs. The context index includes exact
references for retrieval, current source-use metadata, inbox cursor and event
high-water mark. New messages saved during an active call remain in the store and
appear in a later assembly; an already dispatched request is immutable.

The caller may provide an input allowance and a token estimator for the selected
endpoint. The caller must first reserve room for pinned instructions, tools,
selected source text and output/reasoning. When detail exceeds the remaining
allowance, assembly records an explicit omission and exact fetch reference. It
never silently truncates a record. If the required task and reference index does
not fit, `ContextOverflow` identifies the affected turn. There is no mission
duration or research iteration limit in this module. A temporary prompt-cache hit
or miss has no effect on durable recovery.
Optional detail packing uses saved recency and open-request state, so newer
requests, answers and critiques are considered before older completed board
outcomes. This orders context bytes; it does not rank scientific claims. Packing
uses a compact model-facing task projection, short recent-reference previews and
record counts. It keeps pinned configuration, full task history, all exact refs
and source-use evidence in durable records. `PACKER_VERSION` identifies this
model-facing layout for restart recovery. If detail is omitted, the prompt gives
the saved inclusion ID and a `read_context_page` cursor; the inclusion receipt
retains the omitted exact refs in priority order. A very short detail can replace
an omission marker when that uses fewer bytes.

Packer v6 keeps this peer's routine, non-completing executor outcome wrappers
as a count, recent references and pageable omissions. Classification checks the
saved executor artifact, receipt and trace together; peer outcomes, dissent,
completing outcomes and report-bearing results retain their ordinary treatment.
This removes repeated execution bookkeeping from model input while keeping the
original records available. A controlled 18-outcome fixture reduced serialized
context from 13,342 to 2,886 bytes; this is a context-size measurement, not a
provider-token or latency guarantee.

When required context is tight, identical selected and current source references
and access statuses are represented once, with an explicit equality marker and
the exact current source-use reference. Differing versions or statuses remain
separate. Two optional recent-outcome preview references may also be removed
from the overview when needed; their exact records remain in the pageable
omission list. The current tool request/result pair, including a rejected report
draft, is preserved. These changes reduce repeated metadata rather than limiting
the number of research steps or discarding durable history. Token estimates are
approximate; provider-reported usage remains the basis for live comparisons.

Packer v7 preserves the current assistant tool call, its exact call ID and
arguments, and the matched tool result. It does not resend provider-returned
reasoning in a later request. That reasoning remains in its restricted saved
record; the context inclusion receipt labels this saved-only policy. Omitting
reasoning from the next input reduces repeated private text without changing
the selected model's high-reasoning setting or capture of new returned reasoning.

Packer v9 adds a factual continuation recap. When the mission host supplies
its exact current task control, the working view names selected passage
locators and the latest saved source-window cursor. These are inspected spans,
not proof that every part of an original source was read. A bounded recent
search recap names the routes, query preview/digest, outcomes and hit counts of
completed scholarly searches. An older completed turn remains in the omission
index; `read_context_record` can return its safe search projection without raw
tool-result text or private reasoning. This lets a peer recognize a repeated
lookup while preserving its choice to search again for a scientific reason.
The host may omit the derived recap under a tight input allowance; exact source
packets and archived tool/result records are still retained. Saved live runs
created before v9 do not retroactively gain the recap in their old requests.

Packer v10 also includes the current run's saved scope in the next working
view. A scope too long for the preview has an exact, inclusion-scoped
`read_context_record` continuation. A child follow-up carries its exact parent
snapshot and accepted target report as labeled prior output data, with bounded
previews and an authorized reader for the full target. The child has its own
question, scope, mission and selected configuration lineage; the parent report
is context for reconsideration, not an instruction or automatically accepted
child finding. The immutable completed parent snapshot is not revised when a
child starts. Older saved model requests retain their original input bytes.

`read_omitted_page` validates the saved call, actor and task before returning a
bounded page of exact omitted refs with safe summaries. `read_omitted_record`
returns bounded character windows of non-source peer/board/note projections and
reports linked from an omitted outcome or answer. Source copies, passages,
retrieval hits, arbitrary peer payloads and private reasoning are excluded from
this generic read path. Source text still uses an explicit checked
`SourceSelection` with current-use validation. The mission host must supply its
authenticated actor/task to these methods; model arguments never set scope.
Older completed tool turns may be passed as `archived_tool_turn_refs`; they stay
as exact receipt omissions and page as tool/result IDs, function names and effect
refs. Their raw provider reasoning and tool-result content do not re-enter later
model input. The latest unfinished tool pair remains in `tool_turn_refs` until
that turn is resolved.

Page replies are bounded by serialized size as well as item count. A reply that
cannot fit every requested item returns the exact next cursor, leaving all
unreturned refs in the saved inclusion receipt. If a previously saved page result
is too large for the next model input, assembly retains the original result blob
and tool-call ID while putting a bounded representation in the current tool
message: saved result ref/hash, the summaries actually included, omitted count
and continuation cursor. The next inclusion receipt records those included and
omitted item refs separately. This is local input compaction, not a repeat of the
tool effect or a claim that all saved page items entered the model context.

After the model bridge saves a request intent, call
`save_inclusion(operation_id, pack, intent_ref)`. It compares the retained request
bytes, exact call/configuration and source selections with the pack, then saves a
`context_inclusion` record linked to that intent. This is proof of prepared input,
not provider delivery, scientific use or accepted output. Actual dispatch and
provider response require their own receipts. A separate intake `source_use`
record is resolved for the selected and current copy versions. Missing policy
is labeled unknown; a known restriction is shown and rejected by
`save_inclusion`. The host may call `require_current_source_use(source_refs)`
again for early local feedback before external dispatch. `ModelSession` also
checks the selected copy's separate exact-version `source_use` head at source
selection, intent commit, and inside the atomic dispatch marker, where it saves
the policy references. A policy change after preparation can therefore stop the
affected dispatch.
The selected copy version's current policy controls its use; a newer copy's
separate policy is visible but does not by itself revoke a frozen older version.
`ModelSession` also checks copy-body restrictions, retained bytes and the explicit
selection. For checked passage selection, `SourceSelection.passage_refs` identifies
the exact passages; the prepared source packet carries the original copy SHA and
the selected text SHA separately.

`save_tool_result` records one completed tool result against the provider's saved
tool-call ID. Passing its response reference as `tool_turn_refs` on the next
assembly rehydrates the ordered assistant/tool pair without its returned
reasoning. An unresolved result stops that dependent continuation so the host can
reconcile the affected tool effect. Completed older turns can be summarized; their
raw reasoning is not reintroduced by this API. The host still owns idempotent tool
execution and any uncertain external side-effect reconciliation.

`ora.mission.memory.WorkingNotes` provides `write`, `read` and `search` for one
named peer and task. Notes may be hypotheses, contradictions, next actions or
checkpoints. Every revision retains an exact task and input-reference lineage.
They are labeled peer drafts and never replace primary evidence or accepted
findings. A later run's cross-run cache is outside this within-run API.
Search matches all case-insensitive query terms in the saved note ID, category
or text, prefers an exact text phrase, and then orders current revisions by
update time. It returns every scoped match; this ordering is only a retrieval
aid. The compact working view presents newer note references first, while the
full note and its earlier revisions remain available by exact ID/version.

## Reading and unfinished-output continuity

Separate model calls do not share automatic memory. Each call receives its
selected source windows, current task records and bounded saved context. Older
source bytes remain retrievable by exact reference. Reading progress reports
covered and missing ranges of retained extracted text, including an exact first
missing cursor; reaching the last window alone does not prove earlier ranges
were covered. A partial original stays labeled partial even when all retained
text has been exposed. Saved-window coverage does not establish which bytes
entered a past model request or what the model understood; exact request intent
and inclusion receipts answer the delivery question.

For a current source input, the working view separately reports coverage by its
selected checked passages, without counting older saved windows. After an
explicit `replace`, saved windows can cover all retained abstract text while the
next input selects only the final excerpt. The selected-input count and first
omitted cursor make that difference visible. When selected input is partial,
the view suggests consulting omitted exact spans or source-linked saved notes
before a whole-copy absence claim, or limiting the claim to the selected
excerpt. This is guidance for the peer, not a publication gate or a claim that
the model understood every supplied passage. Raw-copy inputs without checked
passage mapping retain unknown selected-passage coverage. A compact scope/count
and first omitted cursor remain in the required source index when input pressure
removes the more detailed reading-progress view; this applies to initial
`inspect_hit` passages as well as later `read_source_window` selections.

A saved `read_source_window` result is not proof that its text reached the next
model call. If the exact checked passage is already selected as that call's
source input, packer v17 replaces the latest duplicate tool preview even on
an ordinary-fit turn with a small
result envelope containing the saved result, window/passage references, content
status and `next_request` cursor. The exact saved result and inclusion receipt
carry hashes; the complete saved result remains fetchable.
The source bridge and inclusion receipt must still prove
the selected passage entered the prepared input; dispatch needs a separate
receipt. Without that exact selected passage, the packer keeps the preview or
reports overflow rather than claiming the text was supplied elsewhere. For
example, in a synthetic 2661-character retained abstract, windows covering
characters 0–1600 and 1600–2400 leave 261 characters unread. The cursor is a useful prompt
for the peer's next choice, not a fixed scientific gate or an absence finding.
When a source exceeds the ordinary host reserve, the host previews the complete
checked request, including passage labels and tool schemas, before admitting it.
Estimator v8 also checks the complete request when a follow-up's required task
context exceeds that reserve. Packer v19 can archive an exact completed,
successful scholarly search reply into a compact navigation view with its saved
result reference and returned hit IDs. The full tool result remains saved; an
unresolved or failed search is never treated as completed. Under tighter input
pressure, repeated parent question/scope preview text can be omitted with an
exact `read_context_record` continuation, while the parent target reference,
source-dependency status and selected checked source stay visible. The host
first packs to 70% of an observed provider token-rate limit. That is a soft
operating target, not the model context limit. If a complete checked request
misses it, the host may preview against the observed rate ceiling minus the
same prior-completion estimate reserved by capacity, while retaining the
model's separate context/output limit. The exact request and reservation must
fit that ceiling before enqueue; provider headroom can still require a quota
wait. An above-soft retry after HTTP 413 must be smaller than the saved
rejected request and cannot repeat its exact hash. With no observed limit, the
host does not invent a rate ceiling. If neither selected-source nor source-free
preparation fits, that branch keeps a local waiting diagnostic; no provider
request is inferred. A text-free inventory tool result is carried into the next
source-free turn only while its exact current-use status still matches. These
estimates are not the provider tokenizer, and a full selected abstract may still
need split windows under an observed limit.

Replacing an undispatched request preserves the last captured response's
completion-token allowance. The replacement has no response of its own yet, so
the scheduler falls back to the same task's saved response reference and records
that reference with its estimate. A missing response is not treated as measured
zero usage, and the model's configured output limit remains separate.

Packer v20 also carries the latest bounded `read_context_record` result with its
matching call into a source-free turn when the complete request fits. It rechecks
the saved projection and current source use, and leaves repeated inventory and
scope detail fetchable by exact reference. A read that cannot fit remains a local
wait; the prepared-input receipt, not the saved read alone, proves delivery.

When a newly selected exact window is already present in the checked source
packet, the latest successful window reply can use a compact text-free envelope
with its saved result, passage identity, original-source qualification and next
cursor. This removes duplicate source text, not the chosen window or its
qualification. An older queued source-free request can be superseded only before
capacity acquisition or dispatch, when its saved estimator or packer differs;
the original intent stays immutable and the next prepared call gets a new
identity. If the full selected request still does not fit the observed target,
the host keeps source-free navigation and lets the peer choose a smaller window.
When an older context overflow is retried under a compatible new packer, the
full mechanical repair notice and exact prior budget reference remain in task
control. That host-only version notice need not be repeated in the model input;
scientific feedback and unresolved work still enter through their normal
current records. A prepared input and inclusion receipt are required before
calling the recovered turn delivered.

When source text cannot enter the current call, the compact reading view retains
exact saved window positions and the first uncovered range. It explicitly marks
the current call as source-free and prior model delivery as unverified. Saved
coverage measures retained windows, not what a model has seen or understood.
The initial view includes two copies; `read_source_inventory` pages the same
navigation for later permitted copies without exposing their source text. The
peer can reopen an earlier requested window or fetch an uncovered range.
Checked-window compaction also retains the original source's partial status and
any verified PDF page/table caution; passage text alone does not preserve those
qualifications. Exact prepared inputs remain the evidence for text delivery.

Source-free recovery also identifies the latest saved read by its exact result,
window and source references. It states explicitly that those bytes are absent
from the current input, and gives the saved offsets so the peer can choose a
smaller replacement window or source-linked notes. This feedback preserves the
original stored result; it does not claim that the model saw the earlier text.
A current source-use restriction is reported separately from a size problem.

Source-linked report text may remain readable even when its source passage is
not selected for that prompt. Any report preview, record window or page title
that survives packing binds its exact source dependencies to the prepared
request. Current use is checked again before dispatch; saved windows and pages
are rechecked when replayed. A title or preview removed by packing adds no text
dependency. A queued report-only dependency that loses permission currently
prevents dispatch and leaves a local repair wait; automatic continuation from
that specific wait still needs qualification.

Packer v24 pages repeated parent fetch coordinates and selected-window read
identifiers only when the same exact records already have a saved omission
entry. The full child scope, available parent preview, peer request states and
source qualification remain. If the ordinary complete-request preview still
does not fit, the host can archive the latest completed source-read turn when
its exact checked passage is already in the selected source packet. That
fallback retains the source-selection tools and rechecks the full request,
selected text, source use and call identity before saving the intent. It does
not archive a current report read or choose a smaller passage for the model.
An oversized source-plus-report selection can still require a model-chosen
window change; a successful saved read is not proof of delivery to a later call.

A length-cutoff response remains a partial result. No interrupted tool arguments
execute. A subsequent call can receive at most 600 characters of its saved
visible draft and page the remainder through `read_context_record`. The reader
checks exact call/actor/task lineage and source-use dependencies again. Private
reasoning and tool fragments are not drafts. When source ancestry is unavailable
or the needed sources are no longer selected and permitted, the context gives a
text-free diagnostic so the peer can choose a safe reissue. This conservative
draft recovery does not yet provide independent provenance for every derived
peer statement or for note derivations outside recorded exact `input_refs`.

Do not split an input that already fits merely to create more turns, or resend
the growing full text on every section call. For genuinely large material,
compact source-linked notes and selectively reread passages preserve continuity.
The model can keep a whole-text assessment provisional while extracting supported
facts or pursuing independent work. Later long-document qualification must test
summary loss, cross-section arguments and late contradictory evidence.

Focused offline checks live in `tests/mission/`. They cover stale summaries,
late messages, explicit omissions, pinned settings, exact intent binding,
provider-returned tool-turn reasoning, and notes after reopening the state store.
They do not establish a live model call, scientific usefulness, source retrieval,
mission wake/recovery, or Atlas presentation. Those checks belong to the
integrated Stage 4 mission run and its provider/UI qualification.
