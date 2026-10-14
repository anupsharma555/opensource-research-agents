# Stage 4 source intake and extraction

`ora.evidence.intake.IntakeService` inspects actual source bytes before a caller
uses them as evidence. The caller supplies an exact publication reference and
an explicit source-use basis. A discovery hit can supply provenance through
`retrieval_hit_ref`, but its HTTP status, title, access label, or provider text
does not establish that a later response is the right article or complete text.
The library stores an immutable copy and selected passages only after concrete
content and policy checks. The result reports the actual classification and a
per-copy quality reference even when no passage can proceed.

The callable boundary is:

```python
service = IntakeService(EvidenceLibrary(store))
result = service.inspect_response(
    operation_id, publication_ref,
    content=response_bytes, content_type=mime_type, source_url=final_url,
    retrieved_at=retrieved_at, source_version=source_version,
    rights_status="permitted", rights_basis=source_policy_evidence,
    declared_surface="abstract", expected_doi=verified_doi,
    identity_basis=verified_article_identity_evidence,
    selected_texts=[exact_text_in_extracted_source],
    retrieval_hit_ref=hit_ref,  # optional
)
```

The result includes `classification`, `reason`, `quality_ref`, and, when
appropriate, `copy_ref`, `passage_refs`, and `source_use_ref`. A selected passage
must occur in exactly one extracted block. Its locator names the section or
PDF page and exact character offsets within that block. `source_for_model`
resolves an exact passage, re-extracts its retained copy, checks the locator and
returns its source-use reference. A model-input assembler must use that
source-use decision; a raw passage read is not a policy check.

For a selected-model input, `checked_model_text(library, copy_ref,
passage_refs)` builds one text packet from exact checked passages of **one**
original source copy. It returns locator-labeled `text`, its separate
`text_sha256`, the original `copy_ref` and `content_sha256`, exact selected
`passage_refs`, current `source_use_refs`, and a per-passage manifest. It also
returns partial/truncation status, selection omissions, named information
needs, and correction/review flags. A disputed source remains available to
explain a discrepancy; the flags are data for agents to judge. A mismatched
passage, failed locator replay, cross-copy selection or revoked current
model-input use rejects the affected packet. The helper selects passages; it
does not assert that a passage supports a proposed scientific claim. A model
session must preserve the original copy and selected passage manifest next to
this text packet when sending or recording a model call.

An original pasted input uses a whole-submission locator rather than a parser
block locator. That passage can be replayed only when its text, offsets and copy
match the retained UTF-8 submission and its accepted, checked user-supplied
origin. Appending a window preserves both selections; replacing the selection
keeps only the requested window, without rewriting earlier records. Coverage
of the whole submitted text does not establish coverage of the original
publication. Current source-use checks still apply to both forms of passage.

Older checked local text may lack a complete inspection record. At new model
intent and dispatch-marker commits, `ora.evidence.local_text_quality` validates
otherwise-authorized retained bytes and links an exact quality receipt. For plain
UTF-8 text, it can record a separate current inspection with byte/character lengths,
a retained-text locator and the saved encounter provenance. An incomplete but
otherwise valid older quality receipt stays unchanged and is linked to the new
inspection; contradictory or missing referenced quality is rejected. Non-plain
formats require their existing complete intake quality rather than being labeled
as parsed plain text.

This inspection creates no source-use grant. Current restrictions still apply,
and unknown historical extraction, clipping or external completeness remain
unknown. It covers direct source inputs and source dependencies carried through
derived model context. Read-only previews and historical request inspection do
not backfill records. An uncaptured old request can acquire the separate receipt
before actual dispatch without changing its saved bytes; already captured calls
remain unchanged. This does not migrate passive historical UI views or establish
scientific support for an old report.

Packet construction reuses one checked extraction per exact copy within a
single call; it does not cache policy across calls. On the saved real PLOS PDF
with four selected passages, three local repeats went from four PDF-parser
invocations and a 130.57 ms median to one invocation and a 43.40 ms median.
The text SHA-256 was unchanged. Private measurements are in
`.local/stage4/packet-performance-before.json` and
`.local/stage4/packet-performance-after.json`. These timings describe this
machine and case, not a general latency guarantee. The current source-use
record is checked again after assembling the packet; a mid-build revocation
rejects it.

`read_source_window(library, operation_id, copy_ref, *, block_offset=0,
block_limit=3, char_offset=0, char_limit=4000)` lets a model request later
sections/pages or continue within one long extracted block. It returns a saved
`window_ref`, exact new or reused `passage_refs`, page/section locators and
bounded text previews, `total_blocks`, a `next_cursor`, and explicit omissions
for content outside the window or a partial original. Character offsets count
characters in the parser's extracted block, not byte positions in a PDF. The
agent passes `next_cursor.block_offset` and `next_cursor.char_offset` in a new
operation to continue; there is no fixed limit on how many windows may be
requested. Each operation has a bounded response size. The original copy and
older passages stay immutable, and an identical operation replays its saved
result. A changed current source-use grant rejects reading at the transaction
and return boundaries. On PDFs, page locators identify the original page; a
rendered table check is shown only for a selected page it actually covers.
The window never asserts that a table cell or scientific claim is valid.

An invalid or ambiguous selection appears in `selection_omissions` with its
input index and reason; it does not turn a valid copy or other selections into
a parser failure. Each operation checks at most 100 selections, and another
operation can inspect an additional batch against the same exact source
version. If bytes are partial but an exact selected span survives and identity
and use are established, it is retained at `passage` scope. Callers may pass
named `missing_fields` to open information needs for details the partial copy
cannot answer. The absence of complete text does not stop other evidence work.

`open_information_need` saves a named missing field and two distinct candidate
paths. A same-study companion requires a verified shared study-family link.
Independent context is recorded separately and cannot fill the target study's
unknown details. The information need leaves useful abstract facts available
while deeper details remain open.

## Classification and retention

The intake distinguishes `abstract`, `passage`, `full_text`, `teaser`,
`provider_summary`, `access_page`, `wrong_article`, `partial`, `empty`,
`identity_unverified`, `unverified`, and `parser_failure`. A short selected
article section may be a usable `passage` without claiming complete full text.
Visible mismatched article DOI rejects a copy. Missing DOI needs an independent
article-identity basis; a declared surface alone is insufficient. These are
source-integrity checks, not a global research-completion gate.
Unmistakable PDF, XML, and HTML byte signatures take precedence over a
misleading MIME label; both reported and detected types are saved in quality.

Retained bytes and passages require an explicit `permitted` policy with a
recorded basis. `restricted` and `unknown` keep a hash and locator only, with
source policy pending. The same-URL restrictions on a saved retrieval hit remain
in force. A newly fetched alternate source can carry its own policy and exact
provenance when the caller marks `separate_source=True` and supplies a distinct
URL and explicit policy basis. A redirect or alternate URL alone does not erase
a restricted discovery hit. `source_use` currently grants discovery, storage and model input for
a permitted retained copy; export stays ungranted unless a future explicit
policy operation establishes it. Each retrieval version creates a new copy.

The baseline parsers use Python's HTML and XML parsers and an optional installed
Poppler `pdftotext -layout` executable. PDF absence is a scoped capability
failure. There is no OCR, table-structure reconstruction, or silent truncation.
The direct-copy byte ceiling is 32 MiB; an oversized source is rejected for a smaller,
selected or artifact-backed route. This is separate from discovery's 8 MiB
response ceiling and from the model's context/account limits. Direct `fetch_public_source` exposes a typed
`OversizedSourceError.receipt()` with the declared response size when available,
the observed prefix size/hash when bytes were read, an opaque request-URL hash,
and `complete_body_sha256=None`. It never presents a prefix digest as the full
source checksum. The caller can retry a permitted smaller page or selected
passage/table; this function does not choose an alternate scientific route.
Source quality records include source hash,
type, policy, parser/version, inspected size, clipping report and locators.
A claim-critical PDF table still requires a rendered-original review of its
page, row, header, units and footnotes.

## Verification and limits

The focused offline test checks exact abstract use, HTML/XML numbers and
sections, teaser/login/wrong-article/partial distinctions, rights-limited
storage, parser corruption, missing identity and exact passage replay. Run it
with `.venv/bin/python -m pytest tests/evidence/test_intake.py -q`.

The explicit live qualification command is
`.venv/bin/python scripts/qualify_stage4_intake.py --live`. Its latest observed
private receipt is
`.local/stage4/qualifications/20261009T191945Z/receipt.json`, with raw
responses and StateStore under the same ignored directory. It made five public
requests plus one public registry request: Europe PMC core and open XML, PMC
HTML, a PLOS article page and PDF, and the same-study ClinicalTrials record.
All returned HTTP 200, and the inspected formats were classified from
their actual bytes. Eleven case entries cover the five difficulty levels through
live deterministic processing, saved replay, and labeled injected failures.
One controlled partial copy of the live abstract retained its intact selected
span at passage scope while opening a subgroup-outcome information need.
The registry response was inspected for its exact study ID and hashed; its raw
bytes were not retained because a separate reuse basis was not established.
The open PDF's Table 1 page 5 was rendered and visually compared with the
parser's header, row, value `33 (86)`, and denominator footnote. A hash-pinned
private review record is at `.local/stage4/table-check/table-review.json`.

The first PDF wrong-article check exposed a real intake defect: the article
DOI was just beyond the first 2,000 extracted characters. The classifier now
checks the first PDF page for identity; the failure case and abstract control
passed on the subsequent live run. The latest run had zero model calls. It
does not prove a live research mission, downstream model/report citations,
provider endurance, OCR quality, or general scientific support. A core abstract
field and an XML source may differ in formatting; the private receipts retain
both exact versions rather than conflating them. The selected table spot-check
is one verified page, not a general PDF-table accuracy claim.

Europe PMC's [REST documentation](https://europepmc.org/RestfulWebService)
defines the core search response and open full-text XML route. The exact
article XML and publisher page in the private receipt supplied the reuse bases
for this run. The Stage 4 discovery path owns retrieval-envelope errors and
pagination; this intake consumes its exact saved hit reference without
reinterpreting the provider response.
