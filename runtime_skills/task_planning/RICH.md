# Task planning

Planning is an optional aid for uncertainty, dependent actions, or collaboration. Any role may plan its own task. A simple supported answer can finish directly. A small task may need a goal and a next action; a longer task may benefit from explicit dependencies and evidence links. There is no compulsory planning peer, fixed scientific sequence, or mandatory template.

## Optional good-plan scaffold

Adapt these prompts to the task; omit unnecessary detail. The attached tool schema defines valid checkpoint fields, not a required research method.

- **Goal:** What useful decision or output is requested, for whom, and within what scope?
- **Completion criteria:** What observable result would answer the request? Prefer "compare the two alternatives with supported findings and unresolved limits" to "understand the topic better."
- **Uncertainties:** Which missing facts, rival interpretations, assumptions, or access limits could change the answer? Keep negative results and dissent visible.
- **Actions:** Choose a few concrete steps that produce evidence, a comparison, or a decision. Give steps stable IDs. Separate intended work from work actually performed.
- **Dependencies:** Record only genuine prerequisites. Link existing source, work, or peer records at their exact versions. Listing a dependency does not dispatch work or begin waiting.
- **Revision trigger:** What new evidence, feedback, failed route, or changed constraint would justify changing direction?
- **Completion or useful pause:** What is supported now, what remains uncertain, and what condition would make further work useful?

For a synthetic feasibility comparison, a small plan might identify the decision, compare two supplied evidence records, check staffing and access assumptions, then recommend an option with limits. If a later staffing reduction invalidates one option, revise the affected step and uncertainty while retaining completed evidence checks. Do not recreate unchanged steps, erase an inconvenient result, or claim that a planned study has been conducted. This is an illustrative approach, not a fixed tool sequence or a model-visible answer key.

## Create, modify, and recover

`save_plan` creates the first checkpoint for this actor/task; it cannot replace an existing one. `modify_plan` applies targeted changes to an existing checkpoint. Use the current `expected_version` and explain why the change is warranted. Top-level lists replace those fields; `update_steps` changes named existing steps, `add_steps` adds new IDs, and `remove_step_ids` removes selected steps. Other fields and steps remain intact. Correct affected dependencies in the same edit; unknown steps, conflicting changes, and cycles are rejected atomically. History is preserved even when a step is removed from the current version.

Use a revision returned by a successful save/read/modify; do not add a read solely to confirm a revision you already hold. After lost context or a stale-version rejection, `read_plan` provides the exact checkpoint and changed-reference markers. Do not overwrite another actor's plan. References establish identity, not that a source was read or that it supports a claim. Source-use restrictions still apply to retained plan text and replay.

## Keep planning proportionate

Existing `propose_work`, `revise_work`, `peer_request`, and `wait_for_dependencies` own execution and communication. A completed plan does not publish an answer or close a task; a paused plan does not create a wake. Use normal publication or waits when appropriate. Plans add no permissions or independent scheduler.

Change a plan because inspected evidence, a peer's criticism, a completed dependency, or a new constraint changes the next useful action. Preserve exact source versions, counterevidence and unresolved disagreement. Explain the problem, options, evidence, trade-offs and next observable check without exposing private model reasoning. Repeated editing without new progress can receive a local diagnostic; other research remains available.

Offline tests establish these mechanics. They do not establish natural planning quality, token savings, or long-running mission reliability.
