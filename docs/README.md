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
| Where should a module, helper or skill go? | [Module map](development/modules.md), [role/skill packages](development/skills.md) |
| How can I review an example research journey? | [Mac prototype guide](../ui/README.md) |
| How do multiple developers integrate work? | [Private Git workflow](development/git-workflow.md) |
| How do cloud tasks use the same setup? | [Cloud development](development/cloud.md) |

Decision records state the problem, alternatives, choice, supporting evidence,
remaining uncertainty and the test that would change the choice. This is how a
future reviewer can understand a sophisticated implementation without reverse
engineering every detail or relying on hidden model reasoning.
