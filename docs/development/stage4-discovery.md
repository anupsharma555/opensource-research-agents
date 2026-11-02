# Stage 4 discovery boundary

`ora.retrieval.discovery.run_discovery_page` accepts one agent-selected route,
query or identifier, filters, and page window. It makes one page request, saves
the response through the existing retrieval receipts, and returns exact saved
hit references, source identifiers and URLs, the next window when the provider
exposes one, and route-specific errors or waits. It does not decide which query
is scientifically useful, infer study identity, or promote a search hit to
claim-ready evidence. Peers may submit further queries and switch routes based
on what they learn. The 100-item per-call admission limit controls one HTTP
request's working set; it is not a mission, query, or research-iteration cap.

PubMed Search has two HTTP legs: ESearch selects a page of PMID IDs, then
ESummary retrieves their metadata. A successful nonempty ESearch ID list is
saved as an exact `discovery_esearch_leg` before ESummary dispatch. If ESummary
is rate-limited, malformed, interrupted or otherwise fails, the result retains
`esearch_ref` and its original retrieval time. The agent may explicitly pass
`resume_esearch_ref` on a new `pubmed.search` attempt with the same task, actor,
query, filters and page window; that attempt sends only ESummary for the saved
IDs. Omitting the ref requests a fresh ESearch. A changed question or page
cannot borrow the old IDs, and no hidden cache or age-based scientific choice
is imposed. The new attempt's HTTP receipt and request record link to the
older ESearch observation so reviewers can judge its freshness. A partial
ESummary keeps valid selected siblings and marks missing selected IDs as item
errors; an unselected ID is not silently accepted.

PubMed publication-date (`pdat`) search filters can match a print or electronic
publication date. ESummary's displayed `pubdate` remains `publication_date`,
with `publication_date_basis=pubmed.esummary.pubdate`; a reported `epubdate` is
retained separately as `electronic_publication_date`. Thus a result displayed
as 2017 can legitimately match a 2016 filter when its reported electronic date
is in 2016. Neither field alone proves which date PubMed used for a particular
match. Exact EFetch XML similarly retains `ArticleDate[Electronic]` when
reported, alongside the journal issue's `PubDate`, without filling missing day
precision. Older saved hits are not reinterpreted or rewritten.

`pubmed.abstract` is a separate, agent-selected exact-PMID EFetch XML call.
It accepts one positive numeric `selected_id`, `filters.pmid`, or matching
query identifier; conflicting identifiers reject before HTTP. The returned
`MedlineCitation/PMID` must equal that selection. The adapter maps the
published `Article/Abstract/AbstractText` with section labels and reported
DOI/PMCID; an absent abstract remains a metadata-only hit. A PubMed record's
reported `ABSTRACT TRUNCATED` marker is preserved and labeled as limited text,
not a complete abstract. An unexpected record type, duplicate article,
conflicting selected PMID, or malformed XML cannot create an accepted hit.
This route does not run a topic search, retrieve article full text, or change
`pubmed.pmid`'s ESummary-only meaning.

Europe PMC and Crossref abstract cleanup removes recognized HTML/JATS formatting
while preserving comparisons such as `score < 8`, entity-encoded text and
paragraph boundaries. Unknown or ambiguous angle-bracket expressions remain
visible instead of being discarded. This applies to newly mapped responses;
it does not rewrite retained copies or prove that an older damaged abstract
has been recovered.

## Call and handoff

`available_routes()` reports route name, provider, index, operation, keyed
status, supported filters, query description and actual returned-content depth. A call uses the existing normalized request
fields: `mission_id`, `task_id`, `request_id`, `attempt_id`,
`agent_instance_id`, `route`, `provider`, `index`, `operation`, `query`,
`filters`, `requested_at`, and `requested_window`. For example, an agent can
request `europe_pmc.search` with its own query, `from_date` and `until_date`,
and `{"cursor": "*", "limit": 10}`. It can then choose whether to use the
returned `window.next_window`, reformulate the question, or use another route.
The adapter rejects filters unsupported by the selected route before dispatch,
and rejects an inverted publication-date interval. Europe PMC accepts a journal
name; Crossref search accepts its publication-type token; Exa Search accepts a
selected `instant`, `fast` or `auto` mode. Synonyms and subject syntax remain
caller-selected query text rather than hidden host expansion.

The return value includes `delivery_ref`, `attempt_ref`, `page_ref`,
`accepted_hit_refs`, `reused_hit_refs`, projected `hits`, `window`, `outcome`,
`http_call`, `http_legs`, and any `retry_at`. A hit projection includes the
provider item ID, original URL, source surface, rights and access status,
content hash, and mapped scientific metadata. The full normalized source
candidate is `store.get(**hit_ref)["body"]["source"]`. The intake owner can
accept that exact ref together with a separately fetched byte copy, declared
rights basis, and identity check. A deeper article fetch creates another
encounter; it does not rewrite the search hit's surface.

New missions and follow-ups pin `fetch_source_autoattach_version: 1`. When a current, permitted scholarly hit has an original-source URL and `inspect_hit` is attached, the host can attach the granted `fetch_source` schema in the same turn. Explicit release still wins. This exposes a choice; it does not fetch, inspect, or promote the hit to checked evidence. The handler rechecks the exact current hit, shared-hit access and source-use limits when selected. Saved runs without the pin keep their original attachment policy. Saved request blobs are never rewritten; an eligible unclaimed pre-dispatch request can be superseded only through the host's existing recovery policy. Offline attachment tests establish the menu and permission checks; a natural model's selection and useful source reading need separate run evidence.

A direct-web hit owned by another task or peer can expose permitted metadata
without granting task-local fetch authority. Its metadata-only hint uses the same
ownership check as `fetch_source` and points to an own-task search, a peer request
for a checked finding, or a deliberately retained permitted library copy when one
exists. It does not suggest fetching an ineligible hit or widen source access.

For a returned published abstract, the hit and projection also expose
`source_use_policy`: a versioned private-research-use decision with the exact
abstract hash, HTTP-response hash, route, source URL, timestamp and source
documentation. Intake retains this provenance in its checked copy and
source-use record. A different URL or different bytes need their own use basis.
When a provider supplies a copyright statement, license URL or full-text OA
indicator, a sanitized notice is saved with that hit. A narrowly explicit
prohibition on text mining, automated analysis or model input blocks the
private abstract default. A license URL or a plain copyright notice is a
clue to review, not an inferred grant or automatic block.

Page dispatch is committed before HTTP. Repeating a completed attempt reads
its saved result without another provider call. An interrupted dispatch is
reported as `uncertain_dispatch`; the agent or host may choose a new attempt
ID after inspecting the ambiguity. A 429 or 503 with `Retry-After` saves a
wait for that route's capacity group only. The other providers continue. The provider's wait is
an admission constraint, not a scientific stop instruction. The HTTP receipt
keeps status, observed byte count, digest scope and elapsed time for each leg;
it does not retain raw provider bodies. PubMed Search has separate ESearch and
ESummary legs; an exact-PMID abstract fetch has its own EFetch XML leg. A
response rejected at the 8 MiB transport ceiling is reported
as `oversized_response`, distinct from malformed JSON, a valid empty page and
known pagination exhaustion. Its HTTP-leg digest covers only the observed
prefix, with no asserted complete-body hash. The mapped delivery retains its
own hash; `provider_payload_condition` and `upstream_payload_condition` identify
the size rejection. A smaller page, selected passage or table, artifact-backed
intake, or another permitted route can be attempted separately. One local size
failure does not exhaust the evidence or close a mission.

NCBI's public E-utilities mode permits at most three requests per second
without an API key. A durable shared reservation paces PubMed ESearch,
ESummary and EFetch legs across concurrent local processes and restarts; its admission
wait is included in each HTTP-leg receipt. A provider 429 still takes
precedence through `Retry-After`. This rate applies to NCBI actions, not to
other scholarly or broad-discovery routes. See the official
[NCBI usage policy](https://www.ncbi.nlm.nih.gov/books/NBK25497/).

## Current routes and source depth

| Route | Translation and returned depth | Important limit |
| --- | --- | --- |
| `europe_pmc.search` | Core JSON search, cursor pages, explicit publication-date interval and optional journal; metadata plus returned abstract when present | A returned published abstract can be read and retained for this project's private research use. `isOpenAccess` describes full-text OA, not the abstract's license; it is never used as the abstract permission proxy. |
| `pubmed.search`, `pubmed.pmid` | ESearch then ESummary for search, or ESummary for one PMID; PMID, metadata, and DOI/PMCID only when reported | Search metadata is not a full text or abstract. PubMed's ESearch 10,000-result exposure limit is reported as a route limit, not an exhausted literature claim. |
| `pubmed.abstract` | One exact PMID through EFetch XML; citation metadata and the published abstract when present, including structured labels and reported truncation | No abstract is fabricated for a metadata-only record. Its private-research use decision records the exact abstract and response hashes; copyright/license remains unverified, explicit restrictions take precedence, and no full-text or redistribution grant is inferred. |
| `crossref.search`, `crossref.doi` | Bibliographic query or exact DOI; deposited metadata and abstract surface when returned | A returned published abstract can be read and retained for private research. Publisher/author copyright remains unverified; no redistribution or full-text use is inferred. A missing DOI gives an empty result. |
| `exa.search`, `exa.contents` | Search results are teasers. An explicitly selected Contents call may return extracted text or a provider-generated summary, labeled separately. | No inferred pagination or exhausted web coverage. Contents records request ID, item status, cache source, requested cache age, inspected character count, possible clipping and provider-estimated cost. Extracted text remains a candidate until source identity, rights and completeness are checked. |

Provider-specific translation is saved in `http_call` and the original query
in the normalized request. There is no hidden synonym expansion. Route
capabilities can be shown to a model-led peer without mandating use of every
route. The Exa route exposes `instant`, `fast`, and `auto` Search modes; the
separate deep-search and Agent products are not activated through this
adapter. Exa credentials are passed only for an explicit selected call and are
never included in saved HTTP headers. Exa Search and Contents are separate
products and receipts. An `exa.contents` call requires one selected ID or URL;
it can add a bounded `selection_reason`, `max_characters` and
`max_age_hours`. Direct URL selection is valid without a prior Exa Search hit.
Exa's `startPublishedDate` and `endPublishedDate` are exclusive timestamps.
The date-only `from_date` and `until_date` filters request inclusive UTC calendar
days: the adapter sends the microsecond before the first day's midnight and the
midnight after the last day. The unrepresentable outer boundaries (`0001-01-01`
as a start or `9999-12-31` as an end) are rejected rather than broadened.
These are requested bounds; returned publication dates still need inspection.
The exact selection, a supplied reason or an explicit `not_supplied` status,
and a stated freshness basis are saved with the page
result, while the normalized delivery retains the request settings and returned
candidate. A missing `max_age_hours` is labeled as an unqualified provider
default, not a freshness measurement. The agent can avoid a duplicate fetch
when adequate inline/cached text is already available. `costDollars.total`, when returned,
is a provider estimate, not a measured account charge or Groq usage.

### Groq browser discovery and original-page reading

The mission capability `browser_search` selects one provider-managed browser turn
on the configured non-streaming GPT-OSS model. That turn exposes the native
descriptor alone and uses low reasoning; ordinary turns restore the configured
reasoning setting and granted local tools. The capability catalog is visible
before selection. `request_capability` normally selects the native turn.

A captured HTTP 400 can also identify an explicit attempt to call this granted
capability before its descriptor was exposed. The host checks the exact saved
request and rejection, current descriptor and non-streaming configuration, then
records `recover_rejected_capability_choice` for the next normal turn. It does
not execute rejected arguments or issue a request during repair. Unavailable
capabilities and other errors do not take this path. Native-turn rejection and
unselected-capability rejection are separate diagnostic cases.

Returned text and URL metadata remain discovery material. Literal safe URLs
become task-bound leads; opaque citation markers do not establish an inspected
source. When discovery prose exceeds the input allowance, the context builder
keeps as many exact hit IDs and URLs as fit before optional board details. These
remain labeled as uninspected metadata. The saved-result reader preserves access
to the complete text and leads, including when no lead preview fits.
`fetch_source` can retain an exact public HTML fetch from such a lead as
a `public_document_fetch`, without inventing a publication title or identity.
The normal source-policy and extraction checks govern a checked nonpublication
copy, and `read_source_window` supplies exact citable text. Restart reuses the
saved fetch only for the original valid operation; revoked use or replacement
work cannot inherit permission from that fetch.

Controlled tests establish these mechanics. Natural model selection, successful
live continuation and factual support require separately saved run evidence.

The route shapes were checked against the official [Europe PMC REST guide](https://europepmc.org/RestfulWebService),
[NCBI E-utilities guide](https://www.ncbi.nlm.nih.gov/books/NBK25501/),
[Crossref REST guide](https://www.crossref.org/documentation/retrieve-metadata/rest-api/),
and [Exa Search](https://exa.ai/docs/reference/search) and
[Contents](https://exa.ai/docs/reference/get-contents) references on 9 October
2026. Europe PMC and Crossref caution that access to an abstract does not
itself resolve its copyright or reuse terms. A search result's identifier
join is therefore a reported metadata observation, not a verified study-family
merge.

The abstract rule is deliberately use-specific: public API delivery of an
article's published abstract supports private reading, local source-grounded
analysis and model input under this project's user authorization. In the
existing wire model `rights_status=permitted` means permitted **for that
recorded private use**, not a verified copyright license. The per-hit policy
keeps `copyright_license_status=unverified`, `full_text_access=not_inferred`,
and `redistribution=not_authorized_by_this_decision`; intake sets
`export_allowed=false`. An actual source restriction or explicit stricter
caller policy wins. Inaccessible responses, full text and Exa extraction do
not gain this rule. Source identity, bytes, completeness and passage locators
still need intake checks; missing full text prompts complementary search.
Earlier saved hits keep their original rights/source version; the new decision
applies only to newly accepted abstract hits and never rewrites prior evidence.
Europe PMC's [copyright notice](https://europepmc.org/Copyright) and
[API reference](https://europepmc.org/docs/EBI_Europe_PMC_Web_Service_Reference.pdf)
distinguish access, license and full-text OA. Crossref likewise says its
[public REST API](https://support.crossref.org/hc/en-us/articles/214320426-REST-API)
distributes abstracts while some remain under publisher or author copyright.

## Venue catalog

`venue_seeds.json` contains the 25 official navigation seeds from the private
nine-role planning document, whose links were noted as inspected on 7 October
2026. Their `navigation_verified_by_plan` state reports that source precisely;
it does not claim an adapter or API was tested. Records distinguish journal,
conference, repository and registry. Catalog revisions can identify journal
issues/volumes/archives, conference programs/abstracts, repository collections/
datasets/searches, and registry searches/studies. These labels describe navigation
destinations, not a publication's identity, scientific quality or permitted use.
Unclassified new leads remain usable as `unverified_navigation`; a precise label
does not upgrade their qualification. Topic and geographic scope are absent when the plan did not
verify them. The APA annual-meeting lead is navigation-only with its
generative-AI input restriction retained.

`seed_catalog`, `list_venues`, `resolve_venue`, `propose_venue`,
`revise_venue`, `record_navigation` and `catalog_export` provide a local
query/write surface. A relevant unlisted venue can be proposed and used at
once; a proposal is not an admission request. Revisions create new versions,
and navigation outcomes remain dated separate records. A publisher 403 is
reported as blocked from that HTTP route, not a claim that the venue is dead.
An alternate permitted copy can be linked without changing the original
route's rights state. Catalog entries, provider indexes, source works and
retrieval encounters remain separate identities; study-family deduplication
belongs to evidence intake.

The mission-facing functions `list_catalog_leads(store, mission_id, run_id,
actor_id, text, kinds, offset, limit)` and `propose_catalog_lead(store,
mission_id, run_id, actor_id, operation_id, proposed_at, name, kind, url,
rationale)` authenticate the agent's run. `CATALOG_TOOL_SCHEMAS` and
`CATALOG_TOOL_DESCRIPTIONS` are ready for the mission tool registry. Listing
filters and pages navigation leads; it never ranks scientific merit or bars an
unlisted source. A proposal becomes usable at once, records the proposing
actor and reason, and stays private to its mission until catalog maintenance
promotes it. `record_navigation(..., mission_id=...)` can track that proposed
lead without exposing another mission's proposals.

`list_catalog_leads` adds an exact `catalog_ref` to each listed entry, and
`propose_catalog_lead` returns the proposal's exact ref. The model-facing
`navigate_venue` schema accepts that ref and an optional catalog link index;
the host calls `navigate_catalog_lead(store, mission_id, run_id, actor_id,
operation_id, catalog_ref, link_index, link_offset, link_limit)`. This opens a
proposed unlisted venue immediately through the guarded public fetch, without
admission or a venue whitelist. A dispatch marker precedes HTTP, and an exact
operation replay reads its saved result; an incomplete dispatch reports
`uncertain_dispatch` instead of silently repeating an external request. The
private navigation record retains the dated HTTP status, original/final URLs,
redirect chain, content hash and byte count, a bounded title/heading and link
preview, and the fetched bytes. The separate `navigation_context` keeps the
catalog resource type, link type and year, qualification, verified route and
access note next to the actual page result. Tool results page public HTTPS
links as navigation leads only (`evidence_status=navigation_lead_only`). They
do not return article-body paragraphs or create an inspected source copy. A link or page
does not establish article identity, permission or claim support; agents can
select further search or inspection based on the observed page.

The optional `inspect_navigation_document` tool inspects the exact retained
HTML from a task's saved navigation without fetching it again. A matching
public, unauthenticated fetch receipt and the existing private-research-use
policy are required before text becomes a checked source copy. Explicit page
or catalog use restrictions take precedence; older navigation records without
authentication evidence remain metadata only. This copy retains its exact
navigation origin with no inferred publication identity. It appears as
`navigation_text` in source inspection, and `read_source_window` supplies its
checked passages through the normal current-use checks. JSON document intake
is not supported by this path. An interrupted local inspection can complete
its original saved operation after restart without another HTTP request;
revocation, cancellation and replacement ownership are checked before delivery.

Direct intake fetch now accepts `fetch_public_source(url)` for any HTTPS host
whose DNS answers are globally routable; callers may still set
`allowed_hosts={...}` for a narrower access rule. It pins the checked public
IP during connection while using the original hostname for TLS, repeats the
check for redirects, rejects credentials and nonstandard ports, disables
ambient proxies, and keeps bounded reads. Its result includes requested URL,
final URL and redirect chain. Reachability alone grants no use rights or
evidence status; an HTTP 404 remains a fetch failure, not a negative research
finding.

The direct-copy use decision is separate from an abstract hit. The optional
`IntakeService.inspect_response(..., rights_status="unknown",
public_fetch_receipt=fetched, identity_basis=...)` path saves an
`ora.evidence.direct_private_use.v1` decision after matching the actual public,
unauthenticated 200 response's bytes, SHA-256, length, MIME, time, final URL
and redirect chain. It grants local storage, model input and factual synthesis
only when intake classifies an abstract, passage or full article and checks
the work identity through a visible matching DOI or a host-verified
`verified_source:` basis. Generic discovery-hit identity text is insufficient.
Copyright license stays unverified; export/redistribution remain ungranted.
An explicit source prohibition, login/paywall response, wrong article,
unresolved identity or mismatched receipt blocks the grant. A changed redirect
URL is provenance for the identity check, not itself proof of a wrong article.
The selected abstract's policy cannot authorize a deeper copy.

For HTML, a delimited abstract without a verified article body stays an
`abstract` even when its Methods/Results text is long. A checked article body
after the abstract, or a sufficiently substantive JATS `<body>` with sections,
can be `full_text`; a short JATS body remains `passage`. The exact bytes,
parser blocks and locators remain in intake records. The mission runtime must
pass the optional fetch receipt and independent identity basis; otherwise its
existing explicit source policy still governs.

## Qualification

The default offline gate is `scripts/dev check`; focused checks use
`.venv/bin/python -m pytest tests/retrieval`. Live calls require the explicit
`.venv/bin/python scripts/qualify_stage4_discovery.py --live` command. Add
`--check-seeds` to navigate the 25 catalog links. The script saves full
receipts, exact requests, source refs, source hashes, provider statuses and
timing under ignored `.local/stage4/discovery/`, with restricted file modes.
It never prints the configured Exa key. Its five named difficulty tiers are
deterministic API qualification probes, not evidence of a model-led mission.
The script's finite case list is a test resource allowance and does not limit
production research. Keep private source copies and provider payloads out of
tracked files.

Useful interpretation still requires checking actual returned publications,
distinct eligible studies, source copies and passages, and the shared
model-led mission. Provider HTTP success, a catalog homepage, or a title
match does not establish that a work answers the research question. This
document and offline tests establish the local boundary; individual live
receipts establish only the routes and cases they actually exercised.
