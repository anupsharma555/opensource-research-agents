# Stage 3: collaboration and execution boundaries

The implemented slice connects peer-owned research work, resource admission,
concurrent callbacks, durable messages and inspectable model turns. It extends the
existing Python/SQLite foundation. It does not replace the optional live Atlas
service or install a production mission supervisor.

## What is implemented

| Component | Responsibility | Observable check |
| --- | --- | --- |
| Capacity scheduler | Separate model, retrieval and compute permits; per-call provider/model/account reservations; durable waits and owner generations | Ten queued workers at one model permit, independent resources, two-process contention, 429 recovery and uncertain-effect reconciliation |
| Proposal board | Peers propose, negotiate, claim, disagree, revise and reopen work; the host checks identity and versions | Competing claims, stale invitations/results, preserved alternatives, scoped correction and compound-commit rollback |
| Peer mailbox | Save messages and delivery intent together; distinguish delivery from a reviewed answer; target subscriptions and recover saved cursors | Acknowledged-but-unanswered requests, partial/declined/replaced answers, offline-requester replacement and lost-notification replay |
| Concurrent executor | Run registered independent callbacks outside database transactions; accept related local effects together | Independent overlap, a fast dependent running before a slow sibling finishes, exact model-call versions, cancellation/retry/restart and no duplicate accepted effects |
| Selected-model adapter and session | Use the saved model, reasoning settings, source versions and tools; retain request intent, response, partial status and usage | Offline protocol and source-use fixtures, duplicate-dispatch prevention, atomic capture, backup/restore, and a separate bounded live protocol check |
| Trace and review projection | Explain accepted activity through exact records; keep reports and restricted diagnostics separate | Read-only Atlas/CLI projection, missing-parent and privacy cases, typed usage and paged disconnected-viewer recovery |

The [capacity guide](capacity-scheduling.md), [peer collaboration guide](peer-collaboration.md),
[executor guide](concurrent-execution.md), [model adapter guide](model-adapter.md)
and [trace guide](tracing.md) describe the concrete interfaces and failure paths.
The [disposable demo](stage3-inspection.md) provides a navigable saved run without
manufacturing a scientific report or model invocation.

## Why these boundaries were chosen

Scientific choices stay with peers. The proposal board preserves disagreement
and evidence-linked revisions rather than appointing a scientific planner.
Mechanical checks protect exact versions and ownership; an invalid operation
returns a scoped result while other eligible branches continue. A recipient's
acknowledgment does not resolve an evidence request. Replacement review requires
saved host availability information and an independent qualified role.

SQLite serializes short write transactions, while callbacks and provider calls
can overlap outside them. Results, message delivery intents, traces and capacity
settlement are accepted together. External execution cannot be made exactly once
by a database transaction: a durable attempted marker and retained reservations
preserve uncertainty until the host reconciles the outcome. New research turns
have their own call identities. A bounded transport retry policy is not a cap on
scientific iteration or mission duration.

The [write comparison](concurrent-write-measurements.md) compares a representative
full-snapshot transaction with small record updates and per-worker connections.
It favors existing short transactions for this slice. These are disposable local
measurements informed by inspected code, not measured production acceleration.
No writer queue, framework replacement or database migration is justified by this
evidence. The earlier [runtime decision](../decisions/runtime-comparison.md)
remains provisional and should be revisited when a measured need changes.

The session bridge reconstructs a request from the exact saved configuration and
call manifest. Permitted abstracts are valid inputs. Missing full text does not
invalidate an abstract's explicit report. Each copy still needs the host's saved
model-use decision; cached or peer references do not override an explicit use
restriction. Newer source versions do not silently replace a pinned input, and a
new head alone does not prevent use of a valid frozen version.

## Verification and limits

`scripts/dev check` is the shared offline validation command. Focused suites live
in `tests/coordination` and `tests/models`; retained private receipts identify the
exact uncommitted source manifest and command results. The write and diagnostic
measurement scripts use new disposable databases and preserve historical state.

A separate selected-model protocol check made five successful Groq requests:
four nonstreamed requests covering two dependent tool turns and strict final
output, plus one streamed tool request. All returned the selected GPT-OSS model;
requests used high reasoning with returned reasoning enabled. The observed total
was 4,196 tokens. The check established protocol behavior on a tiny fixed packet,
not scientific role competence, actual billing, shared-account capacity, broad
retrieval, or multi-hour endurance. A preliminary sandbox transport failure had
no returned HTTP response or observed usage. Later bridge changes were tested
offline; this report does not imply another live check of those changes.

The complete mission loop, authenticated remote transport, context recovery over
hours, real retrieval-provider qualification and sustained scientific runs remain
later integration work. A2A and MCP are optional interoperability directions;
no remote protocol adoption is claimed. The executor exposes durable waits and
ready work, but a continuously running host must still arrange timer wakeups and
reconcile uncertain external calls. Atlas's saved fixture is read-only review,
not evidence that the production host is running.
