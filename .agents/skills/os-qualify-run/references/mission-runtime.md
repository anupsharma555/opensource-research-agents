# Inspect the mission runtime's saved records

Read this reference when the selected run uses the repository's mission service
and SQLite store. These commands and the helper were introduced in Stage 4;
they can support later-stage runs that retain the compatible record shape.
They are not a generic reader for every earlier or future runtime.

## Confirm support before invoking commands

Check for `src/ora/mission/`, `docs/development/stage4-local.md` and
`docs/development/stage4-atlas.md`. Read those guides and the current mission,
context and provenance documentation for the chosen checkout. Confirm the
selected store has the `records`, `heads` and `links` tables and the requested
`mission_run` record before using the receipt helper. Its model/tool/context and
snapshot counts are specific to the record kinds in `scripts/receipt.py`.
New or unrecognized kinds are not automatically covered.

If prerequisites or a compatible store are absent, report this command path as
unavailable. Continue the shared diagnostic workflow with documented read-only
inspection for that runtime or a private manual receipt; do not create a store,
launch a host or interpret unsupported records as zero activity.

## Read saved work without dispatching it

From the repository root:

```sh
scripts/dev mission status --state-dir DIR --run-id RUN_ID
scripts/dev mission calls --state-dir DIR --run-id RUN_ID
.venv/bin/python .agents/skills/os-qualify-run/scripts/receipt.py --state-dir DIR --run-id RUN_ID
```

Repeat `--run-id` for an explicitly selected parallel batch in the receipt command.
Keep call records with raw responses private. The helper writes one mode-0600
JSON under ignored `.local/checks/` and prints only its path. It uses a WAL-aware
read-only SQLite transaction, metadata and small exact linked tool-result blobs.
It never reads source, model or snapshot blob contents or scans all object bytes.

Reconcile its output with available runner `manifest.json`,
`qualification-status.json`, per-case `observation.json`, `last-state.json`,
`calls.json` and saved snapshots. An observation window is not mission termination.
A run labeled `completed` can still have `continuingTaskIds` or other host work
consuming resources. Preserve the attempt's selected configuration; the currently
selected reasoning/tool/text path is Groq `openai/gpt-oss-120b` with high reasoning.
Do not apply that setting to a different explicitly configured vision or future run.

## Interpret the helper's limits

- Prepared calls, dispatch markers, captured responses, usage-bearing responses
  and saved tool outcomes are separate counts. A marker is the saved pre-dispatch
  boundary, not provider billing proof. Missing usage remains unknown.
- Source-copy counts cover referenced copies, not every discovered or inspected
  source. Inspect retrieval/source-inspection records for coverage and actual
  passages for claim support. Saved tool-result statuses do not cover every
  attempted action; inspect exact calls/tasks for rejected or missing results.
- Snapshot counts separate legacy full blobs, manifests and exact linked component
  records. Unique referenced-object totals count shared hashes once; logical
  references count each snapshot's use. Missing component records or metadata
  leave complete totals unknown. Manifest-to-link completeness and physical bytes
  remain unverified by this helper.
- Selected-run references, control revisions and whole-store SQLite files describe
  different scopes. The latter can include other runs and WAL activity. A current
  size measurement alone is not before/after optimization proof.

## Only when a new live test is authorized

The existing `scripts/qualify_stage4_mission.py` requires `--allow-live`. It defers
new submissions while work remains unless the operator deliberately selects
`--allow-overlap` for a parallel batch. Neither option changes agent lifetime.
Verify current runner help and task authorization before invoking it. This is the
existing Stage 4 runner, not automatic coverage of later scientific/endurance
cases. Do not call it merely to generate a diagnostic receipt.
