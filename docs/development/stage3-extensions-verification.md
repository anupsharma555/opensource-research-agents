# Stage 3 contribution review and burst checks

The two research-led follow-ups are implemented and checked in the uncommitted
working tree. They add reviewable peer contributions and explicit explanation
coverage, then measure how the existing executor handles bursts of queued work.
They do not activate a provider or replace Atlas's local live execution engine.

## Acceptance evidence

| Requirement | Implemented behavior and observable check |
| --- | --- |
| Discover discussion and counterevidence | `ContributionReview.proposal_context` pages proposal history for a registered late peer, including the prior critique and exact evidence versions. `test_contributions.py` checks discovery and permission boundaries. |
| Distinguish receipt from use | Optional append-only peer statements link an exact update to a stated effect. The connected example retains an acknowledged update with unknown use. Missing uptake does not block work. |
| Keep questions open | Clarification is linked to its original request, remains distinct across delivery versions, and cannot be recorded as ordinary-update uptake. Original unanswered and partial-review states remain intact. |
| Preserve multiple inputs and relationships | Trace tests retain exact inputs, one execution parent, and separate input/reply/revision/retry links across duplicate and out-of-order events. The connected example links two input versions and a peer update. |
| Explain summary coverage truthfully | Summary tests cover late events, missing records, intentional omission, pending detail, old summaries and explicit scope. An event boundary does not prove complete source coverage or scientific correctness. |
| Share the same readable record view | CLI and Atlas inspect the same saved proposal projection and summary records. Integration tests verify identical projections, paged reads and unchanged database bytes. UI tests verify readable distinctions and fixed routes without research dispatch. |
| Preserve independence and privacy | Reads do not acquire research permits or invoke models. Tests cover foreign missions, private messages, restricted reasoning, restart and duplicate operations. Caller authentication remains a later host/service responsibility. |
| Measure backlog and resource use | Six disposable arms compare 8, 24 and 96 work items with one or three model permits. The workload measures backlog, active callbacks, admission delay, first artifact, elapsed/active/wait time, local commit visibility, memory and database/WAL growth. |
| Retain work and healthy progress | Each arm accepts all 7, 23 or 95 noncanceled items once, preserves cancellation, and makes no additional dispatch on duplicate notification or reopen. Independent retrieval progresses during the slow model callback. |
| Make the design decision proportional | Active callbacks stay within permits. At 96 items the dependent waits behind older work but completes. The measured result supports retaining the current queue design at these scales; it does not establish an arbitrary task or duration limit. |

The unavailable-evidence disposition requires a peer explanation and permits
an optional linked follow-up. It is inspected service behavior; the fixture's
challenge is not a separate live model judgment. The connected revision and
execution-parent test are complementary evidence, not one end-to-end mission.

## Verification on 2026-10-08

`scripts/dev check` passed: **436 Python tests, 105 React tests and 20 shared
JavaScript contract cases**, plus Python lint, schema/type drift checks,
benchmark/skill references and the frontend production build. No provider or
E2B call was made. `scripts/dev desktop` also built the unsigned Mac bundle.

Native interface inspection opened **Previous samples → Open shared contribution
review → Communication** at the recorded contribution route. It confirmed the
prior critique, linked challenge, acknowledgment with unknown use, and earlier
and later explanation boundaries with omitted/pending detail. The bundled
snapshot matches the browser's saved example. This is native review of saved
records, not a live coordination run.

The final six-arm burst receipt uses the final audited executor and capacity
source hashes. At 96 items, total elapsed time was 12.89 seconds with one model
permit and 6.06 seconds with three. First accepted output was 266.0 and 290.8 ms
respectively; parallelism did not improve first-result latency in that pair.
These are single observations of controlled callbacks, with an intentional
1 ms precommit hold. The [measurement review](queued-work-bursts.md) records the
full table, definitions, source identities and limitations. Historical baseline
receipts remain separate; these numbers do not isolate the effect of the audit
fixes or imply scientific improvement.

Private verification receipts retain the source manifest, logs and local state
paths. Git HEAD alone is not the tested source identity because this checkout
contains uncommitted work. The original Stage 3 recorded example is preserved.

## Remaining proof boundary

Stage 4 connects normal Atlas start/follow-up to the shared mission loop and
executes the added live acceptance test. Its explicit review checkpoints are
transparency, visibility, fidelity and value. They require actual run evidence
and a reviewer assessment; they are not runtime approval gates. In-flight
process-kill recovery, live provider behavior, scientific advantage and
multi-hour endurance remain separate qualification work.
