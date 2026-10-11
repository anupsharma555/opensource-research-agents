# Research Atlas sample review prototype

This guide describes the default sample mode of the React/Vite interface inside a Tauri v2 macOS shell. That mode opens two **scripted sample snapshots** for review and comparison, and makes no research-host, model, provider, or sandbox request. The yellow `SCRIPTED SAMPLE` marker remains visible while reviewing them. A simulated queue receipt is a preview, not submitted work.

The separately launched [local research workspace](../docs/development/stage4-local.md) connects real Start and Follow-up requests to the shared research loop, with automatic status refresh and saved run history. Its [Atlas review map](../docs/development/stage4-atlas.md) owns the connected workflow: compact saved-work bars in Research review open exact chronological steps in Activity. The earlier [supplied-evidence extension](../docs/development/live-research.md) remains available for its saved workspaces. Sample-mode restrictions below do not describe either explicit live route.

## Run and verify

Use the pinned Python 3.12.12, uv 0.9.21, Node 24.21.0 and npm 11.19.0 toolchain. Native Mac builds use Rust/Cargo 1.93 and Xcode Command Line Tools; the prototype was checked on macOS 15.5 arm64. [Development setup](../docs/development/setup.md) owns these versions and the shared commands. From the repository root:

```sh
scripts/dev bootstrap
scripts/dev setup
scripts/dev check
```

Run `scripts/dev ui` separately for a browser preview, or `scripts/dev desktop` to build the unsigned Mac app.

The native app is titled **Research Atlas · Sample Prototype**. `scripts/dev desktop` builds an unsigned macOS app bundle, which was opened independently of the Vite server. Native review at approximately 1504 × 1013 confirmed the cited finding, source and claim history, peer work, diagnostics, follow-up previews and two-run comparison. After a usability fix, switching screens from a scrolled page returned to the top. Signing, notarization, updates, installer recovery and long-running host behavior remain untested. The shell owns a review window only; no Python mission host is bundled or started.

The Python contract source is `src/ora/contracts/models.py`. The checked-in schema `contracts/generated/research.schema.json` is its generated wire definition. `scripts/dev generate` rebuilds schemas and `ui/src/generated/contracts.ts`; `scripts/dev check` detects generated drift. Do not edit generated files by hand. The fixtures `ui/public/sample-run.json` and `ui/public/sample-run-baseline.json` use that `ResearchSnapshot` shape and the public sample abstract-disagreement benchmark scenario version 1.0.3. Both validate with the canonical Python model. Their source IDs, passage IDs, locators and content hashes are stable packet references; the withheld evaluator answer key is never loaded into the UI.

## Start in the app

**Research review** presents one overall research task in order: input question, scope and supplied source data; work by named agents; and the current saved output. **Work toward this answer** puts each contribution on the same elapsed-time scale. Separate attempt bars preserve retries and gaps, while striped bars show waiting. Overlapping work shares the same time positions. Select a row to read **Contribution to this answer** directly underneath: its purpose, request, recorded work and result. Expand task details in place to see IDs, timing, tools and returned-content status. A request to another agent means a saved message asking for specific work; it is not a user request. The mission remains active, so report v2 is a saved partial output, not a completed final answer. `(i)` controls explain how to use each view or section on hover and keyboard focus.

The saved output leads with the readable answer. Sources and supporting passages,
related peer exchanges, and exact report metadata expand separately. Recorded
citations remain distinct from references recovered from older answer text;
neither establishes semantic support on its own. Saved limitations appear after
the answer, while an absent limitations field is not presented as certainty.
The original report text remains available in Report details.

Each of the four main review cards has an expand/collapse arrow. Folding a card keeps its heading, named contributor or report context, and status visible. **Collapse all** gives a compact overview; then open only the sections needed. Card expansion choices survive tab changes. Folding a card preserves the expansion state of its nested source disclosures. Choosing a contribution on the timeline opens its details underneath automatically, without reopening the other cards.

The input source disclosure reads the canonical public `benchmarks/v1/scenarios/syn-abstract-disagreement.json` packet. Its `initial_source_ids` establishes that A1–A4 were supplied at the start: three simulated abstracts using real condition and treatment names, plus one access notice. The active packet uses depression, generalized anxiety disorder and cognitive behavioural therapy. Its studies and counts remain simulated and are not findings from REEACT or another real publication. Expanding a source shows its complete public packet text, not just the retained citation passage. Native review of version 1.0.2 confirmed the updated question and expanded A1/A3 text with the simulated-data labels. This mapping is limited to the two known sample snapshots and matching scenario version; other snapshots report that initial source material is not recorded. The UI does not infer that every source collected during a run was provided as input, and it never imports the withheld evaluator reference.

The sidebar starts with **Start or follow up**, then groups the current task views: Research review → Activity → Communication → Evidence → Details → Compare. Dividers separate **Start or follow up** and **Previous samples**. **Communication** gives each request its own full-width card. Within it, the request and its result or current status appear side by side on wider windows and stack on narrower ones. Expanded attempt summaries use full-width paragraphs next to their status; delivery history is a separate list. **Activity** orders saved agent attempts, messages and host events by time. Each timestamp names what it records and includes the full date, seconds and local time zone. Expanded records explain the status, named agent, purpose, tools, request context and elapsed time; raw IDs sit in a separate disclosure. It has no live connection or automatic refresh. **Details** holds complete task and attempt records, exact IDs, timing, tools, run metadata and alternate interface-state previews. **Evidence** holds exact source passages and claim versions. **Compare** shows a separate baseline snapshot whose difference reflects source coverage, not model quality.

**Clear review** removes the bundled sample from the active review locally; it does not delete its fixture. **Previous samples** can load it again. **Replay saved activity** starts an empty Activity feed and reveals existing records in timed steps, with pause and resume. This playback is useful for trying the review flow, but no agent or backend runs during it. The cleared/loaded choice is stored locally; the app does not retain a history of actual prior runs.

The actual small Groq skill-fit experiment is separate from these fixtures. Its synthesis and appraisal outcomes are summarized in [model-fit.md](../docs/development/model-fit.md), with detailed local run evidence under ignored `.local/stage1/skill-fit-live/`. The app does not import those outputs. Its scripted critique task is a navigable example, not a dedicated study-appraisal screen or the Groq appraisal result.

Native review of the current UI confirmed that all four review cards collapse into a compact overview and the output expands into reported results, interpretation and remaining unknowns. Communication showed one request per card with a horizontal request/result flow and readable full-width attempt paragraphs. Expanded Activity showed the named actor, status explanation, tool record, elapsed span, and full timestamps with seconds and EDT. Earlier native review checked original A1 source text, the contribution timeline, inline contribution selection, source links and sample replay. These observations check navigation and labeling in the bundled interface; they do not qualify the simulated conclusions or a live model run.

## Review journeys

| Review task | Path through this prototype | Observed in fixture and native review | Proof boundary |
| --- | --- | --- | --- |
| Read one research task | Research review → input and supplied sources → contribution timeline → selected contribution underneath → current report | The timeline uses saved attempt times and keeps retries and waiting distinct; the selected contribution remains on the page. | A saved partial report does not prove a completed mission or scientific validity. |
| See who asked whom | Communication → requester and assignee → exact request and response → expand contribution | Native review showed the fulfilled A2 request beside the acknowledged but unfulfilled methods request. | The messages are scripted records, not a live agent inbox or verbatim private model reasoning. |
| Follow recorded activity | Activity → Agent work / Messages / Host events filters → expand a record | Native review showed the time-ordered saved feed and no-live-connection label. | There is no event subscription, automatic refresh or live task update. |
| Clear and replay the test sample | Clear review → Previous samples → Replay saved activity → load sample again | Automated UI checks cover clear, local reload and staged replay. | Playback is timed display of fixed records, not a live run; actual run history is unavailable. |
| Check a source passage | Research review citation or Evidence → exact saved source copy and passage | Native review followed the A1 citation to its highlighted passage; the complete source list remained available. | Rendering a citation does not establish that the passage supports a claim. |
| Inspect raw metadata | Details → Task records or Attempt records → run metadata | Native review exposed IDs, timing, tools, failures, returned-content availability and sample-state previews outside the main review. | Provider-returned content is availability-limited; hidden model chain of thought is not accessible. |
| Record a review question | Evidence → select source and claim version → save local note | A local note retains the visible source-copy and claim-version labels, plus sample version. | Persistence, permissions and feedback synchronization require the connected app. |
| Ask for new or version-specific work | Start or follow up → select exact claim/task/report → scope preview → simulated receipt → recorded child task | Native review showed both the new-work preview and report-v1 follow-up receipt; its scripted child task has a failed first route, successful second route, A2 contribution and revised claim. Other targets remain previews without scripted outcomes. No backend call occurs. | Real authentication, acceptance, idempotent receipt and queued-task continuation require the connected app. |
| Understand interrupted or partial views | Details → Run metadata → state selector → empty/loading/disconnected/quota/failed/partial | Empty and disconnected show explicit uncertainty; supported findings remain visible in other partial states. | Host cursor freshness, reconnection and quota reasons need live events. |

The tests use a fixed snapshot, not a real provider or model result. The example returned text is explicitly **illustrative fixture content**; “absent,” “restricted,” and “truncated” are distinct states. The concise report is a derived reader artifact and does not reveal raw attempt traces or imply access to hidden model reasoning.

## Screen-to-record map

| Screen | Required snapshot records | Future connected-app seam |
| --- | --- | --- |
| Research review | Mission question/scope/status, explicit initial source packet, named contributors and attempt times, original request, saved summaries, current report/citations/limitations | Review API returns one versioned mission view with input provenance, contributions and current output; sources supplied at the start remain distinguishable from later discoveries, and a partial output stays labeled partial. |
| Activity | Events, attempt summaries, agent messages and captured time | A live host must provide an authorized event stream or cursor-based updates with gap recovery, freshness, and last-seen status before claiming real-time behavior. |
| Communication | Request IDs, sender/recipient agents, ordered message statuses/summaries and source/claim links | Host commits request/delivery/acknowledgment/fulfillment and distinguishes acknowledgment from an actual reply. |
| Details | Tasks, attempts, timing, tool/provider status, run IDs/versions and returned-content availability | Host serves permitted trace records and run metadata separately from the reader output. |
| Evidence | Source copy IDs/versions/access state/hash/passages and claim support edges | Source retrieval returns permitted text, locator and rights; exact copy versions survive restart. |
| Start or follow up | User-entered scope plus exact target task, claim or report version | A validated command produces an accepted/queued/rejected durable receipt and child link. |
| Compare | Two snapshot/mission IDs, source counts, captured times, versioned report artifacts and citations | Live API must provide both exact snapshots and a comparable evaluation basis before interpreting differences as quality gains. |
| Previous samples | Bundled sample reference and local active/cleared choice | A connected app needs durable authorized run history and an exact snapshot loader; the current fixture is not run history. |

## Feedback and limits

The connected desktop work should retain the overall input → named agent contributions → current output view, the separate Communication and Activity views, stable source/claim/task IDs, exact follow-up target version, permitted attempt details, persistent sample/live label, and branch-local waiting/failure language. A live Activity view needs an authoritative event feed with a cursor, reconnection and gap recovery, saved provenance, authorization, and a visible freshness indicator. Do not substitute polling of these fixture files or an animated preview for live-run proof.

Open design questions: how many source passages fit at the minimum supported window width; which diagnostic fields can be shown by default; how to summarize concurrent work without implying false sequence; and how a true two-run comparison should normalize scope and source versions. The prototype leaves these choices open rather than inventing live behavior. VoiceOver, full keyboard traversal of every view, and minimum-size window review have not been completed.

## Saved Stage 2 inspection

`ResearchApp` also renders the canonical optional `ResearchSnapshot.inspection`
projection. The backend owns these records. The UI resolves exact record kind, ID
and version; it does not maintain a research state store or reconstruct missing
provenance. Saved inspection details expand within the review: agent instance,
configuration, calls, inputs, accepted output references, and source discovery
receipts. Empty or unresolved references remain explicit. Backend record fields
are presented as labeled values, with nested data available on expansion.

For this projection, supplied source IDs distinguish initial inputs from later
retrieval and reuse. Accepted report references must match the exact report ID
and version; a successful attempt alone does not make its output accepted. The
report and comparison citations expand their own snapshot's retained passage,
copy and discovery records. A parent citation never opens a newer child's source.

The recorded demonstration uses `proofKind=offline_test`. Its label and any
controlled-clock timestamps remain explicit; these are not live provider latency
or scientific validation. This mode provides read-only review navigation. Older
sample and live snapshots without inspection remain supported and identify the
missing inspection records. Live transport and run loading are owned by the
existing live integration, outside these presentation components.

### Open the persisted demonstration

Choose **Previous samples → Open recorded Stage 2 review**, or open the frontend
with `?recorded=stage2`. This is an explicit read-only entry point. It fetches only
`/stage2-recorded/child.json` and `/stage2-recorded/parent.json`, verifies their
recorded proof mode and parent relationship, and rejects an incompatible pair.
The same path works in the bundled Mac app through the Previous samples link.
The generated-assets README records the demo command and source of the exports.
Provider and query details resolve through exact saved encounter/result/receipt
references. A linked record can be expanded in place; missing versions do not
resolve to a different version. Inspection data is rendered as text, never HTML.

### Open the Stage 3 coordination fixture

Choose **Previous samples → Open recorded Stage 3 coordination review**, or use
`?recorded=stage3`. This separate read-only route loads only
`/stage3-recorded/snapshot.json`; arbitrary URL parameters cannot select another
fetch destination. The JSON comes from a fresh persisted callback fixture,
described in [its generated-assets README](public/stage3-recorded/README.md).

Research review shows three completed local callback observations and an active
mission, with one waiting task, an unanswered peer question, and a separate
partial critique still awaiting requester review. There are no
source studies, selected models, scientific claims, or reports in this fixture.
Accepted callbacks do not imply a completed research mission. **Details → Saved
agent steps** and **Saved coordination records** retain exact inputs, callback
receipts, capacity/proposal records and traces separately from findings. The
recorded timestamps describe local test execution, not model or provider latency.

**Communication** separates work proposals, exact version relationships, retained
critique and peer requests. Delivery, acknowledgment, submitted partial answers
and requester review have separate fields; none is inferred from another.
**Details → Saved work and waits** explains dependencies, saved state and next
actions. The overlap summary distinguishes actual callback intervals from
elapsed attempt spans that may include waiting. Timing fields use only saved
measurements; missing peer or provider timing is not counted as zero.
**Attempt records → Execution diagnostics** separates configured model/skills/tools
from saved calls and invocation receipts. Exact input and output versions remain
expandable. Reasoning request settings, response availability and permission to
view content are separate; an empty attempt field does not mean content was
never requested or returned. Restricted material stays outside the findings.

No live service or database is opened by this route. Stage 2's paired report
review and the explicitly launched live workspace remain separate entry points.
