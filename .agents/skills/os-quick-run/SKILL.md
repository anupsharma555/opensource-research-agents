---
name: os-quick-run
description: Start one immediate research run that may make multiple API calls, then explain its saved result with os-run-details. Use for a quick live behavior check, not a staged evaluation or read-only run review.
metadata:
  version: 0.1.2
---

# Quick live research run

Use this Codex development skill when the user wants to test the selected
research API now. It starts at most one new case, inspects what actually happened,
and uses [`$os-run-details`](../os-run-details/SKILL.md) for the user-facing
account. Read that skill before reporting. For a comparison, staged condition,
recovery experiment or broader scientific qualification, use
[`$os-eval-testing`](../os-eval-testing/SKILL.md) instead.

One case means one submitted research run, not one API request. Its agent may
make multiple model requests and may call external retrieval services as it
works. Count model-provider attempts separately from retrieval calls, and make
any authorized ceilings cover the relevant calls across the whole run.

## Move quickly without losing the boundary

Read current `AGENTS.md`, `README.md`, `docs/development/setup.md`, and the
selected runtime's local launch guide. For the mission runtime, use
`docs/development/stage4-local.md`. Check only the facts needed to run safely:
the selected state directory and live host, current model and credential profile
label, relevant holds or concurrent provider work, the chosen case and input,
and the user's authorization and aggregate API-call/token ceiling. Inspect
configuration names and status, never credential values. Preserve the selected
model, profile, reasoning settings and workspace. Do not start a replacement host
that could resume unrelated saved work.

Reuse current relevant offline evidence. Do not run the full `scripts/dev check`,
build Atlas, devise a test matrix or add harness code before an ordinary quick
run. A focused check is warranted only when a new or changed runner is needed,
the chosen path has an unresolved failure, or the requested behavior depends on
an unverified mechanism. If live authorization or an enforceable aggregate
allowance is missing, state the smallest useful proposed boundary and obtain the
missing decision before dispatch. A quick-run request authorizes a quick test
within its stated bounds; it does not authorize profile switching, unrelated
work or unlimited retries.

Choose one small, natural-language case that answers the immediate question.
Keep the worker's task free of evaluator instructions or expected answers. Tell
the user, briefly, what the case checks, the selected profile and the actual
attempt/token ceiling before dispatch. Use a fresh private run identity and
ignored `.local/` records. Prefer an existing single-case path. For the mission
runtime, inspect `scripts/qualify_stage4_mission.py --help` and the selected
case before using its `--case ... --allow-live` path. The runner's
`--observe-seconds` is a viewing window: it does **not** cap provider calls or
stop an unfinished mission. Do not describe it as a request budget. If the
available path cannot enforce the authorized attempt boundary at dispatch,
do not launch an autonomous case under a claimed cap; identify that gap and
offer a genuinely bounded path or obtain authorization for the actual behavior.

After submission, observe the exact new run and its saved calls. Count attempted
requests, including failed or uncertain dispatches, against the same allowance
across retries. Stop adding cases at the first unexpected failure; inspect that
failure before retrying. A provider minute wait can remain pending under the
host's saved schedule. An observation deadline is not a mission cancellation or
proof of failure. Preserve unrelated runs and holds.

Then apply `$os-run-details` to the exact state directory and run ID. Its report
must show the delivered request, meaningful agent/tool actions, actual accepted
output, source support, provider-reported usage, timing, and current state. Say
clearly when the run is still waiting or working, when usage is unknown, or when
the behavior was not exercised. A quick result establishes only what this case
actually demonstrated; it is not a broad evaluation or scientific validation.

After reporting each concluded run or observation, apply
[the Linear closeout step](../os-qualify-run/SKILL.md#close-the-loop-in-linear):
update authorized issue checkboxes only from fully met criteria, record meaningful
progress or blockers succinctly, and verify the saved result before selecting
the next test. Preserve incomplete items and explicit private-only restrictions.
