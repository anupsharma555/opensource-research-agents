# Shared contract guide

The [Python models](../../src/ora/contracts/models.py) define the current wire shapes. [Generated JSON Schemas](../../contracts/generated/) expose those shapes to other clients, and the [contract service](../../src/ora/contracts/service.py) is an offline example of command validation and durable receipts. This is an unreleased version 1.0 contract, not a running research host or a network API. A JSON `actor.id` is a claim made by the caller; production transport must authenticate it and enforce permissions.

## What crosses the boundary

| Record | Purpose | Current rule |
| --- | --- | --- |
| `Command` | Requests one change to a mission entity or operation. | Carries a stable `commandId`, actor, mission, optional task, `expectedVersion`, pinned artifact references, and one discriminated payload. |
| `Receipt` | Says whether that change was accepted, rejected, deferred, or was a duplicate. | An accepted change, its event, and its receipt are saved in one SQLite transaction by the example service. A deferred operation retains a stable ID for later observation. |
| `Event` | Records an accepted change for replay and inspection. | Has a sequence within its mission, an entity ID and version, command ID, actor, state, reason, and evidence references. Receiving an event is not acknowledgment that a client applied it. |
| `Query` and `Subscription` | Describe read intent and a mission event cursor. | These are shapes, not implemented HTTP routes. A client should persist its view and cursor together before acknowledging progress. |
| `JobRequest` and `OperationResult` | Describe an external tool or sandbox operation and its later result. | An accepted request can outlive the caller. The operation has its own state and version; a completed model turn or client disconnect does not finish the mission. |
| `ResearchSnapshot` | Supplies a read-only review projection to the desktop sample. | Reports, sources, attempts, diagnostics, and provider-returned reasoning are separate fields; a client view does not confer command authority. |

New entities use `expectedVersion: 0` and begin at version 1. A subsequent `Transition` must name the current entity version; an accepted transition increments it by one. An operation start likewise uses 0, and each accepted result uses its current operation version. A stale version is rejected for the affected action, leaving other work eligible. Repeating an identical command ID and body returns a duplicate receipt referring to the original effect; reusing that ID for changed content is an ID collision. A client changing an action should assign a new command ID. Artifact references include ID, version, and SHA-256; the example service resolves these against retained fixture bytes before accepting evidence-bearing commands.

The event sequence is **per mission**, while entity versions track one entity. The example includes the mission ID in each event ID. A combined index should still retain the explicit mission and event identifiers. A client should request events after its saved cursor, detect gaps, replay in order, and durably save its projection before moving the cursor. The [replay projection](../../src/ora/contracts/replay.py) rejects duplicate IDs with different contents and regressing entity versions within its one-mission stream. It is an in-memory example; persistent client projection and atomic cursor commits remain integration work.

## States and who may change them

The table summarizes the executable transition sets in the [service](../../src/ora/contracts/service.py). `completed`, `cancelled`, `failed`, or `withdrawn` are terminal only where the listed set has no outgoing edge. An accepted partial report is a useful milestone, not mission completion.

| Entity | Main allowed progression and backward edge | Current authority check |
| --- | --- | --- |
| Mission | `active ↔ waiting`; either can become `completed` or `cancelled`. | The host cannot make a scientific disposition. Positive completion needs the mission's saved terminal authorization and disposition of pending tasks, requests, and operations. |
| Branch | `open ↔ waiting`; either can complete/cancel. A completed branch can reopen as `open` for new evidence. | A peer or user decides scientific disposition; the host cannot close or reopen it. |
| Task | `queued → running/waiting/cancelled`; `running → waiting/completed/failed/cancelled`; `waiting → queued/running/cancelled`; `failed → queued/cancelled`. | The host may record mechanical running, waiting, failed, or queued changes. A peer or user supplies scientific completion. |
| Attempt | `running ↔ waiting`; either can complete, fail, become uncertain, or cancel. `uncertain → completed/failed/cancelled`. | The host is permitted to record these mechanical changes. The example does not yet enforce host-only actor identity. An uncertain outcome cannot silently regress to running; it needs reconciliation or a new attempt. |
| Operation | `running ↔ waiting` with same-state progress updates; either may finish, fail, become uncertain, or cancel. `uncertain → completed/failed/cancelled`. | The saved attempt and owner token must match. The result must retain the operation's originating task ID. Production still needs authenticated ownership and effect reconciliation. |
| Request | `requested → delivered/withdrawn`; `delivered → acknowledged/answered/withdrawn`; `acknowledged → answered/withdrawn`. | The host may record delivery. Acknowledgment and answer require the addressed recipient. An answer must link a retained artifact; delivery or acknowledgment alone is not success. |
| Invitation | `offered → accepted/declined/countered/withdrawn`; `countered → accepted/declined/withdrawn`. | Accept, decline, and counter actions check the addressed recipient. |
| Report | `draft → partial/published/withdrawn`; `partial → published/superseded/withdrawn`; `published → superseded/withdrawn`. | A peer or user makes these scientific dispositions; partial output may be shown while the mission continues. |

Peers or users propose scientific tasks and originate peer requests, invitations, responses, and notifications. The intended host validates identity, versions, available resources, and allowed transitions; it does not choose the source, hypothesis, critique, or next scientific task. The current example checks versions, transitions, model selection, and fixture artifact references. It permits multiple eligible branches and local repair after a rejected action. It does not impose a fixed role sequence, a mission turn cap, or routine human approval. Authentication, role authorization, resource admission, and actual peer delivery are production work beyond the trusted-actor fixture.

## Evidence, model, and wait semantics

An `ArtifactRef` proves only that a specific retained copy can be found and hashed. The example `EvidencePacket` distinguishes `published_evidence` from a `checked_result`, and requires a locator and validation record. `published_evidence` must refer to a permitted source copy and its passage or record locator; `checked_result` must refer to the relevant experiment output and its validation. The fixture verifies byte hash, kind, and validation-record identity. It does **not** verify a scientific claim against a passage, a study's validity, source-use rights, or a sandbox result's protocol and units. Those checks belong to later source and scientific-result services. A search hit, abstract, code exit, and checked result must not be silently treated as the same evidence depth.

`ModelSelection` names provider, model, reasoning effort, settings, and a hash of those values. The service recomputes that hash and compares it with the mission's saved selection before admitting an operation. The hash detects a changed request; it cannot guarantee that a hosted model alias points to unchanged weights. The actual provider response and any retainable returned reasoning require separate provenance and access controls.

`LatencyRecord` categories describe expected task complexity; they are not deadlines. `JobRequest.requestTimeoutSeconds` bounds one request, and a lease bounds temporary ownership, not mission lifetime. A `waiting` operation needs `nextCheck` with a durable wake or alternative action; an `uncertain` operation needs `uncertaintySource`. These conditions are expressed in both Python validation and generated JSON Schema. Quota wait, peer wait, or a failed optional source stays scoped to the affected work; other eligible tasks and useful partial reports continue. Explicit mission cancellation and positive scientific completion use different states and receipts.

For changes to these wire shapes, saved records, or client generation, use the [compatibility guide](compatibility.md).
