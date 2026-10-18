# Source-to-answer walkthroughs

Use these checks when a downloaded source is technically readable but the model
does not receive useful evidence, repeats reading steps, or misses a qualification.
Start with the existing reader and the first failing layer. A new parser dependency
should earn its place by correcting a demonstrated case without losing required
content or exact source mappings.

The walkthrough is a development evaluation, not a mandatory research workflow.
The research peer still chooses which sources, passages and tools are useful.
Current reader support and local executable requirements are described in
[document readers](document-readers.md); [source intake](stage4-intake.md) owns
the permission and evidence boundary.

## Inspect six stages

1. **Original:** retain the intended bytes, type, origin, version and current use
   permission. Detect error/login pages separately from valid empty material.
2. **Extracted:** inspect reading order, headings, page identity, table headers,
   units, captions and notes. Nonempty text does not prove these survived.
3. **Navigable:** offer verified source positions and an executable read or saved
   text search. A navigation map is source-derived data, not inspected evidence.
4. **Selected:** keep the exact passage and the context needed to understand it.
   A table row may require a separately selected caption, header and footnote.
5. **Delivered:** inspect the complete serialized model request, including actual
   callable tools, current permissions, citations and source text. Count repeated
   context and schema overhead as well as passage text.
6. **Answered:** inspect the real output against the requested facts, material
   qualifications and source links. Scripted responses verify delivery plumbing;
   they cannot establish natural tool choice or answer quality.

## Small offline matrix

| Case | Material | Required distinction |
| --- | --- | --- |
| H1 | Navigation-heavy HTML | Page furniture versus useful source sections; exact next locations |
| H2 | HTML table, caption and footnote | Row values retain headers, units and qualification |
| P1 | Short text PDF | Benefit and later exception are both reachable and deliverable |
| P2 | Multicolumn or table PDF | Reading order and value-to-label relationships survive extraction |
| O1 | Scanned page | OCR preserves a tested number, decimal, unit and negation; remains unverified transcription |
| O2 | Mixed PDF | Text, scan and blank pages retain physical page identities |
| X1 | XML/JATS | Abstract, body and later qualification stay identifiable; exact locators remain stable |
| T1 | UTF-8 text | Heading/line context and repeated phrases keep exact character positions |

Use small synthetic fixtures with known expected facts kept outside the model
prompt. Keep saved provider/source regression material private. Reuse existing
permission, revocation, pagination, restart and source-delivery tests rather than
duplicating every check for every format.

Record each case's first failing layer, required content retained, source/version
and locators, selected text, actual delivered text, and any untested relationship.
Distinguish preserved words from preserved table meaning. Do not change historical
extraction locators just to make a new fixture pass: a changed extraction requires
a versioned mapping and compatibility checks for old citations.

## HTML extraction fidelity

Establish the HTML baseline before extending PDF or figure OCR work. Check navigation-heavy
legacy pages, semantic articles, nested lists, inline emphasis and units, simple
tables, grouped headers and malformed markup. Retain legitimate prose about
searching: filtering depends on structure and visibility, never a banned word.

New HTML intake uses `stdlib-htmlparser-structured-v5` in
`ora.evidence.html_content_v5`; the v2/v3/v4 modules stay frozen for retained copies.
It preserves preformatted code lines and authored inline word boundaries,
groups description-list terms with their definitions, separates disclosure
summaries from their bodies, honors supported inline whitespace declarations,
selects independent content regions, excludes
explicit page controls and hidden ancestors, and keeps table values with their
caption, row and column labels. Explicit `headers` associations take precedence
over positional inference. Depth and element limits fail explicitly instead of
returning silently clipped evidence. This is a bounded structural reader, not a
browser-equivalent HTML5 tree builder or a scientific relevance classifier.

Complex description-list definitions keep nested code, lists and tables in
separate term-scoped blocks. Code indentation and line breaks survive even inside
wrapped definitions or list items; table cells retain their labels. Plain grouped
definitions keep their existing shape. Verify the serialized model input as well
as extraction: preserving code in a saved file is insufficient if a later reader
or context projection flattens it again.

Nested description lists still have a known parent-term labeling limitation;
check their hierarchy separately. Bare `<article>` fragments declared as HTML can
also be detected as XML. Complete web-page fixtures should remain complete HTML;
keep fragment detection as a separate case instead of silently wrapping a real
download and claiming the original passed unchanged.

Readers use `extract_copy_blocks` to replay a retained copy's recorded mapping.
Old HTML copies keep the legacy parser and exact citations; fetching an already
retained old copy does not silently upgrade it. A changed representation of those
bytes needs a separate inspected copy and permission checks, not overwritten
historical passages. This slice does not migrate cached copies automatically.

For an authorized provider comparison, save each stage separately: Exa query and
candidate excerpt, selected URL, original HTML bytes, optional Exa Contents text,
local extracted blocks, checked reader passages, and the serialized source packet.
Excerpts are intentionally partial. Exa Contents may reflect a different crawl;
do not assume it has the same bytes or is a ground-truth transcript. Assess source
text and relationships against the original, not merely equality between two
extractors. Table labels may add characters while making a selected passage more
self-contained; whole-document character reduction is not the success metric.

Run the focused HTML checks with the repository interpreter:

```sh
.venv/bin/python -m pytest -q tests/evidence/test_html_content_fidelity.py tests/evidence/test_html_content_edge_cases.py tests/evidence/test_html_mapping_compatibility.py
```

Also run `tests/evidence/test_html_robustness.py` for code whitespace, inline
boundaries, footnoted lists, meaningful asides and interactive controls.
The tests include retention and exclusion checks, old-copy replay and revocation.
They do not prove arbitrary CSS visibility, JavaScript rendering, every malformed
table, natural source selection or a supported research answer. For article
selection alternatives, inspect [Trafilatura's extraction stages](https://github.com/adbar/trafilatura/blob/master/trafilatura/core.py)
and [Mozilla Readability](https://github.com/mozilla/readability); compare on the
same saved bytes before adding a dependency. Table associations need separate
checks against the [HTML table model](https://html.spec.whatwg.org/multipage/tables.html#forming-relationships-between-data-cells-and-header-cells).

Before repairing a reproduced formatting or selection failure, consult the
relevant primary standard and maintained implementation code. Record the failing
source structure, the documented behavior, the smallest proposed change and a
counterexample that must retain its content. Verify the same saved bytes before
and after; do not accept a lower word/block count as proof of improvement.
For example, [`pre` whitespace](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/pre)
must preserve line breaks/indentation, including edge newlines except the single
leading LF ignored immediately after the opening tag. Ordinary inline text needs
its fragments joined before whitespace normalization.

Keep responsive duplicate panes and dynamic chart views separate from formatting
repairs. Do not globally remove repeated scientific sentences. Prove complete
subtree equivalence before suppressing a responsive copy. Static chart metadata
may describe a different initial view than the requested URL; rendered content or
a selected data download needs a separately checked representation. Image-only
tables/formulas remain image evidence even when their captions are readable HTML.

### When visual inspection adds evidence

Choose the representation from the question. Selectable prose/code and accessible
table or chart data usually provide the most direct exact text and values. Use
OCR for printed text available only in pixels; use a targeted visual question
when marks, axes, legends, arrows, spatial relationships or image-only notation
carry needed information. Preserve the caption, units and variable definitions.
These are optional complementary readers, not a mandatory sequence for every
source. Menu leakage and repeated responsive panes still need extraction fixes.

For interactive charts, verify that the data or rendered capture matches the
requested configuration. A static default view is not evidence for another
selected metric. Keep original data, OCR transcription and generated visual
interpretation distinct; cross-check consequential pixel-derived numbers.

Current OCR/figure tools accept selected pages of retained public PDFs or
standalone PNG/JPEG images. `list_source_assets` and `fetch_source_asset` now
connect selected authored HTML/XML image links to these readers, preserving the
parent-source/element/caption relationship, exact image and current parent/image
permissions. They do not automatically process every figure or accept arbitrary
browser screenshots. See [document reader boundaries](document-readers.md)
before using or qualifying this route. Qualify a formula or chart against its
original plus a text-only control; source fidelity and natural tool selection
need separate evidence.

## Paired XML, PDF and image checks

Where the publisher supplies several representations, compare the same article's
XML, PDF and selected figure or table image. A table displayed as an image in
HTML may have a structured counterpart inside JATS `alternatives`; inspect that
before assuming OCR is necessary. Keep each original hash and extraction mapping
separate. JATS `table-wrap` binds the label, caption, table and notes; an embedded
graphic is an alternative representation, not a second independent result.
See the [JATS table wrapper specification](https://jats.nlm.nih.gov/publishing/tag-library/1.4/element/table-wrap.html).

Check nested section scope, authored inline word boundaries, superscripts and
subscripts, merged-cell spans, header relationships and table notes. A spanning
cell must retain the columns or rows it covers; do not silently assign its value
only to the first column or invent independent repeated measurements. A conflict
between a formula and its source footnote is a source discrepancy, not permission
for the extractor to correct the author.

For PDF, compare rendered pages with the exact layout-text blocks. Numbers can
survive while a sidebar interrupts the abstract's reading order. Physical page
locators remain useful, but aligned spaces are not typed table relationships.
For images, retain the original pixels, selected page or element, exact caption,
units and source identifiers. A chart's season or panel definitions may be in the
caption rather than inside the image. OCR can recover labels without recovering
the plotted points, error bars or their relationships.

Test OCR on exact signs, decimals, units and subscripts, including at least one
small-text failure. A correctly recognized title does not establish numerical
accuracy. [Tesseract's quality guidance](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html)
recommends adequate resolution and page segmentation, and identifies tables as
requiring additional layout handling. A higher-resolution or selected-region
diagnostic needs its own image/hash and must not be described as the production
path until implemented and verified. Keep OCR unverified and visual interpretation
separate from checked source text.

Run focused cross-format checks with the repository interpreter:

```sh
.venv/bin/python -m pytest -q tests/evidence/test_xml_content_fidelity.py tests/evidence/test_xml_mapping_compatibility.py tests/mission/test_cross_format_delivery.py
```

These checks verify extraction, historical mapping replay and selected text in
saved model requests without dispatch. They do not establish natural model use,
arbitrary PDF layout accuracy, or live figure interpretation. Publicly fetched
image bytes alone do not give a runtime tool the same-task discovery/permission
lineage it requires. Use the exact source-asset fetch path for article-linked
images; preserve that distinction in manually assembled diagnostics.

## Progressive qualification

First make the saved complete request fit offline with its selected model,
completion cap and tools unchanged. Check current source permissions and exact
passage recovery after compaction. Retain original rejection records and unknown
failed-call usage; a smaller local estimate is not provider acceptance.

After separate live authorization, start with one retained-HTML task and one
retained-PDF task, serially. Keep a supplied-correct-packet test separate from
natural source navigation. Then add one mixed-source comparison if it answers a
remaining question. Record useful new text per read, repeated actions, first useful
passage, complete request tokens, actual usage and supported output.

Scans, figure interpretation and JavaScript rendering need deliberately relevant
cases. Office-document adapters remain a separate extension. Passing a text-PDF
case does not qualify these routes. Use the Codex `os-eval-testing`,
`os-qualify-run` and `os-run-details` skills for authorized live evaluation and
the full readable input-to-output report.

## Technical index

- [Reader formats and boundaries](document-readers.md)
- [Source intake and exact references](stage4-intake.md)
- [Context efficiency and request sizing](stage4-context-efficiency.md)
- [Progressive search and provider handoff](progressive-web-search.md)
- [Development commands](setup.md)
