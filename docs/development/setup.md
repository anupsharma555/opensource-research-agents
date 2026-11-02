# Reproducible development

The supported foundation toolchain is **Python 3.12.12**, **uv 0.9.21**,
**Node 24.21.0 LTS** and **npm 11.19.0**. Native Mac builds use Rust/Cargo 1.93
and Xcode Command Line Tools. The Mac prototype was checked on macOS 15.5 arm64;
Linux is the cloud/CI target for Python and React, not a substitute Mac test.

## Start a clean checkout

```sh
scripts/dev bootstrap
scripts/dev setup
scripts/dev check
scripts/dev ui
```

`bootstrap` uses an existing exact toolchain or downloads an isolated copy below
`.local/`. Node archives are verified against the official release checksum.
uv's package is pinned from PyPI. Nothing changes the global Python, Node, model
configuration or credentials. A system Python 3.10 or newer can run this bootstrap;
the installed project code runs in the pinned Python 3.12 environment.

`setup` runs `uv sync --locked` and `npm ci`. Both package managers use their
committed lockfiles. A before/after hash check verifies the lockfiles did not
change. Python's virtual environment is `.venv/`; caches, temporary receipts and
downloaded toolchains are under ignored `.local/`. The wrapper does not load `.env`
or call a research provider. Child processes inherit the caller's environment and
package-manager configuration; this is not a secret-isolation sandbox. Do not put
research-provider credentials into CI or the offline cloud environment.

The shared wrapper also keeps uv-managed Python installations and pip caches under
`.local/`. This was added after an actual Codex Cloud setup found that its default
home-directory Python installation path was read-only. Local and cloud setup now
use the same repair automatically; no competing cloud dependency list is needed.

`check` uses the installed environment and runs canonical contract generation
checks, benchmark and skill references, Python lint/tests, shared JavaScript
contract fixtures, React component tests and a production frontend build. It uses
no model/provider or E2B key. Localhost port-allocation tests require an environment
that permits loopback binds; a sandbox denial is a test-environment failure, not
evidence of a concurrency bug or a scientific outcome.

Scripted mission tests should fail promptly on unexpected transport assertions
instead of allowing the recovery loop to consume more synthetic turns. Assert
the current serialized evidence (including exact peer-answer references), and
represent historical runs by their saved version fields. Keep diagnostic holds
explicit and reversible; a bare waiting state can strand a fixture indefinitely.

On the initially observed Mac sandbox, uv 0.9.21 failed while reading macOS network
configuration. The same exact tool succeeded under the task's approved execution
boundary. Do not silently replace it or change global configuration to hide that
failure. Run the existing `.venv` checks independently while repairing setup access.

## Commands and optional work

For browser tests, the isolated Mac test app, and real-time viewer diagnostics,
read [Atlas interface verification](atlas-testing.md). It owns those commands and
their browser/native/synthetic proof boundaries.

| Command | Purpose |
| --- | --- |
| `scripts/dev doctor` | Show actual runtimes and lock hashes without reading secrets. |
| `scripts/dev generate` | Regenerate schemas, examples and TypeScript from canonical Python models. |
| `scripts/dev inspect --view sources` | Inspect the same synthetic source records visible in the Mac app. |
| `scripts/dev inspect --view diagnostics` | Inspect operational sample data separately from findings. |
| `scripts/dev desktop` | Build the unsigned debug Mac app with embedded frontend assets. |
| `scripts/dev live serve --runtime mission --allow-model-calls --state-dir .local/research` | Explicitly start the local research host; new and saved unfinished work may call configured providers. |
| `scripts/dev mission status --state-dir .local/research` | Read that host's saved research state without starting work. |
| `scripts/dev setup --offline` | Install from populated caches; report an unavailable package precisely. |
| `scripts/dev setup --observability` | Install the pinned, optional OpenTelemetry SDK and OTLP HTTP exporter for explicit trace export. |
| `scripts/dev setup --audit` | Install the pinned Python advisory scanner; it does not run a scan. Combine with `--observability` if needed. |
| `scripts/dev audit-install` | Install pinned cargo-audit into `.local/tools/cargo-audit` with a separate local Cargo cache. |
| `scripts/dev audit` | Explicitly query public Python, JavaScript and Rust advisories; save reports under `.local/audits` without modifying lockfiles or upgrading dependencies. |
| `scripts/dev evaluate-fts` | Compare metadata term search and FTS5 on versioned synthetic relevance scenarios, without changing research state. |
| `scripts/dev analysis-eval-setup` | Install the isolated locked statsmodels experiment; optional `--offline` uses cached packages only. |
| `scripts/dev analysis-eval` | Compare synthetic study-level calculations against independent reference algebra. |
| `scripts/dev analysis-eval-test` | Test the isolated statistical comparison, including unsupported numerical/method boundaries. |

`ORA_NODE_BIN` may select an explicit **matching** Node executable. It is not a
version fallback. Optional runtime comparisons have their own pinned dependency
snapshots under `experiments/runtime_comparison/`; the chosen first runtime is
standard-library asyncio and SQLite, so those frameworks are not core dependencies.
The selected-model skill experiment is separately invoked and never part of CI.

Observability dependencies are opt-in. `setup --observability` uses the same
committed `uv.lock` and ordinary frontend setup; it does not enable trace export,
read `.env`, or contact LangSmith. Export requires separate explicit runtime
configuration. A later default `setup` returns the Python environment to its
default dependency set.

Advisory checks are separate from the offline gate because they need current
network data. `audit --ecosystem python` (or `javascript` / `rust`) narrows a scan.
Python exports the locked current-platform requirements, including all extras
and dependency groups, then audits without resolving or installing packages.
Exit 0 means no reported findings or warnings, 1 means advisories or Rust
maintenance/soundness warnings, and 2 means an incomplete check. Read the private
JSON and stderr reports before selecting fixes; a registry outage is not a clean
result. npm audit is already supplied by the pinned Node toolchain. No audit
command activates a research provider or exports research content.

Research peers have bounded statistical and physical-unit calculations through
SciPy and Pint; read [agent analysis](agent-analysis.md) for inputs, lazy loading,
source-permission checks and scientific proof limits.
Read [offline package evaluations](package-evaluations.md) before running the
synthetic search/statistical comparisons or interpreting generated peer tests.
Read [discovery HTTP](discovery-http.md) before selecting the optional HTTPX pool;
its proxy policy and measured connection benefit differ from a latency claim.

## Deliberate dependency updates

Change a supported version or direct dependency in its owning manifest, explain
the reason and compatibility impact, then explicitly regenerate `uv.lock`,
`package-lock.json` or the desktop Cargo lock as appropriate. Run the affected
checks plus a clean install. Normal setup and checks never resolve a new version.
Do not copy `.venv`, `node_modules`, local caches or native build output into Git.

An optional provider, framework or sandbox being unavailable affects its own
qualification. It does not prevent the shared offline foundation from being used.

## Optional live Atlas connection

Public PDF text reading uses Poppler (`pdftotext`). Optional selected-page OCR
also needs Poppler's `pdfinfo`/`pdftoppm` and Tesseract with English language data
on `PATH`. The pinned Python Pillow dependency prepares bounded PNG/JPEG crops
and scaled OCR images. Poppler/Tesseract are local OS packages; setup does not
install them or activate vision calls. See [document readers](document-readers.md)
for formats, bounds, run-level vision opt-in and verification limits.

The offline GitHub check installs Ubuntu's `poppler-utils`, `tesseract-ocr` and
`tesseract-ocr-eng` packages and verifies those commands and English language data
before testing synthetic documents. It also installs the pinned observability
extra so exporter tests run; this does not activate provider calls or trace export.
CI stops at the first failing Python test and prints its traceback.

See [Stage 4 local research](stage4-local.md) for explicit model activation,
private state, native/browser launch, inspection commands and qualification.
The [earlier supplied-evidence extension](live-research.md) uses its explicit legacy runtime.
The default setup and checks do not activate this provider.
