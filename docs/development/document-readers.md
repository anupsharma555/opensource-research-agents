# Public documents, scanned text and figures

`fetch_source` inspects retained public HTML, PDF, XML/JATS and UTF-8 plain text.
The original bytes, source hash, discovery route and use decision stay linked.
`read_source_window` delivers selectable text with exact page/section locators and
checked passage IDs. A downloaded document alone is not a checked passage.

New complete XML/JATS copies use `stdlib-elementtree-structured-v2`. The reader
keeps abstract and nested body sections distinct, renders superscripts/subscripts
with explicit `^`/`_` notation, and associates table values with captions, row and
column labels, merged-cell spans and notes. It uses a structured table inside
JATS `alternatives` when present and retains figure/table graphic references as
references, without interpreting their pixels. Retained complete and partial XML
copies replay their recorded legacy mapping; they are not silently upgraded.
This is not a general MathML renderer or a guarantee for every XML dialect.

Fresh PDF copies use `poppler-bbox-layout-reading-order-v2`: installed Poppler
provides layout text and geometric text blocks. Clearly separated prose columns
are read contiguously; table-like, dense or ambiguous pages keep layout text.
Every non-whitespace character must survive the changed order, including signs,
decimals and unit glyphs. Physical page numbers remain exact, including across
blank and scanned pages. Older `poppler-pdftotext-layout` copies keep their exact
historical text and locators.

This is conservative page-level ordering, not general table recognition. Check
selected rendered pages when columns, tables or figures carry needed meaning.
Forms can retain printed values while detaching line numbers and entry boxes;
test those relationships against the rendered page separately from word matches.
When the publisher provides XML as well, its structured table may preserve
relationships that PDF layout text or OCR loses. Processing and geometry bounds
prevent runaway local work; dense-page analysis falls back without removing text.

## Selective reading and progress

Source-window results include a short `reading_progress` description that also
survives model-input compaction. It identifies the exact physical PDF page from
its extraction locator, the extracted block and character range, and whether
more retained text follows. Total physical pages remain unknown when no verified
count is available: nonempty extracted blocks exclude blank or image-only pages
and must not be relabelled as a complete page count. HTML/XML/text results use
block and section positions instead of fictitious PDF pages.

The description concerns the current window. Earlier saved windows and the
current model input have separate coverage records. Reaching the last extracted
block does not prove the whole original document was read, and a complete saved
source does not mean every passage reached a model. Exact source versions,
citation references, `next_request` and PDF table cautions remain available.

Short HTML/XML reads also carry a bounded map of section changes observed in the
retained locators. Each entry has an exact block offset; the next section may have
an executable read request. This map is navigation-only source data, not checked
claim evidence or a judgment of relevance. It does not rewrite old blocks.

Repeated reads report how many exact source-location characters are newly checked
within the task. Zero new characters does not mean the model previously received
or understood them. Public-document fetch reuse reports whether HTTP occurred and
the latest permitted read window. Existing scholarly fetch routes retain their
own retrieval behavior.

Continuation is optional. Peers may stop when the requested answer has sufficient
support, read an adjacent window, or locate a missing detail through
`search_saved_passages` and read its returned coordinates with
`read_source_window`. This is lexical search over permitted retained text,
including public PDF/HTML/XML/text copies, not navigation by arbitrary section
names. It makes no web request; the search snippet is a navigation aid and does
not itself create a checked citation passage. A no-match result is scoped to
the extracted material actually searched.

Saved-PDF search accepts the same 32 MiB input bound as PDF intake; ordinary
text sources keep their smaller raw-byte limits. Extracted-text and aggregate
scan limits still apply, with partial coverage reported. This allows a modest
amount of text inside a larger PDF to remain searchable locally; it does not
increase the model-input allowance or send the complete document to the model.

When the task retains currently permitted searchable text, the host attaches
both `search_saved_passages` and `read_source_window`, even before a passage is
selected for model input. An empty task or a revoked/unavailable copy does not
cause automatic attachment. The request's trusted tool-availability notice
distinguishes attached tools from those that require `request_capability` first.

Search-hit read requests append their checked passage to the selected source
windows by default, so finding a qualification does not silently remove the
earlier result from the next model input. The peer may explicitly choose
`selection_mode=replace` to narrow that input. The complete-request fit and
source-use checks still apply; retaining a passage does not waive either check.

Prefer coherent sections when the complete model request fits. Use the question
and available evidence to choose further reading, saved-text search, another
source, or an answer. Reading every page is optional. Wait when retrieval is
pending, not merely because already retained text has not been read. Include a
requested source URL from verified saved metadata rather than inventing a link.

Targeted reading can reduce source tokens per model request. For a short
document, reading the next page may be cheaper. Offline tests establish the
navigation and input
presentation; natural use and answer quality need their own live evidence.

The optional readers are discoverable through the ordinary capability catalog:

| Tool | Purpose | Result and resource use |
| --- | --- | --- |
| `list_source_assets` | Find captioned figures/tables in this task's retained HTML/XML. | Local, paged source-derived candidates with exact asset references; no HTTP. Unresolved links or partial inventory remain labelled. |
| `fetch_source_asset` | Fetch one selected source-authored PNG/JPEG asset. | Uses its exact asset reference, rechecks the parent source, and saves a separate inspected image document. Repeated selections reuse a completed fetch; uncertain attempts are not blindly replayed. |
| `read_document_ocr` | Recover printed text from a selected scanned PDF page or saved PNG/JPEG. | Local Poppler/Tesseract, no model HTTP call. Returns bounded unverified OCR text, exact page/image/text hashes and a continuation request. |
| `inspect_figure` | Ask a specific visual question about one selected page or figure. | Local render and OCR restriction check, then one separate Groq Qwen call. Returns a generated, unverified interpretation and provider usage. |

The OCR and figure tools take an exact `document_ref` from `fetch_source` and a one-based
`page_number`. They do not automatically process every page. OCR recovers labels;
it cannot establish what a chart means. Neither reader manufactures checked
citation passages or verifies numerical data. TIFF, Office files, encrypted/broken
PDFs and arbitrary binary formats are outside this implementation.

The OCR/figure readers also accept `document_ref` from `fetch_source_asset`, with
page 1 for a standalone image. Figure discovery uses an exact selected source
ID/version, then an offset/limit for subsequent pages. It reads authored figure
containers and captions rather than selecting arbitrary images or social-preview
metadata. XML graphic DOI references without an authored fetchable HTTPS URL
remain unresolved; no provider-specific image endpoint is guessed.

Large asset listings retain their complete saved result and expose bounded
`read_tool_result` pages. A compact navigation index keeps recent distinct
fetched-image references, their source-authored captions and exact reading actions
available across capability selection and context compaction. It is a limited
preview, not the complete asset inventory or checked scientific evidence.
OCR takes the exact document reference and page 1 for a standalone image; vision
also needs the peer's own question and the run's explicit authorization. The tool
availability notice distinguishes attached tools from capabilities that must first
be requested. Listing pages and fetched-image references are reauthorized,
and both parent and image source dependencies follow the prepared model request
so a later permission change can prevent dispatch.

If the peer tries `read_source_window` on a currently permitted PNG/JPEG, the tool
returns an image-specific correction with those same document/page references.
This does not run OCR, interpret the image or fetch it again. The correction is
preserved in the next working context even when the ordinary tool exchange has
been archived. Source-use denials remain denials; they do not expose a stale
caption through the corrective guidance.

The latest permitted `search_saved_passages` query and its exact read locations
also survive history compaction. A bounded preview links to the complete saved
result through `read_tool_result`, so inspecting already returned matches needs
no repeated search. The saved result page and any continuation over additional
matches remain distinct. These are lexical navigation aids, not checked claims.
Source permissions are checked again when projecting or paging the result,
including sources behind the producing model input and all searched copies.

The image, parent source, figure identity and caption are bound by exact hashes.
Both parent and image permissions are checked on fetch reuse, OCR, vision and
later model delivery. Vision receives the verified source caption as explicitly
untrusted source content alongside the image; the caption does not become an
instruction or a generated interpretation. Model-facing links use the authored
public URL, while any temporary signed redirect stays in private receipts.
Multi-image containers and oversized captions have explicit bounded limitations;
their metadata must not imply the complete visual evidence was processed.

## Source use and local processing

Choose the reader from the information gap and extraction quality, not the file
extension. Clean selectable PDF text uses `read_source_window`; unreadable scanned
text uses local `read_document_ocr`; a question about visual marks, layout or a
figure may need explicitly authorized `inspect_figure`. These are distinct paths,
not a mandatory three-step pipeline. Keep source text, OCR transcription and model
interpretation labelled separately.

Tokenization measures the complete serialized request; it is not a compression
step and does not imply sending raw PDF bytes as text. Count instructions, attached
schemas, context, provenance and source windows together. Preserve exact selected
passages once, with their citations and continuation, and compact duplicate context
before considering smaller evidence windows or a provider-feedback probe. A vision
request has its own image-token charge plus text/output and may cost more than a
short extracted passage; do not switch readers merely to evade a text-input limit.

Verified unauthenticated public scanned PDFs/images may receive the existing
private-research source-use decision as `image_document`. Their quality records
mark incomplete text inspection. This is not a copyright license or redistribution
grant. Restricted leads, explicit prohibitions, changed provenance and revoked
source use still block delivery. Selected-page OCR checks explicit use restrictions
before any image is sent to Qwen. One-page inspection does not cover the document.

Vision rendering selects one page from at most 200 PDF pages and keeps its existing
1,600-pixel scale and image bounds. Local OCR now uses a separate preparation path:
PDF pages render at a longest side of 3,200 pixels, under 12-megapixel and 12 MiB
bounds. Small standalone images may be enlarged by at most 2x, toward a longest
side of 2,400 pixels. Enlargement does not restore missing source detail. Original
bytes remain unchanged.

`read_document_ocr` accepts an optional `region` with normalized `x`, `y`, `width`
and `height` from the top-left of the rendered PDF or EXIF-oriented image. Use
known coordinates, not guessed locations from a textual section name. Crops have
a 12-pixel white border for OCR, a 16-pixel minimum source dimension, and separate
2,400-pixel/5-megapixel output bounds. Saved metadata records source dimensions,
crop coordinates, scale, orientation handling and border, so recognized word
positions can be mapped back to their selected source region.

The optional `layout` is `auto` (page segmentation, or a single block for a crop),
`block` (one text block), or `sparse` (separated labels without guaranteed reading
order). These select Tesseract modes 3, 6 and 11. Reading every region or retrying
every mode is not required. Use the information gap and observed quality to choose
further reading. Higher resolution alone does not guarantee better OCR.

Each fresh OCR variant runs Tesseract once to save both literal text and bounded
TSV word geometry. The tool exposes only a compact diagnostic; engine confidence
is not verified accuracy. Exact engine binary/language-data hashes, layout and
prepared image identify new OCR results. Current permissions are rechecked on
cached reads and model delivery. Continuation requests retain region, layout and
text position. Historical page/OCR records retain their references and old output
shape; selected-region restrictions are explicitly scoped to the inspected region,
not unseen raster text elsewhere on a page.

Local commands use bounded captures and timeouts. Pillow handles PNG/JPEG crop,
orientation and scaling; Poppler and installed English Tesseract remain optional
native tools. Other languages, handwriting, complex tables and figure interpretation
require separate evaluation. Missing executables return a local diagnostic.

## Explicit vision authorization and accounting

The main reasoning model remains `openai/gpt-oss-120b`. The auxiliary reader uses
`qwen/qwen3.8-27b`, documented in [Groq's vision guide](https://console.groq.com/docs/vision),
with one saved PNG/JPEG image, no browsing/tools, and a 1,024-token completion cap.
Account-level availability remains untested until an authorized live call succeeds.

Set `allowVision: true` in a new `LiveStartRequest` or `LiveFollowupRequest` only
after vision calls are authorized. Missing/false means disabled, including old
runs; follow-ups do not inherit permission. The saved OSRA credential profile and
quota domain are reused. There is no automatic model/account fallback. Submit JSON
through the existing `scripts/dev mission start --input` or `followup --input`
interface; there is no new UI toggle.

Vision work uses the shared executor's model capacity. Its estimate counts 2,048
tokens per image plus text, rather than counting base64 as text. The default
natural-run HTTP budget rejects vision; a deliberately scoped runner must also
pass `AgentBudget(..., allow_vision=True)`. Vision and reasoning then share the
model-attempt and planned-token ceilings. Actual usage is retained separately.

Each vision operation saves a request intent, pre-HTTP dispatch marker and response
receipt. Known rate rejections return a tool diagnostic and update shared capacity;
they do not automatically resend the same operation. Uncertain dispatch preserves
the reservation and pauses the affected branch for reconciliation. Restart never
blindly resends it. Independent branches retain their own source/capacity checks.

## Verification boundary

Use the [source-to-answer walkthroughs](source-quality-walkthroughs.md) when
checking extraction, navigation and the actual model-delivered passage together.
They separate format fidelity from natural model understanding and answer support.

Focused offline tests exercise local PDF extraction/rendering/OCR, injected Qwen
responses, selected-page provenance, current permission checks, serialized model
inputs, accounting, opt-in, known 429 and uncertain dispatch handling. They do not
establish live availability, real figure/OCR accuracy or a supported research
answer. Live evaluation needs fresh explicit limits and must report the actual
reading method, model calls, source support and remaining uncertainty.
