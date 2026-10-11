# Stage 4 snapshot storage

Stage 4 saves an immutable `ResearchSnapshot` whenever the exact meaningful
mission state changes. The old layout saved the entire cumulative projection in
one blob. In the inspected local state, 13,965 such snapshot blobs referenced
16,986,976,581 bytes. The largest was 2,512,762 bytes; its ordered `events`
array occupied 2,484,706 bytes across 14,067 entries. This is a read-only size
observation, not a claim about source-copy growth or future workload frequency.

New snapshots keep the same IDs and public `ResearchSnapshot` JSON. Their
internal `research_snapshot.body.blob` is a versioned manifest containing exact
scalar values and ordered references to 64-entry array chunks. Each chunk is an
immutable `snapshot_component` record whose top-level `body.blob` names the
content-addressed bytes. Consecutive snapshots reuse unchanged chunks, while a
changed tail chunk gets a new hash. The manifest includes every event, source,
report, task and attempt value; there is no trimming or data cap. Source and
attempt provenance, exact detail references, event cursor and parent relation
remain in the snapshot record. The reader reconstructs the same array order
before applying the existing inspection and redaction policy.

`snapshot_component` records are internal global byte objects. They carry no
mission authority. A mission-owned accepted snapshot names its exact components
through `snapshot_part` links; only the snapshot reader exposes the reconstructed
view. Component creation emits no event and is absent from the meaningful-input
fingerprint. Existing full-blob records have no `storage_format` field and remain
on the legacy read path. No old record or object is rewritten.

The component records and snapshot record commit in one SQLite transaction after
their immutable bytes have been installed. Readers can only observe a complete
accepted manifest; a failed transaction may leave unreferenced content
objects, which reconciliation reports without deleting them. Concurrent writers
reuse the same hash, and a collision with different saved metadata is rejected.
The existing backup, restore and reconciliation paths discover each component's
top-level blob. A corrupt or missing component makes that snapshot unreadable
with an integrity fault; unrelated records remain available. Parent and child
snapshots continue to resolve by their exact saved IDs.

## Reading saved views

`load_snapshot` reconstructs the immutable snapshot, then adds the inspection
records used by Atlas and the command-line reader. One fresh, query-only SQLite
transaction resolves those records and event explanations. Repeated requests
for the same kind, ID and version reuse the decoded record within that view;
the reader and its connection are discarded on return or error. The saved event
cursor still bounds the displayed history, and the existing mission visibility,
field allowlist and redaction rules still apply.

This reuse is deliberately limited to one read. A later refresh checks current
review requirements and rechecks referenced blob integrity even when the
snapshot ID has not changed. It does not rewrite saved findings, cache a view
between workspaces or skip missing-record diagnostics. Immutable component
reconstruction retains its separate bounded connection and hash checks.

`tests/state/test_inspection_reads.py` covers connection/query reuse, reader
cleanup, concurrent review updates, refresh after source damage and workspace
separation. Saved-read timing and exact-output comparisons belong in private
qualification receipts; they do not by themselves prove running-host adoption
or native viewer latency.

## Disposable measurement

The offline script at
`.local/stage4/followup-20261010/storage/measure_snapshot_storage.py` starts
both modes from the same synthetic recorded workflow and saves 24 successive
views with 4,000–4,023 ordered events. The legacy mode reproduces the previous
full-blob record layout from validated fixture views. The component mode uses
`save_snapshot`. The script checkpoints each disposable SQLite database before
measuring file sizes. Results are in the private `measurement.json` beside the
script.

| Added 24 snapshots | Legacy full blobs | Component manifests |
| --- | ---: | ---: |
| Added deduplicated snapshot/component blob-reference bytes | 18,597,350 | 1,200,384 |
| Added object-file bytes | 18,597,350 | 1,200,384 |
| Added SQLite file bytes | 352,256 | 1,351,680 |
| Combined added bytes | 18,949,606 | 2,552,064 |
| Last-view read, warm-cache median of five | 371.28 ms | 340.54 ms |

The combined fixture growth fell about 86.5%. The measured read times overlap
and do not establish a latency improvement. These are logical file sizes from a
small, synthetic local fixture, not allocated disk blocks, provider throughput,
live Atlas performance or an endurance result. Repetition patterns, SQLite page
layout and operating-system caches can change both ratios and read times.

Focused tests cover legacy and new reads, exact ordered reuse, repeated
meaningful progress, unchanged persistence, concurrent saves/readers, rollback,
backup/restore and component corruption. The real interface and production host
were not exercised by this storage change.
