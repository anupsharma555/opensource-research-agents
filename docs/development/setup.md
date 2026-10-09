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

On the initially observed Mac sandbox, uv 0.9.21 failed while reading macOS network
configuration. The same exact tool succeeded under the task's approved execution
boundary. Do not silently replace it or change global configuration to hide that
failure. Run the existing `.venv` checks independently while repairing setup access.

## Commands and optional work

| Command | Purpose |
| --- | --- |
| `scripts/dev doctor` | Show actual runtimes and lock hashes without reading secrets. |
| `scripts/dev generate` | Regenerate schemas, examples and TypeScript from canonical Python models. |
| `scripts/dev inspect --view sources` | Inspect the same synthetic source records visible in the Mac app. |
| `scripts/dev inspect --view diagnostics` | Inspect operational sample data separately from findings. |
| `scripts/dev desktop` | Build the unsigned debug Mac app with embedded frontend assets. |
| `scripts/dev setup --offline` | Install from populated caches; report an unavailable package precisely. |

`ORA_NODE_BIN` may select an explicit **matching** Node executable. It is not a
version fallback. Optional runtime comparisons have their own pinned dependency
snapshots under `experiments/runtime_comparison/`; the chosen first runtime is
standard-library asyncio and SQLite, so those frameworks are not core dependencies.
The selected-model skill experiment is separately invoked and never part of CI.

## Deliberate dependency updates

Change a supported version or direct dependency in its owning manifest, explain
the reason and compatibility impact, then explicitly regenerate `uv.lock`,
`package-lock.json` or the desktop Cargo lock as appropriate. Run the affected
checks plus a clean install. Normal setup and checks never resolve a new version.
Do not copy `.venv`, `node_modules`, local caches or native build output into Git.

An optional provider, framework or sandbox being unavailable affects its own
qualification. It does not prevent the shared offline foundation from being used.

## Optional live Atlas connection

See [local live research](live-research.md) for explicit model activation, private
state, native/browser launch, inspection commands and repeatable test inputs.
The default setup and checks do not activate this provider.
