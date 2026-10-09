# Research-informed improvements to Stage 3 coordination

Reviewed 8 October 2026. This is a research and implementation audit, not a model
benchmark or an endurance result. Three GPT-6 Sol reviewers at extra-high reasoning
inspected 42 distinct primary or official source documents; the parent reviewer
reconciled their recommendations against the live checkout and the full Linear
backlog. Source IDs below identify the inspected document, not an independent
confirmation of a recommendation.

## Decision

Keep the current peer-led architecture and its Python/SQLite foundation. Add two
focused Stage 3 follow-ups: make shared contributions and their effects easier to
review, and measure how bursts of queued work affect healthy research. The first
joins critique discovery, optional records of how an update was used, and
explanations that say which events they cover. The second measures pressure on
local execution before selecting any additional queue or scheduling mechanism.

The literature supports shared artifacts, selective communication, external
evidence for correction, and task-dependent concurrency. It does not establish
one universally best communication graph or show that more agents, longer debate,
or more elaborate explanations consistently improve results. Architecture and
budget affect outcomes differently across tasks [C02](https://arxiv.org/html/2406.11776v1), [C03](https://arxiv.org/html/2503.01935v1), [C12](https://arxiv.org/html/2305.14325v1), [C13](https://arxiv.org/html/2511.07784v1), [C14](https://arxiv.org/html/2609.35875v1), [R03](https://arxiv.org/html/2512.08296v3), [T10](https://arxiv.org/html/2303.17651v2), [T11](https://arxiv.org/html/2305.11738v4), [T12](https://arxiv.org/html/2310.01798v2), [T13](https://arxiv.org/html/2210.08726v3), [T14](https://arxiv.org/html/2305.14251v2).
These are reasons to expose useful choices and measure their effects, not to add a
scientific planner, mandatory consensus, or a fixed research-round limit.

The six original Stage 3 issues are now recorded as Done for their defined
component scope. That changed during this audit. Refreshing the implementation
prevented several already-completed mechanisms from becoming duplicate tasks.
The two additions extend that baseline; they do not withdraw its existing proof.

## Tracked follow-ups


Tracked follow-ups: **ANU-437 — Make peer contributions and their effect on research reviewable** and **ANU-438 — Measure queued-work bursts and keep independent research moving**. Both are now implemented and verified within their Stage 3 component scope. The six completed baseline issues retain their status. See [completion evidence](../development/stage3-extensions-verification.md) for the shared checks, native review and final burst measurements; live mission integration remains Stage 4.

## What is already covered

| Capability | Current owner and inspected implementation | Disposition |
| --- | --- | --- |
| Independent callbacks overlap; a ready dependent starts before a slow unrelated sibling ends | ANU-398/436; `coordination/executor.py`, `test_independent_overlap_and_fast_dependent_before_slow_finish` | Already implemented; retain as a regression case |
| Artifact, required message/outbox intent, receipt, trace, board outcome and capacity settlement commit together | ANU-395/400/436; `ConcurrentExecutor`, `ProposalBoard.accept_result`, `CapacityScheduler.settle_tx` | Already implemented for local confirmed effects; uncertain external work remains distinct |
| Peers propose, negotiate, claim, disagree, revise and reopen work | ANU-399; `coordination/board.py` | Already implemented; no scientific leader needed |
| Delivery and acknowledgment differ from a reviewed answer; subscriptions resume from saved positions | ANU-400; `coordination/mailbox.py` | Already implemented; ordinary-update uptake and proposal-context discovery remain additions |
| One operation parent plus several exact input/output references; paged traces expose missing records | ANU-401; `coordination/traces.py` | Existing traceability is substantial; summary coverage and clearer link meanings remain additions |
| Selected model/settings, repeated tool turns, cutoff/failure states, restricted returned reasoning | ANU-402; `models/groq.py`, `models/session.py` | Early missing-reasoning finding was repaired during the audit; do not reopen it as unimplemented |
| Phase timing, quota waits, fairness, short transactions and local write measurements | ANU-398/436 and development measurement guides | Existing evidence covers the baseline; burst admission and memory pressure need a distinct workload |

The parent implementation thread owns test receipts and subsequent code fixes.
This audit inspected code, test cases and its verification guide; it did not rerun
the implementation suite or its live Groq qualification. See
[Stage 3 verification](../development/stage3-verification.md) for those proof
boundaries. The optional live Atlas host is a separate path; completion of these
components does not prove that it has been migrated to the new executor.

## Ranked additions

Ranking favors fit to the current project, then reliability and scientific value,
strength of evidence, and adoption effort. It is a judgment, not a numerical score.

### 1. Make shared contributions discoverable and their use reviewable

**Problem.** A peer can acknowledge an ordinary evidence update without using it.
A later peer may miss a relevant critique because it was exchanged directly with
someone else. Neither successful delivery nor agreement proves useful cooperation.
MAST documents ignored input and missing clarification among observed agent
failures [C01](https://arxiv.org/html/2503.13657v3). AutoScientists uses shared proposals and experiment records, and
MetaGPT/AgentScope illustrate common artifact and message surfaces [C04](https://arxiv.org/html/2605.28655v1), [C07](https://arxiv.org/html/2402.14034v2), [C09](https://arxiv.org/html/2308.00352v7).

**Adaptation.** Extend the existing board/mailbox with one proposal-context read
operation. It should assemble permitted critiques, messages, alternative findings
and exact source versions, including material posted before the reader joined.
Do not copy every source into each message or wake every model. Let a recipient
optionally record that an update was considered, used, challenged, deferred or
inaccessible, with links to a revision, follow-up, or explanation. These are peer
statements; the host must not infer scientific correctness from them. No recorded
disposition means unknown, not ignored, rejected, or complete.

**Reference boundary.** Inspected AutoScientists client code creates a workshop,
subscriptions, shared workspace and kickoff post. The external coordination server
was not inspected. Its fixed voting/team/comment rules should not be adopted.
The transferable detail is discoverability around shared work, not those controls.

**Smallest proof.** A late peer finds a prior contradiction and its exact source
version without a new broadcast. Another acknowledges an update but leaves its
use unknown; another challenges it and links a revision. A clarification keeps the
original request open. Duplicate delivery and restart preserve those distinctions,
and independent work continues. A later model evaluation judges whether the
correction was actually justified; this component only makes the evidence visible.

**Ownership.** New Stage 3 contribution-review follow-up, reusing ANU-399/400/401.
ANU-430/419 retain scientific usefulness and claim-review evaluation.

### 2. Say exactly what each run explanation covers

**Problem.** A derived summary can remain persuasive after a late reply or source
correction arrives. Existing summaries name their sources but do not record an
explicit event-coverage boundary. Existing multiple input references also need
clear meanings when a decision consumes a source, responds to a peer, or retries
an attempt. An operation's single parent cannot express every association.

**Adaptation.** Reuse the current exact references and saved event positions. Store
the summary's scope, event position or explicit event set, contributing records,
generation state, and known omitted/delayed detail. A label such as “covers events
through 120; a later reply is available” is useful. “Complete” without a scope is
not. Keep one execution parent and add relationship labels where needed, such as
input consumed, reply to, revision of, or retry of. Do not infer these links from
fluent prose or treat a link as evidence that a claim is true.

OpenTelemetry separates nesting from additional asynchronous associations and
documents optional exporter loss [T05](https://opentelemetry.io/docs/concepts/signals/traces/), [T06](https://opentelemetry.io/docs/collector/resiliency/). Inspect AI makes logging policy
explicit [T09](https://inspect.aisi.org.uk/eval-logs.html). These are useful engineering patterns, not a reason to introduce
another telemetry service or a model call on every event.

**Smallest proof.** Save a summary, append a peer reply and corrected source while
a viewer is disconnected, then reconnect. Preserve the old summary's scope and
produce or label the newer view accurately. Distinguish “newer events exist” from
“a known record is unavailable.” A revision consuming two sources and a peer reply
must show all three exact inputs through the same backend projection used by Atlas
and Codex, including after duplicate/out-of-order events.

**Ownership.** Include this in the same contribution-review follow-up: it completes
the reader's path from shared input to changed work to an honest explanation.
ANU-406/409 retain full API/desktop rollout; ANU-405 retains context reconstruction.

### 3. Measure queued-work bursts before adding more scheduling machinery

**Problem.** Correct single-call admission and durable records do not by themselves
show how a burst of eligible work affects memory, cancellation, writer contention,
or another healthy route. More parallel work can add coordination overhead [R03](https://arxiv.org/html/2512.08296v3).
An in-memory queue has its own capacity and durability properties [R14](https://docs.python.org/3.12/library/asyncio-queue.html), while
SQLite permits concurrent readers with one writer and requires attention to
checkpoint behavior [R12](https://sqlite.org/wal.html), [R13](https://sqlite.org/isolation.html).

**Adaptation.** Extend the existing measurement helpers with a declared burst
workload and slow-provider/slow-writer cases. Measure ready-work backlog, active
working set, process memory, admission delay, first useful artifact, cancellation
response, commit visibility and database growth. Retain durable work even when
notifications repeat or transient buffers fill. Change the implementation only
where evidence identifies loss, unbounded accumulation or unfair delay. Prefer
paged durable scans or existing permits before introducing a writer service.

**Smallest proof.** Queue more ready work than the active allowance while one
route is delayed. Show progress on another route, no duplicate accepted work,
observable cancellation, recovery from interrupted dispatch and retained pending
work. If a local buffer exists, exercise saturation. Compare the same input and
selected configuration under serial and eligible-parallel execution. A buffer
limit controls memory use; it must not cap total research tasks, scientific turns
or mission duration. Report a suspected bottleneck as suspected until measured.

**Ownership.** Separate Stage 3 burst-measurement follow-up, reusing ANU-398/436.
ANU-432 integrates the full loop; ANU-422 remains the hours-long observation owner.

### 4. Preserve the corrected reasoning boundary; evaluate benefit later

The early audit found that a whole-response cutoff could be labeled as truncated
reasoning even when no reasoning text arrived, and a transport failure could be
described as absent reasoning. The refreshed adapter now records unknown in those
cases, alongside response status and finish reason. Tests cover interrupted output
without reasoning and cutoff without reasoning. Do not create a duplicate repair.

Keep presence, response completion and access/retention distinguishable in later
views. Provider-returned reasoning is observed output, not a complete account of
causes. Primary studies show both diagnostic value and omitted influences
[T01](https://arxiv.org/html/2305.04388v2), [T02](https://arxiv.org/html/2307.13702v1), [T03](https://www.anthropic.com/research/reasoning-models-dont-say-think), [T04](https://openai.com/index/chain-of-thought-monitoring/). The reader-facing explanation should use the problem, evidence,
alternatives, decision and observable outcome. No private reasoning is required
to establish explainability, and no new raw-chain deliverable is proposed.

The useful later comparison is whether source-grounded critique improves claims,
including a correct minority confronting an incorrect majority, compared with
independent work under comparable model, source, context and inference budgets.
Count false corrections, unresolved dissent, quality, elapsed time and usage
separately. Do not score consensus or conversation length as scientific success
[C13](https://arxiv.org/html/2511.07784v1), [C14](https://arxiv.org/html/2609.35875v1), [T11](https://arxiv.org/html/2305.11738v4), [T12](https://arxiv.org/html/2310.01798v2), [T13](https://arxiv.org/html/2210.08726v3), [T14](https://arxiv.org/html/2305.14251v2). This belongs to existing ANU-430/419/423, not a new Stage 3 gate.

## Framework and protocol decisions

| Candidate | Useful pattern | Decision for this stage |
| --- | --- | --- |
| Current Python/SQLite components | Shared state, exact versions, short transactions, direct peer operations | Extend the existing boundaries; they now have component and integration evidence |
| LangGraph | Dynamic work creation, checkpoints and saved partial work [R04](https://docs.langchain.com/oss/python/langgraph/graph-api), [R05](https://docs.langchain.com/oss/python/langgraph/persistence) | Reference; no demonstrated need for migration. Do not equate a checkpoint boundary with scientific synchronization |
| Microsoft Agent Framework | Concurrent branches and checkpointed workflow steps [R08](https://learn.microsoft.com/en-us/agent-framework/workflows/orchestrations/concurrent), [R09](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints?pivots=programming-language-python) | Reference; a join/barrier can delay useful partial results. Compare only on a representative need |
| Temporal | Durable workflow replay and explicitly retried activities [R06](https://docs.temporal.io/workflows), [R07](https://docs.temporal.io/activities) | Later operational option; does not make external effects exactly once or choose research direction |
| Ray / LlamaIndex Workflows | Actor concurrency and early event delivery [R10](https://docs.ray.io/en/latest/ray-core/actors/async_api.html), [R11](https://developers.llamaindex.ai/python/llamaagents/workflows/concurrent_execution/) | Reference; local concurrency is not shared quota control or durable evidence acceptance |
| AutoScientists / AutoGen / AgentScope / MetaGPT / CAMEL / ChatDev | Shared work, peer exchanges and clarification [C04](https://arxiv.org/html/2605.28655v1), [C06](https://arxiv.org/html/2308.08155v2), [C07](https://arxiv.org/html/2402.14034v2), [C08](https://arxiv.org/html/2303.17760v2), [C09](https://arxiv.org/html/2308.00352v7), [C10](https://arxiv.org/html/2307.07924v5) | Adapt the small useful patterns. Do not import fixed rosters, manager authority, mandatory debate or a whole framework |
| A2A | External-peer task/message/artifact semantics [C05](https://a2a-protocol.org/v1.0.1/specification/) | Optional adapter when external peers exist. Internal durable records remain authoritative; sending a message is not guaranteed idempotent |
| MCP | Tool interoperability rather than peer scientific coordination | Already a conditional ANU-402 extension. This audit did not add a new MCP source or execution claim |
| OpenTelemetry / OpenInference / Inspect AI | Causal associations, tool-call IDs, scoped feedback and explicit log completeness [T05](https://opentelemetry.io/docs/concepts/signals/traces/), [T06](https://opentelemetry.io/docs/collector/resiliency/), [T07](https://github.com/Arize-ai/openinference/blob/0101ec1661dbdbb80d85b961826e3eae1d45a900/spec/tool_calling.md), [T08](https://github.com/Arize-ai/openinference/blob/145e3c6d0626f37396b32d8e33347d386aa4097c/spec/annotations.md), [T09](https://inspect.aisi.org.uk/eval-logs.html) | Borrow record semantics; adding another logging stack is not required |

LLMCompiler is a particularly useful code example of dispatch after dependencies
complete [R01](https://arxiv.org/html/2312.04511v3). Its inspected implementation uses in-memory scheduling/polling;
retain the project's durable board/commit boundary instead. ReWOO's separation of
planning from tool observations is useful for some independent work, but cannot
predetermine research whose next question depends on a retrieved result [R02](https://arxiv.org/html/2305.18323v1).

## Implementation sequence and boundaries

1. Define the contribution-context projection and optional uptake record using
   existing identities, permissions and exact versions. Add the late-peer and
   clarification fixtures. Keep the source text behind existing references.
2. Add summary coverage and relationship meanings to that same review path. Prove
   old/new view behavior on a disposable database. Keep operational records out of
   the research report's prose. A concise readable explanation is the output.
3. In parallel, run the burst comparison against the completed executor and
   record any smallest justified correction. State unchanged behavior if no
   correction is needed. Publish no latency claim without comparable evidence.
4. Hand the resulting records and packets to the existing full-loop, viewer and
   selected-model tasks. Development against interfaces can proceed in parallel;
   these issue labels do not impose a scientific execution sequence.

| Question intentionally left to an existing later issue | Owner |
| --- | --- |
| Does a continuously running mission use these records to choose useful follow-ups? | ANU-432 |
| Can context and selected settings survive interruption over hours? | ANU-405/422 |
| Do broad live searches and complementary providers improve coverage? | ANU-412/413/414/415/417 |
| Does peer critique improve scientific fidelity or preserve a correct minority? | ANU-419/430 |
| Can the full desktop/API show these exact records interactively? | ANU-406/409; the new work supplies the common projection |
| Does collaboration beat a simpler or frontier-assisted baseline? | ANU-423 |
| Are remote hosts, external peer protocols or E2B reliable for this workload? | ANU-411/424/431 |
| Does cross-run memory improve later research? | ANU-433 |

## Method and evidence limits

The 42 sources comprise 23 research papers/preprints, two original laboratory
research reports, and 17 official engineering documents/specifications. Related
pages from one framework answer different implementation questions but are not
independent empirical replications. Abstract and full-text versions of the same
paper count once. Author-code inspections enrich their source entries rather
than inflate the count. The three reviewers exchanged overlaps and the parent
verified leading mechanisms, counterexamples and changed local implementation.

Selection was purposive: peer interaction and failure analysis; dynamic concurrent
execution and durable state; reasoning faithfulness and trace semantics. This is
not an exhaustive systematic review. Older short-task studies and newer preprints
are both included, with conditions recorded below. No source establishes a
universal optimum for psychiatric research, the selected model, or a ten-hour run.
Reported gains were not reproduced. No downloaded code was executed, framework
installed, provider activated, host restarted or historical database migrated by
this research branch.

Code references under `src/ora` are relative to the current workspace; it contained
uncommitted concurrent changes. The private audit retains local implementation
file hashes and dated backlog snapshots; it does not retain checksums of every
external publication. Research and new issue scope are separate from the parent
implementation thread's saved checks. Published-page dates below refer to the
inspected revision where available, not always the first release.

## Source and code-reference appendix

The following table records the decision-useful finding and its main limitation.
IDs are grouped by review lane for lookup, not ranked by authority. Live technical
documentation was inspected on 8 October 2026 and can change.


### Peer communication and collaboration

| Source | Inspected version/date | What was inspected and what it contributes | Main limit |
| --- | --- | --- | --- |
| C01 — [Why Do Multi-Agent LLM Systems Fail? (MAST)](https://arxiv.org/html/2503.13657v3) | 2025-10-26, arXiv v3 | Taxonomy and failure examples; ignored input and unasked clarification motivate visible uptake. | Mixed tasks and largely model-assisted annotation; taxonomy does not predict this project's failure rates. |
| C02 — [Improving Multi-Agent Debate with Sparse Communication Topology](https://arxiv.org/html/2406.11776v1) | 2024-06-17, arXiv v1 | Controlled sparse/full debate comparison; targeted exchange can reduce unnecessary discussion. | Six-agent, fixed-round math/label tasks; no universally optimal graph. |
| C03 — [MultiAgentBench: Evaluating the Collaboration and Competition of LLM agents](https://arxiv.org/html/2503.01935v1) | 2025-03-03, arXiv v1 | MARBLE task, coordination and team-size comparisons; communication scores can diverge from results. | Short simulated research with model judges; no fixed team-size recommendation. |
| C04 — [AutoScientists: Self-Organizing Agent Teams for Long-Running Scientific Experimentation](https://arxiv.org/html/2605.28655v1) | 2026-05-27, arXiv v1 | Shared forum, experiment log, ablations and pinned launch client. | Computational ML tasks; server durability uninspected; fixed votes/rosters not adopted. |
| C05 — [A2A Protocol Specification v1.0.1](https://a2a-protocol.org/v1.0.1/specification/) | Version 1.0.1; inspected 2026-10-08 | Message/task/artifact semantics, idempotency and history/reconnect limits. | Specification only; no external-peer adapter execution here. |
| C06 — [AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation](https://arxiv.org/html/2308.08155v2) | 2023-10-03, arXiv v2 | Conversable interfaces and configurable replies. | Framework examples do not establish durable scientific provenance. |
| C07 — [AgentScope: A Flexible yet Robust Multi-Agent Platform](https://arxiv.org/html/2402.14034v2) | 2024-05-20, arXiv v2 | Message hub and dynamic participation. | Broadcast examples do not prove useful uptake or required model wakeups. |
| C08 — [CAMEL: Communicative Agents for Mind Exploration of Large Language Model Society](https://arxiv.org/html/2303.17760v2) | 2023-11-02, arXiv v2 | Role-play exchanges and requests between agents. | Exploratory early-model evidence, mostly two-agent interactions. |
| C09 — [MetaGPT: Meta Programming for a Multi-Agent Collaborative Framework](https://arxiv.org/html/2308.00352v7) | 2024-11-01, arXiv v7 | Structured shared artifacts and subscription pool. | Fixed software SOP and manager authority are not adopted. |
| C10 — [ChatDev: Communicative Agents for Software Development](https://arxiv.org/html/2307.07924v5) | 2024-06-05, arXiv v5 | Proactive clarification and retained phase summaries. | Software workflow; waterfall and hierarchical control are not adopted. |
| C11 — [ProtocolBench: Which LLM MultiAgent Protocol to Choose?](https://arxiv.org/html/2510.17149v3) | 2026-06-02, arXiv v3 | Protocol workload, recovery and latency comparisons. | Some validity comparisons are described as planned; simulated/planner workloads limit transfer. |
| C12 — [Improving Factuality and Reasoning in Language Models through Multiagent Debate](https://arxiv.org/html/2305.14325v1) | 2023-05-23, arXiv v1 | Independent answers followed by debate on bounded tasks. | Historical models, extra inference and fixed consensus rounds. |
| C13 — [Can LLM Agents Really Debate? A Controlled Study of Multi-Agent Debate in Logical Reasoning](https://arxiv.org/html/2511.07784v1) | 2025-11-11, arXiv v1 | Controlled correct-minority/incorrect-majority debate cases. | Logical puzzles and supervisor tie-breaks; useful regression design, not general scientific evidence. |
| C14 — [Beyond Symmetric Agents: Cognitive Diversity and Multi-Agent Debate in Small Language Models](https://arxiv.org/html/2609.35875v1) | 2026-09-26, arXiv v1 | Budget-matched debate versus independent sampling; context-overflow analysis. | Recent small-model preprint; serving differences confound some results. |


### Serial and parallel execution

| Source | Inspected version/date | What was inspected and what it contributes | Main limit |
| --- | --- | --- | --- |
| R01 — [An LLM Compiler for Parallel Function Calling](https://arxiv.org/html/2312.04511v3) | 2024-06-05, arXiv v3; ICML 2024 | Dependency-aware function dispatch; pinned task-fetcher code. | Selected tool benchmarks; in-memory polling is not a durable mission host. |
| R02 — [ReWOO: Decoupling Reasoning from Observations for Efficient Augmented Language Models](https://arxiv.org/html/2305.18323v1) | 2023-05-23, arXiv v1 | Separate foreseeable work from observations to reduce repeated prompts. | Observation-dependent follow-ups cannot always be planned ahead. |
| R03 — [Towards a Science of Scaling Agent Systems](https://arxiv.org/html/2512.08296v3) | 2026-04-08, arXiv v3 | Compare five architectures across task types under matched compute. | Use inspected v3; v2 has different sample counts and metrics. No universal architecture or threshold. |
| R04 — [LangGraph Graph API overview](https://docs.langchain.com/oss/python/langgraph/graph-api) | Official documentation; inspected 2026-10-08 | Graph execution, dynamic work and synchronization boundaries. | Official API semantics, not comparative research-quality evidence. |
| R05 — [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) | Official documentation; inspected 2026-10-08 | Checkpoint and pending-work persistence patterns. | Checkpoint storage does not make external execution exactly once. |
| R06 — [Temporal Workflow](https://docs.temporal.io/workflows) | Official documentation, inspected 2026-10-08 | Durable workflow history and replay. | Framework operational contract; adoption and migration were not evaluated here. |
| R07 — [What is a Temporal Activity?](https://docs.temporal.io/activities) | Official documentation, inspected 2026-10-08 | Activity retries and external-effect considerations. | External side effects still need idempotency or reconciliation. |
| R08 — [Microsoft Agent Framework concurrent workflow orchestration](https://learn.microsoft.com/en-us/agent-framework/workflows/orchestrations/concurrent) | Official documentation, inspected 2026-10-08; page example updated 2026-08-25 | Parallel branch orchestration and aggregation. | An aggregation barrier may delay useful partial results. |
| R09 — [Microsoft Agent Framework Workflows Checkpoints](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints?pivots=programming-language-python) | Official documentation, inspected 2026-10-08; Python entry-checkpoint behavior noted from version 1.13.0 | Workflow checkpoint and restart semantics. | Version-sensitive behavior; not a shared scientific evidence store. |
| R10 — [Ray AsyncIO / Concurrency for Actors](https://docs.ray.io/en/latest/ray-core/actors/async_api.html) | Official Ray 2.59.0 documentation as displayed on 2026-10-08 | Async/threaded actor concurrency and event-loop constraints. | An async actor can still block; actor limits do not establish shared account admission. |
| R11 — [LlamaIndex Workflows: concurrent execution](https://developers.llamaindex.ai/python/llamaagents/workflows/concurrent_execution/) | Official documentation, inspected 2026-10-08 | Early events, workers and local concurrency controls. | Sample semaphores are process-local, not global quota or durability guarantees. |
| R12 — [SQLite Write-Ahead Logging](https://sqlite.org/wal.html) | Official SQLite documentation, inspected 2026-10-08 | One-writer WAL behavior, busy conditions and checkpoint growth. | Deployment/workload dependent; source inspection is not measured contention. |
| R13 — [Isolation In SQLite](https://sqlite.org/isolation.html) | Official SQLite documentation, inspected 2026-10-08 | Committed-reader isolation and write transactions. | Atomic local writes cannot cover uncertain external effects. |
| R14 — [Python 3.12 asyncio Queues](https://docs.python.org/3.12/library/asyncio-queue.html) | Python 3.12.15 documentation, updated 2026-10-01 | Queue bounds, waiting and event-loop/thread semantics. | An in-memory queue is not durable; bounds should not cap scientific duration. |


### Reasoning, feedback and traceability

| Source | Inspected version/date | What was inspected and what it contributes | Main limit |
| --- | --- | --- | --- |
| T01 — [Language Models Don't Always Say What They Think: Unfaithful Explanations in Chain-of-Thought Prompting](https://arxiv.org/html/2305.04388v2) | first submitted 2023-05-07; inspected arXiv v2 revised 2023-12-09 | Bias interventions and unfaithful generated explanations. | Older models and selected quiz tasks; no selected-model faithfulness rate. |
| T02 — [Measuring Faithfulness in Chain-of-Thought Reasoning](https://arxiv.org/html/2307.13702v1) | first submitted and inspected arXiv v1 2023-07-17 | Early-answer, mistake and paraphrase interventions. | No independent ground truth for internal reasoning; no universal score. |
| T03 — [Reasoning models don't always say what they think](https://www.anthropic.com/research/reasoning-models-dont-say-think) | 2025-04-03 | Hint and reward-hack studies of omitted influences. | Contrived settings and limited models; returned reasoning is incomplete evidence. |
| T04 — [Detecting misbehavior in frontier reasoning models](https://openai.com/index/chain-of-thought-monitoring/) | 2025-03-10 | Reasoning-plus-action monitoring and obfuscation experiments. | Specific coding/training setting; not a reason to score trace fluency. |
| T05 — [OpenTelemetry Traces: spans, events, context, and links](https://opentelemetry.io/docs/concepts/signals/traces/) | live official documentation, inspected 2026-10-08 | Parent spans, asynchronous links and event identity. | Declared associations do not prove scientific support or application durability. |
| T06 — [OpenTelemetry Collector Resiliency](https://opentelemetry.io/docs/collector/resiliency/) | official page last modified 2026-01-14; inspected 2026-10-08 | Queue overflow, retry expiry, crash loss and persistent export buffers. | Exporter guidance; no new collector or broker is required. |
| T07 — [OpenInference Tool and Function Calling](https://github.com/Arize-ai/openinference/blob/0101ec1661dbdbb80d85b961826e3eae1d45a900/spec/tool_calling.md) | file commit 2026-09-01; pinned commit | Tools exposed versus selected; exact tool-result IDs and ordering. | Pinned specification, not proof of a provider's implementation or scientific correctness. |
| T08 — [OpenInference Annotations and Evaluations](https://github.com/Arize-ai/openinference/blob/145e3c6d0626f37396b32d8e33347d386aa4097c/spec/annotations.md) | file commit 2026-07-31; pinned commit | Feedback target identity, annotator and post-hoc links. | Pinned annotation semantics; claim validity remains a separate evaluation. |
| T09 — [Inspect AI Log Files](https://inspect.aisi.org.uk/eval-logs.html) | live official documentation, inspected 2026-10-08 | Summary/sample logs, payload policy and logging omissions. | Evaluation logging patterns, not a production database recommendation. |
| T10 — [Self-Refine: Iterative Refinement with Self-Feedback](https://arxiv.org/html/2303.17651v2) | first submitted 2023-03-30; inspected arXiv v2 revised 2023-05-25 | Draft-feedback-revision experiments and feedback-quality analysis. | Older models and mixed generation tasks; extra turns are not automatic improvement. |
| T11 — [CRITIC: Large Language Models Can Self-Correct with Tool-Interactive Critiquing](https://arxiv.org/html/2305.11738v4) | first submitted 2023-05-19; inspected arXiv v4 revised 2024-02-21 | Tool-grounded critique and tool/no-tool ablations. | Search/code conditions differ from scientific appraisal; extra tools add latency. |
| T12 — [Large Language Models Cannot Self-Correct Reasoning Yet](https://arxiv.org/html/2310.01798v2) | first submitted 2023-10-03; inspected arXiv v2 revised 2024-03-14 | Intrinsic self-correction versus externally grounded feedback. | Task/model-specific failures; does not prohibit useful revision. |
| T13 — [RARR: Researching and Revising What Language Models Say, Using Language Models](https://arxiv.org/html/2210.08726v3) | first submitted 2022-10-17; inspected arXiv v3 revised 2023-05-31 | Attribution and preservation assessed separately after revision. | Selected open-domain material; incomplete long-document coverage. |
| T14 — [FActScore: Fine-grained Atomic Evaluation of Factual Precision in Long Form Text Generation](https://arxiv.org/html/2305.14251v2) | first submitted 2023-05-23; inspected arXiv v2 revised 2023-10-11 | Atomic claim support rather than a blanket answer verdict. | Biography/Wikipedia validation; factual precision omits coverage and scientific design. |

### Pinned implementation references and reuse boundary

- **AutoScientists:** commit `c71a92343b9a488ed10134be805845b9473ad18f`, [launch client](https://github.com/mims-harvard/AutoScientists/blob/c71a92343b9a488ed10134be805845b9473ad18f/launch.py) and [API reference](https://github.com/mims-harvard/AutoScientists/blob/c71a92343b9a488ed10134be805845b9473ad18f/system/reference/API-REFERENCE.md). Shared-post/workspace wiring was inspected; the external server, recovery behavior and license were not verified in this audit. No code is copied.
- **LLMCompiler:** commit `a00c9d35507507da70e8c637eee64efc8c1857ae`, [task fetcher](https://github.com/SqueezeAILab/LLMCompiler/blob/a00c9d35507507da70e8c637eee64efc8c1857ae/src/llm_compiler/task_fetching_unit.py). Dependency checks and concurrent dispatch were inspected. The pinned [license](https://github.com/SqueezeAILab/LLMCompiler/blob/a00c9d35507507da70e8c637eee64efc8c1857ae/LICENSE) is MIT; this review recommends a pattern, not vendoring.
- **OpenInference:** tool-calling spec commit `0101ec1661dbdbb80d85b961826e3eae1d45a900` and annotation spec commit `145e3c6d0626f37396b32d8e33347d386aa4097c` are linked in T07–T08. These are inspected specifications, not executed SDK code. License/reuse terms were not audited.

Other frameworks were compared through papers or official documentation, not through complete source audits. Their licenses and full transitive dependencies would need separate verification only if adoption becomes justified. Paper availability, a repository being public, and production reliability are separate facts.
