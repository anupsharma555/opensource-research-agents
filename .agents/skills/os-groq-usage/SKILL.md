---
name: os-groq-usage
description: Track saved Groq token usage by credential profile with a private daily log, seven-day view, and dated quota observations.
metadata:
  version: "0.2.1"
---

# OS Groq Usage Tracking

Use this Codex development skill in OpenSource Research Agents to answer how much
Groq capacity research and testing have consumed. It reads saved records; it does
not submit inference, change credentials, restart research, or schedule monitoring.
For quality and efficiency of an individual run, use `os-qualify-run` alongside it.

## Collect and summarize

1. Identify the durable workspace(s) used in the requested period. Read their
   `model_response_receipt` records and exact-linked `figure_response_receipt`
   records, not repeated snapshot exports or prior totals.
   Include separately saved one-call checks when relevant. Do not search all source,
   reasoning or snapshot objects. Never load `.env` to calculate usage.
2. Run the helper from the repository root, repeating `--state-dir` when needed:

   ```sh
   .venv/bin/python .agents/skills/os-groq-usage/scripts/usage.py \
     --state-dir .local/research --timezone America/New_York
   ```

   It writes private JSON and Markdown receipts under `.local/checks/` and updates
   `.local/checks/groq-usage-daily-log.json`. Each invocation refreshes the selected
   day and six preceding local days from the selected saved records. The receipt
   includes the selected calendar day, a separate trailing 24-hour window, and a
   seven-local-day view. Defaults use `openai/gpt-oss-120b`. For saved figure
   calls, run a separate report with `--model qwen/qwen3.8-27b`; do not add
   vision tokens to the GPT-OSS model's allowance. Use
   `--date YYYY-MM-DD`, a timezone-aware `--as-of`, or `--model` for another scope.
   Inspect both output files and the daily log update status.
3. Account for unlabelled older calls. Resolve the exact saved configuration first.
   If historical execution evidence establishes that older calls used primary,
   supply `--legacy-primary-before <ISO timestamp>` and explain the evidence. These
   calls remain labelled **inferred primary** in the report. Otherwise leave them
   under **unknown**. Do not infer a key from a quota-domain name or current `.env`.
4. Add an explicit `--supplement <path>` for a saved standalone check or normalized
   call ledger described in [accounting.md](references/accounting.md). The helper
   deduplicates provider response identities; never add a previous daily aggregate.
5. Report input, completion and total tokens, captured calls, missing usage, and
   relevant error counts for each profile. Reasoning is a subset of completion;
   cached input is a subset of input. Show subset coverage rather than treating
   missing details as zero. Include the period, observation time, and excluded work.
   In the seven-day view, missing dates mean no saved observation for the exact
   selected sources, model, timezone and attribution assumptions. They do not mean
   zero usage. Rerun with the same source set after research or tests to refresh a
   day; a narrower invocation retains a separate history lane.
   Dated quota observations in the daily log are historical headroom measured
   within that local day; they never establish current remaining capacity.
   Report uncaptured figure dispatch attempts separately as unknown consumption,
   never as zero-token calls. A saved figure interpretation is unverified image
   reading, not evidence that its scientific claim was checked.

## Remaining capacity is a separate question

Read [accounting.md](references/accounting.md) when interpreting remaining tokens
or reset timing. Confirm current Groq documentation or a dated provider observation
before changing assumptions. The helper has **no default daily allowance**; an
explicit `--daily-limit 200000` provides arithmetic planning estimates only.

- Token response headers describe **minute** capacity. Request headers describe
  **requests per day**, not daily tokens. Neither proves a midnight token reset.
- The helper extracts a timestamped daily-token `Limit / Used / Requested` observation
  from an exact linked 429 error, when retained. Its retry time means when that
  request may be retried, not when an entire day's allowance resets.
- Show provider-observed headroom with its timestamp. Current account-wide remaining
  tokens stay **unknown** without current provider evidence and matching scope.
- A local estimate subtracts recorded total usage from an explicitly supplied
  allowance; it ignores unreported consumption, cache deductions and uncertain
  window semantics. Label it as planning arithmetic, never available capacity.
- Profiles select keys, while quotas apply to organizations. Two keys, emails or
  the shared local `groq-selected` ledger do not establish quota independence.
  Retain existing primary holds and selected-profile policy.

Keep receipts private and concise. Do not print keys, key fingerprints, account
identifiers, prompts, provider reasoning or source bodies. No API call is needed
to use this skill. Rerun after a test batch when requested; this skill creates no
background collector or automatic schedule.
