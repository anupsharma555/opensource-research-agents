# Documentation

Start with the [project overview](../README.md), then follow the question you need
to answer. These documents describe either implemented behavior or a clearly
labeled design decision; none should be read as evidence of a live research run.

| Question | Read |
| --- | --- |
| How do I install and test it? | [Shared setup](development/setup.md) |
| Why are the components separated this way? | [System architecture](architecture/system.md) |
| What was chosen and what could change it? | [Storage](decisions/storage.md), [runtime](decisions/runtime-comparison.md), [Mac shell](decisions/desktop-shell.md) |
| What crosses the app/host/agent boundary? | [Contract guide](interfaces/contract-guide.md), [compatibility](interfaces/compatibility.md) |
| What is measured, and what is still untested? | [Benchmark proof matrix](benchmarks/proof-matrix.md), [foundation evidence](development/foundation-evidence.md) |
| How did the selected model use the first skills? | [Model-fit comparison and limits](development/model-fit.md) |
| How are state, source evidence and agent steps preserved? | [State and evidence records](development/state-records.md), [Stage 2 verification](development/stage2-verification.md) |
| How do identities, model settings and capabilities remain consistent? | [Agent configurations](development/agent-configurations.md) |
| How can peers coordinate and execute work concurrently? | [Stage 3 implementation and proof](development/stage3-verification.md), [peer collaboration](development/peer-collaboration.md), [concurrent execution](development/concurrent-execution.md) |
| What research informs the next coordination improvements? | [Stage 3 research review and focused follow-ups](research/stage3-coordination-research-review.md) |
| How are model turns, waits and tool results inspectable? | [Selected-model adapter](development/model-adapter.md), [traces and timing](development/tracing.md), [saved Stage 3 review](development/stage3-inspection.md) |
| How can I see whether a peer used an update, and what an explanation covers? | [Shared contribution review](development/peer-contribution-review.md), [summary coverage](development/summary-coverage.md) |
| What happens when many tasks become ready together? | [Queued-work burst measurements](development/queued-work-bursts.md) |
| How are retrieved information and exact source copies linked? | [Provider receipts](development/provider-receipts.md), [evidence library](development/evidence-library.md) |
| What realistic research tasks should be tested next? | [Digital mental-health benchmark cases](benchmarks/realistic-digital-mental-health.md) |
| Where should a module, helper or skill go? | [Module map](development/modules.md), [role/skill packages](development/skills.md) |
| How can I review an example research journey? | [Mac prototype guide](../ui/README.md) |
| How do I start and inspect the optional live Atlas task? | [Local live research](development/live-research.md) |
| What external UI research informs Atlas? | [Eight-resource UI review](research/research-atlas-ui-review.md) |
| How do multiple developers integrate work? | [Private Git workflow](development/git-workflow.md) |
| How do cloud tasks use the same setup? | [Cloud development](development/cloud.md) |

Decision records state the problem, alternatives, choice, supporting evidence,
remaining uncertainty and the test that would change the choice. This is how a
future reviewer can understand a sophisticated implementation without reverse
engineering every detail or relying on hidden model reasoning.
