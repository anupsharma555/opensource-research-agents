# Research Atlas sample review prototype

This is a React/Vite interface inside a Tauri v2 macOS shell. It opens two **scripted synthetic snapshots** for review and comparison, and makes no research-host, model, provider, or sandbox request. The yellow `SAMPLE DATA` marker remains visible while reviewing them. A simulated queue receipt is a preview, not submitted work.

## Run and verify

Use the pinned Python 3.12.12, uv 0.9.21, Node 24.21.0 and npm 11.19.0 toolchain. Native Mac builds use Rust/Cargo 1.93 and Xcode Command Line Tools; the prototype was checked on macOS 15.5 arm64. [Development setup](../docs/development/setup.md) owns these versions and the shared commands. From the repository root:

```sh
scripts/dev bootstrap
scripts/dev setup
scripts/dev check
```

Run `scripts/dev ui` separately for a browser preview, or `scripts/dev desktop` to build the unsigned Mac app.

The native app is titled **Research Atlas · Sample Prototype**. `scripts/dev desktop` builds an unsigned macOS app bundle, which was opened independently of the Vite server. Native review at approximately 1504 × 1013 confirmed the cited finding, source and claim history, peer work, diagnostics, follow-up previews and two-run comparison. After a usability fix, switching screens from a scrolled page returned to the top. Signing, notarization, updates, installer recovery and long-running host behavior remain untested. The shell owns a review window only; no Python mission host is bundled or started.

The Python contract source is `src/ora/contracts/models.py`. The checked-in schema `contracts/generated/research.schema.json` is its generated wire definition. `scripts/dev generate` rebuilds schemas and `ui/src/generated/contracts.ts`; `scripts/dev check` detects generated drift. Do not edit generated files by hand. The fixtures `ui/public/sample-run.json` and `ui/public/sample-run-baseline.json` use that `ResearchSnapshot` shape and the public synthetic abstract-disagreement benchmark scenario version 1.0.1. Both validate with the canonical Python model. Their source IDs, passage IDs, locators and content hashes are stable packet references; the withheld evaluator answer key is never loaded into the UI.

## Review journeys

| Review task | Path through this prototype | Observed in fixture and native review | Proof boundary |
| --- | --- | --- | --- |
| Find the bounded answer and its passage | Findings → A1/A2 citation → Evidence passage | Native review followed the A1 citation to its highlighted passage; the complete source list remained available. | Rendering a citation does not establish scientific validity. |
| See why a claim changed | Findings claim lineage → Evidence versions → Compare | Native review showed the two sample run IDs, report versions and different source coverage. | A real cross-run comparison needs matched scope/source versions and connected API data. |
| See who helped and what is still open | Work → timestamp-based overlap bars and serial event trail → request status → methods task | Native review showed the acknowledged but unfulfilled request, unaffected sibling and failed retrieval; requested, delivered, acknowledged and fulfilled stay distinct. | A live peer inbox and durable task handoff are not implemented. |
| Record a review question | Evidence → select source and claim version → save local note | A local note retains the visible source-copy and claim-version labels, plus sample version. | Persistence, permissions and feedback synchronization require the connected app. |
| Inspect failed retrieval and returned-content limits | Work/Diagnostics → failed methods attempt, critique attempt, A2 attempt | Native review kept diagnostics separate from findings and showed failed work; the fixture distinguishes absent, restricted, truncated and illustrative returned content. | Actual provider-returned reasoning availability and access policy require live contracts. |
| Ask for new or version-specific work | Start or follow up → select exact claim/task/report → scope preview → simulated receipt → recorded child task | Native review showed both the new-work preview and report-v1 follow-up receipt; its scripted child task has a failed first route, successful second route, A2 contribution and revised claim. Other targets remain previews without scripted outcomes. No backend call occurs. | Real authentication, acceptance, idempotent receipt and queued-task continuation require the connected app. |
| Understand interrupted or partial views | State selector → empty/loading/disconnected/quota/failed/partial | Empty and disconnected show explicit uncertainty; supported findings remain visible in other partial states. | Host cursor freshness, reconnection and quota reasons need live events. |
| Navigate without a pointer | Tab between source items → Return on A2 | Native Tab+Return moved from A1 to A2 at the reviewed window size; the component test also opens Work from keyboard focus. | Minimum-size layout, VoiceOver and full keyboard traversal remain untested. |

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

The first handoff to the connected desktop work should retain: the six navigation destinations, stable clickable source/claim/task IDs, exact follow-up target version, separate diagnostic view, persistent sample/live label, and branch-local waiting/failure language. The approximately 1504 × 1013 native review covered the journeys above; its observed navigation fix is usability evidence, not backend or scientific-quality evidence. Future native findings should remain linked to the screen and sample version.

Open design questions: whether a real mission overview needs a persistent event timeline in addition to the task bars; how many source passages fit at the minimum supported window width; which diagnostic fields can be safely shown by default; and how a true two-run comparison should normalize scope and source versions. The prototype leaves these choices open rather than inventing live behavior. VoiceOver, full keyboard traversal of every view, and minimum-size window review have not been completed.
