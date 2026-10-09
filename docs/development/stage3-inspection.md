# Inspecting the offline Stage 3 collaboration fixture

Create a fresh, private fixture from the repository root:

```sh
.venv/bin/python -m ora.coordination.demo
```

The command prints a new `.local/stage3/demo-<id>/state` directory, a
`snapshot_id`, and `.local/stage3/demo-<id>/snapshot.json`. It refuses an
existing state directory. The JSON is the canonical `ResearchSnapshot` read
back from `StateStore`, using the same shape as the Atlas research review.
It is an **offline test**, not a live mission, provider run or scientific
finding. The fixture uses one synthetic input record and three registered
local callbacks: two overlap, and a dependent callback begins while a slower
sibling is still active. The accepted artifacts, receipts, board outcomes,
capacity state, trace events and exact actor/configuration/input references
are saved in one shared database. A saved proof record retains the observed
monotonic callback intervals and the two overlap checks.

The snapshot shows three completed callback attempts, one deferred proposal,
and one delivered and acknowledged peer question that remains unanswered.
It has no source-study view, claim, report artifact or selected model. In
particular, `artifacts=[]`, `mission.reportArtifactId=null`, and the mission
remains active. `attempts[].selectedModel=null` and returned reasoning is
absent. The callback payloads are saved as `executor_artifact` records; the
reader-facing attempt summaries and saved steps state only the observed local
result. Diagnostic records retain exact versions and operational lineage.
They do not convert fixture results into a scientific report.

Use the paths printed by the creation command for read-only inspection:

```sh
.venv/bin/python -m ora.coordination.inspection --state-dir .local/stage3/demo-<id>/state --snapshot-id stage3-offline-concurrent-snapshot-v1 --view all
.venv/bin/python -m ora.coordination.inspection --state-dir .local/stage3/demo-<id>/state --snapshot-id stage3-offline-concurrent-snapshot-v1 --view steps --after 0 --limit 10
.venv/bin/python -m ora.coordination.inspection --state-dir .local/stage3/demo-<id>/state --snapshot-id stage3-offline-concurrent-snapshot-v1 --view diagnostics
.venv/bin/python -m ora.state.inspect --state-dir .local/stage3/demo-<id>/state --snapshot stage3-offline-concurrent-snapshot-v1 --view findings
```

The Stage 3 inspector opens `StateStore(read_only=True)`. `--view all` returns
the same saved JSON that Atlas can consume. `--view steps` pages the
snapshot's saved step sequence with `nextCursor` and `hasMore`; it does not
resume work. The shared `ora.state.inspect` command reads the same exact
snapshot and can select findings, sources or diagnostics. A refresh or page
read performs no callback, provider call, message delivery or database write.
Step paging currently slices the loaded saved snapshot; large-run read cost
has not been measured or optimized by this fixture.

The diagnostic projection uses an allowlist. Private instruction text and
unrestricted callback payloads are excluded from the general snapshot;
their hashes and safe recorded summaries remain inspectable. Source rights,
model permissions and returned reasoning cannot be acquired by following a
message or trace reference. The fixture intentionally uses no actual source
publication or provider-returned reasoning. This tests the inspection
boundary, not the scientific adequacy of a model or evidence corpus.

The focused test `tests/coordination/test_inspection.py` compares the exported
JSON, both CLI readers, paged steps, exact saved versions, no-report state,
and before/after database bytes and events during refresh. It also checks
the private synthetic instruction marker is absent. UI display still needs
the Atlas entry point to load this generated JSON; the saved contract and
read-only CLI are usable without starting a service.
