# Inspecting shared state records

The shared `StateStore` keeps accepted, versioned research records in SQLite
and retained content in hash-named private files. Schema version 3 adds
readable SQLite views and a `record_origins` table. They help a developer
inspect relationships without opening every JSON body; the underlying
`records`, `heads` and exact-version `links` remain authoritative. The
[Stage 2 record guide](state-records.md) explains acceptance, backup and
recovery rules. These local records are an offline foundation, not evidence
that a production research host or provider ran.

Use the repository's installed Python to page through one view without
starting a host or changing the database:

```sh
.venv/bin/python -m ora.state.inspect --state-dir STATE_DIR --overview call_overview --limit 100 --offset 0
.venv/bin/python -m ora.state.inspect --state-dir STATE_DIR --overview invocation_overview --limit 100 --offset 0
```

For the locally generated Stage 2 demonstration in this checkout, use
`--state-dir .local/stage2/demo-state-v8`. It is ignored private test state;
the views and read-only command work with any compatible version 3 store.
The demonstration records `offline_test` proof and `controlled_clock` timing.
Its prepared model calls and saved local helper receipts remain separate;
neither makes a live model or search-provider request.

`--overview` accepts the view names below. The output includes `nextOffset`
when another page exists. Keep page sizes small when exploring a large store.
This route opens the store read-only and sets SQLite query-only mode. It does
not use a live session token, migrate an older schema, or call a provider.
The separate `--kind` and `--snapshot` modes can return full saved detail;
review those exports for private content.

The views are:

| View | What its rows expose |
| --- | --- |
| `record_provenance` | Every saved record revision, current-head flag, origin metadata when recorded, and exact IDs useful for joining. |
| `agent_overview` | Agent instance ID, display name, role, run, predecessor and revision. |
| `call_overview` | Prepared call manifest, instance, requested provider/model, configuration reference, fingerprints and any separately saved execution status. |
| `call_skills` | Skill/version/instruction fingerprints and whether its instructions were loaded for that manifest. |
| `call_tools` | Permitted tools, which were exposed, and saved implementation/schema identities. |
| `invocation_overview` | Saved local tool or skill invocation receipts and statuses, separate from call preparation. |
| `retrieval_overview` | Normalized delivery, route/provider, request identity, outcome, mapping version and returned-window metadata. |
| `retrieval_usage` | Each saved usage amount with its observed or documented basis. |
| `source_overview` | Exact source-copy and publication versions, access/rights/inspection state, retained-content flag and content hash. |
| `record_dependencies` | Exact-version record links and their relations. |

All revision-bearing views retain historical rows. `is_current` means a row
matches the current head; it does not erase or downgrade older revisions.
`record_origins` is written alongside each newly accepted record in version
3. Its format version, operation ID, proof basis, timing basis and
implementation reference are visible through `record_provenance` when supplied
at write time. The version 2 → 3 migration adds the table and views in one
transaction without rewriting old bodies or inventing old provenance. Origin
fields for earlier records remain empty, and the view labels their basis
`not_recorded`.

The call view labels a manifest `prepared`. That establishes the model,
settings, instructions, skills, allowed tools and input references selected
for that call; it does not establish that a model request occurred. Likewise,
`call_skills.instructions_loaded` does not mean a skill helper ran, and
`call_tools.permitted` or `exposed` does not mean a tool executed. Inspect
`invocation_overview` for a saved local helper receipt, and then inspect its
exact underlying record if the outcome matters. A retrieval row reflects a
saved normalized delivery and its provenance basis; an offline fixture row
does not prove live external contact. Source-copy metadata and a citation
locator do not establish that a passage supports a claim.

When the same provider response contains conflicting mapped metadata, the
selected field and its alternative remain available under `_mapping_conflicts`,
with their original field locations. Mapping precedence is not a scientific
resolution. Missing optional bibliographic fields do not prevent storage.

The views avoid private instruction text, prompts, query text, tool arguments,
raw results and source bodies. They do show metadata such as display names,
publication titles and source IDs, so review an export before sharing it.
Exact detail remains accessible from the private store by `{kind, id,
version}` and is subject to its normal content and disclosure controls.

On this workstation, VS Code has `qwtel.sqlite-viewer` 26.9.1 installed. Its
local changelog documents displaying views in the sidebar and notes a large
view performance limitation; its README says there is no query runner. The
view queries have been exercised against disposable SQLite stores here, but
these new views have not been visually opened in that extension. The
repository's read-only inspector provides a tested terminal route.

For an older store, take a verified backup and validate migration on a
disposable copy before opening the selected directory with a writable
version 3 `StateStore`. The read-only inspector does not migrate older schema
versions. `StateStore.backup()` copies the SQLite snapshot and referenced
hash-verified content into a new private destination; `restore()` exposes a
new destination only after validation. When a store uses an alternate
database filename, the backup manifest retains that name for restoration.
