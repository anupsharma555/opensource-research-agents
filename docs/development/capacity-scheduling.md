# Offline capacity scheduling

`ora.coordination.capacity` is a fixture-tested admission boundary for future
research operations. It has no provider client, timer service or scientific
planner. A caller supplies a stable operation ID for each initial call,
continuation, repair or retry, with mission, run, task, attempt, call and budget
version references. The host selects only eligible resource work; agents choose
research steps and interpretations.

The scheduler stores `capacity_operation`, `capacity_quota` and
`capacity_config` records in the existing `StateStore`. Admission reads and
writes them in one short `BEGIN IMMEDIATE` transaction. Separate model,
retrieval and compute permits keep one blocked resource from occupying another.
Each operation lists every applicable scope, such as provider, model and shared
account. Retrieval and compute may use request-only scopes. A model estimate
reserves one request and conservative total tokens before dispatch. This
estimate must already include repeated conversation
history, tool results, anticipated reasoning and output; provider-reported
cached tokens belong in **observed total usage once**, not an extra charge.
`settle` records provider total tokens and any cached-token subset; cached
tokens are not added to that total again. Each `QuotaLimit` records whether its
ceiling is configured or observed. A
missing ceiling means external headroom is unknown; this ledger only covers
requests routed through this database, never other applications on the account.

An admitted operation owns a generation and a short lease. `dispatched` records
the boundary immediately before external execution. Expiry before dispatch
returns its unused reservation to the queue. Expiry or local timeout after
dispatch releases the local worker slot but retains external capacity and
conservative token charge until a new owner confirms completion, failure or no
effect. Old generations cannot settle. `settle_tx` may run inside the same
`StateStore.transact` action that accepts a board contribution, so result
acceptance and capacity release commit together. Provider/network work and
scientific validation must happen outside that transaction.
When the external response is known but local task acceptance fails,
`record_unaccepted_tx` records `known_unaccepted`, reconciles available actual
usage and releases the current owner's permit without claiming accepted science.
An observation from a stale owner remains available for the current owner to
reconcile; it cannot settle a replacement reservation. Unknown token usage and
unknown external completion are distinct facts: a missing usage total retains
the conservative charge even when completion is known.
The operation record accumulates queue, local quota, provider-capacity,
transport-retry and completed active durations from the supplied store clock.
An unknown external effect remains open rather than being assigned an invented
completion time.

`rate_limited` saves a 429 and Retry-After/reset delay, releases the inactive
permit and exposes a durable wake. If the response has no reliable usage total,
the attempt retains its conservative token charge; an explicit observed zero
reconciles it to zero. `due` can be read after restart or multiple
times; `admit` makes the actual state transition atomically. Window exhaustion
also saves a wake. A zero remaining-capacity header without reset information
keeps admission blocked until another observation supplies a condition; no
reset time is invented. A configured per-operation transport retry limit stops
repeated 429 attempts; research continuations use new operation IDs and have no
fixed call or duration cap. A canceled queued operation cannot dispatch, while
a canceled dispatched operation retains its external uncertainty until
reconciled. `transport_failure` distinguishes a confirmed temporary no-effect
failure, a terminal failure and an uncertain external effect; only the confirmed
no-effect path schedules a retry. The caller must establish this classification
from the actual provider/transport response. SDK retry settings belong to the caller;
the current optional Groq adapter uses `urllib` with one request and no hidden
SDK retry, with a 120-second HTTP timeout. Any later SDK adapter must explicitly
disable or account for its own automatic retries before using this host ledger.

The fairness rule prioritizes an already queued independent review and then
chooses the lane with the fewest dispatched attempts, breaking ties by queue
time and stable ID. The model review reserve holds normal admission when a
review is ready in the shared queue, including another mission. A future wake
or unavailable quota does not reserve an otherwise usable slot; the caller's
eligible-operation filter applies to reviews in its own mission. Cross-mission
readiness follows the enqueue contract: callers queue capacity work only when
it is eligible and must withdraw stale work. The capacity ledger cannot infer
another mission's changed scientific dependencies. This is mechanical access
fairness, not relevance ranking. It does
not preempt already active calls. A live worker making no useful progress needs
an explicit diagnosis/replan opportunity: `heartbeat` extends its lease while
separately recording whether useful progress occurred; `diagnose_stalls` saves
the no-progress code at a caller-supplied threshold. It does not cancel the
call. A long latency category does not by itself cancel scientific work.

`admit(owner, kind, mission_id=...)` limits selection to that mission's queued
operations. All missions still count against the same persisted permits and
applicable shared quota scopes.

Run `.venv/bin/python -m pytest tests/coordination/test_capacity.py` after
`scripts/dev setup`. These fixtures use a virtual clock, independent SQLite
connections and two local processes contending for the final request allowance.
They prove local admission, reconciliation and restart behavior only. They do
not measure actual Groq limits, outside account use, live scientific usefulness,
multi-host SQLite behavior or a sustained mission.

| Virtual-clock fixture | t=1000 | Later observation |
| --- | --- | --- |
| Ten workers, model C=1 | One model admitted; nine wait; retrieval and compute admit separately | Completion releases the model permit for another queued worker. |
| 429 and restart | Model call records a 15-second wait; its permit is free; retrieval admits | At t=1015 a new scheduler reads the saved wake and admits the model step once. |
| Expired dispatched owner | External reservation remains while local worker becomes free | New owner reconciles confirmed usage; the old generation cannot settle. |

These are deterministic fixture timestamps, not observed provider latency or an
end-to-end service-level claim.
