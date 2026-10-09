# Stage 3 local write measurement

This is a disposable offline measurement for concurrent execution, recorded on 2026-10-08.
It compares three write shapes under the same synthetic event increments.
The machine-readable receipt is
`.local/stage3/evidence/write-measurements-97329cd7ac43.json` (ignored local
state). Re-run from the repository root with:

```sh
.venv/bin/python scripts/measure_stage3_writes.py --sizes 30,100,300 --workers 4 --payload-bytes 200
```

The command chooses a new ignored output path and creates temporary databases
outside existing live state. The receipt saves Python/SQLite/platform versions,
the sample and script hashes, workload parameters, per-trial timing summaries,
database/WAL sizes and checkpoint results. No provider, model, network call,
actual ResearchHost instance or saved live database was used.

## Inspected boundary

The React live view polls an exact run ID about every 1.5 seconds and sends
start/follow-up requests to the local server. The server invokes
`ResearchHost.start` or `followup`. The host's `ThreadPoolExecutor` can run
separate runs, but `_execute` processes one run's peer queue serially.
`provider.complete` runs outside the host's process lock. `_mutate` holds
`ResearchHost._lock` while opening a connection, reading the current full
snapshot, applying a change, validating the whole `ResearchSnapshot`,
serializing it, inserting a new snapshot and updating the run pointer.
Some read paths also hold this lock while decoding or validating a snapshot.
The shared `LiveProvenance` StateStore opens the same SQLite file in WAL mode.

The measurement's **modeled full-snapshot arm** copies the actual
`_mutate_locked` read/validate/serialize/insert/update sequence and uses the
repository's valid synthetic 19,253-byte sample snapshot. It uses only the
two tables needed for that sequence, rather than constructing the full live
host. The other two arms use real `StateStore.transact` calls, writing one
small record and its event intent per logical update. One adds a process
lock; one lets worker connections compete directly for SQLite's one writer.
They all use four threads, a fresh store at each of 30, 100 and 300 updates,
the same 200-character synthetic event increment and a separate connection
to confirm each exact committed version is visible. These shapes differ in
schema and metadata overhead; the numbers are not a controlled production
before/after test.

## Observations

Times are milliseconds at the 95th percentile unless labeled throughput or
bytes. `Lock` is wait for the optional Python process lock. `Admission` for
record arms includes connection opening and SQLite `BEGIN IMMEDIATE` wait;
it is **not** isolated SQLite lock wait. `Txn` spans first SQL write through
commit in the snapshot arm, and StateStore action entry through commit in
the record arms. `Visible` is time from commit acknowledgment until an exact
version is returned by a new local reader, including its connection/query
work. Every exact-version read succeeded.

| Updates | Write shape | Updates/s | Lock p95 | Admission p95 | Txn p95 | Visible p95 | DB after checkpoint | WAL before checkpoint |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 30 | Modeled full snapshot | 1,636 | 1.94 | — | 0.19 | 0.44 | 680 KB | 1.20 MB |
| 30 | Short records, process lock | 861 | 2.97 | 1.04 | 0.41 | 1.14 | 156 KB | 1.78 MB |
| 30 | Short records, worker connections | 650 | — | 7.40 | 1.10 | 3.74 | 156 KB | 1.78 MB |
| 100 | Modeled full snapshot | 1,380 | 2.45 | — | 0.25 | 0.52 | 3.39 MB | 4.17 MB |
| 100 | Short records, process lock | 908 | 2.69 | 0.96 | 0.39 | 1.01 | 270 KB | 4.15 MB |
| 100 | Short records, worker connections | 660 | — | 4.84 | 1.06 | 3.64 | 270 KB | 5.00 MB |
| 300 | Modeled full snapshot | 884 | 6.49 | — | 0.32 | 0.41 | 20.51 MB | 4.19 MB |
| 300 | Short records, process lock | 915 | 2.71 | 0.96 | 0.40 | 1.02 | 537 KB | 4.19 MB |
| 300 | Short records, worker connections | 580 | — | 3.37 | 1.09 | 3.62 | 537 KB | 18.89 MB |

The snapshot row count is updates plus the initial snapshot; every snapshot
retains the growing event list. The short-record arms retain one versioned
record and one event per update. At 300 updates, the full-snapshot file was
about 38 times larger after checkpoint than either short-record file. This
growth result reflects this fixture and the full-history snapshot shape.
The modeled full-snapshot path was faster at 30 and 100 updates. At 300,
throughput for the process-lock short-record arm was close to the snapshot
arm, while unconstrained worker connections were slower and spent longer
entering their transactions. Run-to-run timing and WAL sizes varied; the
receipt represents one run, not a stable benchmark distribution.

The receipt includes `PRAGMA page_count`, page size, passive and truncate
checkpoint return values, elapsed checkpoint time and bytes before/after.
With no long-lived readers in this fixture, the truncate checkpoint returned
`busy=0` and reduced WAL bytes to zero in all nine trials. Its measured
elapsed time was under 1 ms in this run. This does not establish checkpoint
behavior under active readers, power loss, a different filesystem or a cloud
deployment. The post-ack visibility result does not include the React poll
interval, network transport or UI render time.

## Implementation decision

Use the existing Python/SQLite `StateStore` and short atomic writes for
independent accepted contributions, messages and events. Keep model/provider
work and other expensive independent work outside any write lock. The
process-lock short-record arm is a reasonable single-process admission
policy if actual executor contention resembles this workload; SQLite still
serializes writers, and the process lock is not required for correctness.
Worker-owned connections without a process lock remain viable, but this run
did not show a latency or throughput advantage for them. Version conflicts
must still return specific receipts rather than overwrite a peer's decision.

A dedicated writer queue is not warranted by this measurement. It would add
another pending/committed distinction, overload policy and crash-recovery
state. If later traces show sustained writer contention, measure that path
against the same accepted-write and reader-visibility conditions before
adding it. The current local live host's full-snapshot path remains a
separate implementation; this fixture does not prove that changing it now
would improve user-visible latency or scientific quality.

The next integration proof must time actual concurrent agents, provider and
quota waits, commit duration, reader visibility and SQLite checkpoints in the
selected deployment. It must retain exact accepted input/output versions,
conflict and restart receipts. This measurement alone does not demonstrate
same-run model overlap, long-duration reliability or a production mission host.
