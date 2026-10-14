# Runtime selection, allowance and evidence

Read before executing a live case or implementing its runner. Repository paths
below are relative to the repository root, independent of the checkout location.

## Reuse supported paths

- `docs/development/stage4-local.md` owns mission/credential selection and
  headless commands. `docs/development/stage4-runtime.md` owns the mission loop.
- `docs/development/stage4-context-efficiency.md` describes saved-result paging,
  optional skills and the controlled recovery evaluator.
- `scripts/qualify_stage4_mission.py --help` describes the existing headless
  qualification runner. Inspect the actual selected case before execution; its
  existence does not mean it covers a new task or enforces a requested call cap.
- `scripts/qualify_context_recovery.py` scores selected saved direct-answer
  recovery runs without making provider calls. It is protocol-specific and must
  not be relabeled as a generic scorer for arbitrary natural-language tasks.
- `scripts/qualify_context_recovery_live.py` is a **guided mechanism probe** with
  four planned calls. It prescribes reader actions and is not the default runner
  for natural-behavior evaluation. Use only when that narrower question is wanted.

For a new natural case, prefer the real `ora.mission.MissionService` backend,
normal request/feedback surfaces and actual agent tools. Implement only missing
controller/measurement glue. Prove no-call defaults, future-evidence isolation,
actual per-request limits, first-failure stopping and saved evidence with injected
transport before live use. Do not copy fixture responses into a live run, directly
perform the recovery on behalf of the agent, force tool choice, or replace the
host's decision loop just to satisfy an evaluator. Reconcile exposed tools with
controller restrictions by their actual effects: inspecting an already saved hit
is local, while fetching missing text can be external. A wrong ID may warrant the
host's ordinary actionable correction rather than a controller stop. Prove that
feedback reaches the next input and that prohibited effects still stop before
execution. Bound correction attempts within the authorized allowance; repeated or
unknown failures still require inspection.

## Preflight without spending inference allowance

Verify current model/reasoning settings, selected credential profile, relevant
holds, exact new run IDs, concurrent work and source identity. Inspect `.env`
names/status only; resolve credentials only at the existing provider boundary.
Read current profile policy instead of inferring fallback from an unavailable key.
If the user chose daily profile alternation, preserve its local timezone/date
anchor in the private plan; do not automatically resume work or schedule runs.
Key rotation is a user selection policy, not evidence that provider quota reset.

Use existing authorization without repeated confirmation. A user-approved test
does not silently release unrelated held work. If a selected profile is held,
make any authorized change specific to the new test IDs, preserve the original
configuration, and verify restoration afterward. Do not restart existing hosts
to run an isolated backend case. A failed localhost probe means current host
availability is unknown; inspect through an allowed path rather than launching
a replacement.

For a free-tier provider, explain the expected request sequence or estimated
range and token allowance. Show the per-response configured maximum separately
from likely consumption. No pricing lookup is needed merely to report token
usage. Saved daily totals and historical 429 limits are not current account-wide
headroom. Do not assume midnight reset or independent quotas from separate keys.

## Enforce the authorized batch boundary

Count each attempted provider request before sending it, including requests that
fail or become uncertain. Use the authorized aggregate ceiling across cases and
retries; a new process or state directory must not silently reset it. Check limits
at the transport boundary, not only after an agent turn that may contain several
calls. Honor any token/spend ceiling as well as request count when specified.
An allowance exhausted before a useful answer yields an incomplete test, not a
scientific negative result. After diagnosis and authorized continuation, preserve
the stopped ledger and link any continuation to its attempt count and remaining
allowance; never silently reset the aggregate ceiling. When resuming a saved case,
retain its pinned instructions or label a changed configuration as a new condition.
A test of corrected orchestration with old prompts does not qualify new prompts. Do not change the permanent runtime's research limits.

On a first-failure stop, block new dispatch while allowing the already-attempted
callback a bounded opportunity to save its exact response receipt and settle its
capacity record before closing the host. If either remains missing, preserve the
uncertain state rather than replaying the request. Verify this through the full
runner entrypoint with injected success and transport failure; helper-only checks
can miss configuration-shape errors, capture timing and profile restoration.

Observe saved waiting states as well as returned errors. A quota wait with a
valid future wake is different from a preparation failure that requires local
repair, such as `ContextOverflow`. Stop the evaluation on the latter instead
of leaving the controller idle until its observation deadline. Preserve the
underlying saved failure separately from a later operator interrupt or timeout.
Exercise this path through the runner entrypoint, including captured-response
settlement and exact profile restoration.

Stop to inspect unexpected failures, unauthorized effects, missing capture or
provider quota pressure. A deliberate denied-source condition is expected, not
automatically a failure. If a natural agent chooses a valid alternate action,
judge the task outcome rather than treating script deviation as a defect.

## Minimum private evidence

Save under ignored `.local/`: case/version and evidence hashes, exact execution
command, actual loaded source identity where verifiable, model/profile/settings,
authorization and ceiling, run/task/call references, staged events, condition
checks, output, review and token/request ledger. A dirty checkout needs changed
and untracked source hashes; HEAD alone is insufficient. Preserve the distinction
between checkout bytes and proof of which bytes the process loaded.

Use the repository's saved records to distinguish prepared intents, dispatch
markers, captured responses, accepted tools, evidence delivered in actual input,
and accepted outputs. Count all relevant intermediate inputs when checking
leakage, including partial/error responses and earlier same-task calls. An
unresolved dispatch leaves that part of the audit uncertain.

Finish with separate results for:

- Task usefulness and source support, including review method and limitations.
- Whether the intended condition/capability was actually exercised.
- Execution, recovery and permissions, supported by exact records.
- Provider-reported input/output/total usage, unknown outcomes and optional subsets.
- UI behavior only if tested; broader scientific validity/endurance remain separate.

Only refresh a tracker acceptance item when its evidence requirement is met.
Do not mark a whole stage complete from one case. Keep private runs, source bodies,
provider reasoning, credentials and tracker identifiers out of tracked examples.
