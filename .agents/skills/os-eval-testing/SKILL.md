---
name: os-eval-testing
description: Design and progressively execute realistic research-agent evaluations in OpenSource Research Agents, using natural-language tasks across selected context, evidence, tool, recovery and collaboration conditions. Use to create or compare eval cases, select the next useful test, or diagnose an evaluation; not for routine unit checks alone or claiming scientific validity from a harness pass.
metadata:
  version: 0.1.10
---

# Progressive research-agent evaluations

Use this Codex development skill in the OpenSource Research Agents repository.
It designs tasks and selects tests; it is not loaded into research agents and
does not impose a research workflow or lifetime limit on them.

Start from a realistic user job and the uncertainty to resolve. Prefer natural
language and observable outcomes over prescribed tools, exact action sequences,
or model-visible answers. A valid alternative route is not a failed evaluation.

## Choose the proof needed

Identify the source of uncertainty before spending provider allowance:

| Question | Smallest useful evidence |
| --- | --- |
| Does paging, permission enforcement, activation or restart work? | Offline tests or saved replay; zero model API calls |
| Does the selected model choose and use the capability effectively? | An authorized natural-language backend task with captured model/tool records |
| Does a change improve quality or efficiency? | Matched tasks with the relevant condition changed; compare quality before token totals |
| Does the result display correctly? | Interface inspection only when presentation is in scope |

Injected model responses establish mechanisms. A guided live tool sequence
establishes that the model can follow that sequence. Neither establishes natural
tool selection. Use guided probes only to isolate a specific defect after an
observed failure, and label them separately from realistic task performance.

Read current `AGENTS.md`, `README.md` and `docs/development/setup.md` before
execution. Read only the matching subsystem documentation and tests from
`docs/development/modules.md`. Reuse the existing runtime rather than inventing
an eval-only agent path. Consult [case design](references/case-design.md) when
creating a task, comparison or withheld rubric; it includes adaptable examples.

## Explain the test to the user

Before executing a case, give a short plain-language brief: the task and supplied
material; any later update or condition; the specific behavior or uncertainty
being tested; what observable outcome would support it; and the authorized token
budget, with any separately requested call or run limits.
Name the exact behavior, its trigger, and the saved evidence that would establish
it. For a small set of checks, use a compact Behavior / Evidence table. Explain
why the task gives that capability a useful job. Distinguish task success
from exercising the target capability, and state the main proof limit. Adapt this
to the case rather than reciting a fixed checklist. Put this explanation in chat
for the user; keep withheld review criteria and future events out of worker input. A single
capability-use case can establish observed use and task quality; claims that the
capability improves quality or efficiency require an appropriate comparison.

## Design the task and conditions

Write the user request as a person seeking useful research work would: their
question, audience, evidence available, desired output and real constraints.
Do not tell the worker to activate a named skill, call a reader, use an internal
record ID, or reach the evaluator's expected conclusion unless the human job
itself requires that action. Normal runtime tool schemas and evidence locators
remain available; the prohibition is against scripting the evaluated choice.

Keep three surfaces separate:

- **Worker task:** the natural request and evidence currently available.
- **Controller plan:** condition changes, event timing, evidence identities,
  model/profile/settings, allowed side effects and authorized test ceiling.
- **Withheld review:** expected support, counterevidence, unacceptable claims
  and outcomes. Never put this rubric or future evidence into worker input.

Choose only conditions needed for the question: ordinary context versus
compaction, small versus large results, optional skills available versus an
appropriate baseline, source access changes, interruption/restart, contradictory
evidence, or collaboration. These are options, not a mandatory test matrix.
Freeze evidence and settings for a comparison where practical; record unavoidable
differences. Do not silently substitute models, credentials or permissions.

Verify the condition actually occurred in captured input and state. A larger
document alone does not prove compaction; an available skill does not prove its
use. If a valid answer avoids the target capability, record task quality and
**capability not exercised** separately. Do not force a tool path just to obtain
a green result. Revise an uninformative case without leaking its answer.

## Test progressively

1. Reuse current relevant offline evidence. Check new harness/staging logic with
   injected transport before any real request; no need to repeat unrelated suites.
2. Select one natural task that can resolve the most important remaining gap.
   State what live execution adds beyond the existing offline evidence.
3. Use existing live authorization and its aggregate token budget. A token budget
   is sufficient without a separate call cap; add call or run caps only when the
   user specifies them. Preserve any existing explicit limits unless the user
   changes them. If no budget is authorized, propose the smallest useful one
   before calling a provider. If the user has delegated choosing reasonable token
   caps for scoped live tests, choose and record a proportionate aggregate ceiling
   without another approval question; respect existing explicit ceilings, the
   selected provider/profile and the authorized scope. Count every attempted call and respect provider
   request limits, but distinguish an estimated call count from a stopping rule.
   Explain each planned call's purpose where predictable; autonomous paths have
   estimates, not guaranteed counts. Verify that the runner enforces the chosen
   budget mode before dispatch.
4. Run serially by default, capture evidence, and inspect the first mismatch
   before spending on another case. Diagnose the failing layer and prefer offline
   fixes/replay. Expand only to answer a named unresolved question within the
   remaining authorization. A batch ceiling ends the test, not the research scope.
   Expected minute-capacity waits are normal scheduling: preserve the pending
   request and let the runtime wait until its saved reset/wake time, then recheck
   capacity and the remaining batch allowance. Do not fail or repeat a test merely
   because the current minute is nearly full. Read the
   [token counter and wait procedure](references/runtime-and-receipts.md#token-counter-and-minute-waits)
   before changing a runner; distinguish a replenishable shortage, an uncertain
   over-limit forecast, and an actual provider size rejection.
5. Distinguish an agent failure from a harness defect, invalid comparison,
   capability not exercised, external quota limit, or inconclusive observation.
   Do not penalize useful alternative behavior solely for differing from a script.

For live execution, read [runtime and receipts](references/runtime-and-receipts.md).
Use a fresh private backend workspace when backend testing is requested. Do not
launch Atlas or require UI acceptance for a backend-only evaluation. Inspect
concurrent provider work and preserve existing held work. Source fixtures can
isolate model behavior without live search; external retrieval needs its own
authorized scope.

## Short live tests of observed failures

Start with the failed behavior already observed, then choose the smallest
realistic trigger using the normal runtime, tools and instructions. Allow enough
model turns to observe the requested outcome: the first HTTP 200 or tool call is
only an intermediate result. For two-agent communication, require an actual
reply, recipient use, requester review and final incorporation where the task
calls for them; question delivery alone is partial evidence. Before counting a
peer review, verify that the substantive answer reached a captured sent model
input through a preview or normal reader. A reference in a prepared packet or a
recorded review action alone does not establish that the model saw the answer.
Label staged
single-recipient or guided probes as diagnostics. Declare the behavior evidence
before each test, separately from minor answer imperfections, and classify the
result as pass, imperfect pass, failed, inconclusive or unexercised. An imperfect
pass still demonstrates the target behavior; record a minor answer defect
separately. A missing required reply or review is not an imperfect pass. Inspect
exact sent inputs, saved provider traces and actual effects. When the target
behavior fails or the harness cannot establish it, diagnose before spending on
another case, repair offline, then repeat within the existing authorized ledger
limits. Claim improvement only with a matched comparison. Report one
consistent local structural-input reuse percentage with its denominator and
coverage, separately from provider-confirmed cache. Do not force a tool choice
solely to make the test pass.

## Review and decide the next step

For every live model test, inspect the saved provider-emitted reasoning for each
inference step alongside its actual input, selected skills/actions and observed
results. File presence or a reasoning-token count alone is not a content review.
Record what the trace helps explain, any mismatch with execution, and missing or
truncated captures; do not reconstruct unavailable text or treat the trace as
proof of correctness. Read the [reasoning review procedure](references/runtime-and-receipts.md#review-provider-reasoning-for-every-live-test)
before concluding the test. This review uses saved OSRA provider output, not
Codex's internal reasoning, and adds no research-model calls or prompt tokens.

Use `$os-qualify-run` from `../os-qualify-run/SKILL.md` for the selected saved
run's execution and evidence review, and `$os-groq-usage` from
`../os-groq-usage/SKILL.md` for saved usage accounting. Read their instructions
when needed; this skill does not duplicate their record readers. These companions
ship with this repository; personal KBA skills are not runtime dependencies.

Review actual claims against the permitted passages, including contrary evidence
and limitations. Citation shape, keywords, model self-assessment and completed
status are insufficient. When feasible, review matched outputs without revealing
their condition labels. Record whether semantic review was human or automated;
an automated review is not independent clinical or scientific validation.

Report the task, condition, useful outcome, first mismatch, actual requests and
tokens, remaining uncertainty, and the next discriminating test (or why none is
needed). Separate configured ceilings, local estimates and provider-reported
usage; missing usage is unknown. For free Groq usage, prioritize allowance and
rate limits rather than presenting hypothetical paid prices as incurred costs.
One successful task does not establish generalization, sustained reliability or
token savings. Preserve failures and exact evidence so a later test can compare
against them without repeating provider calls.

After reporting each concluded run or observation, apply
[the Linear closeout step](../os-qualify-run/SKILL.md#close-the-loop-in-linear):
update authorized issue checkboxes only from fully met criteria, record meaningful
progress or blockers succinctly, and verify the saved result before selecting
the next test. Preserve incomplete items and explicit private-only restrictions.
