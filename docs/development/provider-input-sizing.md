# Observed provider limits and complete input packing

The host measures the **complete serialized request** after adding instructions,
source passages, working context and attached tools. A source-text count alone
cannot establish that a request fits. The final saved intent must match the
measured request hash; model-context limits and explicit evaluation budgets remain
separate from provider rate-window observations.

## Reuse observations across fresh local runs

Live `MissionService` instances retain verified numeric provider-limit hints in
`.local/provider-limits/provider-limits.sqlite3`. A hint is keyed by credential
profile, quota domain, endpoint and model. Its exact response, intent, call and
request hash are revalidated against the originating local store. It contains no
source text, answer, raw provider error, key or remaining-balance claim.

The newest observation wins, including a higher limit. Hints expire after 24 hours
or become unusable when their supporting records disappear or fail validation.
**This expiry is not a provider quota-reset time.** Existing same-store capacity
reservations, waits and confirmed-size recovery remain authoritative. A hint does
not carry unused capacity between hosts or coordinate concurrent account usage.

The live evaluation runner links its frozen runtime to the project's shared hint
directory while keeping research state and credential selection isolated. Offline
hosts and snapshots do not read or update live hints automatically. An explicitly
injected cache can exercise the same behavior in offline tests.

If no matching fresh observation exists, capacity is unknown: there is no hardcoded
provider limit. A first request can still reveal a new limit. Cache failure must
not discard or strand a captured response; the host falls back to current-run
observations and logs the missing advisory history.

## Pack before probing

Normal packing first tries its observed-window target and existing margin. It can
compact recoverable context metadata and defer unrequested tool schemas while
keeping selected evidence and the complete capability catalog available.

Before a permissive provider-feedback probe, the host also tries the observed
window, keeping the existing 10% input margin, as an **input-only packing target**.
This avoids jumping from an overly
conservative input-plus-completion forecast directly to a much larger packet.
The first candidate preserves selected source text; the existing exact-source
preview and final hash checks still run after the packing choice.

If that full-source candidate does not fit, try the existing retained-source
inventory representation within the observed input window before sending a larger
probe. The pending peer answer must remain inline or have an exact, callable
reader and its original question. Every retained source stays discoverable through
the inventory and source reader. This handles growth after a peer reply, additional
tool schemas or other context changes; it is not a fixed 8,000-token rule.

Local token estimates are not the provider's tokenizer. A paced provider-feedback
probe remains available if the compact complete request still exceeds a local
forecast. This change does not impose an estimate-only hard stop, lower the
configured output limit, silently summarize evidence, change models, or reinterpret
an HTTP size rejection as a replenishable wait.

The saved `mission_call_budget.request_stage_token_estimate` includes the final
input count, stage additions, output cap, completion forecast, observed hint,
`input_only_limit_check`, and `limit_observation_basis`. These fields distinguish
unknown capacity, input within the observed packing hint, and an explicitly
forecast-based probe. Provider usage and rejected-request admission counts remain
separate measurements.

If a captured 413 rejects a request whose local estimate was already at or below
the observed limit, the next confirmed-size repair also keeps that input margin.
A one-token local shrink near the same boundary is insufficient evidence of a
repair. The provider's rejected-request count is admission feedback, not reported
prompt usage or an exact local-tokenizer calibration. Retain both measurements
and inspect the next actual response before claiming transport success.

## Per-call size is different from total run cost

Moving a saved source or report behind an exact reader reduces the current
request's size, not the amount of information retained. Later reads may add model
turns and repeat instructions or tool schemas. Do not claim total-token savings
from a smaller packet alone. Compare cumulative reported usage, unknown-use
reservations, local structural input reuse, extra reading turns and the supported
result. Metadata compaction can reduce repeated work, but must preserve current
permissions, source identity, reading position and explicit omitted-content access.

A short generated question still follows a full model decision request. Measure
instructions, attached schemas, availability guidance, current task/peer state,
source text and tool history separately. Local tool execution does not itself
require another model request. Reduce duplication or irrelevant context before
removing needed evidence; a smaller input that creates extra reads is not an
established efficiency gain. Local component estimates and structural reuse are
separate from provider usage and confirmed cache hits.

When source text cannot fit, one reading-navigation card carries each displayed
copy's saved coverage, exact version, next cursor and reader arguments. It states
that no source text entered the current call; saved coverage is not proof of past
model delivery or completeness of the original publication. Remaining copies stay
accessible through the paged inventory. Avoid duplicating the same progress in a
second card. Measure the final packet after packing: freed space may admit useful
peer context rather than reduce its total token count.

## Latest-window recovery and truthful coverage

An appended source read may coexist with an earlier full passage in the durable
selection. A transient requested-window recovery packet uses only the latest
checked window's exact passage references, avoiding overlapping source text. It
does not overwrite the saved working-source selection or delete earlier passages.

Pass those transient references through request preview and final context
assembly. Current-input coverage and any window-compaction notice must describe
the passages actually selected for this packet; saved reading coverage continues
to describe the durable selections and recorded windows. Before saving inclusion,
verify the transient references against the prepared intent's source decisions
and serialized source packets, including when detailed progress was compacted.
This prevents a partial packet from inheriting a false full-coverage label.

Verify exact sent text and coverage together under append, replacement, stale
response, changed permission and tight-budget conditions. A fitting packet and a
truthful omission notice do not establish that the model understood or used it.

## Reset headers and saved waits

Provider reset durations can include milliseconds, for example `862ms`, or
compound units. Parse them before recording the next eligible time. Token and
request resets remain separate, and an explicit Retry-After hold still applies.

For a historical quota with a missing parsed reset, recover the duration only
from its exact linked response when the operation, call, intent, request hash,
scope and reported remaining count agree. Derive the time from the original
observation; do not invent a new reset, rewrite historical receipts, or treat a
missing reset as permission to dispatch. Ordinary local charges and concurrent
reservations still apply after that provider reset expires.

## Focused offline verification

Run with the pinned project interpreter:

```sh
.venv/bin/python -m pytest tests/models/test_provider_limits.py tests/mission/test_cross_run_provider_limits.py tests/benchmarks/test_provider_limit_snapshot.py tests/coordination/test_rate_forecast_probe.py tests/mission/test_rate_input_headroom.py tests/mission/test_tokenized_admission.py -q
```

These checks cover exact profile/model isolation, fresh stores, one and two source
copies, malformed/stale/future evidence, newer higher limits, concurrent writers,
missing provenance, cache failure after response capture, offline isolation and
frozen live snapshots. Existing oversize-recovery and tool-attachment tests cover
confirmed failures and continued access to selected tools. Injected transport and
saved-packet reconstruction do not establish future provider acceptance or
scientific quality; any later live test needs its own saved evidence.
