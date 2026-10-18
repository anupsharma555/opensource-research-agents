# Accounting boundaries and inputs

The helper uses SQLite read-only connections including the live WAL and a read
transaction for each database. Databases read sequentially are not one atomic
cross-workspace snapshot. It reads current receipt metadata with the **first saved
receipt timestamp** so record revisions do not move usage into another day.
Calls crossing midnight are attributed when their response was first saved; this
is local accounting, not a claim about Groq's internal time attribution.

The helper also keeps an ignored private daily log under `.local/checks/`.
Each entry is one local date for an exact source-path set, selected model, timezone,
legacy-attribution cutoff and planning allowance. A repeat or older observation
does not replace a newer entry; a narrower source selection is a separate entry.
Each invocation refreshes the selected date and six preceding dates from the
selected saved inputs. The seven-day view reads those matching entries and marks
unobserved days missing; after a complete invocation, no matching date in that
range should be missing. A zero captured-call count means none in those selected
inputs, not zero account-wide consumption.
Its sums cover only observed days and known token fields; it is neither a complete
account statement nor current quota. The log retains older dates without pruning
research receipts or other history. It updates only when this helper is invoked;
there is no background refresh. A report's daily-log status states whether that
invocation added, updated or left an entry unchanged. Each entry also retains the
latest exact provider daily-token observation made within that local calendar day.
This is historical headroom at its timestamp, not current remaining capacity. A
newer scan that loses previously reported token fields leaves the more complete
daily entry intact; the status reports that decision.

Exact configuration references supply provider, requested model and profile when
the receipt omits them. Figure response receipts require an exact matching figure
intent; its saved model and credential profile determine a separate vision-model
usage lane. Reported model takes precedence. Excluded models are
counted in coverage. Missing profile remains unknown unless an explicitly supplied
historical cutoff establishes inferred primary. Profile labels track configuration,
not physical key identity across rotations. Key rotations need a dated local record.

Repeated database inputs and copied receipts are deduplicated by response ID,
provider request ID, or receipt identity. Conflicting duplicates are excluded and
reported for investigation. Repeated prompts are separate calls, not duplicates.
Prepared requests, dispatch markers and captured responses are different facts;
only captured provider usage is added to token totals. A failed response with no
usage is unknown consumption, not a zero-token call. Uncaptured dispatches need
investigation through `os-qualify-run` and are not added as estimated tokens.
The collector counts uncaptured figure dispatch attempts in source coverage so
they remain visible without assigning invented usage.

The helper reads only small, hash-verified objects attached to HTTP 429 receipts
to extract daily quota numbers. It returns neither raw error text nor organization
IDs. Missing, oversized or invalid error objects leave daily observations unknown.
It never reads successful model response, reasoning, source or snapshot objects.

## Standalone calls

`--supplement` accepts the repository's existing
`kind: one_request_backup_credential_check` receipt, selecting only its timestamp,
credential-variable name, model, response ID and usage. It also accepts a private
normalized ledger with schema `ora.groq.usage.calls.v1` and a `calls` list:

```json
{
  "schema": "ora.groq.usage.calls.v1",
  "calls": [{
    "observed_at": "2026-01-15T15:00:00+00:00",
    "credential_profile": "backup",
    "model": "openai/gpt-oss-120b",
    "response_id": "example-response-id",
    "http_status": 200,
    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
  }]
}
```

Normalize from exact provider receipts, retain the original private path as evidence,
and preserve the provider response ID. Do not invent usage, reconstruct token counts
from character lengths, or import an aggregate as a call. The helper rejects unknown
formats. Other standalone experiments or projects are outside coverage unless added.

## Daily limits and official sources

Checked 2026-10-10:

- [Groq rate limits](https://console.groq.com/docs/rate-limits) lists a base free-tier
  allowance of 200,000 tokens/day for GPT-OSS 120B. Organization settings can differ.
  Token headers describe TPM; request headers describe RPD. The page does not
  establish a midnight timezone or a precise rolling-window/refill algorithm.
- [Prompt caching](https://console.groq.com/docs/prompt-caching) exempts cached tokens
  from rate limits, with deductions after processing. A positive remaining header
  does not reserve capacity for concurrent work.
- [Prometheus metrics](https://console.groq.com/docs/prometheus-metrics) is an
  Enterprise feature. Do not assume its API is available on these free accounts.

The report therefore shows calendar-day and trailing-24-hour **usage** independently
of provider window semantics. `--daily-limit` arithmetic is not a certified balance,
upper/lower bound, reset prediction, or permission to dispatch. Even a known token
balance does not establish request/minute capacity. Current account-wide usage may
require the user's Groq Console usage/limits view or a supported provider export;
record its organization/model, period, observation time and coverage privately.
Do not issue deliberate inference probes or provoke 429 errors to refresh a balance.
