# Concurrent local execution fixture

`ora.coordination.executor.ConcurrentExecutor` joins peer-selected board work,
the capacity scheduler, mailbox and trace journal. It is an offline-capable
component, not the complete mission loop or a scientific planner. A trusted
host registers a callback under an explicit handler ID and implementation
version. A peer or caller supplies `WorkSpec`: exact mission/run, proposal,
actor, saved configuration, input versions, resource kind, quota scopes and
attempt identity. A supplied agent call also pins its exact saved version.
The host never chooses a research question, interpretation
or next scientific step.

`submit` saves the intent once. `run_ready` checks the board's current
dependencies and enqueues only ready work. Capacity admission and board claims
are separate short SQLite transactions; a lost claim yields an unused permit.
Callbacks run outside all database transactions. The executor records
`dispatched` immediately before invoking one, so a crash after that boundary
leaves an uncertain external effect rather than silently repeating it. A
pre-dispatch expiry returns an unused reservation to the queue under a new
owner generation. Heartbeats renew live leases without imposing a scientific
duration cap.

On a confirmed callback result, `board.accept_result(save_related=...)` commits
the artifact, result receipt, local peer messages and delivery intents, trace,
capacity settlement, executor status and board outcome together. The
`related_request` descriptor includes exact input and configuration references,
handler implementation version, and canonical work/result hashes. A repeated
submission or wake does not create another accepted result. A proposal revision
or lost claim rejects the old worker's result. A newer scientific input version
alone does not: work may retain an explicitly frozen, valid older input, and
its accepted artifact identifies exactly what it used. ModelSession separately
checks current source-use restrictions before dispatch.

If a callback returned but its local result could not be accepted, the executor
saves a separate observation (`executor_unaccepted_result`) with exact inputs,
the returned result or matching model receipt, and a rejection reason. The work
is `result_unaccepted`, not completed. The observation distinguishes a confirmed
external result (`known`), a saved HTTP 429 rejection (`known_rejection`), a
request never sent because credentials were unavailable (`no_request`), and
unverified completion. A timeout or interrupted stream retains uncertainty even
when its diagnostic receipt was saved. Other HTTP errors after a callback fault
remain unverified until explicitly reconciled. No recovery classification
silently schedules another call. A current capacity owner can
record a known outcome as `known_unaccepted`, reconcile observed usage and
release the permit without accepting scientific work. The matching board claim
is released when it is still owned by this worker. A stale owner preserves the
observation but cannot release a replacement owner's reservation; the current
owner must reconcile it. Reopening or repeated wakes do not repeat that callback.
An exception without a saved matching response remains uncertain unless the
returned `WorkResult` explicitly classifies a confirmed no-effect retry,
terminal failure or provider 429. Saved observations and events keep the result
available for peer review without exposing private payloads in ordinary views.
For a pinned model call, the result must include one exact
`model_response_receipt` reference whose saved call version and configuration
match the WorkSpec. Its exact saved request intent and request digest must
also match. This links the selected-model bridge's capture to the
executor's accepted result; a receipt precheck alone is not dispatch ownership.
The work's source-copy references must match the exact pinned call and saved
request, so its artifact and trace cannot omit a source actually sent to the
model. Missing or substituted copies are rejected before a new submission
dispatches. An already captured response remains available for accounting if
later acceptance fails.

The loop rescans ready work whenever any callback finishes or a submission
notifies it. Executor instances in the same process and using the same resolved
database path share these notifications. A newly submitted independent task can
use a free slot while a slow sibling remains active. A trusted host changing
other board readiness may call `notify` after committing that change. This is
not a cross-process event service, durable timer or continuously running host;
those mechanisms must replay persisted work after restart. A dependent may
start after its parent commits while a slow unrelated sibling remains active.
If no eligible work can run, `run_ready` returns saved work phases, open board
proposals, unresolved peer requests and the next known wake. It reports the
mission as open; it does not infer mission completion from one callback ending.
The persisted configuration reference remains pinned across restart. A missing
registered handler version leaves work pending for that exact implementation.

Accepted receipts and traces record total elapsed, queue access, local quota,
provider-capacity, transport-retry and active execution durations from the
store clock. The timing basis is saved as `store_clock`; fixture clocks are
virtual, and these durations are not a provider latency or throughput claim.
Peer waiting is explicitly marked unmeasured by this executor. The mailbox's
request timing remains a separate source of truth for peer delivery and review.

The selected write approach uses the existing `StateStore` connections and
short atomic transactions. Model/helper execution occurs outside those
transactions. A dedicated writer queue would add acknowledgment and crash
states without evidence that this local fixture needs it. No database or
language replacement is required. The fixture demonstrates concurrent work
with serialized SQLite commits; it does not imply simultaneous writes.

Run `.venv/bin/python -m pytest tests/coordination/test_executor.py` after
`scripts/dev setup`. The tests prove two callbacks overlap, a fast dependent
commits before a slow sibling finishes, local commit rollback leaves no partial
accepted artifact/message while preserving a known returned result, a 429 frees
a permit for a healthy provider route, and
pre-dispatch versus uncertain post-dispatch restart behavior. They use
synthetic callback results and local SQLite state. They do not prove live
provider capacity, scientific quality, remote delivery, multi-host database
operation, sustained uptime, or measured lock/WAL/storage performance.
