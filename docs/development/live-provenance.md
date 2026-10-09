# Inspecting local live provenance

The local live database can be inspected after its host stops. The saved records
answer which agent and configuration made a call, what tools were requested and
executed, what skills were loaded, and where supplied sources came from. Reading
these records does not start a research host, require `connection.json`, or make a
model or search request.

```sh
scripts/dev live provenance --state-dir .local/live --section all --limit 100
scripts/dev live provenance --state-dir .local/live --section calls --run-id RUN_ID
scripts/dev live provenance --state-dir .local/live --section tools --run-id RUN_ID
```

`--section` selects `calls`, `tools`, `configurations`, `skills`, `sources`, or
`all`. The direct installed command accepts the equivalent `--view` spelling:
`.venv/bin/ora live provenance --state-dir .local/live --view calls`. Results are
JSON, with up to 100 rows per section by default (`--limit` accepts 1–500 and
reports truncation). The inspector opens the selected `research.sqlite3` in
SQLite read-only and query-only modes, reads only approved overview columns,
and does not copy prompt, tool-argument, result, or source-content payloads
into its output. The database itself remains private research state, so review
any exported result before sharing it.

The SQLite views are `live_call_overview`, `live_tool_overview`,
`live_configuration_overview`, `live_skill_overview`, and
`live_source_overview`. The tool view has one row per invocation and points to
the originating model call and provider tool-call ID. It stores references to
inputs and outputs rather than duplicating large payloads. Configuration and
skill views expose fingerprints, versions and references; the underlying saved
records remain the authority for exact permitted details. Source provider
`user_supplied` means evidence was pasted or already saved; it is not a web
search receipt. A model provider such as Groq is likewise not a source provider.

`tool_invocation_events` retains timestamped status transitions separately
from each invocation's latest status. A historical event is backfilled only
when its timestamp was saved, and its basis says that it was reconstructed
from that timestamp. Missing start or finish times remain missing; they are
not inferred from the final tool row.

New calls pin the actual mission separately from the run that first created an
agent instance. A follow-up may reuse that instance without changing its original
run identity. The saved model input refers to the exact `calls.messages` payload
and its fingerprint rather than duplicating the conversation. The built-in
provider compares its outgoing request with that configuration before dispatch.

The local host still has an explicit recovery limit: a publish or peer-request
state change can commit before its tool completion receipt. If execution stops
between those writes, the started invocation becomes uncertain; it is not
automatically executed again. This preserves the accepted artifact without
claiming an atomic tool effect and receipt. Pending peer work and general
mid-mission continuation still need the later durable scheduler. These records
make that boundary inspectable; they do not establish autonomous recovery or
ten-hour operation.

Historical rows label their evidence boundary. A call with no recorded
configuration is `unknown_historical_configuration`; it does not inherit the
current model settings. `historical_snapshot` names an agent recovered from
the selected saved snapshot rather than a separately recorded instance. A
tool result recovered only from a later saved message
is `reconstructed_later_message`. `recorded`, `recorded_request_only`, and
ambiguous or uncertain labels keep direct observations distinct from missing
outcomes. A historical skill version from a snapshot establishes that the
instruction package was listed for the attempt; it does not establish that its
helper executed. No search activity is inferred for `user_supplied` sources.

## Explicit upgrade

An older live database has no overview views until upgraded. The command below
refuses a state directory held by an active host. It first saves a private
SQLite backup below `provenance-backups/`, checks that backup against original
run, snapshot and call records, applies the versioned migration to a temporary
copy, and verifies SQLite integrity, foreign keys, view readability and
historical-record hashes. Only then does it upgrade the selected database.
The backup path is returned in the local receipt and retained if verification
fails. Repeating a completed upgrade reports `already_current`.

```sh
scripts/dev live migrate-provenance --state-dir .local/live
```

This is an explicit local maintenance action. It does not instantiate
`ResearchHost`, launch a viewer, contact a provider, or restart an active host.
An active database should instead be copied through a coordinated host stop or
operator backup; the command will not upgrade it while `host.lock` is held.
Existing run IDs, snapshots and payloads remain intact. The live provenance
schema has its own version ledger, `live_provenance_migrations`; the shared
StateStore schema retains its separate `PRAGMA user_version`.

The installed VS Code SQLite Viewer extension is `qwtel.sqlite-viewer` 26.9.1.
Its local changelog documents table/view entries in the sidebar and view
display, with a known performance limitation for large views; its README says
it has no query runner. We have verified the SQLite views and CLI against
offline databases, but have not opened these new views in that extension's UI.
The CLI above is the tested read-only route even when the extension is
unavailable or a large view is slow to display.
