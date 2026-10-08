# Research Atlas sample review prototype

This is a React/Vite interface inside a Tauri v2 macOS shell. It opens one **scripted synthetic snapshot** and makes no research-host, model, provider, or sandbox request. The yellow `SAMPLE DATA` marker remains visible while reviewing it. A simulated queue receipt is a preview, not submitted work.

## Run and verify

Use Node 24.21.0 or a compatible Node 24 release, Rust 1.90 or newer, and Xcode Command Line Tools on the Mac. Dependencies are pinned in the root lockfiles. From the repository root:

```sh
npm ci
npm run generate:types:check
npm test
npm run build
npm run desktop:dev
```

The native app is titled **Research Atlas · Sample Prototype**. `npm run desktop:build -- --debug --bundles app` made an unsigned local macOS app bundle, and the bundled app was opened independently of the Vite server. The native window visibly loaded the saved finding and source list. After a usability fix, scrolling Findings to the bottom and switching to Evidence returned the page to the top with the source list visible. Signing, notarization, updates, installer recovery and long-running host behavior remain untested. The shell owns a review window only; no Python mission host is bundled or started.

The Python contract source is `src/ora/contracts/models.py`. The checked-in schema `contracts/generated/research.schema.json` is its generated wire definition. `npm run generate:types` rebuilds `ui/src/generated/contracts.ts`; `npm run generate:types:check` fails if that file differs. Do not edit the generated TypeScript by hand. The fixtures `ui/public/sample-run.json` and `ui/public/sample-run-baseline.json` use that `ResearchSnapshot` shape and the public synthetic abstract-disagreement benchmark scenario version 1.0.1. Both validate with the canonical Python model. Their source IDs, passage IDs, locators and content hashes are stable packet references; the withheld evaluator answer key is never loaded into the UI.

## Review journeys

| Review task | Path through this prototype | Browser component-test observation | Remaining native/live proof |
| --- | --- | --- | --- |
| Find the bounded answer and its passage | Findings → A1/A2 citation → Evidence passage | Exact cited passage opens and is highlighted in the component test; the packaged Mac window visibly renders the finding and complete source list. | Native citation focus and scientific validity are not established by rendering. |
| See why a claim changed | Findings claim lineage → Evidence versions → Compare | Two distinct sample mission snapshots display their report versions and different source coverage; the native accessibility view showed both run IDs and report versions. | A real cross-run comparison needs matched scope/source versions and connected API data. |
| See who helped and what is still open | Work → timestamp-based overlap bars and serial event trail → request status → methods task | Requested, delivered, acknowledged and fulfilled are separate; the methods request has no fulfilled stage. | A live peer inbox and durable task handoff are not implemented. |
| Record a review question | Evidence → select source and claim version → save local note | A local note retains the visible source-copy and claim-version labels, plus sample version. | Persistence, permissions and feedback synchronization require the connected app. |
| Inspect failed retrieval and returned-content limits | Work/Diagnostics → failed methods attempt, critique attempt, A2 attempt | Failure, absent, truncated and illustrative returned content are distinguished from the reader report. | Actual provider-returned reasoning availability and access policy require live contracts. |
| Ask for new or version-specific work | Start or follow up → select exact claim/task/report → scope preview → simulated receipt → recorded child task | The default report-v1 path opens task-a2 with a failed first route, successful second route, A2 contribution and revised claim; other targets explicitly have no scripted later outcome. No backend call occurs. | Real authentication, acceptance, idempotent receipt and queued-task continuation require the connected app. |
| Understand interrupted or partial views | State selector → empty/loading/disconnected/quota/failed/partial | Empty and disconnected show explicit uncertainty; supported findings remain visible in other partial states. | Host cursor freshness, reconnection and quota reasons need live events. |
| Navigate without a pointer | Tab into sidebar → Enter on Work | The Work view opens from keyboard focus; visible focus styles are defined. | Confirm window-size, VoiceOver and focus order in the native Mac view. |

The tests use a fixed snapshot, not a real provider or model result. The example returned text is explicitly **illustrative fixture content**; “absent,” “restricted,” and “truncated” are distinct states. The concise report is a derived reader artifact and does not reveal raw attempt traces or imply access to hidden model reasoning.

## Screen-to-record map

| Screen | Required snapshot records | Future connected-app seam |
| --- | --- | --- |
| Findings | Mission ID/version/status/question, current report artifact ID/version/body/citations/limitations, claims and captured time | Review API returns a versioned snapshot and a cursor; report content remains reader-facing. |
| Work | Tasks, attempt start/end times, agents, messages and events with stable IDs and parent links | Host commits request/delivery/acknowledgment/fulfillment and distinguishes active from waiting. |
| Evidence | Source copy IDs/versions/access state/hash/passages and claim support edges | Source retrieval returns permitted text, locator and rights; exact copy versions survive restart. |
| Diagnostics | Attempt status, tool/provider names, selected model/skill versions, returned-content availability and faults | Restricted traces and provider-returned content have separate access from reports. |
| Start or follow up | User-entered scope plus exact target task, claim or report version | A validated command produces an accepted/queued/rejected durable receipt and child link. |
| Compare | Two snapshot/mission IDs, source counts, captured times, versioned report artifacts and citations | Live API must provide both exact snapshots and a comparable evaluation basis before interpreting differences as quality gains. |

## Feedback and limits

The first handoff to the connected desktop work should retain: the six navigation destinations, stable clickable source/claim/task IDs, exact follow-up target version, separate diagnostic view, persistent sample/live label, and branch-local waiting/failure language. Native inspection should record the window size, task attempted, expected versus observed path, and any defect against the affected screen and sample version. Those observations are usability evidence, not backend or scientific-quality evidence.

Open design questions: whether a real mission overview needs a persistent event timeline in addition to the task bars; how many source passages fit at the minimum supported window width; which diagnostic fields can be safely shown by default; and how a true two-run comparison should normalize scope and source versions. The prototype leaves these choices open rather than inventing live behavior. VoiceOver, full keyboard traversal of every view, and minimum-size window review have not been completed.
