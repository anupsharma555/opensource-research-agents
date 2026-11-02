# Continue a closed evaluation without losing its history

## Choose a checklist outcome before a run

Before a new evaluation or continuation, record the exact acceptance item and
its required conditions, the missing evidence, the selected case, and what the
result will decide. Reuse qualifying saved evidence for conditions already
proved. A precise diagnostic should resolve one named uncertainty; a full run
should exercise the complete natural task. Give either enough allowance to
observe its outcome, without treating an estimated call count as a stop rule.

Prefer one coherent natural task for related checklist conditions that share
sources, collaborators or an output. Declare and score each condition separately;
one run's overall status must not pass or fail every item automatically. Use a
focused diagnostic when a concrete failure needs isolation or a condition was
not exercised, then return to the integrated task where safe. Checklist-focused
testing does not require one live run per checkbox.

Keep the review criteria outside worker input. After execution, link each claimed
condition to exact sent inputs, accepted effects and reviewed outputs. Distinguish
pass, failure, partial evidence, unexercised behavior and provider-blocked
observation. A passing offline mechanism check supports readiness; it cannot
close an item requiring natural model behavior. Complete every condition before
proposing checklist closure, and preserve any separate tracker-update boundary.

For repeated source reads, compare the saved reader result with the actual next
model input after completed tool turns are shortened. A correct reader result
does not prove delivery of its continuation, section navigation or repeat-work
feedback. Test compact same-copy navigation under the real request budget and
current source permissions before a live rerun; distinguish omitted cues from a
model ignoring cues that were delivered.

Score the named condition before judging the run as a whole. If it is
demonstrated, retain its evidence and move to the next missing condition instead
of repeating the case until every output detail is polished. Record significant
off-target limitations in the appropriate later-stage backlog. If a limitation
invalidates the targeted condition (for example, a required source-backed reply
contains a material unsupported claim), diagnose and repair that failure before
retesting; deferral must not weaken the item's actual requirement.

Changing the model or credential is a new condition, not a same-run continuation.
Prepare a separate request and record all differences before comparing results.

## Preserve the stopped case

`scripts/validate_search_agent.py` is an explicit evaluation controller, not a
production supervisor. Its normal live loop stops for inspection at unexpected
provider failures. A mission may therefore remain active in saved state after
the private host has closed. That status does not mean a host is still running.

Use `--resume-case` only for a selected, closed case after examining its saved
failure. The controller accepts a captured HTTP 413, a recognized HTTP 400 whose
tool-validation recovery is already saved by the host, or a captured daily-limit
HTTP 429 with the host's matching retry state. A finite case may also resume
after a terminal `uncertain_transport` attempt in any phase when its cumulative
ledger and exact saved intent, dispatch marker, capacity, work, control and
request blob agree. Earlier unknown attempts must have matching saved recovery
records; their reservations remain charged. If the final attempt is still
pending, the copied host supersedes it locally. If the host already saved that
recovery, the controller checks the exact recovery and follows its saved backoff.
Neither path replays an uncertain intent or executes unseen tool output. The
controller rejects incomplete or inconsistent uncertain state,
unrecognized failures, changed requests, different model/profile identity,
and unrelated active runs. The provider's saved wait remains in force.

A clean token stop can block several peers before any HTTP request, even when
all earlier responses are known. Validate each blocked candidate separately,
including its intent, configuration, actor/task identity, work and capacity
record. A prepared request replaced by new peer input is also distinct from an
attempted request. Its saved no-effect history must agree; matching a call and
work record to each other is insufficient if they disagree with the real agent,
intent, task or capacity lane. Never discard these records to make a resume pass.

For each historical request blocked locally before HTTP, the controller verifies
its saved estimate and no-effect status against that request's original phase.
The current and prior ledgers pin recent ceilings; older phases require matching
versioned evaluation records and the prospective ceiling-grant history. Admission,
dispatch-marker creation and historical ready-control times must fall within the
recorded controller interval, which includes draining active callbacks but ends
before final report and shutdown bookkeeping. The same actor may have distinct
denials in successive phases; duplicate denials within one actor/phase are rejected.
Missing phase proof remains a rejection. Never rewrite a marker, estimate or
historical control to make a resume pass.

For an original start request, retain the exact request JSON and use a new direct
child directory beneath `.local/search-validation`. If the stopped case was
originally authorized with unbounded accounting, retain that mode:

```sh
.venv/bin/python scripts/validate_search_agent.py \
  --resume-case .local/search-validation/selected-stopped-case \
  --start-request .local/cases/original-request.json \
  --output .local/search-validation/selected-continuation \
  --no-aggregate-limits --until-complete --live
```

For a stopped follow-up, replace `--start-request` with
`--followup-request .local/cases/original-followup.json`. Do not add
`--followup-state`: the closed case already contains its exact parent chain and
child. All ancestors must be completed, the selected child must be the only
active run, and its parent snapshot and accepted target must match. The host
must return the existing run identity rather than create a second mission.

If the stopped case had a **finite aggregate allowance**, repeat its original
total limits and observation deadline instead. For example, a case with no
model-call cap, a 120,000-token cumulative ceiling, zero retrieval and a
1,800-second phase deadline continues with:

```sh
.venv/bin/python scripts/validate_search_agent.py \
  --resume-case .local/search-validation/selected-stopped-case \
  --start-request .local/cases/original-request.json \
  --output .local/search-validation/selected-continuation \
  --no-model-call-cap --token-limit 120000 --retrieval-limit 0 \
  --deadline-seconds 1800 --live
```

`--token-limit` here names the **original cumulative ceiling**, not a new
120,000-token phase. The controller verifies the unchanged prior HTTP ledger,
subtracts its reported usage plus unknown-use reservations, and gives the new
phase only the remaining allowance. It similarly subtracts prior model and
retrieval attempts from any finite count limits. A different model-call cap,
retrieval bound, token ceiling, deadline or accounting mode is rejected before
creating the continuation directory. A token-cap stop needs a prospectively
authorized addition through `--resume-token-ceiling-increase`; retain the original
`--token-limit` argument. For example, adding 60,000 to an original 120,000 ceiling
sets the cumulative ceiling to 180,000, then subtracts all earlier usage and
unknown reservations. This is an explicit evaluation allowance change, not a
provider reset or permission to rewrite the saved ledger.
No source is restaged: the copied closed state retains the original run,
accepted work, source versions and unresolved peer obligations.

These options require explicit live authorization. Omitting `--live` only prints
the parsed preview; it does not prove that the saved case passes preflight.
Accounting-only observation has no aggregate call/token/retrieval allowance or
wall deadline, but ordinary provider limits, per-response configuration, source
permissions and first-error inspection stops still apply. Finite continuation
keeps its cumulative caps and a finite phase deadline. Neither mode permits an
account or model switch, and a stopped case cannot change modes on resume.

The source case stays unchanged. The controller verifies its hashes, copies its
closed state into the fresh output, freezes current code and the normal catalog,
and enables only the selected private run/profile. Shutdown disables the private
profile and records whether callbacks remain. Original root profile settings are
checked for preservation.

When inspecting a closed case whose exact file tree is pinned, open a byte copy
of its SQLite files, including any existing WAL, and verify the original hashes
remain unchanged. A read-only SQLite connection can create empty WAL and SHM
sidecars even when the main database and accepted rows are unchanged. Those new
files can invalidate a whole-state hash; they are not proof of changed research
content. The resume validator already inspects a private SQLite copy.
Cached provider-limit verification likewise reads a temporary database/WAL copy
and the original immutable blobs, preserving the original provenance path. If
the database or WAL changes while copying, the hint is unavailable; do not treat
a mixed snapshot as verified advice or relax the stopped-state integrity check.

Follow-ups in the same store must reuse verified provider-limit advice as well
as fresh stores do. The numeric observation remains scoped to its exact provider,
model and credential selection; the import transaction is scoped to the receiving
mission so an earlier parent's import cannot suppress the child's bound. Reusing
this advice does not establish current remaining provider capacity.

Later continuations also pin the prior cumulative ledger. A missing or changed
protected file makes the saved observation's integrity check false; it must not
erase the observation itself. Missing observations from older controller failures
are not automatically resumable. Preserve the original case and distinguish any
separately reviewed, derived receipt and reconstructed timing from original capture.

## Read phase and cumulative evidence separately

- `budget.json` records only new attempts in the selected phase, with its
  remaining finite limits when applicable.
- `prior-cumulative-budget.json` is the exact pinned prior ledger.
- `cumulative-budget.json` combines every phase without dropping failed attempts
  and retains the original cumulative limits. An observed provider usage overrun
  remains reportable with `reported_token_cap`; it does not authorize another
  dispatch or a further resume.
- `case.json` pins the original request, state hashes and continuation lineage.
- `observation.json` gives phase and cumulative accounting, active controller
  segments, shutdown status and source/profile integrity.
- The durable evaluation observation advances by version and directly hashes the
  cumulative ledger; earlier observation versions remain available.

A failed call with no reported usage retains an explicitly unknown reservation;
it is neither measured consumption nor zero usage. Sum phase ledgers when
building a batch total: summing both original and cumulative ledgers would count
earlier attempts twice.

Active controller durations exclude the interval between closed cases. The
first-to-last ledger window includes that interval. The read-only
`ora.observability.run_reports.load_run_report(..., kind="natural_agent")`
validates the cumulative ledger, saved response identities and each controller
segment. A report that loads is accounting evidence, not proof of a correct
research answer. Review the actual final output, permitted sources, peer delivery
and explicit review before calling a run qualified.

## Offline checks

The runner tests use injected transports. They exercise copied-state 413
continuation, recognized tool-error follow-up recovery, observed daily-limit
waiting, unchanged parent records, unsafe-source rejection and cumulative report
loading. They do not spend provider tokens or establish natural model behavior.

```sh
.venv/bin/python -m pytest -q tests/benchmarks/test_search_agent_runner.py \
  tests/benchmarks/test_search_agent_token_cap_resume.py \
  tests/observability/test_evaluation.py tests/observability/test_run_reports.py
```
