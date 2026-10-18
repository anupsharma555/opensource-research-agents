# Optional planning for research agents

Complex work can benefit from a small, revisable plan. A directly answerable
question can finish without one. The `task-planning` runtime skill supplies
optional guidance to all research roles; it does not install a compulsory planner
peer or prescribe a scientific pipeline.

## Guidance and durable tools

The skill helps a peer frame the requested outcome, distinguish uncertainties
from known evidence, choose useful actions, and revise its approach after new
evidence or criticism. It preserves competing interpretations, negative results
and useful partial outcomes. Planning effort should be proportionate to the task. The optional template covers
the goal, observable completion criteria, uncertainties, actions and dependencies,
evidence, revision triggers and a useful completion or pause condition. Peers can
use only the parts that help; the richer guide includes a synthetic example.

Three deferred tools make that guidance inspectable across turns and restarts:

- `save_plan` creates the first structured plan for the current peer/task, with
  `expected_version: 0`. A new operation cannot overwrite an existing plan.
- `modify_plan` edits selected fields and stable step IDs against the exact
  current `expected_version`, with a reason for the revision. Omitted fields are
  retained. Supplied lists replace that field; step additions, edits and removals
  are explicit. The complete candidate is validated atomically, including local
  dependencies, source access and size limits. Invalid edits leave no revision.
- `read_plan` reads the current or an exact historical revision, with current
  lifecycle state and markers for references whose heads have changed.

The plan records the goal, completion criteria, uncertainties, intended steps,
local dependencies, status and exact links to existing evidence, work and peer
records. Its owner and task come from the host's authenticated execution context.
Reference shape and existence do not establish that an agent read a source or
that a passage supports a claim.

Instructions alone cannot provide atomic version checks, replay protection or
permission-aware recovery. The tools add those mechanics using the existing
versioned `working_note` store: one reserved checkpoint identity per peer/task,
with structured JSON text defined by `ora.contracts.models.RuntimePlan`.
They reuse working-note source provenance, context inclusion, bounded readers
and current-use checks. There is no separate plan database or work queue.

## Scientific choices and execution

A plan is advisory. Its local step dependencies describe intended order, while
the existing proposal board and executor retain actual dependencies, ownership
and admission. Listing a peer does not assign work. Existing work proposals,
invitations, peer requests, answer reviews and dependency waits perform those
effects. A plan's completed status does not publish a result, answer a request,
complete a task or close mission scope. A paused plan does not register a wake.

Creating, modifying, reading or restoring a plan starts no background work, timer, subprocess
or provider call. Completed and cancelled runtime state remains authoritative.
Plans cannot grant tools or enlarge resource authorization. Revisions preserve
their prior versions and exact links instead of silently rebinding old evidence.
Partial edits retain prior source provenance even when a visible link is removed;
revoked source access prevents reuse of the retained prose.

The host allows three substantive plan writes without newly observed research
progress. It remembers observed progress across revisions, so rereading a passage
or toggling old source selections does not replenish that allowance. Replay and
identical content do not spend it. Exhaustion
returns a local planning diagnostic so the peer can do useful work, use an
existing durable wait, or publish a qualified result. It neither limits scientific
turns nor disables other capabilities. Each document is limited to 64,000 bytes
and 24 local steps; larger work can stay represented by existing proposals.

## Discovery, compatibility and observability

The normal complete capability catalog remains visible. `save_plan`, `modify_plan`
and `read_plan` stay outside the core attached tool set. A peer can request an
individual tool or activate `task-planning`. Creation is available before a plan
exists; modification is available afterward. The availability notice explains
this prerequisite without removing tools from the complete catalog. Releasing
the skill preserves the plan and independently requested tools.

For each peer/task in new runs, the first successful planning-tool selection or
use includes the
hash-verified concise template in `optional_planning_guidance` on the normal tool
result. It needs no extra model call and does not activate the skill. A durable
receipt prevents another insertion across restart and replay. Explicit activation
loads the concise instructions in the ordinary way and suppresses the automatic
insertion. The existing conversation may retain the original result; one-time
insertion does not promise that its tokens disappear from later context. The rich
instructions and example remain available after optional skill activation.

New runs pin `task-planning` 2.0.0 and the create-only `save_plan` tool version 2.
Saved v1 calls retain the original create-or-replace contract; their skill pins
resolve the exact archived v1 instructions and two-tool selection. The archive
must still match every original content hash. Existing runs never silently gain
new tools or a newer template.

The actual serialized research-model instructions include a labelled optional
planning section with its purpose, complex-task triggers, discovery/loading
routes and tool identifiers. Simple tasks may skip planning. This section is
conditional on runtime availability; older pinned catalogs and the legacy
supplied-evidence runtime do not receive unavailable planning guidance.
Repository `AGENTS.md` documents this contract for developers, but is not itself
injected into the model prompt.

The ordinary synthesis and critic catalogs each contain 46 tools, including all
three planning tools. Plan tools remain deferred and their applicability follows
the saved task state. Under input pressure in a selected planning workflow, default search, board,
peer and record-reader schemas can be deferred before dropping selected input.
Their catalog entries and grants remain available through `request_capability`;
explicit selections and active skill tools stay attached. Historical catalog
counts describe earlier checkpoints, not qualification of these new tools.

Instruction-only runtime skill manifests are explicit and carry no executable
helper. Existing helper-backed packages retain their required provenance and
invocation behavior. Planning schema/types are generated through `scripts/dev
generate`; stored plans are private draft aids, not accepted findings.

Saved revisions emit `runtime_plan_saved` through the existing state transaction with
exact plan/task/actor references and status, without source or plan prose. A
future read-only Live Run view can use those events and authorized readers.
This slice does not implement that UI or claim production uptime.

## Verification boundary

Small output budgets use the versioned ordinary-request tokenizer estimate
(`ora.o200k_harmony_ordinary_canonical_json.v3`). The prior estimator admitted
only the default 8,192 completion allowance; a smaller cap forced the byte
fallback and could block a follow-up after deferred tool attachment. The new
version counts exact canonical bytes for integer caps within the adapter's
existing 1–65,536 bounds. This does not reduce the catalog, change output limits
or bypass quota admission. Historical v1/v2 estimates retain their original
shape gates and counts, and saved byte estimates are not repriced. The host
estimator version advances so an undispatched overflow can be reconsidered
through its existing recovery checks.

Input estimates remain empirical admission estimates, not provider billing
counts. Existing unsupported shapes (including native tools and assistant tool
messages carrying non-null prose) retain the conservative fallback. Scripted
small-budget tests check exact request hashes, reservations, restart, and plan
creation/revision against an 8,000-token observed boundary; they do not establish
that a natural response will choose the same sequence or fit the same budget.

A larger preceding completion can leave less input space even when the overall
test allowance is ample. For an explicitly selected planning skill or tool, the host first retries with
fewer unselected default schemas. Other workflows keep their existing recovery
path. A successful plan-write exchange may also be archived when its exact
same-owner/task checkpoint is still current and readable. The immutable exchange
and checkpoint remain available through saved references; failed edits and
explicitly selected read results are not treated as redundant writes. Complete
request previews include the current task and feedback. They first try the
observed rate allowance with the unchanged completion prediction. If only that
forecast prevents preparation, a provider-feedback attempt retains the required
current packet within the model context window; it does not silently truncate
output or remove the selected read to satisfy an unverified rate estimate.
The shared capacity ledger waits for a clear window and admits that attempt
exclusively. Actual provider refusals and explicit local test ceilings still
apply; see [runtime pacing](stage4-runtime.md). A bounded
minimum-size retry handles a refined estimate that undershoots the required
context. Scripted pressure tests cover plan recovery, targeted revision, explicit
tool selection and the exact admission boundary; they are not live-use proof.

Tests use the ordinary `MissionService` and default capability setup with
explicitly scripted model responses. They establish storage, discovery,
permission, lifecycle and replay mechanics. They do not establish natural model
planning choice, scientific quality, token savings or long-running endurance.
Live research API calls remain held; natural-use qualification requires a
separate authorized run. No reinforcement learning or training infrastructure
is part of this capability.

The [intermediate planning case](../benchmarks/planning-intermediate/controller.md)
separates the initial worker request, later constraint update and withheld review.
It is prepared for bounded natural-use qualification, not evidence that a model
has already used the capability productively.
