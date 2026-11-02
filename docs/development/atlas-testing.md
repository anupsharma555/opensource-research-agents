# Atlas state, updates and interface verification

Read [setup](setup.md) first for pinned tools and installation. The browser and
native app consume backend projections; they never become a second research
database. Pydantic remains the contract authority. `scripts/dev generate` now
also emits standalone Ajv response guards: no runtime schema compilation or
relaxed Content Security Policy is required.

## State ownership and freshness

`LiveResearchApp` scopes a TanStack Query client to one mounted viewer session.
Query keys include origin, session, workspace and run. React Router owns view/run
navigation; feature reducers own command outcomes and Activity filters. React
Hook Form owns request text fields. Native/browser draft adapters still preserve
the frozen request ID, exact target and explicit save-before-submit behavior.
No query persistence plugin or client analytics exporter is enabled.

The authenticated `/api/live/updates` stream sends content-free invalidations
after SQLite changes. Every connection starts with an invalidation; reconnect
requests a complete authorized view rather than replaying an event history.
Notifications are coalesced before refreshing. The stream does not prove a
provider ran, an agent read a reply, or a scientific result was accepted.

While notifications are connected, the viewer also verifies the full state every
five seconds, including current source permissions and artifact integrity. It
falls back to 1.5-second polling if notifications are unavailable. Only hosts
advertising `X-Atlas-Updates: sqlite-invalidation-v1` on a successful state read
receive stream subscriptions. Transient errors, including initial connection
failures, retain automatic reconnect. An Internet-offline signal does
not disable local loopback reads. Authentication or identity failure clears the
viewer cache and requires reopening the selected workspace.

Technical run details show update mode, read/failure/cancellation counts and last
read duration, plus command attempt/error counts and last command response time.
Command response time ends at the acceptance/error response, not research completion.
These are in-memory viewer observations, not provider timings or
research progress. The saved snapshot time and last successful read remain
separate. Cancelling an obsolete GET never issues a research cancellation. Writes
are not automatically retried; uncertain writes retain their original identity.

## Repeatable browser checks

After ordinary setup, install the pinned browser once:

```sh
scripts/dev browser-install
scripts/dev browser
```

Linux CI can use `scripts/dev browser-install --with-deps`; that explicitly
installs operating-system browser prerequisites as well as browser binaries.
Browsers are cached under `.local/cache/playwright/`. Browser results, traces and
connection files stay under `.local/atlas-browser/`. Trace contents are private
test artifacts and must not be committed.

The suite builds real frontend assets and starts `scripts/atlas_fixture.py` on
loopback. The fixture uses bundled synthetic records and the real HTTP/session
and SQLite command boundaries. It never reads `.env` or dispatches research.
The fixture refuses to seed an unrelated nonempty directory. A displayed offline
proof label remains visible even though this test viewer receives fresh updates.

Playwright covers navigation/reload, exact evidence, follow-up acceptance and
lost-response replay, reconnect, workspace history, authentication, storage
privacy and automated request-form accessibility. MSW tests separately exercise
the fetch/query boundary. Neither mocked HTTP nor browser automation qualifies
scientific claims or native webview behavior.

Focused local commands are available through the pinned wrapper:

```sh
scripts/dev frontend lint:ui
scripts/dev frontend test ui/src/LiveQuery.test.tsx
scripts/dev frontend build
scripts/dev desktop-test
```

The hook lint gate currently covers the new live-data boundary. It is not a
claim that every legacy component has been audited or that all existing effects
meet the same stricter baseline. `scripts/dev check` includes this gate, shared
contracts, Python tests, component tests and the frontend build.

## Separate Mac app verification

Tauri's desktop WebDriver support does not include macOS. Playwright Chromium
tests therefore do not certify the Mac webview. Build the isolated test app:

```sh
scripts/dev desktop --test-app
scripts/dev atlas-fixture --state-dir .local/atlas-native/workspace --port 4188
```

Open `desktop/src-tauri/target/debug/bundle/macos/Research Atlas Test.app`.
Its application identifier and settings are separate from the ordinary app.
From Previous samples, open a recorded review, choose Start or follow up, and
connect the fixture directory. The fixture's private `connection.json` is for
the launcher; do not paste its token into reports or tracked files.

Verify a saved source and exact passage, write a synthetic draft, navigate away
and back, then choose File → Reconnect saved workspace. Confirm the same workspace,
run and draft are present. Submit only to the synthetic fixture and inspect the
accepted request identity. Inspect Technical run details for the notification
mode and freshness. Keep the offline proof label distinct from a live model run.

Native Rust checks cover URL/capability/draft authorization. This manual/native
journey covers the actual webview and IPC. Neither alone covers the other.
Stop the fixture when finished; its retained synthetic state may be reused.

## Performance and research-state checks

`scripts/measure_atlas.mjs` compares prepared baseline and current frontend assets
in Chromium against equivalent synthetic host workspaces. It records full-read
counts/bytes, visible-update samples, browser version and asset hashes below
`.local/reviews/tech-stack-implementation/browser-performance/`. Its source header
documents the required pinned environment and prepared asset locations. Do not
interpret a small local sample as production throughput or long-run endurance.

`scripts/evaluate_stack_candidates.py` provides separate synthetic FTS5 and
HTTPX evaluations. It does not replace production search/transport adapters.
FTS matches still require current record/version/permission checks; HTTPX pooling
does not establish safe retries or faster remote calls. Hypothesis-generated
store tests vary command replay/conflict sequences, while existing context tests
verify exact reconstructed model inputs. These mechanical checks do not prove
natural tool selection or incorporation of peer evidence by a model.

References: [TanStack Query defaults](https://tanstack.com/query/latest/docs/framework/react/guides/important-defaults),
[Tauri WebDriver limits](https://v2.tauri.app/develop/tests/webdriver/),
[Playwright accessibility boundaries](https://playwright.dev/docs/accessibility-testing).
