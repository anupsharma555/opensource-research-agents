# Local state-recovery acceptance review

This is a bounded ANU-395 proof supplement for the SQLite store selected in
[the storage decision](../decisions/storage.md). The synthetic cases in
[`test_acceptance_review.py`](../../tests/state/test_acceptance_review.py)
exercise saved records and immutable files, not a live research host or a
scientific finding. Read it with the [Stage 2 verification guide](stage2-verification.md)
and the shared [compatibility procedure](../interfaces/compatibility.md).

## What the added cases establish

| Acceptance topic | Local observation | Boundary |
| --- | --- | --- |
| Exact completion replay | One completion request names an exact task, source input and configuration version. Replaying its operation ID after reopening returns the same accepted result and no second event; changing the configuration version under that ID conflicts. | The caller must supply these references; `StateStore` does not infer them from a task name or qualify the model's scientific work. |
| Cache and index loss | Active and completed **status records** each retain their exact configuration, source bytes, claim, report, dependency links, open peer request and pending event after a disposable cache is removed, an index is dropped/rebuilt and the store is reopened. The two source records share one content-hash file. | A completed status with an open request is deliberately preserved as data, not endorsed as a valid mission-completion decision. This is not a production cache service or a browser reconnect test. |
| Scoped rejection | A malformed artifact reference leaves a scheduled quota wake, waiting task, unanswered peer request, accepted report and pending event intact. The failed operation has an `IntegrityFault` diagnostic. | If the authoritative database itself cannot write, even a diagnostic may be unavailable; the caller must report that storage failure separately. |
| Capacity recovery | Injected `ENOSPC` during object staging names the `objects` path; injected `ENOSPC` before the outbox write names `state.sqlite3`. Neither accepts the attempted artifact or loses earlier work. The same intended operation succeeds after the injected fault is removed. | The filenames come from the controlled fault injection. They are not automatic production disk telemetry. The separate [`test_actual_sqlite_capacity_failure_rolls_back`](../../tests/state/test_store.py) exercises a real SQLite page-capacity error, but does not identify an operating-system disk-full path. |
| Stored-schema migration | A seeded v1 active mission with source, configuration, claim, report, links, open request and pending event survives an interrupted v1→v2 migration, then a successful migration and restore from a v1 backup. Exact IDs, versions and source bytes still resolve. | No mixed-version production host, power cut or active provider operation was run. |

The earlier [store tests](../../tests/state/test_store.py) separately cover
process termination at selected file/commit boundaries, uncertain responses,
lease replacement, duplicate delivery, backup verification and SQLite capacity.
The added cases connect the saved research records to those mechanics; they do
not turn ten logical mission hours into wall-clock endurance.

## ANU-389 change-impact handoff: stored schema v1→v2

| Item | Decision and checked result |
| --- | --- |
| Changed structure | [`ora.state.schema`](../../src/ora/state/schema.py) adds `review_requirements` and the rebuildable `record_kind_time` index in v2. Existing accepted record bodies, links, operation receipts, leases and outbox rows keep their exact IDs and versions. |
| Readers and writers | `StateStore.get/records/links/events`, the read-only inspector, and agent, retrieval, evidence and review-projection callers read the store. `StateStore.transact` and those domain writers create versions and events. Rebuildable indexes can be discarded; accepted SQLite rows and referenced files cannot. |
| Supported versions | The current store upgrades a v1 database to v2 in a transaction. A requested v1 reader refuses a migrated v2 store; there is no dual-version live-host qualification. Wire `schemaVersion: "1.0"` is a separate version and is not changed by this migration. |
| Failure and restore | A fault after the first v2 schema step rolls back to v1. The old records are readable at v1; a later v2 opening succeeds. A v1 online backup restores into a new verified v2 destination, with exact record links and pending event intact. See `test_v1_to_v2_migration_preserves_richer_active_work_and_restore` and the original migration/restore tests. |
| Deployment limitation | These results are local synthetic storage proof. A deployed host must inventory active readers/writers, pending requests and model manifests; retain an old-record fixture and backup; verify compatibility and continuation before switching versions. An incompatible action should fail locally without stopping unrelated research. Follow the shared compatibility procedure rather than adding a second migration policy. |

## Still open after this supplement

Reconciliation reports staged files, unreferenced objects and pending events.
New source-dependent links now check retained bytes along their exact dependency
path; the corruption tests preserve unrelated work and historical reads. The
combined two-provider, same-work, late-failure and open-request fixture is also
covered in the evidence handoff tests. The controlled workload measured growth and lookup over
ten **logical** hours in seconds of wall time; actual sustained host operation,
power-loss recovery, provider behavior and scientific validity remain future
proof. No mission-wide pause or arbitrary research duration follows from a
single damaged source or this local measurement.
