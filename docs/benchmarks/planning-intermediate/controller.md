# Intermediate planning qualification case

Case ID: `survey-pilot-planning-v2`.

The question is whether the selected model finds a durable plan useful for a
small multi-constraint task and revises it when one constraint changes. The
synthetic survey-feasibility scenario differs from prior abstract-retrieval
cases. It uses one research peer and two short supplied records, without live
search or real participants.

## Separate inputs

- [Initial worker request](worker-initial.md): the only task and evidence visible
  at the start. Submit the synthetic cohort figures as task assumptions in the
  question. This planning-only case does not create source-library records or
  test retrieval and citation handling. Normalize paragraph whitespace for the
  existing board objective contract without changing the wording.
- [Late update](late-update.md): withhold until the event below.
- [Withheld review](reviewer-rubric.md): evaluator-only arithmetic, support checks
  and capability observations. Never concatenate this or the controller into a
  model request.

Use the ordinary MissionService and its current planning skill, deferred tools,
source handling and captured requests. Do not add an eval-only agent prompt,
name the planning tools in the worker request, or script the model's choices.
The request for a revisable working plan is natural task intent; it does not
require a particular skill or storage mechanism.

## Event and stopping boundary

After the first plan checkpoint is committed, stop the controller at the saved
boundary before the next model dispatch. Deliver the late constraint as a
persisted user/task update to the same still-open task, preserving plan ownership
and history. Verify the next prepared model input contains the exact update.
Do not rewrite history or silently modify the worker's existing plan.

Use one saved user-update event and reattach it through the existing host-feedback
surface before later model preparations, since ordinary feedback is consumed
per turn. Validate the controller offline with injected transport before live use. Verify both branches: delivery before the next model turn,
and a model that completes without saving a plan. If no compatible user-update
surface exists, implement bounded controller staging against existing records
and test it first; do not claim an unimplemented public API. A new follow-up task
has a different plan owner/scope and cannot stand in for same-task revision.

If the agent completes without a checkpoint, record task quality and planning
not exercised. Do not reopen a completed task or force an activation to obtain a
passing result. Stop on a failed delivery condition, unexpected scope expansion,
provider limit or exhausted case allowance; diagnose offline before another run.

## Future live scope

Live execution is paused. A proposed ceiling is **8 model API calls and 40,000
planned tokens for the whole case**, covering both phases, retries and any
credential fallback. This is a proposal, not a fresh authorization or guaranteed
usage. Earlier allowances for different scenarios do not carry over. Confirm
the bounded invocation, inspect current usage, and pin the selected model,
credential profile and output settings before dispatch. No external search,
participant contact, UI launch or pilot execution is part of this case. The compact
task preserves the decision constraints while avoiding source-library bookkeeping;
a heavier source-backed version needs separate capacity qualification.

Use the repository's `os-eval-testing` skill for staging, `os-qualify-run` for
saved-evidence qualification, and `os-run-details` for the final chat report.
Separate task quality, plan usefulness, guidance delivery, optional activation,
mechanical correctness and API usage in that report.
