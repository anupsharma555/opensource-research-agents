# Corpus readiness and future additions

The SSD archive retains original material, reviewed metadata and immutable releases.
The hosted vector store is a derived retrieval copy. OSRA searches that copy and
checks selected source text against the release before including it in model input.
A reachable store alone does not prove that the local source or its current use
permission is available. See [the runtime data flow](bheb-corpus.md).

## Check readiness without API calls

After the repository's [normal setup](setup.md), run:

```sh
scripts/dev corpus
```

The wrapper reads only the configured `OSRA_BHEB_CONFIG` path from the environment
or ignored `.env`; it does not resolve a credential or contact any provider. An
explicit `--config /absolute/private/path/corpus-config.json` selects another
trusted local binding. It checks receipt identity, release/manifest identity,
exact upload hashes and the current source-use policy. A mounted body must also
have a supported source structure before it is counted as usable.

Ordinary output contains aggregate counts and reason codes, without article
content, credentials, store IDs or private paths. `metadata_only` means no checked
source is ready. `partially_ready` means some sources can be used; missing siblings
do not impose a collection-wide stop. These are local readiness results, not a
live access check or evidence that a model answered correctly.

When the SSD is unavailable, preserve that finding and continue candidate planning.
Do not synthesize a permission from an access label or change the selected release.
After mounting, rerun this command. Establish policy from the actual approved
operations and exact source versions, then replay saved hits through inspection
and check the serialized model input offline before a new model evaluation.

## Plan additions without modifying the archive

Create a private metadata file under `.local`, for example:

```json
{
  "candidates": [
    {
      "title": "Synthetic comparison study",
      "doi": "10.0000/example",
      "canonical_url": "https://example.org/article"
    }
  ]
}
```

Then run:

```sh
scripts/dev corpus --candidates .local/corpus/candidates.json --private-output .local/corpus/additions-plan.json
```

The output file must be new and inside this checkout's `.local` directory. It is
created with owner-only access and is never overwritten. The command reads no
candidate article bodies, writes nothing to the SSD and makes no network requests.
Omit `--private-output` for aggregate output only.

The comparison covers the selected verified release, not the complete Zotero
catalog. `added_candidate` means its exact identifier was absent from that release;
`duplicate` means an existing publication identity matched, not proof that unseen
bytes are identical. A supplied changed source version or upload checksum yields
`version_changed`. Title-only matches, conflicting identifiers and an unavailable
manifest yield `unknown`. The checksum, if supplied, must describe the same indexed
upload representation, not an original PDF or a different extraction. Optional
`source_id` and `source_version` provide exact known identities. Review unknowns
and reconcile all candidates with the authoritative catalog before importing.

## Future archive and index update path

Use the existing BHEB maintenance pipeline in its own checkout. Read its applicable
instructions and the scripts' current help before execution. OSRA's planner is a
handoff aid; it does not create another ingestion database.

| Stage | Existing owner / command | Required result |
| --- | --- | --- |
| Resolve additions | `backend/scripts/manage_rag_corpus.py reconcile-acquisition-batch` | Compare with current Zotero export and rights records; resolve duplicates and versions. |
| Prepare selected downloads | `backend/scripts/prepare_single_bheb_source.py` | Stage an already downloaded permitted PDF, checksum and review artifacts in the mounted archive. This stage writes files. |
| Preview import | `manage_rag_corpus.py capture-import-dry-run` and `check-acquisition-readiness` | Review the proposed new-only batch and resolve errors before applying changes. |
| Review catalog | BHEB catalog build, review and finalization workflow | Retain exact identity, source surface, provenance and operation-specific rights. |
| Plan release | `backend/scripts/plan_expanded_release.py` | Compare finalized catalog with the baseline and name a new immutable release. |
| Check publication | `manage_rag_corpus.py verify-release` and `preflight-publication` | Verify files and record finite size, retention and cost assumptions. |
| Publish when authorized | `manage_rag_corpus.py publish-release`, or its explicit resume path | Save a new store/publication receipt; preserve uncertain attempts rather than replaying blindly. |
| Verify and adopt | `manage_rag_corpus.py verify-publication`, then OSRA readiness and saved-hit replay | Verify remote parity separately; deliberately bind new OSRA runs to the new release. |

Publication needs authorization for uploads/storage and an appropriately permitted
credential. The current offline plan provides neither. Preserve the old release,
store and citations until a separately reviewed retention decision. Switching a
new run's binding must not silently change an existing run's evidence. A later
verified local bundle could allow selected source inspection without the SSD, but
copying it still requires the applicable storage and model-use permissions.

## Focused verification

```sh
.venv/bin/python -m pytest -q tests/retrieval/test_bheb.py tests/retrieval/test_bheb_maintenance.py tests/mission/test_bheb_corpus.py tests/development/test_bheb_maintenance_cli.py
```

Synthetic transports test the callable runtime tools and source-text delivery.
Maintenance tests cover unavailable archives, partial readiness, changed bodies
and policy, identifier/version decisions, redaction and output confinement. These
checks do not exercise real source adoption, catalog writes or publication.
