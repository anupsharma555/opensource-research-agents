# Optional planning for research agents

Complex work can benefit from a small, revisable plan. A directly answerable
question can finish without one. The `task-planning` runtime skill supplies
optional guidance to all research roles; it does not install a compulsory planner
peer or prescribe a scientific pipeline.

## Guidance and durable tools

The skill helps a peer frame the requested outcome, distinguish uncertainties
from known evidence, choose useful actions, and revise its approach after new
evidence or criticism. It preserves competing interpretations, negative results
and useful partial outcomes. Planning effort should be proportionate to the task.

Two deferred tools make that guidance inspectable across turns and restarts:

- `save_plan` validates and saves one structured plan for the current peer/task,
  using the exact expected version and a revision reason.
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

Saving, reading or restoring a plan starts no background work, timer, subprocess
or provider call. Completed and cancelled runtime state remains authoritative.
Plans cannot grant tools or enlarge resource authorization. Revisions preserve
their prior versions and exact links instead of silently rebinding old evidence.

The host allows three substantive plan writes without newly observed research
progress. It remembers observed progress across revisions, so rereading a passage
or toggling old source selections does not replenish that allowance. Replay and
identical content do not spend it. Exhaustion
returns a local planning diagnostic so the peer can do useful work, use an
existing durable wait, or publish a qualified result. It neither limits scientific
turns nor disables other capabilities. Each document is limited to 64,000 bytes
and 24 local steps; larger work can stay represented by existing proposals.

## Discovery, compatibility and observability

The normal complete capability catalog remains visible. `save_plan` and
`read_plan` stay outside the core attached tool set. A peer can explicitly request
either, or activate `task-planning` to attach both. Releasing the skill preserves
the plan and any independently requested tool. Existing runs retain pinned
configuration and skill catalogs; new capabilities are not retroactive grants.

The actual serialized research-model instructions include a labelled optional
planning section with its purpose, complex-task triggers, discovery/loading
routes and tool identifiers. Simple tasks may skip planning. This section is
conditional on runtime availability; older pinned catalogs and the legacy
supplied-evidence runtime do not receive unavailable planning guidance.
Repository `AGENTS.md` documents this contract for developers, but is not itself
injected into the model prompt.

The ordinary synthesis and critic catalogs now both contain 45 tools. The critic
gains the two plan tools (43 → 45). Synthesis gains them plus the existing skill
activation/release tools because planning is its first optional skill (41 → 45).
Those existing lifecycle controls add two attached tools for synthesis: the
supplied-evidence setup changes from 10 to 12, and question-only input from 9 to
11. The plan tools themselves remain deferred.
The earlier 43-tool selection checkpoint describes its historical catalog. It
does not qualify these new tools, and this change does not repeat that broad
selection experiment.

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

Tests use the ordinary `MissionService` and default capability setup with
explicitly scripted model responses. They establish storage, discovery,
permission, lifecycle and replay mechanics. They do not establish natural model
planning choice, scientific quality, token savings or long-running endurance.
Live research API calls remain held; natural-use qualification requires a
separate authorized run. No reinforcement learning or training infrastructure
is part of this capability.
