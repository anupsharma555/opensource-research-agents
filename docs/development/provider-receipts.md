# Retrieval envelopes and receipts

`ora.retrieval.normalize_response` converts a provider-shaped search or fetch
response into an internal, allowlisted result. `record_delivery` validates the
persisted envelope with the canonical `RetrievalEnvelope` in
`ora.contracts.models` and saves it through `StateStore`. This stage uses two
**reference** response layouts, `count_offset` and `opaque_cursor`; neither is a
claim that an Exa, Crossref, NCBI, or other live API has been integrated.

## Caller and evidence handoff

A caller supplies stable mission, task, logical request, attempt, and delivery
IDs; provider, route, optional index, search or fetch operation, requested time,
query, filters, and requested offset/limit or cursor. The caller also supplies
retrieval time and the provider response. `record_delivery` returns exact
`retrieval_delivery`, `retrieval_attempt`, and accepted `retrieval_hit` references.
An optional `agent_instance_id` records which configured peer made the request.

Each `retrieval_hit` contains the sanitized source candidate, its provider and
route, logical query/request ID, retrieval time, and the delivery/payload refs.
The canonical request also keeps a SHA-256 of the original UTF-8 query so a
redacted query remains distinguishable without retaining credential text.
The evidence module can pass that exact hit ref to its separate
`source_encounter` record. A retrieval hit is not a verified publication,
study-family identity, inspected article copy, or claim-support judgment. Source
identity and rights-aware copy acceptance belong to the evidence module.
Failed attempts stay in retrieval state and cannot downgrade an accepted copy.

The stored delivery envelope uses `schemaVersion: "1.0"` and the canonical
camel-case wire names. The normalizer's adapter-specific `schema`, `window`, and
redaction diagnostics are internal and do not define a second wire schema. The
shared schema generator remains the only source for client schemas and types.

## What a page receipt means

The result window keeps the requested slice distinct from the returned slice,
provider-reported total, known unseen count, and whether another page is known
to exist. `null` means a total or next page is unknown, not zero. A provider
exposure limit can leave known results unseen with no obtainable next window.
The provider stop reason is distinct from a peer's optional `continue`, `stop`,
or `switch_route` decision. A provider quota wait is a failed attempt with its
own status, never a valid empty literature result.

The following is an excerpt of the **controlled** `count_offset` fixture checked
by `test_count_offset_keeps_first_twenty_and_reports_known_unseen_twenty_first`:

```json
{
  "request": {"requestedWindow": {"offset": 0, "limit": 20}},
  "returnedWindow": {
    "returnedCount": 20,
    "acceptedCount": 20,
    "providerTotal": 21,
    "knownUnseenCount": 1,
    "moreAvailable": "yes",
    "nextWindow": {"offset": 20, "limit": 20},
    "providerStopReason": "more_available"
  },
  "outcome": "success"
}
```

The separate cursor fixture leaves `providerTotal` and `knownUnseenCount`
unknown while retaining the next cursor. Even when a cursor response reports a
total, it does not yield a known unseen count without a page position. Other
fixtures distinguish an empty
page, one bad item, mixed valid/invalid items, provider error, quota wait,
malformed top-level data, provider exposure limit, duplicate delivery, a new
retry, a same-attempt redelivery, and a late failed retry. Valid sibling hits
survive item-local errors. Original-source abstracts and provider-generated
summaries have different `origin` and `accessSurface` values; a generated
summary cannot pass as inspected source text.

| Reference input | Expected outcome | Window or persistence distinction |
| --- | --- | --- |
| 20 of 21 offset results | `success` | One known unseen; next offset retained |
| Cursor page, unknown total | `success` | Next cursor retained; unseen count unknown |
| Zero results, reported total zero | `empty` | Exhausted, no accepted hit |
| One invalid item only | `item_failure` | Item diagnostic, no accepted hit |
| Valid hit plus invalid item | `partial` | Valid hit and item diagnostic both saved |
| Provider error or quota wait | `provider_failure` | Error versus quota stop reason; valid observed usage survives |
| Malformed top-level response | `malformed_payload` | Attempt/delivery saved, no accepted hit |
| Known results past exposure limit | `success` | Unseen count retained; stop reason `provider_limit` |
| Duplicate delivery | Prior outcome replayed | No second delivery, hit, or event |
| Distinct retry or late failure | Own attempt outcome | Accepted hit survives; usage remains per attempt |

An actual offline `StateStore` run with a fictional abstract and one bad sibling
produced this shortened receipt. The `retrieval_hit` and `retrieval_delivery`
records resolve by their exact refs; the returned source URL has a token-like
query value removed. The full sanitized blob hash and original mapping hash are
different, making the transformation inspectable without storing original text.

```json
{
  "outcome": "partial",
  "delivery_ref": {"kind": "retrieval_delivery", "id": "retrieval-delivery-5f124056a222541dbce21ca3a83d729f", "version": 1},
  "accepted_hit_refs": [{"kind": "retrieval_hit", "id": "retrieval-hit-a411fd639050c351f7c3896a7f5a829d", "version": 1}],
  "item_errors": [{"position": 1, "code": "invalid_item", "reason": "Provider marked this item as failed"}],
  "raw_payload_ref": {
    "original_sha256": "8b6a356900c39b34e13a8387a7ae660c5d3a1a8f98b024337ece9c2284ee0a40",
    "sanitized_sha256": "90d7a7b0bb71fdd05b00cc834db5fe9344a96e784b049661c580e289746bc931",
    "transformation_version": "allowlist-and-redact-v1",
    "original_retained": false
  },
  "stored_hit_url": "https://example.test/article/1?api_key=%5BREDACTED%5D"
}
```

## Payload, rights, and usage

Provider bytes are hashed exactly when supplied as bytes; a supplied mapping is
hashed in canonical JSON form. The original response is **not** stored. The
retained blob is an allowlisted and redacted projection, linked by its own SHA-256, original
SHA-256, transformation version, and changed-field paths. URL userinfo,
fragments, and token-bearing query values are removed from stored URLs; common
inline credentials and credential-named request filters are redacted or dropped.
The projection contains no unknown top-level provider fields or raw error text.
Only content explicitly marked `rights_status: permitted` can be retained;
otherwise its content digest and access/rights states remain while text is
omitted. This is conservative local receipt handling, not a universal PII
scrubber or a legal rights determination.

Usage entries preserve the provider's unit name and distinguish `observed`
response values from `documented` estimates. Valid observed usage on an error or
quota-wait response is retained with that failed attempt; an invalid usage field
adds a diagnostic without discarding valid units or implying a successful search.
A duplicate delivery returns the
same transaction result. A different delivery of the same attempt is visible
but marked as not counted a second time for attempt-level usage; a new attempt
has its own receipt. An accepted candidate can be reobserved in another retry
without making a duplicate hit. No unit conversion or account billing claim is
made here.

## Verification boundary

The focused offline checks cover both reference layouts, the outcome matrix,
retained-hit links, canonical wire validation, replay and retry behavior, and
secret/rights handling. Run them with:

```sh
.venv/bin/python -m pytest -q -p no:cacheprovider tests/retrieval
.venv/bin/ruff check src/ora/retrieval tests/retrieval
```

On 2026-10-08 these commands passed: **16 tests** and Ruff. This proves the
offline normalization and store behavior exercised by those fixtures. It does
not establish live provider adapter conformance, scientific source identity,
claim support or billing accuracy. The separate [recorded integration](stage2-verification.md)
checks this receipt format in Atlas and Codex; live provider display requires its
own adapter and saved evidence.
