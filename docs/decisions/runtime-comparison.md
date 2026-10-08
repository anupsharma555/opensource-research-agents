# Initial runtime comparison

**Provisional choice:** use a small `asyncio` scheduler with an application-owned SQLite board for the first single-host slice. Keep a narrow task-execution adapter boundary so LangGraph or Microsoft Agent Framework can replace it if a larger qualification shows a benefit. This is an offline engineering decision, not evidence of model quality, paid-provider latency, multi-host safety, or a ten-hour mission.

The comparison ran on 2026-10-08 with Python 3.12.12 on macOS arm64. This folder was not a Git checkout during the run, so there is no tested commit. [`observations-2026-10-08.json`](../../experiments/runtime_comparison/observations-2026-10-08.json) records SHA-256 hashes of the tested Python files, all five raw observations per candidate, and the conditions. [`failure-observations-2026-10-08.json`](../../experiments/runtime_comparison/failure-observations-2026-10-08.json) records each interruption and subsequent process restart.

## What was compared

All three candidates received the same saved model/settings record and deterministic specialist responses. Two independent collectors start together; one simulated step takes 0.20 seconds and the other 0.02 seconds. The quick collector chooses a directed request to a critic and creates that work. The critic first selects an invalid tool, sees the recorded rejection on its next attempt, repairs its action, and proposes synthesis. Synthesis waits for the relevant versioned artifacts. The board admits up to two active tasks. It schedules and validates actions but never chooses the specialist's source, tool, request, or scientific follow-up. This mapping is a **test script**, not a production scientific router.

The common SQLite board holds task claims, the selected-model manifest, artifact version and hash, directed messages, and the event trace. Its explicit `BEGIN IMMEDIATE` transaction commits an accepted artifact, peer request, and proposed task together. Repeated task completion is idempotent; an older attempt cannot write after restart reclaim. Each adapter executes a two-step unit of work and returns a proposed action. Framework checkpoints are separate from accepted board state.

| Candidate | Actual code exercised | Persistence observed | Additional burden in isolated environment |
| --- | --- | --- | --- |
| Minimal custom | `asyncio` task and application SQLite board | Board survives a new process; unfinished task restarts from its beginning. No native step checkpoint. | Standard library only. Application owns future step receipts and upgrades. |
| LangGraph 1.2.14 + SQLite checkpoint 3.1.1 | `StateGraph` with `prepare → act`, `AsyncSqliteSaver`, synchronous checkpoint durability | Node-boundary checkpoints remain after cancellation and restart. The board still decides whether a result committed. | 42 installed distributions, including `pip`, in the isolated venv; per-task SQLite checkpoint files in this test. |
| Microsoft Agent Framework core 1.21.0 | `WorkflowBuilder`, fresh `Executor` instances, `FileCheckpointStorage` | Entry/superstep checkpoints remain after cancellation and restart. The board still decides whether a result committed. | 12 installed distributions, including `pip`, in the isolated venv; checkpoint files require private/trusted storage. |

The LangGraph adapter uses a separate SQLite checkpoint file for each task attempt. Two independent saver instances initializing the same fresh file concurrently produced `sqlite3.OperationalError: database is locked` in the first test. Separate files removed that fixture collision; this does **not** establish the right production checkpoint topology. The first Microsoft adapter reused global executor objects and delayed the quick task behind the slow task. Rebuilding executor instances for each workflow, as the [official example](https://github.com/microsoft/agent-framework/blob/main/python/samples/03-workflows/_start-here/step1_executors_and_edges.py) recommends, restored the intended concurrency. Those flawed timing runs were discarded.

## Observed results

Five fresh runs per candidate used no model, provider, or external tool calls. Times are seconds. `scheduler` begins after process import and board setup; `process` includes Python startup and imports. These tiny synthetic delays cannot predict paid-model throughput. The full raw values and file sizes are in the observation file.

| Candidate | Median scheduler | Median first accepted artifact | Median process wall | Median native checkpoint bytes |
| --- | ---: | ---: | ---: | ---: |
| Custom | 0.214 | 0.022 | 0.276 | 0 |
| LangGraph | 0.237 | 0.030 | 0.576 | 143,360 |
| Microsoft Agent Framework | 0.229 | 0.030 | 0.362 | 20,059 |

All candidates reached the same four artifact IDs, one directed peer request, one repaired invalid action, and one agent-proposed follow-up. The proposal was recorded before the slow collector completed, demonstrating useful parallel progress rather than a fixed serial role list. Maximum active tasks was two. Board storage was 45,056 bytes in each fresh baseline run. The elapsed differences reflect this fixture and package paths only; invariant pass and operational simplicity drive the recommendation.

| Injected boundary | Board before restart | Result after a separate-process resume | Native checkpoint implication |
| --- | --- | --- | --- |
| After first accepted artifact | One artifact; another task still claimed | Four artifacts, one request; abandoned claim reclaimed | Both frameworks retained checkpoints, but the application board determined which effect had committed. |
| After task result, before board commit | No accepted artifact; two claims | Four artifacts, one request; both claims reclaimed | A framework-complete step did not make its proposed artifact or message accepted. The task was retried. |
| During a simulated tool/model step | At least one task still claimed; other work could already be useful | Four artifacts, one request; only uncommitted work retried | Each framework retained some pre-step checkpoints. The interrupted `act` step was not treated as an accepted result. |

The full nine-case fault record gives exact before/after counts and checkpoint inventories. Tests also rejected attempted model-name and temperature changes on resume for **each** candidate, rejected a late stale worker and a conflicting artifact ID, and proved that a repeated committed message/artifact has one effect. The model manifest is saved by the application, not inferred from a graph checkpoint. Hosted model aliases may still change underlying weights; this fixture does not prove provider-side model immutability.

## Benchmark packet handoff

The supplementary `probe_packet.py` uses the benchmark module's public `make_worker_packet` and `load_event_script` APIs. It delivered the same `syn-route-recovery` worker-packet hash and initial source `B1` through each real adapter, then delivered events `B-E1`, `B-E2`, `B-E3` one at a time in their declared order. It did not load future events into the initial worker packet or read evaluator-only reference judgments. This verifies packet and event **transport identity**, not interpretation of the sources, route recovery, or scientific output. The public packet version and hash are loaded at run time so a revised packet is not silently treated as an old one.

## Why the custom path starts first

The project needs its own durable board, mailbox, selected-model manifest, artifact commit, and stale-worker protection under every candidate. Neither tested framework makes that application transaction atomic with its checkpoint. For this single-host first slice, adding a graph runtime therefore buys node-level execution history while retaining the same critical board code. The custom path passed the shared behavioral and restart tests with fewer dependencies and a smaller persistence surface. It is a provisional fit decision; native checkpoint inspection and replay can matter if later tasks have many costly internal steps.

The next qualification should use the canonical shared task/message/model envelope and benchmark worker packet in a true end-to-end run. It should inject a process kill and uncertain remote result after a tool call, verify exact provider-request/result receipts, run concurrent SQLite writers with schema upgrade/recovery, and compare an hours-long mission with realistic quota waits. A framework may be adopted if those tests show simpler recovery or clearer maintenance without changing peer autonomy. This experiment did not run such calls, a production database, a cloud host, or a scientific evaluator.

## Reproduce

Use two isolated Python 3.12 environments outside the repository. The tested package identities are [LangGraph 1.2.14](https://pypi.org/project/langgraph/), [LangGraph SQLite checkpoint 3.1.1](https://pypi.org/project/langgraph-checkpoint-sqlite/), and [Microsoft Agent Framework core 1.21.0](https://pypi.org/project/agent-framework-core/); all list MIT licenses and Python 3.12 compatibility. No provider extras or services are required for these adapters.

```sh
python3.12 -m venv /tmp/ora-langgraph-venv
python3.12 -m venv /tmp/ora-maf-venv
/tmp/ora-langgraph-venv/bin/python -m pip install --no-cache-dir -r experiments/runtime_comparison/langgraph-requirements.txt
/tmp/ora-maf-venv/bin/python -m pip install --no-cache-dir -r experiments/runtime_comparison/maf-requirements.txt
ORA_LANGGRAPH_PYTHON=/tmp/ora-langgraph-venv/bin/python ORA_MAF_PYTHON=/tmp/ora-maf-venv/bin/python python3.12 -m unittest discover -s tests/runtime_comparison -v
python3.12 experiments/runtime_comparison/measure.py --langgraph-python /tmp/ora-langgraph-venv/bin/python --maf-python /tmp/ora-maf-venv/bin/python --repeats 5 --output /tmp/ora-runtime-observations.json
python3.12 experiments/runtime_comparison/probe_failures.py --langgraph-python /tmp/ora-langgraph-venv/bin/python --maf-python /tmp/ora-maf-venv/bin/python --output /tmp/ora-runtime-failures.json
```

The full transitive versions actually installed for the two optional adapters are frozen in the two experiment requirement snapshots above; they are comparison inputs, not the project's selected dependency file. These are exact version pins, not wheel-hash locks, and were tested on Python 3.12/macOS arm64. The host's `uv` 0.9.21 failed before environment creation in this sandbox (default cache permission, then a macOS `system-configuration` panic with a temporary cache). Standard-library `venv` and `pip` completed the isolated installs. This is an observed setup limitation, not evidence that `uv` generally cannot install these packages. The repository setup work owns the shared project interpreter and dependencies; none were changed here.

Primary API evidence: [LangGraph graph API](https://docs.langchain.com/oss/python/langgraph/graph-api), [persistence and replay](https://docs.langchain.com/oss/python/langgraph/persistence), [SQLite saver source](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/aio.py), [Microsoft checkpoints and rehydration](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints?pivots=programming-language-python), [Microsoft executor example](https://github.com/microsoft/agent-framework/blob/main/python/samples/03-workflows/_start-here/step1_executors_and_edges.py).
