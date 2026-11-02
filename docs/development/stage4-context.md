# Stage 4 working context and within-run notes

For bounded note/board tool results, optional skill activation, model-authored
conversation compaction and the controlled evidence-recovery evaluation, also
read [context efficiency](stage4-context-efficiency.md).
It owns those additions and their separate offline/live proof boundaries.

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
The incoming card marks an unanswered or partly answered request with
`response_needed: true` beside its text when the input allowance permits. A fully included question carries the
exact `publish_result(answer_request_id=...)` reply route when callable, or a
`reply_setup` action to request that permitted tool first. A prefix-only question
keeps its full-question reader instead of a premature reply route. Answered,
awaiting-review and cancelled requests do not keep an incoming response cue.
If the complete current question is already in a verified tool-reading result,
the compact card can carry the exact reply action without duplicating that text.
This presentation does not raise the peer text's authority or prescribe its
scientific answer; source scope, tool grants and accepted request state still
govern execution. Packer v94 records this input change without rewriting saved
requests or changing the mailbox/tool schemas. Added response cues are fitted
after text packing, so they cannot shorten a previously fitting question or
source; current status and standing reply instructions still identify the work
when there is no room for the added cue.
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
Accepted follow-ups can save notes linked to an inherited checked public copy,
passage or reading window. The note path verifies accepted-parent ancestry and
current task membership. Ownerless reading windows also require their exact
saved copy, quality, source-use and passage links; changing the current passage
selection does not erase a previously checked read. Foreign notes retain their
ordinary scope restriction, and inherited source text remains subject to current
source-use checks on note reads, search, replay and prepared inputs.
New notes also retain the source dependencies of their producing model input,
including source-derived notes and history when no source packet was selected.
The writer checks the exact saved request and context inclusion before copying
those dependencies; missing lineage cannot become a source-free note.
Prepared inputs carry those exact copy dependencies into the saved intent. The
model session checks them again inside the dispatch-marker transaction, so a
later source-use change blocks only the affected call before provider dispatch.

Reports inherit the direct and context source dependencies of their exact
producing request through `ora.mission.report_lineage`. The mission service
rechecks current use inside both the report-save transaction and the later
board-acceptance transaction. A source-use change after dispatch therefore
cannot silently accept source-dependent report text; the captured response and
usage remain saved. Later report reads and current Atlas projections withhold
the affected title, body, limitations and summaries. Reading current state
refreshes that local projection without dispatching research; historical saved
snapshots remain immutable, and parent comparisons are masked when returned.
New provisional-answer feedback retains its exact candidate reference without
copying report prose outside the context dependency record. Previously prepared
legacy inputs and exported snapshots remain separate historical proof limits.
These checks establish source-use lineage, not whether a cited passage supports
the report's scientific interpretation.

The task-specific shared-material catalog connects separate peer work items in
the same research mission. `ora.mission.shared_materials` supplies compact
titles, saved author names, status and exact references for ordinary scholarly
hits and accepted sibling reports. It does not generate a short finding or copy
a teammate's full report into the next input. Native browser leads, private
notes and unaccepted report drafts are excluded. A completed report is an
accepted workflow result, not a certification of scientific correctness.

Peers can discover omitted entries through `read_context_page` and open a
listed report through `read_context_record`. Reading a scholarly hit uses
`inspect_hit` and the existing source-copy admission and window readers. These
are internal reads of saved material; web search is needed only for additional
retrieval. Each new input lists its actually attached function names so the
peer can distinguish tools it can call now from tools it must request first.
The catalog preserves source-use checks and leaves scientific choices and
whether to request critique with the agents. Access, actual delivery, reading,
peer review and useful contribution remain separate evaluation outcomes.

New runs pin the optional `peer_turn` contract. It lets a peer submit a report,
review an exact answer, and ask an independent follow-up in one model response.
`ora.mission.peer_turn` records each operation separately: an invalid review does
not discard a valid reply, while a final revision depends on that turn's saved
review. Reply reports keep the producer's task open. The recipient can read the
linked reply under the existing ownership and current source-use checks; it is
not a final shared result. Version checks also apply inside the review and reply
transactions, including replay after restart.

The bundle remains optional and may be deferred when its schema competes with
source or peer text. Existing single-action tools and older pinned runs keep
their contracts. Inspect the exact prepared tools and included question, answer
or reader cue before claiming that bundling saves a model call. Offline combined
actions establish routing and acceptance behavior, not natural model uptake or
a completed reciprocal scientific exchange.

When the complete selected-source request cannot fit, packer v85 also tries the
latest requested checked window together with all checked selections from other
previously read copies. This is one transient group, not a relevance ranking or
a change to the saved source selection. Saved read history, current permissions
and the complete serialized request determine whether it is usable. Source-window
candidates must preserve pending peer material or an exact usable reading route;
otherwise preparation keeps the established recovery path. Coverage remains
explicitly partial when other passages are omitted.

Navigation choices may be paged when their exact requested ranges and checked
passage references already appear in those source packets. The versioned
projection retains uncovered choices and a reader for the original saved list,
and is recomputed during input validation. A matching source ID alone is
insufficient. Older prepared requests retain their original bytes.

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

Packer v37 also projects an exactly verified rejection of an unexposed local
function as a short error notice naming the rejected function. The complete
provider receipt and host feedback remain saved; the next input already carries
its exposed tool definitions and capability-selection guidance. Unknown,
scope-mismatched, native-browser and route-specific errors retain their original
feedback. A preparation retry preserves existing provider or scientific feedback
instead of replacing it with a mechanical version-change notice.

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

For an observed provider rate limit, ordinary request packing now leaves 10% of
that limit as explicit input-estimation headroom, in addition to the predicted
next completion. This is a conservative packing policy, not a calibrated
provider tokenizer or a fixed account limit. It first compacts recoverable
metadata and defers unselected schemas while preserving selected source text.
The existing paced provider-feedback path remains available when only the local
forecast exceeds the limit; its receipt records zero applied headroom. An actual
413 still requires a smaller request. Qualification-imposed input limits keep
their explicit experimental meaning. Raw tokenizer counts, this packing reserve,
and successful-response usage are recorded separately.

Fresh live runs can reuse verified, project-scoped provider-limit observations
before their first request. Before a permissive forecast probe, an additional
input-only packing pass preserves selected source text while reducing context
metadata. Read [provider input sizing](provider-input-sizing.md) for the scoped
cache, expiry, offline isolation, final count fields and proof limits.

Packer v7 preserves the current assistant tool call, its exact call ID and
arguments, and the matched tool result. It does not resend provider-returned
reasoning in a later request. That reasoning remains in its restricted saved
record; the context inclusion receipt labels this saved-only policy. Omitting
reasoning from the next input reduces repeated private text without changing
the selected model's saved reasoning setting or capture of new returned reasoning.

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

A public document inherited by an accepted follow-up keeps its original source
ownership. `ora.state.source_lineage.inherited_public_source` checks the child's
original and current source references, accepted parent snapshot and target,
exact copy and acquisition records, and current task membership. This narrowly
permits inherited source inputs across later follow-ups; current source-use
permission, checked passages and pinned input hashes still apply. Other foreign
records and outputs retain the ordinary mission boundary.
Conversation compaction and history paging apply this same inheritance check to
their input and archived dependencies. A parent-owned source is usable only while
that exact inheritance remains valid and its current use is permitted.

`read_omitted_page` validates the saved call, actor and task before returning a
bounded page of exact omitted refs with safe summaries. `read_omitted_record`
returns bounded character windows of non-source peer/board/note projections and
reports linked from an omitted outcome or answer. Source copies, passages,
raw retrieval-hit bodies, arbitrary peer payloads and private reasoning are
excluded from this generic read path. An exact shared-material omission can
additionally expose a permitted sibling report or safe scholarly-hit metadata;
it does not expose a hit's retained source text. Source text still uses an explicit checked
`SourceSelection` with current-use validation. The mission host must supply its
authenticated actor/task to these methods; model arguments never set scope.
An incomplete record window returns an executable `next_request` with the same
inclusion and exact record version. Historical working-note results that lack
this additive field can receive it in a derived replay view only when every
saved field still matches the current authorized projection. The original result
and call arguments remain unchanged; changed or revoked note content cannot be
silently treated as the previous window.
Older completed tool turns may be passed as `archived_tool_turn_refs`; they stay
as exact receipt omissions and page as tool/result IDs, function names and effect
refs. Their raw provider reasoning does not re-enter later model input. Packer
v38 can retain exact earlier `read_context_record` windows for reports and
working notes, with their original assistant/tool pairs, before optional board
metadata. Each retained window rechecks its scope, saved projection and current
source use. Duplicate windows and tool-call ID conflicts are omitted; windows
that do not fit remain explicitly fetchable. Other archived result content stays
outside the input. The latest unfinished tool pair remains in `tool_turn_refs`
until that turn is resolved.

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

An original pasted submission with a current use grant can default to its saved
whole-submission passage. The host verifies its retained bytes, exact offsets
and locator through the same checked-passage boundary, so the first source
packet includes a usable citation reference. Explicit passage selections still
take precedence, and historical submissions without a separate use record keep
their raw-text route. The complete request must still fit normal admission;
citation coordinates do not establish claim support or successful model use.

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
estimates are not an exact reproduction of provider rendering, and a full selected abstract may still
need split windows under an observed limit.

Preparation v12 also previews the hard bound when a fitting soft-target request
omits an already-read working-note window. It uses the expanded request only if
additional exact note windows fit without losing previously retained report or
note windows. Complete-request, completion-reserve and current-use checks still
apply; no unread text or generated summary is added by this retention step.

For supported medium- or high-reasoning, nonstreaming GPT-OSS local-function requests,
`ora.models.token_estimate` counts the complete canonical request with pinned
`tiktoken==0.11.0`. New requests use the versioned canonical-JSON estimate without
an added token cushion. The current v4 basis accepts medium and high effort;
saved v1–v3 bases retain their original high-effort request checks and counts.
Saved `plus128.v1` requests retain their original
128-token allowance. The bundled vocabulary is hash-checked locally; runtime
counting never downloads an asset. Native
browser requests, unfamiliar formats or unavailable assets use the labeled
legacy byte estimate for new requests. Context-fragment counts guide packing;
only the complete request estimate can admit a call, subject to both the model
context ceiling and the observed account ceiling with its completion reserve.

New intents, call budgets and capacity records retain the estimate basis and
exact request identity. Dispatch rechecks a saved tokenizer estimate with that
same basis and fails closed if it cannot be reproduced. Historical intents are
not repriced, and legacy queued-placeholder repair retains its byte basis.
The canonical-JSON count is an empirical estimate of provider-rendered input,
not a guaranteed upper bound or evidence of improved live research quality.
Changing this estimate does not change the observed provider quota or the
completion reservation. Preparation v13 records the new estimate for new
requests while dispatch continues to verify historical requests under their
saved basis.

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
For a permitted public web document, the inventory also retains its exact saved
URL after checking the linked fetch, copy and current use record. This helps a
peer identify the page behind a reading reference without treating the URL as
source text. An unusually long URL can be explicitly omitted to keep that
bounded inventory page readable; the copy and reading reference remain.
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

Packer v55 can include the exact peer question and the complete title, body,
citations and limitations of its report in the requester's next working context
when an exact `mail_answer` is awaiting review. This applies only to the
requester's task and an accepted report whose producing lineage and current
source use permit model text. The
whole projected report must fit within 2,000 characters, the complete question
within 600, and both within the measured input allowance. The packer prioritizes
this review card ahead of optional historical detail and avoids repeating its
request and answer detail. Under input pressure it can omit only duplicate
navigation for that exact pair; other peer requests and source-reading progress
remain. It never substitutes a prefix or summary. It labels the text
as model-authored peer data, preserves the exact request, answer, report and
read coordinates, and leaves larger or budget-omitted reports available through
their existing exact reader. Included source dependencies are saved with the
prepared request and checked again before dispatch. Visibility does not prove
that the requester read, accepted or used the peer's assessment. Earlier pinned
requests keep their original bytes and packer version; a later call on the same
saved run may use the new layout.

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

Newly prepared peer instructions make successful publication's effect explicit:
it finishes that peer task and can close the run once required work is done.
Before publishing, peers assess the saved question's roles and requested outcome.
Peers develop complementary perspectives from their roles, exploring relevant
assumptions, alternatives or counterevidence within scope. When the outcome needs
or could benefit from another perspective, comparison or review, they can read a
shared contribution or seek a focused response. Requests carry a focused question,
brief context and exact saved references when available; replies separate supported
findings from interpretation and uncertainty. Peers review the reply and describe
its actual effect on their assessment while independent work can continue. A catalog
title alone does not establish the report's findings. Independent completion
remains valid when peer input would not add useful value. This guidance changes no saved
request, tool schema or completion policy; offline packet and publication checks
do not establish that a live model will choose useful collaboration.

Ordinary research turns attach the existing permitted `board_read` function
alongside peer requests and context readers, so an agent can inspect shared work
without spending a separate turn to select that small local reader. Permission
and descriptor checks still apply; optional tools remain selectable on demand.
Saved requests retain their original tool lists. Direct board access is separate
from whether a model chooses a useful peer exchange.

### Choosing skills and tools

The model chooses an operation from the task's needs. A supported direct answer
is valid when no additional information or action is needed. Each newly prepared request
includes a tool-availability notice derived from that exact call's manifest and
the current matching permission descriptors:

- `call_now_local_functions`: attached functions that can be called directly.
- `request_first_local_functions`: permitted optional functions to select with
  `request_capability`; the model waits for the schema on a later turn.
- `not_ready_local_functions`: a concrete missing-data explanation. For example,
  `inspect_hit` needs an eligible saved search result. A capability request cannot
  create that result.

The chooser labels distinguish listing saved records (`read_context_page`),
reading saved report/message/note text (`read_context_record`), reading source
text with citation spans (`read_source_window`), and saving a report that ends
the peer's task (`publish_result`). Labels describe results; stable tool IDs
preserve saved-call identity.

Every mission tool has both a short purpose in the catalog and a description
attached to its local function schema. Descriptions state the useful operation,
input references and returned information or saved effect. Common collaboration
tools receive mission-specific descriptions too, without changing the generic
capability library. Native browser search retains its provider descriptor;
its purpose and selection instructions appear in the mission catalog instead.
Catalog coverage and serialization checks verify that descriptions reach model
requests; they do not establish that the model will choose every tool correctly.
Saved requests retain their original descriptions. During mission continuation,
prose-only description changes do not revoke a permitted tool: the host still
requires matching names, versions, schemas, permissions and implementation
references, and validates each saved schema hash before comparing it. A changed
argument rule or execution contract remains a mismatch.

The original-source reader is attached automatically when source evidence is
selected or source recovery needs it. Source-free tasks keep it requestable;
explicit selection also attaches it. This avoids repeating its full schema in
memo-only turns while retaining the same permission and source-use checks.

Packer v75 keeps the exact `read_context_record` action for the accepted parent **report** in a tightly packed source-free follow-up, shortening a duplicate preview first. A report ID is not a source-copy ID: `read_source_window` needs an inspected source copy and cannot supply invented citation spans for a prior answer. If the peer passes the exact accepted parent report ID to that source reader, the local rejection supplies `read_parent_report.arguments` for the current inclusion and explains the correct reader. Other IDs get no parent-report hint. The report reader still checks the same task, exact parent lineage and current source-use permission before returning text. Very tight selected-source turns can page parent detail through `older_records`; the saved parent, child scope and source restrictions are not rewritten. Saved request blobs are not rewritten; this is a new preparation projection and a narrower correction after a rejected local action, not evidence that a live model will choose it.

Packer v76 removes a duplicate parent preview only after the exact complete,
currently permitted source-free report is present in the current tool result.
It labels that material as prior agent output and explains that it cannot supply
original-source citation spans. A final page alone is insufficient: offset zero,
matching returned/total/text lengths and complete source-free lineage are required.
Partial and source-dependent reads retain their existing navigation and checks.

Native browser turns expose only the provider's browser descriptor. Their notice
states that local functions return on the next ordinary turn. Historical error
feedback is interpreted against the current menu: it must not ask the model to
request a tool that is now attached. Old saved requests are not rewritten.

Skills supply reusable workflow instructions and may expose associated granted
tools. The role catalog distinguishes active skills from optional skills and lists
their tools. An agent can activate a relevant optional skill with its exact ID,
then use the next turn's instructions and tool schemas. One missing operation can
instead be requested directly. Activation does not grant additional permissions
or bypass source/data requirements. Optional selections persist until released;
releasing a skill does not remove core tools or independently requested tools.

Shared report entries and their compact overviews provide an exact `read_request`,
including the context index ID and the separate record reference. The model can copy those arguments
instead of assembling them from separate catalog fields. This pointer contains
no report body and grants no access; the reader still checks the requesting task
and current source permissions. A compact overview therefore does not require a
separate page lookup before reading the identified report. Older saved request
packets remain unchanged.

Pending peer review keeps ownership, the exact question, answer version and
report-reading route separate from delivered report text. Compact cues are
retried after required-index compaction; duplicate navigation for that same
request/answer can be paged out before the cue is dropped. Large report text
remains behind the permitted reader when it does not fit. Adding a tool schema
must be measured against the complete serialized request, not the context
fragment alone. A cue or reader reference never receives report-text delivery
credit, and a previously read page is not assumed present in a later call.
Peer-decline reasons use the same exact producer and current source-use checks
before entering context. Offline cue, paging and decline tests verify these
boundaries; they do not establish natural model review or efficiency gains.

Publication distinguishes record attribution from source citation. A peer report
can be named in the answer, but its ID is not a source-copy ID and reading it does
not inspect its underlying sources. The citation tool fields accept exact checked
source/span pairs. A supported summary based only on task input or permitted peer
records already read can use an empty citation list while stating its basis and
verification limits. Invalid references remain rejected before publication.

Single-agent evaluations use ordinary outcome-based prompts without tool names or
forced selections. Record the actual menu, chosen operation, arguments, delivered
result and useful outcome separately. A correct direct answer can pass the task
while leaving an optional tool or skill unexercised. Refine descriptions, grouping
and examples from observed confusion; offline menu checks alone do not establish
that a live model will choose appropriately.
