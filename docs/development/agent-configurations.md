# Agent identities and effective configurations

An unsupported optional tool schema is recorded as an unavailable route, while
valid shared and specialist tools remain usable. Its descriptor cannot grant
permissions or become executable merely because a model requests it. Required
malformed tools still produce a configuration error rather than silently
weakening their declared argument checks.

The local identity and configuration layer records *which named agent instance*
prepared a call and *which exact inputs that call would use*. The mechanical host
still owns execution, durable transactions, provider credentials, peer delivery
and source permissions. Scientific judgments remain with the peers. These
offline fixtures do not demonstrate a deployed nine-agent mission or model
tool-choice quality.

## Identity

An `agent_instance` has a permanent generated ID, one mission-scoped display
name, a role, a run ID and an instance kind: ordinary role, acquisition-lane
worker, or temporary specialist. Names are normalized for collision checks;
they are never routing IDs. Two workers in one mission cannot claim the same
normalized name. A second registration request with the same operation ID
returns the first result. A different request using that name conflicts. Another
mission may use the same display name.

The nine roles come from the existing role registry. Lane workers keep the
literature-acquisition role plus a lane ID. A temporary specialist uses a
distinct role label. Replacement creates a new instance and a `replaces` link to
the old version; it must preserve the role, instance kind and lane. Old names
and references remain attached to the original instance. Renaming is not
supported. A retry or model change is therefore not counted as another
independent scientific reviewer.

A replacement can receive an informational `agent_handoff` record naming the
unfinished task and exact saved context references. It links the predecessor,
successor and task without reassigning, completing or unlocking work, and
without granting source access. The scheduler and source-rights boundaries
remain authoritative. A foreign-mission context fails locally while the task
and other context remain available.

Mission, run, task, attempt, call, configuration and instance IDs are different
concepts. An `agent_activity` record can link an exact version of a message,
tool receipt, claim or artifact to an agent and its task/attempt/call context.
The target must explicitly own that mission, actor, task and attempt, or carry
an exact originating call version that proves those fields. Missing ownership
is rejected; a caller cannot attach another agent's work by supplying its ID.
The [recorded Stage 2 workflow](stage2-verification.md) uses these same stable
identities in the React projection and Codex inspector. That integration remains
separate from qualification of live instances in a running research mission.

## Saved configuration and call view

An `agent_configuration` snapshot stores the provider and endpoint, selected
model ID, finite nested-JSON generation and reasoning settings, base instructions and their
hash, selected skill instructions and file hashes, granted tool schemas and
versions, a task scope, exact input-record references and a canonical
configuration hash. A catalog snapshot records registered tool versions and
which local handlers were actually available. Executable handlers also need an
immutable host-supplied implementation digest or build reference in the saved
descriptor; changed code under an unchanged tool name/schema/version reports
drift when the host supplies its new identity. The loader does not derive trust
from a Python callable name. It does not store a provider key or load one from
an environment variable. Settings keys are checked recursively before rejecting
common credential aliases, including `apiKey`, `X-API-KEY` and `token`. Arrays,
decimals, booleans and explicit nulls remain intact. This local check does not
decide whether the provider/model supports a parameter; selected-model adapter
qualification owns that decision. An
endpoint cannot contain user info, query parameters or fragments; nonlocal
endpoints require HTTPS. The host must keep credentials in its separate secret
boundary and enforce source-use rights before assembling model input.

A versioned `agent_call` refers to that saved configuration and copies the
effective provider, model, settings, instruction hash, loaded skills, exposed
tools and input refs. Common board, peer request/invite, subscription, claim,
evidence lookup and capability-discovery tools are granted and exposed for
every role and temporary specialist. Specialist tools may be permitted but
left out of the first model tool view. `discover` reports permitted tools and
whether they are exposed; `expose_tool` appends a new immutable call-manifest
version without changing earlier views. A tool invocation identifies the exact
manifest version it came from. Instruction text or a skill's trigger never
creates a permission grant. Selecting a skill also requires its own host grant,
even when the role manifest lists it.

`discover_capabilities` and `request_capability` are callable common tools.
The host supplies their mission and call context; the model supplies only the
declared arguments. Discovery reports the saved grants and current exposure.
A permitted request adds a new call-manifest version and receipt in one state
transaction. Denied or stale requests cannot grant a tool, alter a selected
model, or change an earlier manifest. Other common board and peer actions still
need their production host handlers; their schema exposure here is not delivery
proof.

An explicit configuration change records its predecessor and reason under the
same agent instance. Earlier calls still reference their original snapshot.

The available catalog, selected configuration, loaded skill snapshots, exposed
call view, actual invocation and result receipt are separate evidence. A locally
registered pure handler can be called after schema and grant checks; an
unexposed, denied, missing or effectful tool receives a scoped rejection.
Tool schemas support nested objects and arrays, decimals, nulls, optional
fields, declared additional properties, `enum`, `const` and `anyOf` within an
explicit local JSON-Schema subset. Empty strings are allowed unless a field
declares `minLength`. An unsupported keyword rejects that capability's
registration; an invalid argument rejects only its own call. A completed local
invocation keeps its finite JSON arguments, hash, exact manifest version and
result. Rejected or failed invocations keep a hash and status without retaining
the unaccepted argument body.
The two existing project-owned skill helpers can likewise run locally against
an exact saved `EvidencePacket`; their receipts record a checked candidate
result, not a scientific judgment. An invalid or unserializable handler result
is discarded while a safe failed/rejected receipt remains inspectable.
External effects and provider calls require the host operation protocol.
`jevk5-0.2` is registered as an **optional, unavailable** classification route
with its own model identity. Without its adapter and explicit grant, it does
not enter a research call or change that call's selected model.

Selected skill snapshots keep `value_test_plan_ref` separate from
`value_test_result_ref`. The first names a matching case and SHA-256 of the
project-owned qualification packet when present; it describes an intended test,
not an observed outcome. The second requires an exact saved evaluation record
matching skill version, role, model and task scope. Linking that result does not
automatically promote a provisional skill to qualified. The prior selected-model
comparison used earlier skill versions, so current packages remain provisional.

Existing role/skill manifests are the source of truth for skill versions,
instructions and helper provenance. Loading a skill in this standalone app is
explicit; Codex's `.agents/skills` discovery does not register it here.
[OpenAI's current skill guidance](https://learn.chatgpt.com/docs/build-skills)
describes name/description discovery followed by instruction loading, while
[Groq's local-tool guidance](https://console.groq.com/docs/tool-use/overview)
places tool execution in application code. The current
[Groq reasoning guide](https://console.groq.com/docs/reasoning) documents
model-dependent reasoning parameters. These sources were read on 2026-10-08;
provider support must be rechecked during live qualification.

## Resume and qualification

`resume(call_id, available_models=...)` loads the saved call and configuration.
It compares pinned tool and skill versions with the current catalog and reports
missing or changed dependencies as drift. A changed default does not rewrite
the call. A retired model ID is reported as unavailable; the caller must make
an explicit new configuration/attempt for any fallback. A hosted alias can
still change weights behind an unchanged ID, so a matching name alone is not
proof of identical model behavior; Groq's
[deprecation policy](https://console.groq.com/docs/deprecations) documents
automatic upgrades for some model IDs.
An implementation reference is an attestation from the host build. A host that
reuses the same reference for different code can still conceal drift; production
packaging must bind that reference to the actual installed adapter bytes.

Selected capabilities default to **provisional**. A `qualified` label requires
an exact `capability_evaluation` record matching capability, role and model and
matching the saved task scope. A linked fixture record proves that the
loader checked those fields; it does not prove scientific usefulness. That
requires independent, task-appropriate evaluation. An unavailable optional
route affects that route, while unrelated work may continue.

Run focused offline checks with the repository's configured interpreter:

```sh
.venv/bin/python -m pytest tests/agents -q
```

The fixtures cover all nine roles and a temporary specialist, six parallel lane
identities, collision and restart, replacement history, saved model/settings,
changed tool implementation refs, optional classifier absence, direct
model-requested discovery and exposure, permission rejection, safe invalid
results, nested scientific inputs, actor-to-tool/skill/output links,
informational replacement handoff and actual local execution of search,
critique and data-feasibility callbacks. They
do not call a model or provider, measure scientific benefit, verify production
authorization, or show React/Codex rendering of live instances.
