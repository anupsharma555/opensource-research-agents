# Wire and stored-record compatibility

The current [Python contract models](../../src/ora/contracts/models.py) accept `schemaVersion: "1.0"` for commands, queries, events, and research snapshots. They reject unknown fields. The [generated schemas](../../contracts/generated/) and [desktop TypeScript](../../ui/src/generated/contracts.ts) are derived outputs, not separate authorities. Research snapshots and local live request, receipt and state records have generated desktop types. The [contract service](../../src/ora/contracts/service.py) is a local executable example; it does not yet provide a version-negotiated network transport, production migration runner, or upgrade/rollback mechanism.

## Three different versions

| Version or identity | Meaning | Compatibility consequence |
| --- | --- | --- |
| `schemaVersion` | Shape and interpretation of a wire record, currently only `1.0`. | A client or host that does not understand the version must reject it with an actionable diagnostic. An older strict client may also reject a new optional field. |
| `expectedVersion` and entity/operation `version` | Optimistic check and revision of one mission entity or operation. New entities start at version 1 after an expected version of 0. | A stale action is rejected locally; the caller reads the current entity and retries with a new command ID if it changes its action. This is not a wire-schema version. |
| Artifact `id` + `version` + `sha256`, and model `configurationHash` | Names exact evidence bytes and a selected provider/model/reasoning/settings record. | A migration must retain or explicitly supersede those identities. Reinterpreting an old source or silently changing a selected model under the same identity is incompatible even if JSON still parses. |

The event `sequence` and subscription cursor are scoped to one mission; they are not entity versions. A `commandId` identifies one exact submitted command. Its accepted event and receipt are committed together in the example service, so replaying the same command cannot create a second effect. A new payload under the old ID is rejected as a collision. See the [contract guide](contract-guide.md) for states and authority.

## Change procedure

1. **State the exact change.** Name the field, enum value, default, validation rule, payload variant, or state edge and whether old stored records and clients can still read it. Keep an old-record fixture; do not edit its serialized bytes merely to make a new validator pass. An optional field is not automatically compatible because current models use `extra="forbid"`.
2. **Check active work before writing.** Identify missions with pending tasks, peer requests, subscriptions, quota wakes, uncertain operations, and selected-model manifests. A new host must either continue these records with their original identities and settings or produce a scoped, reviewable incompatibility receipt. A process restart, client upgrade, or provider timeout must not be mislabeled as scientific completion.
3. **Choose a versioned reader/writer or an explicit migration.** If the meaning changes, introduce a new wire version and a bounded compatibility reader or migrate retained records with a backup, audit trail, and rollback plan. Preserve original receipt/event provenance. A migration cannot silently turn a source hit into checked evidence, a failed operation into a null result, or an old model alias into a verified unchanged model.
4. **Regenerate from the source model.** Update the Python model and its tests first, then regenerate `contracts/generated/*.schema.json` with [the Python generator](../../src/ora/contracts/generate.py). Regenerate the desktop type from `research.schema.json` with [the Node generator](../../scripts/generate-types.mjs). Review generated diffs rather than hand-editing them. The repository's `npm run generate:types:check` verifies the current desktop output; run `python -m ora.contracts.generate --check` in the pinned project Python environment for schemas.
5. **Test both sides of the boundary.** Validate old and new records against the intended reader versions, including a waiting operation without `nextCheck`, an uncertain operation without `uncertaintySource`, a changed model name/settings with a stale claimed hash, stale entity versions, duplicate commands, conflicting event IDs, and evidence references to missing or mismatched bytes. Test a running mission across host restart and an older/newer desktop client against its compatible host version. A generated TypeScript type is compile-time guidance; it does not replace runtime parsing or authenticated command validation.
6. **Stage deployment and recovery.** Record the app, host, schema, and storage versions together. Verify a backup and restore before changing stored records. An incompatible client should fail the affected command clearly while read-only inspection and unaffected work remain possible where safe. Do not claim rollback merely because an app bundle can be downgraded; the old host must also read the migrated data.

## Synthetic change-impact drill: unsupported command version

This is a **test case**, not a history of a released upgrade. The current `Command` model accepts `schemaVersion: "1.0"`. A deliberately fictional older client sends `"0.9"` to exercise an incompatible command; no v0.9 client, stored record, or migration has existed in this project. The affected field is the command's wire version, not the mission's scientific content or an entity's `expectedVersion`.

The interface owner maintains `ora.contracts.models`, generated schemas and the `ora.contracts.service` validation/receipt example. The JavaScript schema checker consumes generated schemas; the React prototype consumes a generated **read snapshot**, but does not submit commands. A later scheduler, provider adapter and connected review API must consume compatible command/event records. The storage owner must preserve accepted rows and event cursors across a real upgrade. None of these future consumers gains authority to choose a scientific answer.

| Combination | Current qualification | Affected check or recovery outcome |
| --- | --- | --- |
| Current `1.0` command and current fixture service | **Supported in offline fixtures** | [Shared accepted/rejected fixtures](../../tests/contracts/test_shared_fixtures.py) and [service transitions](../../tests/contracts/test_contract_service.py) validate shape and accepted effects; the JavaScript boundary checks the same fixture set with `scripts/dev check`. |
| Fictional client claiming unsupported `"0.9"` against the current service | **Incompatible by design** | [`test_unsupported_client_does_not_erase_pending_work_across_restart`](../../tests/contracts/test_contract_service.py) records a durable `invalid_contract` receipt with a `1.0` repair hint, no accepted task or event, and no scientific veto. |
| Existing `1.0` mission, queued task and unanswered request, then current service restart | **Supported in the same offline fixture** | The test reloads the pending records and rejected receipt, then accepts a compatible sibling proposal. Its original request remains open; there is no data migration or false mission completion. |
| A real older stored schema, future client/host versions, or a running mission during deployment | **Untested** | Requires an old-record fixture, versioned reader or explicit migration, backup/restore, active-operation and cursor checks, plus an older/newer client compatibility test before release. |

The focused fixture uses a synthetic active mission with one queued task and one peer request. Its rejected command leaves the two accepted events unchanged; after restart a compatible sibling adds a third event. That establishes local rejection and continuation, not production transport authentication or a mixed-version deployment. Recheck only the affected command schema, generated JavaScript validator, receipt/replay, pending-state recovery and client view when this boundary changes; unrelated scientific scoring need not rerun unless its inputs change. The [module map](../development/modules.md) identifies each component's owner and later integration boundary.

The example contract service does **not** implement dual-version negotiation or mixed-version deployment. The separate Stage 2 store implements local schema migration and verified backup/restore, with its own [proof boundaries](../development/state-records.md); this does not qualify production upgrades. The offline service proves a smaller boundary: schema validation, version checks, a durable receipt/event transaction, retained fixture-artifact lookup, and ordered replay. The [independent runtime comparison](../decisions/runtime-comparison.md) likewise used scripted calls rather than a live provider or long mission.

## Evidence and wait rules during upgrades

`published_evidence` and `checked_result` are distinct packet kinds in the [model](../../src/ora/contracts/models.py). For either kind, keep the artifact version, hash, locator, and validation record stable or record an explicit superseding version. A source copy's byte check does not prove claim support or study quality; a sandbox artifact's transport success does not prove a valid scientific result. New client fields should preserve that distinction and the separate display of reader report, diagnostics, and provider-returned reasoning.

A descriptive latency class is not a timeout. A request timeout and lease apply to an operation or temporary owner. A waiting operation must retain a durable next-check/wake instruction; an uncertain one retains what outcome evidence is missing. During an upgrade, preserve those wake and reconciliation records, reattach by stable operation ID, and let other eligible branches continue. Failures should produce affected-action receipts or explicit partial results, without a global human approval gate or an arbitrary mission deadline.

## Local live extension (unreleased)

Current task views add optional `waitingUntil` and `waitingOperationReference`.
They describe an exact saved capacity wait, not a promise of dispatch at that
time; older snapshots leave them absent. The retrieval outcome/failure enums add
`oversized_response`, distinct from invalid data or an empty search. Requests
may carry an exact `resumeEsearchRef` for a caller-selected PubMed summary retry;
omitting it keeps a fresh search. These are matched-checkout additions: regenerate
clients with the host, since older strict readers may reject new fields or enums.
Saved provider receipts and source history are not rewritten.

Research snapshots add defaulted `proofKind`, `parentSnapshotId` and `inputQuestion`
fields; source access adds `supplied_text`. New Live request/receipt/state schemas
include requested agent IDs and exact parent/target versions. Existing snapshots
remain readable by the new models. Older strict clients may reject these added
fields or enum values: regenerate and deploy client and local host together. This
is an unreleased, matched-checkout extension, not negotiated wire compatibility
or a production store migration.

`ArtifactView.producerAttemptId` is optional. New local live reports record the
attempt that published them; existing saved artifacts remain readable without
being rewritten. The reader labels a unique identical-text match in an older
snapshot as a match, not an explicit author pointer. Deploy generated clients and
the local host together; this addition does not qualify older strict readers.

## Stage 2 inspection and retrieval extension (unreleased)

`ResearchSnapshot.inspection` is optional and defaults to absent. Its saved-record
references, source/attempt provenance and default-empty `steps` describe accepted
local records. A step contains a plain-language action, actor when known, outcome,
exact input/output references, and only recorded explanation and next action.
Old sample snapshots still validate; an older strict client may reject the new
field. Deploy the generated client and backend from the same checkout.

`RetrievalEnvelope` is a new canonical `1.0` wire record for reference adapters,
generated alongside the other schemas and desktop types. It separates request,
attempt, delivery, result window, item errors, typed usage and sanitized payload
provenance. It does not assert conformance of any live provider.

The local `ora.state` database records a separate schema version. Its tested
version 1-to-2 migration adds scoped review requirements and an index; migration
failure rolls back, and older unsupported readers reject the affected store.
Recorded development snapshots written before event cursors were introduced are
not a released compatibility format. Regenerate paired demonstration exports
from a fresh private store; preserve old development stores as historical proof.

The additive version 2-to-3 migration adds read-only overview views and
`record_origins` for newly accepted records. Existing bodies, revisions and
relationships are unchanged; missing historical proof/build/timing fields stay
unknown. Current read-only record inspection can still open version 2 without
migrating it. Overview views require the tested migration on a copy. The shared
store may use the live database filename while keeping the same record format;
backup manifests preserve that filename, with `state.sqlite3` as the legacy default.

Retrieval records now optionally carry nested scientific metadata, structured
content, explicit omissions and a mapping version. Model settings accept finite
nested JSON, and evidence packets may declare their mission. Earlier records
remain readable by these models; older strict consumers are not qualified to
accept newly emitted fields. Regenerated schemas/types and the shared fixture
checks are the cross-language proof. Live provenance uses a separate versioned
ledger in the same SQLite file; its explicit migration retains historical
payloads and marks reconstructed or unavailable facts rather than replacing
them with current settings. See the [live inspection guide](../development/live-provenance.md).

## Stage 4 inspection fields (unreleased)

`SourceView.accessState` adds `navigation_text` for checked text inspected from
retained venue-navigation HTML, and `document_text` for a checked public HTML
document fetched from an exact browser URL lead. Neither implies a publication
or study identity.
The fetch origin, bytes, source-use decision and reading windows are separate
saved records; older snapshots retain their existing access-state values. Older
strict clients need the regenerated types and host together before reading a
new snapshot that contains this value.

The matched client/host adds optional saved-run title, creation/progress/completion
times, wait explanation and workspace label. Peer entries may include the selected
model, availability, exact configuration reference and role-template identity.
State may expose a workspace identity, workflow version and available retrieval
routes. Missing historical values stay absent; viewer refresh times never replace
research progress. These additions need the regenerated client and host together;
older strict readers are not qualified for the new fields.

`TaskView.version` is optional. New exact-version tasks can identify their own
saved revision. Historical tasks without one continue using their saved mission
version for the legacy follow-up path. A null default must be treated like a
missing field, not passed onward as a task revision.

Task and mission `latencyClass` may be absent or null when no expectation has
been recorded. The reader shows this as not yet estimated, rather than assigning
an unsupported duration. Existing recorded classes remain readable. The optional
task `waitingReason` explains a saved wait independently of the timing estimate;
neither field creates a deadline or changes scheduling.
`TaskView.firstContributionAt` names the first saved contribution when known.
It is distinct from the legacy `firstUsefulAt` designation: accepting a record
does not establish its scientific usefulness.

`LiveStartRequest.evidenceText` now defaults to an empty string. A host that offers
retrieval can accept a question without pasted evidence. The legacy supplied-text
host still explains that it requires evidence. The form uses the connected
host's declared retrieval routes to show the appropriate capability; changing
the request shape does not itself establish a working search adapter.

`requiredAgentIds` is an optional subset of starting `agentIds` on new mission
commands. The shared runtime saves participation policy version 2: starting a
peer does not by itself require that peer to approve or finish before the
requested scope is complete. Explicitly required contributions remain separate.
Run summaries expose both the saved required set and policy version. Missing
historical fields mean older semantics, not a silently inferred empty set.
Optional `continuingTaskIds` identifies work still progressing after the
requested scope is complete; null means unrecorded, while an empty list means
no continuing task was observed. Task read views also accept `deferred`, which
means a peer postponed that work; it is neither queued execution nor cancellation.
The legacy supplied-evidence host rejects nonempty requirement selections and
excludes the empty new field from its historical request fingerprint. A resumed
shared mission also preserves its original policy and request identity.
