# Stage 2 state and evidence records

Stage 2 provides a local, inspectable record foundation for research work. The
mechanical store accepts versions, ownership, links and delivery events;
scientific peers decide what evidence means and what work to pursue next. This
is a recorded-data foundation, not a production mission host. The optional
[local live extension](live-research.md) has its own state and supplied-evidence
workflow; the Stage 2 demonstration makes no model or search-provider call.

## Where truth lives

| Layer | Responsibility |
| --- | --- |
| SQLite in `ora.state.StateStore` | Accepted `{kind, id, version}` records, current heads, aliases, exact-version links, operation receipts, mission events, worker leases and review requirements. The host writes through short transactions; readers can request an exact version. |
| Immutable content files | Retained source, sanitized retrieval projection and snapshot bytes, named by SHA-256. The store verifies a file's hash and size before accepting its reference. Identical bytes can share one file without merging distinct encounters or copies. |
| Rebuildable indexes and views | Lookup acceleration and UI exports. Rebuilding an index does not rewrite accepted versions, answer a peer request or repair missing retained bytes. |

`StateStore.put_blob` writes and syncs a private staged file before installing
its hash-named object. A metadata transaction then checks the object, expected
record versions and optional worker fence before committing related records and
an outbox event. The file system and SQLite are separate commit domains: a
failed acceptance may leave an unreferenced file, while an accepted reference
to missing or corrupt bytes is an integrity fault. `reconcile()` reports staged
files, orphans, unresolved references, degraded artifacts and pending events;
it does not delete another writer's work or silently repair a claim.

The caller assigns a stable **operation ID** to one request. Repeating that ID
with the same request, mission and worker fence returns the saved result after
an uncertain response. Reusing it with different inputs conflicts. New
attempts and retrieval deliveries have their own identities, so a failed retry
cannot erase an earlier accepted hit. `expected_version` rejects a stale edit
while preserving the old version for historical reading. An invalid operation
rolls back its own records and event; its diagnostic identifies that operation
without storing arbitrary failure text. Unrelated branches remain available.

A worker lease is keyed by **mission and task**. Its owner and increasing
generation form a fence checked before and after a transaction's work; an
expired or replaced worker cannot commit late output. A task ID reused in a
different mission has a separate lease. The store does not schedule scientific
work, impose a global approval gate, or set a fixed number of research rounds
or a research deadline.

When a domain action emits an event, it commits with the accepted records.
`deliver()` sends pending events
in sequence and marks each delivered after the callback returns. A lost
acknowledgment can repeat delivery, so consumers need to deduplicate event
IDs. **Delivered** means the event reached a callback; a peer request remains
`requested` until a separate substantive reply is recorded. A prepared agent
call likewise proves a pinned configuration and inputs, not model execution.

## From retrieval to a revisable finding

`ora.retrieval` keeps the logical request, attempt, delivery, returned page,
usage and valid or failed item outcomes distinct. It retains an allowlisted,
redacted projection and hashes the original response without retaining raw
provider bytes. A valid `retrieval_hit` is a candidate, not a verified study or
claim. `ora.evidence.EvidenceLibrary` links that exact hit to a
`source_encounter`, then to a rights-aware `source_copy` and, where permitted,
passages with exact locators. A provider-generated summary cannot become an
original-source copy. A provider's access claim starts as
`provider_asserted`; a `checked` copy requires a separate validation record.
Restricted content can remain a locator and checksum without retained text.
Failed and quota-limited retrieval attempts retain their own valid observed
usage entries when the response provides them; they are not recast as empty
literature results or assigned to a different attempt.

Publication identity, study-family identity, encounter and copy are separate
records. Verified identifiers can bind aliases; a title match or conflicting
identifier becomes a review candidate. Two discovery routes can point to one
publication while retaining both encounters. A claim links to the precise copy
or passage version used. This establishes traceability, not semantic support:
a reviewer must still assess whether the passage supports, challenges or
qualifies the claim. A correction or retraction links its notice copy to the
affected copy. Passage-scoped corrections mark dependent claim versions for
review without reopening unrelated passages; whole-copy corrections mark all
dependent work. Historical copies and reports remain readable. See
[evidence identity and copies](evidence-library.md) and
[retrieval receipts](provider-receipts.md) for the domain rules.

Agent instance IDs identify contributors independently of display names and
model settings. Saved configurations and prepared call manifests pin the
model, skills, allowed tools and exact input references. Normalized source
references and sanitized operational traces stay separate from reader-facing
findings; neither requires private model reasoning as an explanation.

## Recovery and readers

Schema migrations advance a recorded SQLite version in one transaction; an
unsupported downgrade is rejected. Version 2 adds review requirements and a
rebuildable lookup index. `backup()` takes a SQLite online snapshot and copies
its referenced, hash-verified objects with a manifest. `restore()` verifies
the backup, builds and checks a new staging directory, then exposes the
destination only after successful validation. An interrupted restore leaves
the destination absent. Operators still need separate power-loss and restored
host-continuation qualification before making a production durability claim.

Reopening a store does not globally block it when one accepted content file is
damaged. Reconciliation reports the affected hash and references. A new
snapshot acceptance verifies every source copy it references and rejects work
dependent on corrupt bytes, while an unrelated branch can still accept its
own record. The historical report remains visible with the affected source
marked as unavailable for citation replay.

`ora.state.inspection.save_snapshot` checks exact source copies and passages,
the encounter that supplied each copy, complete source and attempt provenance,
claim and report versions, attempt/configuration/call lineage, accepted output
references and the saved parent target before accepting a review snapshot.
`load_snapshot` resolves that snapshot and its event history. If retained bytes
are corrupt, it keeps the historical finding visible while marking citation
replay degraded. The default detail projection allowlists fields and redacts
credential-shaped values; it does not expose private instructions or returned
reasoning text.

The `inspection.steps` timeline turns accepted events into concise explanations:
who acted when known, the action and outcome, exact saved input and output
references, and a recorded reason or next action when available. A prepared
call is labeled as preparation, and delivery does not imply a peer answer.
These explanations come from saved records; they never invent private model
reasoning.

Research Atlas's **Open recorded Stage 2 review** entry point, also available
at `?recorded=stage2`, reads the fixed parent and child JSON projections under
`ui/public/stage2-recorded/`. Those files were exported from accepted local
records; the bundled app does not open a private SQLite store. It verifies the
offline proof label and parent relationship and resolves inspection details by
exact saved references. Codex or a terminal can read the corresponding local
state store directly, without migration or provider calls, through
`ora.state.inspect`.
The [verification guide](stage2-verification.md) gives its exact commands and
the limits of the recorded demonstration.
