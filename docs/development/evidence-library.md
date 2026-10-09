# Local evidence identity and copies

`ora.evidence.EvidenceLibrary` uses the shared `ora.state.StateStore`; it has no
second database, model call or provider client. It separates four things that a
research run must not conflate:

1. A **publication** has verified work identifiers, such as a DOI, PMID or arXiv
   ID. DOI resolver URLs and case variants normalize to one alias. A matching
   title alone remains a review candidate, not a merge.
2. A **study family** identifies the underlying participant group or study.
   Reports, preprints, abstracts, registrations, supplements and datasets have
   typed links to it. Two registry IDs can identify one trial only when their
   linkage is asserted with evidence; a possible match remains separate.
3. A **source encounter** records one route and time a source was seen. A
   `retrieval_hit` from `ora.retrieval` remains its own exact record; two routes
   finding the same work produce two encounters. Retrieval failures and partial
   windows cannot replace a previously accepted source copy.
4. A **source copy** records the retained source version, reported access depth,
   rights basis, content checksum, retained bytes when permitted, and exact
   passages with locators. Its inspection state says whether source fidelity
   has been checked. A new copy never overwrites an old one.

Records, aliases and links are committed through `StateStore.transact`. The
caller supplies a stable operation ID and unchanged request for replay. Accepted
records have exact `{kind, id, version}` references. Immutable file bytes are
written through `StateStore.put_blob`; a rejected transaction can leave an
unreferenced file for the store's non-destructive reconciliation. Evidence
operations never treat a retrieved title, provider rank or URL alone as proof
that two publications or trials are identical.

The [original REEACT trial article](https://doi.org/10.1136/bmj.h5627) and its
[HTA report](https://doi.org/10.3310/hta191010) illustrate two publications
about one registered trial, [ISRCTN91947481](https://www.isrctn.com/ISRCTN91947481).
The [REEACT correction](https://doi.org/10.1136/bmj.i195) is a linked notice,
not another trial. [REEACT-2](https://doi.org/10.3310/hta20890) uses a separate
registration, [ISRCTN55310481](https://www.isrctn.com/ISRCTN55310481), and a
different randomized comparison. The tests use clearly labeled simulated
identifiers and results; they do not fabricate a cross-registry match for these
real publications.

## Acceptance and review boundaries

`register_publication` binds identifiers only when the caller supplies an
explicit verification basis. If asserted identifiers already name different
publications, it saves an `identity_review` record and does not merge them.
`suggest_title_matches` offers close normalized-title candidates without
binding anything. `register_study_family` and `link_registry_families` likewise
keep possible versus verified cross-registry relationships distinct. A verified
cross-registry edge needs an exact checked source passage; registry IDs are registered
separately before that link so a later correction cannot leave two immutable
aliases silently fused. If the linkage record later needs
review, its trial-count grouping is suspended until revalidated. Typed
publication-to-family and publication-to-publication links preserve the report
map without increasing the number of independent trials.
`study_families_for_publication` returns a count key for verified registry
clusters; an unresolved registry candidate keeps its separate key. A
publication-to-family link carries the caller's stated basis and remains a
research assertion for later appraisal, not a machine-proven claim that two
reports share participants.

The retrieval handoff reads an exact accepted `retrieval_hit` and stores a new
`source_encounter` linked to it. A provider-generated summary can be retained as
a retrieval hit, but cannot become an original-source copy. The normalized hit's
rights status controls whether its sanitized content bytes can enter a copy.
Reference adapter mappings retain explicitly named scientific metadata such as
authors and identifiers, plus permitted structured content such as tables.
Nested JSON cells retain numbers, nulls and objects. The mapping version and an
omitted-field manifest identify unsupported, invalid, sensitive or rights-limited
fields without storing the entire provider response. Source copies carry the
allowed mapped fields and remain `provider_asserted` until separately checked.
Repeated handoff of the same exact hit to the same publication reuses one
encounter even with a different operation ID; a different hit or route remains
a separate encounter. A later delivery with newly available scientific fields
or changed access rights gets a new hit candidate, so a richer source cannot be
hidden behind an earlier deduplicated teaser. Repeating an unchanged delivery
reuses its accepted hit.
Its access depth is still a provider assertion: the first copy is marked
`provider_asserted` and cannot become the accepted inspected copy until a
separate source-checking step saves a `checked` copy with a validation record.
With no retained text, the evidence copy is locator/metadata-only even if a
provider advertised full text; it does not receive a full-text ranking. Exact
DOI/PMID/arXiv values should be verified against the publication, not extracted
as an identity decision from a title or a search rank.

Passages with text offsets are checked against the retained UTF-8 copy. A page
or section passage without text offsets needs a separate extraction-validation
record; its locator alone is not claim entailment. Checked-copy status means
source fidelity within the stated validation scope, never scientific support.
A claim links to the exact passage or copy version it used. A correction or
retraction links to the
affected copy and can name exact affected passages. Invalidation then reopens
only claims dependent on those passages; a whole-copy correction reopens all
of its dependent claims. Unrelated claims remain current, and historical copies
remain available for an as-of report. The host and scientific peers still own
interpretation and the decision to publish a revised claim.
When a mission ID is supplied, the same transaction emits a scoped
`source_review_required` event containing the affected record references;
replaying the operation does not emit a second event.

## Local lookup baseline

`lookup_exact` resolves a publication, study family, encounter, copy or passage
by its stable ID. It records actual connection, indexed SQLite query/fetch,
decode and overall metadata lookup times. The index measure includes database
I/O; it is not isolated index CPU time. Queue time is reported only when a
same-process monotonic enqueue timestamp is supplied. `metadata_lookup_ms`
ends after the record read and decode; it excludes the later review-status
query and optional artifact read. It is not total `lookup_exact` elapsed time. A
retained source file
is read and hash-checked only when `include_content=True` requests it; otherwise
artifact-read time is explicitly not requested. A rights-limited copy has no
file bytes to return. These timings describe the local operation, not a host
scheduler or an end-to-end agent task. `get_passage` resolves exact passage,
copy and publication records.
`search_passages` scans retained passage text with simple token overlap and
returns its method, number examined, rank, review status and observed elapsed
time. It returns retained provider-asserted and checked passages by default,
labeling each result's inspection state without implying scientific validity.
Set `checked_only=True` when the caller specifically needs a checked-copy view.
Metadata-only records and provider-generated summaries are not passage results.
This is a transparent, no-embedding baseline, not a semantic search or
a claim-support checker. The synchronous library does not know how long a
request waited in a host queue unless its caller supplies that start time.
It also does not read a whole PDF to answer an exact passage request. The
verified study-family `count_key` lets a later coverage or appraisal consumer
avoid counting companion reports as independent studies. The local fixture
checks this count-key behavior; live downstream consumers still need their own
integration proof.

Run the repeatable small measurement with:

```sh
.venv/bin/python tests/evidence/measure_lookup.py
```

In the first local four-source run, three literal-term queries retrieved the
expected passage at rank one and one synonym query (`adverse reactions` for
`side effects`) did not: recall@1 was **3/4**. The four scan times were
**0.837–1.622 ms** and exact passage-metadata lookups **1.475–1.939 ms** on
that run. These are observed fixture timings, not an SLA or a prediction for a
long mission. The script prints new timings on each run. Scale, source recall,
clinical synonym handling, permissioned content and app display require their
own later evaluation.

Focused checks:

```sh
.venv/bin/python -m pytest tests/evidence -q
.venv/bin/python -m ruff check src/ora/evidence tests/evidence
```

This package does not provide a production research host or externally exposed
source API. A later authenticated query layer must keep rights, version and
review status attached to source text it returns.
