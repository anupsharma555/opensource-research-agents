---
name: os-review-atlas
description: Review Research Atlas browser and native views against saved Stage 4 records, checking understandable research, communication, timing and provenance. Use for visible interface or same-run parity reviews, not for offline code checks alone.
metadata:
  version: 0.1.0
---

# Review Research Atlas

Before using Stage 4 commands or documentation, check that this checkout contains `src/ora/mission/`, `docs/development/stage4-local.md` and `docs/development/stage4-atlas.md`. These skills may be synchronized before their implementation dependencies. If any prerequisite is absent, report that Stage 4 workflow as unavailable in this checkout and stop before running its commands; do not infer working mission or interface support from this skill package.


This is a Codex development review workflow, not a restriction on scientific agents. Read `AGENTS.md`, `README.md`, `docs/development/setup.md`, `docs/development/stage4-local.md`, and `docs/development/stage4-atlas.md`. Preserve the chosen workspace, run, exact records and current app build. The Atlas review map at `docs/development/stage4-atlas.md` identifies each view's saved authority and proof limits.

Use the existing host's read-only status and call commands to identify the run, workspace, host `hostBuildId`, parent/follow-up target, saved snapshot and outstanding work before opening its view. A recorded example, live workspace and UI poll are different events. Record both the viewer observation time and the saved run/event timestamps; do not turn a refresh into research progress. Opening a disconnected viewer does not stop the host. Do not start, resume, cancel or submit a live mission during a review unless that action was separately authorized.

In the real browser and, when applicable, native app, follow one exact run through **Saved runs**, **Research review**, **Communication**, **Evidence**, **Activity** and **Details**. Use UI automation or Playwright when visible confirmation is requested. Verify that the browser and native app point to the same workspace, run and snapshot. Record the shared host `hostBuildId` and each viewer's browser-asset or native-bundle revision separately; compare presentation only when their versions are known and compatible. Record the observed label, relevant exact record/version, timestamp, and whether the source is live or a recorded fixture. If the viewer versions or connection differ materially, report that boundary rather than claiming parity.

Start with a quick reader pass: can the run be found, can its current and continuing status be understood, can its answer and source be opened, and can a directed peer exchange be followed? Then expand Activity and Details only where the summary is unclear or a failure needs an exact trace. In both browser and native windows, check hierarchy, legible labels, navigation, clipping/truncation, link targets and understandable error or waiting states. A data-correct panel that a human cannot scan or follow is a presentation defect. Label observations **verified**, **inferred**, **missing** or **inconsistent**, and keep execution, content and presentation outcomes separate.

Review in plain language from the reader's point of view:

- Can a reader tell who sent a peer request, to whom, for which audience and purpose, when it was created, delivered, acknowledged, answered and reviewed, and what outcome followed? Keep shared-board visibility separate from directed communication. A peer answer, an accepted report and requester review are different events.
- Can a reader distinguish a prepared model call, dispatched/captured response, tool request, completed tool result, failed or quota-waiting step, an accepted report and work still continuing after a report? A completed tool is not a completed scientific answer.
- Does every consequential claim open the actual saved source copy and passage with version and locator? Compare quoted or paraphrased claims with the passage, check contrary evidence and unresolved limitations, and preserve publication identity separately from duplicate copies. Citation shape alone cannot establish support.
- Does a follow-up open the exact parent snapshot, target ID/version, old answer and changed answer? Are unresolved peer requests and other continuing tasks visible rather than hidden by a completed run label?
- Are terms understandable to a research reader? Keep implementation diagnostics in Activity/Details and research findings in the review; explain unknown or unavailable evidence rather than filling it in. Avoid synthetic sample timestamps or fixture callbacks being presented as live provider performance.

Save a private, reproducible review note containing the exact workspace/run/build/snapshot, browser and native observation times, evidence references, screenshots or interaction trace where useful, observed defects, and unverified boundaries. An interface label or passing component test is not visual proof. Use the smallest relevant UI check after a correction, then compare the same saved run in both surfaces if parity is the claim. Do not place private URLs, source bodies, raw model prompts/reasoning, credentials or personal identifiers in tracked files. For efficiency or provider usage diagnosis, use `$os-qualify-run` separately; this review does not call providers.

Keep each UI finding brief: **view/control and reader task; observed confusion; clearer wording or organization; exact saved record and visible proof; next useful check**. Lead the handoff with the observed outcome and first mismatch or supported cause, stating uncertainty. A same-run control check does not certify the whole app's usability.
