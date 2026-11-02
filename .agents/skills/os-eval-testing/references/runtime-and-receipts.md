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

For a case that copies code into a private runtime, verify the shared checkout
before freezing it. During dispatch, check the immutable copied runtime and
the exact outgoing packet. A per-request hash check of the mutable checkout can
turn an unrelated concurrent edit into an opening-stage transport uncertainty
after the budget has reserved tokens. If a local guard blocks before the network
opener, save explicit blocked-before-effect proof; without that proof, retain the
unknown-use reservation and do not replay the request as if it never left.

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
For a token-only evaluation allowance, keep request counts as accounting and
enforce the aggregate token budget, including reservations for calls already in
flight. Do not add an arbitrary call cap or substitute a very large number for
an absent limit. Verify that the budget implementation supports an absent call
cap with an enforced token cap; accounting-only mode is not a substitute because
it removes the aggregate token limit. Provider request quotas and unexpected
failure stops still apply. Do not rewrite historical ledgers when this policy
changes.
An allowance exhausted before a useful answer yields an incomplete test, not a
scientific negative result. After diagnosis and authorized continuation, preserve
the stopped ledger and link any continuation to its attempt count and remaining
allowance; never silently reset the aggregate ceiling. When resuming a saved case,
retain its pinned instructions or label a changed configuration as a new condition.
A test of corrected orchestration with old prompts does not qualify new prompts. Do not change the permanent runtime's research limits.

On a first-failure stop, block new dispatch while allowing the already-attempted
callback a bounded opportunity to save its exact response receipt and settle its
capacity record and executor acceptance before closing the host. A finished HTTP
ledger entry alone does not mean the callback has finished applying its result.
Use `MissionDispatchDriver.drain_active` after disabling new scheduling, and
inspect the result before close; a drain timeout preserves unfinished state.
Read intent/work/receipt transitions from one coherent snapshot, as in
`ora.mission.evaluation_settlement`, rather than mixing observations taken during
capture. Do not declare review proof missing between a mailbox update and its
executor receipt; judge the settled record. If capture or settlement remains
missing, preserve the uncertainty rather than replaying the request. Verify this through the full
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
unclassified provider quota pressure. Expected minute waits follow the procedure
below. A deliberate denied-source condition is expected, not automatically a
failure. If a natural agent chooses a valid alternate action, judge the task
outcome rather than treating script deviation as a defect.

## Token counter and minute waits

Use the runtime's existing tokenizer and durable capacity ledger, not a second
in-memory counter in the evaluation script. `estimate_request_tokens` in
`src/ora/models/token_estimate.py` estimates the complete serialized request,
including instructions, tools and history. `MissionService._prepare_model`
adds its next-completion prediction before submitting work. `CapacityScheduler`
in `src/ora/coordination/capacity.py` owns shared reservations, rolling-window
charges, provider reset observations and atomic dispatch admission.

1. Compare the next request's estimated demand with both the full window limit
   and currently available capacity. Count recent settled usage plus outstanding
   reservations across the selected shared quota domain. Tokenizer estimates are
   not exact provider counts; replace settled reservations with reported usage.
   Preserve unknown-use holds. Do not count a settled request's estimate and
   actual usage twice, or assume separate keys have independent quotas.
2. If the request fits a fresh window but not the current remainder, keep it
   pending with a saved `wake_at`. Use the scheduler's earliest admissible time,
   respecting applicable `retry-after`, token reset and local charge-expiry times.
   Do not assume a reset at the next wall-clock minute or dispatch immediately
   after an unconditional 60-second sleep. Recheck all relevant limits on wake;
   concurrent work may have consumed the available capacity.
3. Let the existing host/driver continue scheduling while the evaluation observes
   it. Waiting and local status polling make no model calls. Preserve request
   identity and context; revalidate permissions before eventual dispatch. Save
   the wait reason, wake time, estimate and actual dispatch time. Report expected
   waiting as waiting, not failed model behavior.
4. A captured temporary minute-limit refusal can follow the runtime's retry path
   only within existing authorization and reconciled accounting. Count every
   HTTP attempt, including rejected retries; missing usage remains unknown and
   may prevent another dispatch under the controller's budget policy. Daily quota
   exhaustion, an unexplained refusal or an unknown transport outcome is not a
   reason to retry every minute. Never reset the batch ledger or switch profiles.
5. If only the next request's forecast exceeds an observed full window, do not
   treat that prediction as a provider rejection or silently lower its output
   cap. The current backend can preserve the required packet and admit one
   exclusive provider-feedback attempt after recent charges and provider waits
   clear. This still requires the user's remaining batch allowance and the model
   context-window check. Keep the original forecast for audit; do not relabel it
   as a smaller actual request. A previous completion used to predict the next
   one is not expired usage. A real provider size rejection requires inspecting
   and correcting that exact request rather than repeating it unchanged. Preserve
   selected evidence and tool results when reducing overhead.

Set the evaluation's wall-clock observation window to accommodate expected waits.
If it ends first, record the run as waiting/incomplete and preserve the pending
state and allowance; do not silently extend an explicitly bounded deadline or
turn a review timeout into mission cancellation.

For runner changes, use a controlled clock to verify: no dispatch before reset;
exactly one eligible dispatch after reset and rechecking; restart and concurrent
reservations retain the counter; an over-limit forecast gets one exclusive
attempt rather than a permanent local rejection; actual size refusals do not
repeat unchanged; and the aggregate test allowance is unchanged. Reuse
`tests/coordination/test_capacity.py` and
`tests/coordination/test_changed_request_headroom.py` and
`tests/coordination/test_rate_forecast_probe.py`; run the relevant focused
cases rather than repeating the full suite for a guidance-only change.

Provider semantics: [Groq rate-limit documentation](https://console.groq.com/docs/rate-limits)
describes token reset headers and `retry-after`. Read it when changing header
interpretation; use the selected account's observed limits rather than a fixed
8,000-token default. The runtime uses a conservative local window; its counter
cannot observe all usage from other applications.

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

## Review provider reasoning for every live test

Inspect the captured provider reasoning for every model step, including
intermediate agent and auxiliary-model calls when available. Keep the review
separate from checking file integrity:

1. Match the trace to the exact run, agent, call and response receipt. Reconcile
   capture coverage with attempted calls, returned responses and finish reasons.
   Distinguish complete provider text, provider-truncated text, legacy redacted
   text and unavailable capture. Reasoning-token counts cannot supply missing text.
2. Read each available trace and compare its stated purpose and evidence use with
   the input actually sent, skills/tools available and selected, peer questions
   and answers, and subsequent saved effects. Note useful explanations, repeated
   deliberation, mistaken context or tool assumptions, and discrepancies only
   where the records support them. Intended actions are not executed actions.
3. Record **Reasoning trace reviewed: Yes / Partial / No / Not applicable**, with
   reviewed/available/model-step counts and a short finding. Missing or truncated
   traces limit the conclusion; they do not imply zero reasoning or invalidate
   independently demonstrated behavior. Model-free cases are not applicable.
4. Treat provider reasoning as diagnostic model output, not a faithful account of
   every internal computation or independent proof of scientific support. Use the
   actual request, tool outcomes and source evidence to resolve disagreements.
   Keep a public action-purpose field distinct from this trace and from the
   evaluator's interpretation.
5. In a requested private input-to-output report, follow
   [the step-level reasoning format](../../os-run-details/references/report-format.md#provider-reasoning-in-each-step):
   link the matching private, human-readable per-step Markdown directly after
   its capture label, state capture limits and concise reviewed findings, and
   link the combined Markdown trace at the end. Embed full trace text inline
   only when explicitly requested. Keep byte-exact TXT and JSON separately.
   Use saved exports to review existing runs; do not rerun a model
   just to fill a reporting gap. Keep raw text out of cloud telemetry, runtime peer
   context, tracked examples, and Codex conversation-log exports.

Finish with separate results for:

- Task usefulness and source support, including review method and limitations.
- Whether the intended condition/capability was actually exercised.
- Execution, recovery and permissions, supported by exact records.
- Reasoning-trace review coverage and its findings or capture limitations.
- Provider-reported input/output/total usage, unknown outcomes and optional subsets.
- UI behavior only if tested; broader scientific validity/endurance remain separate.

Only refresh a tracker acceptance item when its evidence requirement is met.
Do not mark a whole stage complete from one case. Keep private runs, source bodies,
provider reasoning, credentials and tracker identifiers out of tracked examples.
