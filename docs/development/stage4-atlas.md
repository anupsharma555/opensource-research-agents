# Stage 4 Atlas review map (revision 12, 11 October 2026)

This map describes the implemented reader views and their record boundaries. It is
not evidence that the full mission host or a five-level live study has passed.
The saved Stage 3 examples remain offline callback tests after a live workspace
is connected. Their controlled timestamps describe recorded test events, not
provider latency.

| View | Reader's question | Saved authority and link |
| --- | --- | --- |
| Research review | What was asked, what work is visible, what output was accepted? | Mission, tasks, attempts, sources and accepted output references in the selected snapshot. A saved output is not itself scientific validation. |
| Activity | What actually happened or is waiting now? | Saved attempt, response, tool and event records. A current model-capacity wait is shown only when the task owns the exact waiting operation; its next eligible time is not a dispatch promise. |
| Evidence | Which exact source copy supports a saved claim? | Sources, passages, versions and citation targets in the selected run. Changing runs selects an available copy from the new run; an explicitly opened missing citation remains visibly missing. Local review notes stay with the open run view. |
| Communication | Who asked which peer, what arrived, and what is still open? | Mail request, message and answer references; creation, delivery, acknowledgment, answer and requester review remain distinct. Shared contribution updates retain their own saved evidence and status. |
| Communication → Agent reasoning | What reasoning text did the model actually return for this call? | Explicitly open an attempt and its saved provider reasoning. The authenticated local reader verifies the originating run, call, attempt and restricted reasoning record. Text stays separate from the answer and delivered peer messages; absent or truncated text remains labeled. |
| Details → shared work board | Who proposed work, who could inspect it, who took responsibility, and for which version? | Exact board proposal versions and claims. Board visibility does not prove directed delivery; a historical claim does not prove current ownership. Each available exact version has a direct focus target. |
| Details → task and attempt | Which assignment and exact attempt did an event refer to? | Task ID, status, parent, related agent request, attempt IDs, saved inputs, calls and times. A missing task or attempt stays missing; the viewer does not substitute another record. |
| Saved runs | Which run is mine, when did it progress, and which parent does it follow? | Local host run summaries with source-derived titles, saved created/progress/completion times, status and parent ID. Unknown legacy dates stay unknown. Viewer polling is shown separately from research progress. |
| Start or follow up | Which starting peers and evidence route will be requested? | Enabled roster and workflow version from the selected host. The receipt and later Activity/Details show actual work. A follow-up retains the parent's exact requested peer identities and target version until changed explicitly. |
| Recorded example → live workspace | Can I return to saved real work without submitting again? | Native shell retains only the selected canonical state directory. It asks the local CLI to validate the current private connection and authenticated host before navigation; the browser uses its existing same-origin session. This transition sends no research command. |
| Draft request | Can I return without losing an unfinished question or exact target? | Browser drafts remain in same-workspace session storage. Native drafts use a private application settings file keyed by workspace identity; the Rust shell retains content locally and never sends it to the host until explicit submission. An unknown legacy workspace identity has no cross-restart draft key. |
| File → Reconnect saved workspace | Can I return after the host changes its local address? | The native menu validates the remembered state directory's current connection and opens Saved runs, retaining a safely parsed selected run ID. It submits no research task and gives the served web page no additional native permissions. |

## Reading the visual outline

Run-focused views show the same run number, exact saved ID and full start
date/time with timezone. Snapshot and viewer times remain separate. Display
numbers follow saved history and do not replace durable IDs; workspace-wide
Start and Saved runs views do not imply that a run is selected for submission.
Live navigation keeps the selected view in the URL alongside the exact run ID.
Reloading restores a supported view; an unknown view opens Research review.
Routine polling does not add browser history entries.
Run elapsed time stops at the saved snapshot, includes pauses, and never advances
with the viewer's clock. A completed duration requires a valid saved completion
time; unknown or invalid dates do not acquire an inferred duration.

Each review tab states its purpose and offers a next useful view. Research review
summarizes the request, work and output; Activity supplies the action sequence;
Communication follows directed exchanges; Evidence opens source support; Details
holds exact records; Compare checks the earlier and current answers. These are
optional paths rather than required wizard steps. The selected run and return
path remain visible, with padded navigation buttons.
The sidebar puts Research review, Communication, Evidence and Compare in reading
order, followed by Activity and Details under **Inspect execution**. Page-level
onward buttons use the same destinations; timeline and exact-citation links may
jump directly to their relevant record. A tab does not offer a link to itself.
Run and section titles are more prominent than timestamps and reference IDs.
Sidebar labels retain readable sizing in narrower layouts; pale metadata uses a
darker secondary text color. Agent symbols remain paired with names.

Evidence shows a source selector, numbered excerpt choices and one selected text
pane. Matching ranges explain identical, overlapping or longer selections while
preserving exact copy, version, locator and text. An exact cited passage selects
its saved excerpt; unavailable or ambiguous citations stay unresolved. Sources
supplied at the start, reused from earlier work, and discovered during the run
remain separate provenance claims.

Research review follows Request → Work and exchanges → Output. The exact saved
request appears as a quotation; additional instructions have their own disclosure.
Named agent summaries describe work on that shared request without presenting
internal task numbers as separate research questions. Exact assignment numbers
and IDs remain accessible in technical details. Distinct assignments retain their
own identity; matching names alone do not merge records.

Research review keeps saved work compact: similar actions share one row of bars,
with separate named-agent lanes and a common saved-time scale. Each interval still
represents its own saved step. Group counts include visible failures and system
events; grouping neither reduces executed calls nor proves that both peers read
the same passage. Current-run counts exclude only exactly identified inherited
steps. When the exact parent snapshot is unavailable, the view explicitly labels
run origin as unconfirmed.

Selecting an overview row opens its matching steps in Activity in start-time
order. The group scope, run-wide totals, all-work control and return path remain
visible. Exact requests and results expand within their Activity step. A saved
step that changes action type clears an obsolete group filter with an explanation
so the update remains findable. The complete alternative timeline is available
under **Browse all saved work steps** in Activity and in Details, with time-order
and agent views. Consecutive successful source reads within the same assignment
and lineage may be folded together there; their exact identities, times and
outcomes remain accessible. Cross-agent same-source grouping requires verified
attempt-to-passage provenance, not matching labels or nearby timestamps.

Hovering a bar identifies the actor, action, outcome, start/finish, step elapsed
time and whole-run elapsed time at the saved view. The explanation remains open
when the pointer moves onto it. Keyboard users focus a category, use the arrow
keys to inspect each saved interval, and press Enter to open its exact Activity
record; Escape dismisses the explanation. A single category focus stop avoids
requiring a Tab press for every tiny interval. API involvement is derived from
the exact saved response and configuration: a response, local pre-dispatch stop,
and uncertain outcome remain distinct. A prepared request, successful tool or
missing response alone does not prove an API call occurred or did not occur.

Bars show the saved start-to-finish interval, which can include waiting and does
not measure active tool execution. Points mark starts without a usable end time.
Open Activity and expand a step to read its action, target, result and exact saved
call inline. Internal function names and raw fields are secondary details. A source
reader result identifies its Evidence excerpt only when the exact saved source,
version, passage, locator and text match uniquely. Current waits stay separate
from historical outcomes. Review links lead to Activity, Communication, Evidence
and Compare while preserving the selected run; Details holds exact assignment,
agent, configuration and recovery records.

Activity separates workflow steps, prepared model inputs, captured responses,
accepted reports and peer requests. Icons supplement text; they never replace
status labels. Expand a row for its actual action and outcome, with exact tool records
and technical references available in context. Communication shows the requester → recipient and any saved
answer in the reverse direction. Exact saved request fields, delivery history, shared feedback and provider-returned
reasoning remain available in separate disclosures. Raw mailbox payloads are not
part of the ordinary snapshot API. In a connected workspace, **Read exact saved
message** or **Read exact saved answer** is available on the request card and
requests that specific record and version
from a separate authenticated, uncached reader. The reader checks the originating
run, participants and linked request before returning content; it does not submit
research or mark a message delivered or reviewed. Credential redactions are labeled.
A full saved request quotation leads its card. Loading the exact saved message
replaces that preview in the same place, avoiding duplicate request bodies.
When only a reply summary is included, the viewer labels it as a preview rather
than claiming to show the full message. An unavailable exact record remains
unavailable; the reader never substitutes a newer version or reconstructs wording
from an assignment. Recorded examples retain their saved fields without invoking
the live reader.
Sharing a task ID does not establish that a particular request caused an attempt.
Activity's compact action-category cards filter the chronological record; its
secondary counts expand together rather than competing with the action sequence.
Communication shows an index of every saved request with direction, time, request
preview and independently recorded delivery/reply/review status, then one selected
exchange. If that exact selection disappears, the missing selection stays explicit.
Compare provides independent request, answer and cited-evidence controls for each
side. Missing accepted answers remain visible and cannot open invented content.
Its link count distinguishes exact, available saved passages from unavailable
references. Source names or identifiers written in answer text alone are not
presented as checkable passage links or verified scientific support.

## Dated visual references

Review added information for readability as well as completeness. Keep the
selected run, task, agent, action, outcome and relevant time easy to identify;
move repeated identifiers and configuration into technical details. Use compact
run and task boundaries rather than repeating a long assignment on every row.
Controls provide padded click targets, visible keyboard focus and consistent
agent symbols alongside names. Sticky-header scroll clearance follows the
rendered header height, including changes when its disclosures expand.
Earlier-run activity needs an exact saved relationship: being a follow-up or
reusing a source does not establish that an earlier agent step is included.
Retain direct access to exact calls, returns, source support and saved reasoning,
and check that filtering or a fresh snapshot does not lose the selected step.
Prefer incremental improvements to the existing navigation and visual style.

Keep private UI checkpoints under `.local/atlas-ui-history/<date-time-timezone>/`.
Capture the same named views with their workspace/run, saved snapshot time,
observation time and loaded host/frontend versions. Save a source archive and
file hashes alongside the images: pictures document appearance, but do not
restore code. Before restoring, extract into a separate directory and compare
with the current checkout; do not overwrite unrelated work or research state.
These private screenshots and archives are not published or committed.

Task numbers in current mission projections come from saved proposal-creation
order, so a later task keeps its number across refresh, reconnect and restart.
The root task is 1; missing legacy creation records have no saved number and use
an explicitly explained viewer label. Exact task IDs remain authoritative.
Evidence claim details link to their recorded task when that task is present;
shared source copies are not assigned to a task by inference.

The selected-step badge uses the saved tool or provider outcome, rather than
labeling a rejected tool as completed merely because its model turn finished.
Each requested function identifies its exact linked host result, or explicitly
states that no unambiguous result is saved. Unmatched results remain inspectable.
Parent timelines use only the exact parent snapshot, consistently with Activity
and Details. These inspection changes do not submit research or alter old records.

## Reported label corrections

The live reasoning reader fetches text only on request, with no browser caching.
Ordinary snapshots, call exports and peer context still omit that text. Readable
agent names, numbered attempts/calls and saved times lead the panel; exact IDs
remain in expandable details. Provider-returned reasoning is not proof that an
interpretation is correct or that another peer received it.

| Earlier wording | Current explanation |
| --- | --- |
| Work proposal with only an author | Shared mission work names its author and says direct invitations require separate records. |
| Local callback receipt | A saved result from the local test function; no research deliverable is recorded in this fixture. |
| Run the registered offline callback | Run the local test function and save its result. |
| Claimed proposal v1 | Named peer took responsibility for the titled work, version 1, at its saved time. The exact version can be opened, and the view distinguishes the latest included version. |

The rendered fixture text is a faithful explanation of its saved record values;
the exact original values remain expandable. Direct requests display their
question and expected contribution, while the shared work board sits in Details.
When an answer artifact contains permitted critique text, the peer request shows
it and retains an exact-record link. Missing delivery, answers and reviews are
identified as missing rather than inferred from an adjacent event.

The shared mission projection keeps independently accepted reports as separate
outputs. It names one main answer only when exactly one report was accepted.
A submitted peer answer remains “awaiting review” until the requester accepts
its exact saved version; submission alone is not shown as delivered mail.

Activity and Details join each attempt to its exact saved model response. Rate
limits, oversized requests, validated tool-name errors, metadata-only steps,
unavailable sources and accepted reports have different explanations. A completed
model response or saved tool rejection is not shown as an accepted research
answer. Historical responses without a more specific recorded cause remain
unclassified; private provider messages and account identifiers are not exposed.
When the exact saved provider error identifies a daily or per-minute token limit,
Atlas explains the request size and remaining allowance. Expanded details retain
the recorded use, limit and advised retry interval. That interval applies to the
rejected request; it does not establish a full allowance reset or guarantee that
a larger next request will fit. Unclassified rate limits remain unclassified.
An exact local context-preparation diagnostic identifies a pre-dispatch failure,
including a failed source-free recovery attempt, without calling it a provider
failure. An exact executor result retained after task rejection appears as a
saved local result the task did not accept. If a matching recovery receipt and
non-completing board outcome are accepted, Atlas shows that the local recovery
was accepted while its context tool result and next turn remain pending. A later
matching context tool result takes precedence; the older rejection remains in
the diagnostic record. Missing or inconsistent links remain unresolved.
When a pending model call owns an exact saved capacity wait, the task and active
attempt are shown as waiting. Activity names the saved wait reason and next
eligible time; admission and dispatch can happen later. Older attempts and waits
without that exact link are not relabeled as current capacity waits.
Research review uses that same saved response explanation for a tool-only live
attempt when no scientific result summary exists; the completed tool step does
not become an accepted report. A run change also resets ordinary source and task
selection to available records in that run, including a task with no attempt.
An explicit citation to an unavailable source is preserved as a missing target.

A rejected tool call can identify a missing argument when the saved provider
error and exact tool definition establish that cause. The overview explains
which field was missing and that the rejected call did not execute a host tool;
raw provider error text stays private. Other failures remain unclassified when
the available records do not establish a more precise explanation.

Submission and view refresh have separate outcomes. Without a saved acceptance
receipt, the form says the response is unconfirmed because the mission may
already exist. An explicit retry keeps the same frozen request identity and
settings; it does not create a new request automatically. When an acceptance
receipt exists but the next view cannot be loaded, the form retains that receipt
and identifies the view failure separately. Reused follow-up sources retain
their exact parent snapshot and copy provenance rather than appearing as fresh
retrievals by the child run.

## Read a run quickly, then inspect the exact step

Activity presents saved attempts and outcomes in compact chronological rows.
Its overview separates attempts, saved model-call responses, accepted reports,
recorded issues and peer requests. These are different counts, not interchangeable
measures of useful research. An accepted report whose exact version is unavailable
does not become a misleading zero. Recorded issues include historical failures
and waits; a later result may have resolved them.

Known research tools have plain-language action labels. Expanding one row restores
the full assignment, outcome explanation, exact tool name, timestamps and saved
record links. Opening the full attempt selects that same attempt in Details.
Research review and task timelines use the same action labels, such as **Read a
saved source excerpt**. A pending request, saved metadata and a completed tool
step retain distinct explanations; an unfamiliar tool keeps its recorded name.
Search and agent/category/issue filters narrow the view without changing the run;
clearing them restores all saved activity. The full saved-step ledger remains
available below the compact list.

Saved runs foregrounds the question, status, created/progress times, named peers,
and outstanding work. The full question, copyable identities and completion
requirements remain in expandable run details. A listed parent can be opened
directly. An empty date filter explicitly says **Any date**, because the native
date control can display today's date as a placeholder without applying a filter.
**Clear filters** restores the full history without starting research.
Mechanically shortened question prefixes expand into a two-line heading, keeping
the distinguishing part of the question available to hover and screen readers.
A separately supplied descriptive title remains unchanged.
Historical peer labels come from the selected run's saved names, rather than the
current roster. A missing saved name stays unavailable. Known internal failure
phrases have a readable history summary; the original reason remains in expanded
details. Exact identifier lookups with an explicitly empty text query are labeled
as lookups, while an absent query record remains unknown.

The run header foregrounds saved status and freshness. Run and snapshot IDs and
provider route names remain available under **Technical run details**. When no
directed peer request is recorded, Communication identifies the agents with saved
attempts and links to their activity; it does not invent an exchange. The native
shell derives its window title from the loaded page, so the initial sample title
does not persist on a connected live view. This title update grants no additional
web-page access to native commands.

## Verification record

The focused React checks exercise the recorded coordination fixture, saved-run
filters and local live view transitions with synthetic service responses. The
TypeScript production build checks the current generated schema. Native `cargo
check` and `scripts/dev desktop` compile and bundle the shell without invoking a
research command.

A Chrome walkthrough of the bundled Stage 3 export at `/?recorded=stage3`
confirmed the offline title and marker, two directed requests with saved times,
the partial critique's permitted text, and the shared work board in Details.
Opening the partial request focused `d-evidence-gap`, displayed its referenced
board proposal version 1 and request version 3, and said explicitly that no
agent attempt was recorded for that waiting task. This caught a wrong sibling
request selected by task ID alone; the originating exact request is now carried
into Details. The walkthrough is UI evidence for the saved offline fixture only.

A targeted native and Chrome pass used the same previously saved retrieval-backed
run and snapshot. Both showed matching Activity counts and outcome labels. The
Mac app opened the exact attempt in Details, returned from a recorded example to
the real request form with the same run selected, and changed its window title
between live and recorded views. The Activity layout was inspected at normal and
minimum native window widths; browser keyboard expansion and collapse retained
focus. This review submitted no research task or host restart.

Reconnect after a real host restart, the five retrieval-backed levels and broader
human usability still require their own observed receipts and reviewer answers.
Keep run IDs, event and version references, timestamps, app build, measured
latency and remaining defects with those receipts; do not substitute an offline
fixture or this focused saved-run review for them.

## Plain-language terms and review controls

The ⓘ term buttons open by hover, keyboard focus or click and close with Escape
or when focus leaves. Definitions cover **work step**, **earlier run**, **shared
work board**, **source copy**, **accepted report**, **task assignment** and
**model reasoning**. Details also includes a compact glossary.

A work step is one agent's saved execution on one task in a run; exact records
retain the name `attempt`. A prepared step may not have reached a provider.
The timeline, selected-step view and reasoning list use the same chronological
number within the current run, with earlier-run steps numbered separately.
Filtering or showing earlier work does not renumber the current run. The selected
ID, rather than its display number, remains the navigation authority.

The selected run's summary separates recorded peer communication, accepted-report
availability, and an explicitly recorded operator pause. An assignment that names
a peer does not become a sent message. Communication states when no messages are
recorded. Selecting a timeline step visibly focuses its own review or task record;
Details can open that exact step in Communication's on-demand reasoning panel.

Evidence foregrounds saved titles and retained passages, with discovery leads and
copy provenance expandable. Compare shows the earlier run and follow-up with each
report's own source copy and passage. Report Markdown renders as readable headings,
lists and paragraphs. A missing/mismatched earlier snapshot remains explicit, and
an unverified report artifact is not labeled accepted. UI checks and a saved-run
walkthrough do not establish new live peer collaboration or whole-app usability.

Saving a new snapshot of the review is not an agent work step. Inspection omits
`snapshot_accepted` events from the human work-step replay while retaining their
raw event records in the state store. Actual research events, exact source and
task links, accepted outputs and each saved view's event boundary remain intact.
