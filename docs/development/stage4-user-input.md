# Submit text to an active research task

The local mission backend accepts deliberate user input through authenticated
`POST /api/live/user-inputs`. This command is separate from ordinary discussion,
starting a mission, and creating a follow-up. The Atlas composer is not part of
this backend change. Terminal clients can submit the same saved JSON with
`scripts/dev mission input --input <saved-command.json> --state-dir <local-state-directory>`.

## Exact target and purpose

The command names `requestId`, `runId`, `taskId`, `targetAgentId`,
`expectedTaskVersion`, `kind`, and the original `text`. `targetAgentId` is the
saved agent instance ID, not its display name. Use the current board-task
version. The canonical Python request and receipt models own the contract;
`scripts/dev generate` produces JSON Schema and TypeScript.

Choose `direction` for an amendment within the saved task's authorization.
Choose `unverified_pasted_evidence` for material to assess. Pasted text does not
become a checked publication, source copy, citation, or new source-use grant.
Directions do not change the model, credential profile, available permissions,
or the run's original question and scope. Use an explicit follow-up for scope
expansion.

The server supplies the sender origin and acceptance time. Clients cannot set
an agent sender, system authority or source-use permission. Authentication uses
the existing local launch session: browser writes also require the same origin;
bearer clients use the existing authenticated local API. This is a local
operator boundary, not a multi-user identity or access-control service.

## Acceptance, delivery and recovery

A successful command returns `queued_for_next_boundary` and the exact saved
input reference. This means the host accepted the submission, not that a model
has read it. Repeating the same request ID and command returns the original
record. Reusing that ID for different text or a different target conflicts.
Records survive host reopen and retain the original task/version relationship.

A new submission cannot silently reopen cancelled, deferred or completed work
or bypass an operator hold. Ordinary resource waits retain their pacing. A
submission targets one task and agent; it does not broadcast to siblings or
silently transfer into a later run.

If input arrives before a queued request is dispatched, the affected request is
prepared again with current context. If a model call was already sent, its saved
bytes remain unchanged. The input becomes available at the next eligible
boundary. A final-looking response produced without that newer input is retained
as historical work and cannot silently close the affected task before a fresh
turn.

Context packing can include the exact text or an explicit saved reading
reference. Both the omission list and record reader enforce the exact target.
Large inputs do not require automatic insertion of all text into every model
request. Use saved inclusion and dispatch records to distinguish acceptance,
preparation and actual sending; none of those establishes scientific uptake.
The new event carries references and scope identifiers, not the raw text.

If the preferred request size omits the latest scoped direction, preparation
can retry the same source and tool plan within the already verified input
limit. It adopts the larger request only when that direction gains exact text
or a callable reading route while preserving peer input, delivered peer reads,
host feedback and the selected tool descriptors. Prior completion reservations
and rate headroom still apply; this does not raise a provider or campaign cap.

The final packet is checked again after note and peer-context refits. A preview
alone does not establish delivery. The saved call budget distinguishes exact
direction text from a callable reading route; if neither can be preserved within
the existing bound, that call stops before dispatch rather than sending an input
that silently omits the direction.

## Verification boundary

Focused offline tests use the real authenticated handler without opening a
network listener, plus injected model responses through the normal mission
service. They exercise targeting, replay/conflict handling, lifecycle state,
request freshness, exact context and permitted recovery. They do not establish
natural model interpretation, browser usability, live provider behavior or
multi-user security. Live testing remains separately authorized.
