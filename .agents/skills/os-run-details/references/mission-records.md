# Mission-runtime record map

Read only for a compatible local `ora.mission.MissionService` state directory.
Use the repository's pinned environment and current command help. `scripts/dev
mission status --state-dir DIR --run-id RUN_ID` and `scripts/dev mission calls
--state-dir DIR --run-id RUN_ID` are documented read-only views when the local
host connection is available. If unavailable, inspect the exact saved store via
`ora.state.StateStore(DIR, read_only=True)` or the documented read-only state
inspector; a failed connection does not prove the run stopped. Never instantiate
a writable host simply to read its state.

| Question | Saved authority / limit |
| --- | --- |
| What did the user ask? | `mission_run` request/profile; a child also pins `parent_run_id`, `parent_snapshot_id` and target. Read the original submission/runner plan if available, but reconcile it with the accepted run. |
| What reached the model? | Exact `model_request_intent` request blob plus `context_inclusion` and source-use decisions for each `agent_call`. Inspect captured packet content and source excerpts, not only source availability. The request intent proves preparation; pair it with dispatch/capture records before saying it was sent. |
| What happened? | `agent_attempt`, `agent_call`, `model_response_receipt`, exact `context_tool_result`, task/control history, peer messages and accepted report/snapshot links. `mission calls` uses `ora.mission.call_inspection.inspect_calls` to return a redacted call projection; inspect exact linked blobs only when needed. |
| What does a requested tool do? | Pair the saved tool name, schema and arguments with its matching handler in `ora.mission.service` or the delegated module. Explain expected ID/input type, local or external effects, and the observed result. If current code differs from the saved run, label the version uncertainty. |
| Did a provider request occur? | `model_dispatch_attempt` is a pre-provider marker. A captured response proves a returned result. A failed/uncertain request may lack usage. Reconcile with a runner's transport ledger when present; do not equate prepared, marked, captured and billed counts. |
| What is final and continuing? | Check `report` plus accepted output references in the exact snapshot and current `mission_task_control` phases; a saved report, task completion and whole-run status are different boundaries. |
| What did it cost and how long? | `model_response_receipt.usage` and exact call timestamps, plus runner attempts and saved waits. Cached input and reasoning completion are subsets of their parent totals. Missing or provider-unreported fields stay unknown. |

For a parent/update chain, inspect each run separately and then state what source
copies and accepted parent output were inherited, what later evidence was added,
and which model input actually contained it. Verify event timing against the
first child dispatch. The same source text may be delivered more than once;
distinguish retained meaning from recovery after lost context.

`os-qualify-run/references/mission-runtime.md` owns the detailed mission
inspection prerequisites and optional private receipt helper. Its receipt
counts metadata and selected small tool results, but does not read the full
model input or report body for this in-chat explanation. `os-groq-usage` owns
period-wide usage accounting. Neither helper should be mistaken for a complete
source-support or current-host proof.
