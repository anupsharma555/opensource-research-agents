# Local peer collaboration contract

The proposal board and mailbox are offline Stage 3 components over the existing
`StateStore`. Registered agent instances create and revise scientific proposals;
the mechanical host checks identity, exact versions, dependencies, ownership and
atomic writes. It does not rank hypotheses, approve findings or decide when
scientific exploration is sufficient. The reporting peer can be idle while other
peers propose, critique and revise work.

`ProposalBoard` saves `board_proposal`, `board_invitation`, `board_claim`,
`board_dissent` and `board_outcome` records. An invitation names objective,
expected artifact and prerequisite versions. Accepting one permits a recipient
to claim but never creates a lease by itself. `claim` updates the shared
StateStore lease in a short transaction. A counter is a terminal response to
that invitation; the sender can offer a revised invitation for acceptance.
An addressed peer must name a current accepted invitation when claiming that
proposal; an undirected peer may still self-claim open work.
A revision rejects acceptance of older invitations and durably revokes any
older claim generation, including if the wall clock moves backward. A result needs the current mission,
owner, task ID and generation fence. `accept_result` accepts a callback that
receives the same `Transaction`; the callback may save artifact, message,
tool result and trace records before the board saves its outcome. If validation
or the precommit fence fails, all related writes roll back. Provider/model work
must happen before this short transaction. Compound callers provide a stable
`related_request` JSON descriptor of their exact inputs and outputs; a retry
with changed effects cannot reuse one operation ID. The caller must fingerprint
every callback input and expected write, including the exact configuration and
implementation reference; the function object itself is not inspected.
`ready_work` checks saved proposal
dependencies and current claims; it does not choose scientific priorities.

Proposal revisions and dissent retain exact prior versions. An independent
corroboration task is a separate proposal with a dependency link. Alternatives
use `branch_of` without sharing ownership. A contradiction names an affected
exact record and a contradiction record; `reopen` marks affected proposal
descendants for review and versions their open state. Its scope follows saved
dependency links, so unrelated work stays current. Open and deferred work
always carries a next action. A branch rejection returns a typed receipt and
persists a scoped rejection/diagnostic; it does not pause siblings.

`PeerMailbox.send_message_tx` and `send_request_tx` save message, pending
outbox and request in one StateStore transaction. The envelope has distinct
message ID, sender and recipient instance IDs, mission and correlation IDs,
optional task and exact evidence references, and arbitrary finite JSON payload.
Unknown scientific fields are retained in the payload. Malformed content
rejects that exchange. The recipient's source lookup must separately enforce
authorization; a reference cannot grant source rights, tools, outreach, model
routes or execution permissions.

Delivery and acknowledgment only update message state. `mail_request` remains
unanswered until an answer is linked to an exact artifact version and reviewed.
An answer can be partial; declined and replaced requests remain inspectable.
For an offline requester, a reviewer with the same registered role may assess
the answer only with a current exact `agent_availability` record for that
requester, marked offline and host recorded, and if the reviewer is independent
of both requester and responder predecessor lineages. The caller also supplies
a strict Boolean offline flag. This local API checks the saved fact's shape and
version; an authenticated host must control who may write that fact. It does
not itself establish caller authentication or model competence; those belong
to the future host and selected-model role qualification. Saved
timestamps expose created-to-delivered, created-to-answered and
created-to-resolved durations plus current wait, expected latency category and
last useful progress. There is no automatic deadline or mission-wide timeout.

Subscriptions save a peer's task, claim or artifact interests and cursor.
`catch_up` scans persisted mission events after that cursor and returns only
matching or directly addressed events plus the scanned position. The caller
acknowledges that position after processing. Catch-up durably saves the scanned
ceiling; a cursor acknowledgment cannot jump beyond it. Repeating or reordering a delivery
does not create another request or answer. A temporary notification loss is
repaired by replay; no model call is broadcast to every peer. Event producers
must include `subjects` or explicit `recipient_ids` in their event body for
subscription routing. Answer, review, decline and redirect events address the
affected requester or recipient. Only the original requester or its recorded
independent replacement reviewer can redirect an open request.

A redirect copies the original message's exact task and evidence references.
Its payload keeps the complete original scientific JSON under `original_payload`
and separate routing details under `redirect`, including the original request
and message versions. Nested scientific keys are not overwritten. The new
message also links to the original message; the old request and any partial
answers remain inspectable. This preserves context, not access rights, and a
later source or proposal version does not silently replace the original inputs.
Invitation decisions directly notify both participants. Delivery and
acknowledgment events also notify the sender, even without a matching task
subscription. These events support replay; a continuously running dispatcher
and inclusion in model context are still host integration work.

The internal envelope is transport-independent. An optional future A2A adapter
can map remote agent ID to local instance ID, context ID to mission ID, remote
task ID to local proposal/request IDs, and message/artifact IDs to distinct
local message and versioned record IDs. A remote stream notification is a hint;
the saved StateStore event and record history remain authoritative. A delivered
message, completed remote task and accepted scientific outcome are different
states. No external peer or network framework is required for local exchange.
This implementation does not measure transport overhead or exercise A2A.

## Frozen role-qualification handoff

[`collaboration-v1.json`](../../tests/coordination/fixtures/collaboration-v1.json)
contains synthetic predetermined packets for disagreement, redirected requests
and defer/replan behavior. Its `expected_state_changes` and
`mechanical_failures` are test oracles for this local board. They do not contain
actual model answers or prove that a selected model will make useful scientific
choices. Keep the packet version fixed during a comparison and record the
selected model/configuration and input versions in the later run receipt.

Judge scientific usefulness separately from mechanical correctness. For each
scenario, score **0** when the peer omits or contradicts the behavior, **1** when
it mentions the right action without enough evidence or follow-through, and
**2** when it gives a source-grounded action with a concrete next step. Score
these four dimensions independently:

| Dimension | A score of 2 requires |
| --- | --- |
| Evidence restraint | Distinguishes what the exact source version supports from what remains unknown; does not infer an effect from direction alone. |
| Productive disagreement | Preserves a defensible alternative or critique and identifies evidence that could resolve it. |
| Peer coordination | Routes a bounded question to an appropriate independent peer, interprets partial or declined replies, and does not equate delivery with fulfillment. |
| Replanning | Names a feasible next action or qualified deferral, keeps useful sibling work moving, and revisits affected claims when new evidence arrives. |

The later evaluator should retain the peer's visible actions, exact saved refs,
expected transitions, observed transitions and dimension scores. Mechanical
passes are prerequisites for safe state handling, while the scientific scores
are a separate assessment. No live provider calls or model competence checks
were performed for this handoff.
