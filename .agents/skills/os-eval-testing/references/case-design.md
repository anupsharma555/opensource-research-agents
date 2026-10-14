# Natural tasks and controlled conditions

Read when creating or revising an evaluation. These examples describe user jobs;
adapt them to current capabilities and use synthetic or permitted public sources.
They are not prompts to run as a fixed suite.

## Compact case plan

Save a private case plan under `.local/` before a live batch. Include only fields
that matter to the selected question:

```yaml
case_id: unique-versioned-case
question_under_test: What uncertain behavior would this task reveal?
worker_task: A realistic request, without expected conclusions or tool instructions.
evidence: Exact copies, versions, hashes and use permissions.
condition: One purposeful variation, with an observable manipulation check.
events: Later evidence or user updates, delivered only when their trigger occurs.
comparison: Matched baseline if needed; otherwise why this is a single-case probe.
expected_outcome: Withheld claim-support and task-completion criteria.
execution: Backend or interface scope; model, settings, profile and source identity.
authorization: Existing live scope, maximum requests/tokens/runs and stop conditions.
measurements: Exact records needed to establish delivery, tool use, output and usage.
next_decision: What each possible result would justify doing next.
```

Keep `expected_outcome` outside any worker-facing export. Validate the export
itself, including replay and follow-up messages; a separate rubric file does not
prevent leakage if the runner later concatenates everything into a prompt.

## Evidence recovery and changed conclusions

Natural request: "Prepare a short evidence brief on whether this intervention
has a lasting benefit. Explain who the findings apply to, how long outcomes were
followed, and what remains uncertain. Cite the evidence behind your conclusions."

Use coherent research materials with an early subgroup or follow-up qualification.
Later introduce a new study as a realistic user update: "This follow-up study is
now available. Update the brief and explain whether it changes your assessment."
The controller can compare ordinary context with a recorded context-pressure
condition while preserving the model and evidence. It must establish that the
early qualification was previously delivered, subsequently absent, and actually
retrieved/delivered again if recovery is claimed. The agent chooses how to work.

Review retained qualifications, interpretation of counterevidence, appropriate
uncertainty, and claim support. If the condition never removes the early evidence,
the answer can pass quality review while recovery remains untested. If only exact
source text disappears while its meaning survives in a report or note, label
source-text recovery separately from recovery of lost information. Artificial
filler or lowered input budgets are controlled stress conditions, not realistic
workload evidence; disclose them and prefer coherent longer material when feasible.

For a parent-assessment/follow-up design, pin the accepted parent report. Inspect
the current follow-up contract: it does not currently accept an evidence-text
upload. Staging a later checked source before dispatch requires private controller
glue around the real evidence library, current source-use checks, exact source
references attached to the intended child task, and an event receipt. Merely
registering the later source does not deliver it: follow-up initially inherits
the parent snapshot sources. Verify the attachment reaches actual child input
with injected transport before live use. This is not a documented public staging
API. Do not claim a user-facing upload capability or put uncited source text only
into the question.

## Large results and optional skill use

Natural request: "Compare the methods and limitations of the candidate studies
in this research workspace and recommend which deserve closer review. Explain
the reasons for your shortlist using the relevant source material."

Supply enough distinct, meaningful records for a real comparison; place useful
discriminating details beyond an initial bounded result. Confirm those details
did not already reach the model through another context path. Avoid fabricated
padding merely to demand many page reads. A critic may activate an appraisal
skill, use other valid methods, or decide no optional skill is needed. Record
the actual choice and quality. A task that succeeds without activation cannot
establish activation behavior, but should not automatically be graded as failed.

## Access changes and interruption

Natural update: "The earlier document is no longer available for use. Revise the
brief using the remaining material and explain any resulting limitations."

Apply the actual source-use change through the controller and inspect backend
enforcement offline first. A live test adds evidence about the model's response
to the limitation; it does not replace deterministic permission tests. Do not
require it to attempt an unauthorized read to pass. For interruption, stop at a
safe saved boundary and resume the same authorized task without changing its
model or evidence. Review duplication, lost qualifications and actual continuation.

## Collaboration only when useful

Natural request: "Assess these conflicting study findings and include an
independent methods critique before recommending a next research question."

Use multiple peers only when the task benefits from different expertise or
independent criticism. Review delivered requests, substantive replies and their
use in the final output. More agents, messages or tool calls are not success
criteria. Start with a single-agent baseline when it answers the same question.
