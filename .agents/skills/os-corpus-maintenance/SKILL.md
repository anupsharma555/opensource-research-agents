---
name: os-corpus-maintenance
description: Check OSRA's private BHEB corpus readiness, diagnose unavailable source inspection, or plan versioned article additions to the SSD archive and vector store. Use in opensource-research-agents; not for ordinary web search or automatic corpus publication.
---

# OSRA corpus maintenance

Use this Codex development workflow to make source readiness and future additions
reviewable. Research agents use `search_bheb_corpus` and `inspect_bheb_hit`; do not
insert this maintenance skill into their prompts.

Read [corpus maintenance](../../../docs/development/corpus-maintenance.md) for
commands, candidate format and the BHEB-owned update path. For tool integration or
source permission diagnosis, read [the corpus route](../../../docs/development/bheb-corpus.md),
which owns search-to-inspection behavior and configuration boundaries.

## Verify the requested layer

- Start with `scripts/dev corpus` from the repository root after its documented
  setup. It performs local checks only. Inspect aggregate counts and reason codes;
  never print `.env` values or credentials.
- Distinguish verified receipt metadata, available source bodies, current use
  permission and actual model delivery. An unavailable SSD prevents body checks;
  it does not prove the source is forbidden or prevent metadata discovery.
- Continue useful offline work when the archive is unmounted. After it is mounted,
  rerun readiness, review operation-specific policy and replay saved hits through
  inspection. Do not grant permission from a store label or an old demo approval.
- For partial readiness, report which checks remain unresolved without blocking
  already verified sources. Keep exact source versions, hashes and citations.

## Plan additions

Use a private candidate metadata JSON and `scripts/dev corpus --candidates PATH`.
When a durable detailed report is requested, add `--private-output` with a new
path inside `.local`; an existing report is never overwritten. A missing manifest
means unknown membership, not a new article. Exact identity matches concern the
selected release; title-only matches and the full Zotero catalog require review.

Hand off acquisition, catalog review, new release creation and publication to
BHEB's existing scripts described in the maintenance guide. Do not build another
catalog or modify a published release in place. Honor existing authorization;
complete offline planning without asking again. Actual downloads, archive writes,
uploads or retirement require their own authorized scope when not already given.
Never silently switch keys or stores.

## Verify and report

Use the focused tests linked in the guide when implementation changes. Check the
actual callable schemas and serialized source input for tool integration; scripted
model responses demonstrate plumbing, not natural agent behavior. Report offline,
saved-response replay, live retrieval and live model results separately.

Summarize readiness, useful completed work and the next concrete action. Put real
identifiers and source details only in an authorized private artifact. Update
Linear checklists only when requested and supported by evidence; retain archive,
account and live-model items that were not verified.
