# Peer contribution review

`ContributionReview` adds a small optional record and a read-only proposal
context query to the existing Stage 3 board and mailbox. It is an offline local
service over `StateStore`; it does not call a model, wake every peer, decide a
scientific result or change how a request is answered.

## What the records mean

`record_uptake` saves an append-only `peer_update_uptake` record for one
registered peer, an exact version of a message or board update, a disposition
(`considered`, `used`, `challenged`, `deferred` or `inaccessible`), a short readable
statement, optional exact evidence references and an optional reported effect
reference. The service strips inline credentials, contact details and links
from the statement before saving it. Detailed scientific content remains in
the referenced message or artifact behind its own access checks. A shared
uptake record accepts proposal-linked messages and references with an existing
safe diagnostic projection, including source records, Stage 3 input records,
traces and trace summaries. It rejects restricted reasoning, private
instruction records and unshared direct-message references, including those
offered as evidence or a reported effect. An unrecognized record kind without
a safe projection is reported as unsupported, rather than called restricted.

The disposition is **that peer's statement**, not proof of source access,
entailment, causation or scientific quality. Missing uptake is unknown. Neither
delivery nor acknowledgment creates an uptake record. A peer may amend their
view with a new append-only statement; previous versions and alternatives stay
visible. An update about a shared proposal is available to registered peers in
that mission. An unshared direct message has no shared uptake record; its
sender and recipient may still use the existing direct mailbox. A reference
never grants access to source bytes.
Like the existing board and mailbox, this local service checks saved agent
identity and mission membership but assumes a future host authenticates the
caller before supplying `actor_id` or `viewer_id`; it is not an authentication
service.

`send_clarification` uses `PeerMailbox.send_message_tx` to add a correlated
message and pending outbox record inside one transaction. The recipient of an
open request sends the clarification to its requester. The original request is
left unanswered or in its prior partial/review state; the clarification is a
question, not an answer artifact or review decision. A proposal-context read
marks versions of this message with `message_role: clarification` and an exact
`clarifies_request_ref`, using the saved `clarifies` link rather than its private
payload. Clarifications are excluded from ordinary-update uptake.

## Proposal context read contract

`proposal_context(mission_id=..., proposal_id=..., viewer_id=...,
after=0, through=None, limit=100)` requires a registered viewer in the same
mission. It returns schema `ora.proposal_context.v1`, the mission/proposal/viewer
IDs, `visibility`, the pinned event high-water mark `through`, a page of
`entries`, `next_cursor`, and `has_more`. Each entry has event `sequence`, `type`,
a plain-language `summary`, and projected `records` with exact references,
availability and safe data. The existing `project_record` policy supplies board
and mailbox fields. Readable proposal, critique and request prose is additionally
passed through the same contact/link stripping helper used by traces. Arbitrary
message payloads, result bodies, restricted source text and provider-returned
reasoning are excluded. The new uptake projection shows its already-sanitized
statement and exact input/effect references.

Events are read in saved sequence order, so a late peer can start at zero and
discover earlier proposal, critique, message and result history. Delivery and
acknowledgment events have no task subjects in the mailbox; this reader
resolves their exact saved message version to recover the proposal link while
keeping its payload out of the projection. Delivery, acknowledgment and
scientific uptake remain separate entries. `through` is
fixed on the first page and must be reused to continue that snapshot. `has_more`
means unscanned mission events within that boundary; a page can contain no
proposal entries and still require another scan. `next_cursor` is the last
scanned event sequence, including unrelated events. A fresh call with
`after=<previous through>` and `through=None` finds later messages and proposal
versions, including records whose IDs sort before older IDs. Refresh is
read-only. One page is capped at 200 relevant events and scans a bounded number
of unrelated events; no scientific task or run limit follows from that page size.

The query returns references and projected details, not an authorization token.
When a source reference appears, an authorized source lookup must still decide
whether the viewer may read its bytes. An absent referenced record is labeled
`record_unavailable`; omitted payload remains labeled by the diagnostic
projection rather than silently represented as inspected content. Derived
agent/group/run explanation coverage and Atlas/Codex entry-point wiring are
provided by `TraceJournal.summary_coverage` and the shared recorded review
described below. These fields declare an event boundary; they do not prove that
every relevant scientific source was considered.

## Inspect in Atlas or Codex

The generated fixture is available in Atlas under **Previous samples → Open
shared contribution review → Communication**. From the live workspace, use
**Saved runs → Recorded coordination examples → Open shared contribution
review → Communication**. This opens a separate offline example without
starting or stopping research. **How peers used shared work**
shows a late peer's discovery of prior critique, its optional challenge statement
and linked revision, and an acknowledged update whose use remains unknown.
**What the saved explanations cover** distinguishes an older explanation from a
newer partial one, with exact event boundaries and omitted/pending detail.

Create your own disposable example without modifying earlier state:

```sh
.venv/bin/python -m ora.coordination.contribution_demo
```

The command prints a state directory, snapshot, query IDs, event boundary and
exact summary references. Use those returned values with these read-only entry
points:

```sh
.venv/bin/python -m ora.coordination.inspection --state-dir <printed-state-dir> --snapshot-id stage3-contribution-review-snapshot-v1 --view all
.venv/bin/python -m ora.coordination.contribution_inspect --state-dir <printed-state-dir> --mission-id <mission-id> --proposal-id <proposal-id> --viewer-id <viewer-id> --through <printed-event-boundary>
.venv/bin/python -m ora.coordination.contribution_inspect --state-dir <printed-state-dir> --mission-id <mission-id> --summary-id <summary-id> --summary-version 1
```

The first command returns the same saved projection as the Atlas fixture. The
second replays its exact discussion boundary. Omit `--through` when intentionally
requesting the latest boundary; page with `--after` and reuse the returned
`through`. The summary query keeps the author's saved boundary but compares it
with the current event store, so it may observe activity newer than an earlier
Atlas export. Neither query advances a saved summary, sends a message or invokes
an agent.

`tests/coordination/test_contribution_inspection.py` checks the concrete generator,
CLI/export equality, pagination, unchanged database bytes on read, retained
partial/unanswered requests and no provider calls. The React contribution tests
check readable unknown/partial states and fixed fixture routing. This is a saved
review over local records. Atlas's live execution path remains separate; the
Stage 4 shared-loop acceptance explicitly requires a real start/follow-up run
with transparency, visibility, fidelity and value checkpoints.

The browser preview reads current development assets. The standalone Mac app
embeds the last desktop build; run `scripts/dev desktop` and reopen that viewer
to refresh it. A rebuilt app initially opens its default sample, so select the
same recorded example before comparing desktop and browser views.

## Observable check

`.venv/bin/python -m pytest tests/coordination/test_contributions.py -q`
exercises late-peer critique discovery, unknown uptake after acknowledgment,
peer-stated challenge linked to an exact revision, clarification without closure,
mission and unshared-message scope, replay, and a pinned page followed by a
later insert/revision. This is offline record behavior; it does not qualify a
model's scientific interpretation or prove a live research mission.
