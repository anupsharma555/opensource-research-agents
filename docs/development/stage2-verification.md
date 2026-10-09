# Verify the Stage 2 recorded foundation

These commands inspect or generate **local recorded-data evidence**. They do
not qualify a live provider adapter, a scientific finding, production uptime
or ten-hour wall-clock endurance. Follow [setup](setup.md) for the pinned
Python environment; use a fresh ignored state directory for each generation
or scale probe. The commands below match the current `ora.state.demo`,
`ora.state.inspect` and `ora.state.probe` module entry points.

For queryable database views, copy-tested migration and the fresh demonstration,
see [state inspection](state-inspection.md). The separate [live provenance](live-provenance.md)
guide covers actual-call records without treating this fixture as live execution.

## Inspect the saved parent and follow-up

The recorded workflow uses a simulated clinical benchmark, nine registered
agent instances, pinned call configurations, retrieval receipts, accepted source
copies, versioned findings and a parent-to-child follow-up. Prepared call
manifests identify the inputs available to each attempt; they are not model
responses. The exported parent and child are the fixed projections that
Research Atlas loads in **Open recorded Stage 2 review** or
`?recorded=stage2`.

To build a separate local copy from the repository root:

```sh
.venv/bin/python -B -m ora.state.demo \
  --state-dir .local/stage2/demo-state-new \
  --export-dir .local/stage2/demo-export-new
```

Choose directory names that do not already contain a different workflow. The
command prints JSON with `proof` set to `recorded_offline_integration` and
`provider_calls` set to `0` when it completes. Inspect the accepted child snapshot or current domain
records through the read-only Codex/terminal reader:

```sh
.venv/bin/python -B -m ora.state.inspect \
  --state-dir .local/stage2/demo-state-new \
  --snapshot stage2-recorded-child --view sources
.venv/bin/python -B -m ora.state.inspect \
  --state-dir .local/stage2/demo-state-new \
  --snapshot stage2-recorded-child --view diagnostics
.venv/bin/python -B -m ora.state.inspect \
  --state-dir .local/stage2/demo-state-new \
  --kind source_copy --limit 100
```

`--view findings` selects the accepted report projection; `--view all` returns
the whole saved snapshot. `--view diagnostics` includes `inspection.steps`,
which explains recorded actions, outcomes and exact references. `--kind` pages
current records by stable ID with
`--after-id`; an exact snapshot reference stays pinned to its saved version.
The reader opens SQLite read-only and never starts a mission or invokes a
provider. The Atlas exports are generated artifacts: inspect a fresh export
and update the parent and child together through the generator workflow in
`ui/public/stage2-recorded/README.md`, never by hand.

## Focused local checks

The smallest repeatable Stage 2 checks use the pinned interpreter and avoid
network, model and sandbox calls:

```sh
.venv/bin/python -B -m pytest -q -p no:cacheprovider \
  tests/state tests/agents tests/retrieval tests/evidence
.venv/bin/ruff check src/ora/state src/ora/agents \
  src/ora/retrieval src/ora/evidence \
  tests/state tests/agents tests/retrieval tests/evidence
```

The fixtures exercise failed file and metadata writes, uncertain committed
responses, parallel identical-byte writes, mission-scoped lease replacement,
repeatable outbox delivery, rollback on an actual SQLite capacity error,
process termination at selected commit boundaries, migration rollback,
backup/restore, corruption reporting, exact source and claim links, rights
limits, correction scope and parent/child inspection. The inspection regression
also checks complete source and attempt provenance, each copy's supplying
encounter, and recovery after one source file is damaged: dependent acceptance
fails while unrelated work remains available. Retrieval fixtures retain valid
usage reported with failed or quota-limited responses. A passing fixture proves
only its asserted behavior in that local setup. In particular, a saved
citation and a `checked` source copy do not establish scientific entailment.

The retrieval/evidence checks also retain mapped author metadata and permitted
structured table cells, with explicit omission receipts for unsupported or
rights-limited fields. A saved 20-of-21 page keeps all first-page hits and the
known unseen result; a cursor page with unknown total does not claim exhaustion.
Two synthetic routes to one DOI remain distinct encounters, while repeated
handoff of one exact hit reuses its encounter. The evidence lookup fixture times
actual SQLite query/fetch and an explicitly requested retained-file read; a
caller-supplied monotonic enqueue timestamp is needed to measure queue time.
The separate scale workload below did not measure scheduler queue time.

For a repeatable controlled local workload, use another fresh local directory:

```sh
.venv/bin/python -B -m ora.state.probe \
  --state-dir .local/stage2/scale-state-new \
  --output .local/stage2/scale-result-new.json
```

The saved local result inspected for this guide reported **960 accepted
transactions**, **3,840 records**, **960 pending peer requests**, **960 pending
outbox events**, **16 parallel collectors** on **8 executor workers**, **4
unique content files**, two restarts/index rebuilds, and a verified
backup/restore. The median and maximum three-record lookup measurements were
**1.447 ms** and **2.724 ms**. Its event clock spans **10 logical mission
hours**, while execution took **4.888 seconds of wall time**. It made **zero
provider calls**. These are one local recorded workload's measurements, not
performance targets or ten hours of actual operation. Scheduler queue time,
power-loss behavior, provider reliability and scientific validity were not
measured.

## Integrated verification on 2026-10-08

The final `scripts/dev check` completed successfully: **227 Python tests**,
**20 JavaScript contract cases**, **59 React tests**, canonical-schema and
generated-type checks, Ruff, TypeScript and the production frontend build.
The Stage 2 subset accounted for **106 tests**. One earlier UI assertion
was corrected to inspect the intended waiting attempt after the new saved-step
view made a page-wide text match ambiguous. Native review then found that ID-only
history filtering hid revised attempts. Version-aware classification and its
regressions now keep those updates visible. Retrieval-step regressions also
preserve failed, malformed and empty outcomes from the canonical envelope.

The paired recorded exports contain one source and 38 steps in the original
snapshot, then four sources and 77 steps in the follow-up. Every displayed step
reference resolves to an included exact record version. The command-line source
and diagnostic views matched the exported Atlas records. The original history
does not contain the follow-up's later retrievals.

The Mac bundle built successfully with those same assets. Native review verified
the saved report, exact citations and retrieval receipts, named requests/replies,
prepared configuration and inputs, failed/waiting states, saved steps, and
parent-versus-child comparison. The final targeted recheck displayed seven
current attempts including the two revised attempts, with only the unchanged
attempt under parent history; original timestamps were preserved. Empty skill
objects explicitly show that no fields were recorded. The app remains on the
recorded review; the separately running live host and its state were untouched.

These checks apply to the current uncommitted local
checkout; they are not a published release receipt. No Stage 2 provider or model
call was made. Live adapter conformance, scientific usefulness, production
authorization, power-loss recovery and actual multi-hour endurance remain
separate qualification work.
