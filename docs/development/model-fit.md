# Selected-model fit of two runtime skills

**The bounded comparison is complete; the answers are not scientifically
qualified.** Concise instructions are the provisional development default, with
versioned source packets and targeted peer feedback. The test supports a usable
skill-loading and review scaffold, while showing why neither longer instructions,
valid citations nor one correction turn can establish a trustworthy final answer.

On 2026-10-08, Groq `openai/gpt-oss-120b` produced nine model responses: one empty,
truncated pilot and eight completed answers. All requested and returned model IDs
matched. The [sanitized receipt](evidence/skill-fit-summary.json) records versions,
request hashes, usage, timings and review hashes. Raw answers and detailed reviews
remain in ignored local evidence, separate from the findings in this report.

## Design and source identity

The initial comparison used skill v1.0.0: concise versus richer instructions for
`evidence-linking`, then the same comparison for `study-appraisal`. Each pair saw
identical task text and sources from `syn-abstract-disagreement`, scenario v1.0.1
in benchmark bundle v1.0.4. Evidence linking saw A1–A4; appraisal saw only A1 so it
could not borrow another study's findings. Evaluator-only references were excluded
from every request. A separate GPT-6 Sol reviewer, at extra-high reasoning,
received opaque answer IDs and source packets without the model, variant mapping
or usage data. The coordinating reviewer checked the findings against the sources.
This was agent review, not a human panel or blinded clinical validation.

The active benchmark now uses real clinical labels in A v1.0.3, bundle v1.0.6.
That later revision preserves simulated counts but changes input text and hashes;
it has not been tested on a model. This report and its saved requests still refer
to the original placeholder-labelled inputs, not the current offline plan.

Requests used high reasoning, `include_reasoning=false`, no tools and no streaming.
The 4,096-token pilot returned `finish_reason=length` and an empty final answer.
The matched comparison therefore used 8,192 completion tokens for each case.
That is this experiment's allowance, not a fixed research-task limit. It includes
capacity consumed before final text is produced; a high-effort request alone does
not guarantee enough room for an answer. No private reasoning text was retained.

The original comparison used base commit `2fc86246fde795d5f1048b285c17ed24c2b2192c`
with an explicit application User-Agent and the revised output allowance. It is
not an exact clean-commit run. Revised concise instructions v1.0.1 used frozen
working-source snapshot
`819a0ae1c8a4d9b0093eeddac638935f44f1ceafbb316630eee538531e14f24e`.
Two subsequent peer revisions have exact request, prior-result and feedback
hashes; they used the working-tree helper rather than an isolated committed build.
All nine request hashes were reconstructed successfully from their saved inputs.
Later receipt and output-path checks were tested offline, without more API calls.

## Observations

| Phase | Evidence-linking result | Appraisal result |
| --- | --- | --- |
| v1.0.0 matched concise/rich comparison | Both preserved the two studies' counts. Concise added an unsupported magnitude label; rich falsely treated missing reported variance as making pooling impossible. Both overextended one source's reporting gaps. | Both proposed unsupported method versions. Rich also treated missing study details as reported and made an unsupported certainty downgrade. |
| v1.0.1 concise instructions | Exact citation IDs improved, but a false quantitative-synthesis restriction and cross-study absence claims remained. | Exact IDs and separation of appraisal dimensions improved; an unsupported method version and causal wording remained. |
| Same-model revision after source-specific peer feedback | Corrected A2's derived difference and narrowed A1's access notice. It still attached an A2 absence claim to A1's passage and overstated the restriction on synthesis. | Used the supplied official method date and formal categories, and improved applicability. It reverted to non-ASCII citation IDs, omitted source versions and retained an unsupported magnitude label. |

All four initial answers and all four follow-ups contained usable preliminary
facts, but none was accepted as a final answer by the independent review. The
concise initial variants avoided some errors introduced by the richer variants
on this packet; that is a sample-specific observation, not a general winner.
Updated instructions and peer feedback each corrected some errors while leaving
others. These follow-ups changed instructions or supplied critique, so they are
not additional replicates of the initial matched comparison.

A concrete example explains the distinction between useful data and an unjustified
block. A2 reports 20/50 versus 21/50 responses. Their difference is calculable as
minus two percentage points, even though A2 reports no interval. Pooling these
studies would require explicit scientific assumptions; missing a reported variance
alone does not make arithmetic impossible. Similarly, an omission in A1's abstract
does not establish that A2 omitted the same information. Exact passage identity
and the meaning of the claim must be checked together.

The appraisal feedback supplied authoritative method guidance: the official RoB 2
site dates the individually randomized parallel-group version to 22 August 2019,
and Cochrane gives the formal judgment categories. A1 does not establish its unit
of randomization, so selecting that particular variant remains conditional. No
formal rating was justified from this abstract. The corrected method date shows
use of supplied guidance, not unaided recall. Sources: [RoB 2 tool](https://www.riskofbias.info/welcome/rob-2-0-tool)
and [Cochrane description](https://methods.cochrane.org/bias/resources/rob-2-revised-cochrane-risk-bias-tool-randomized-trials).

## Loading and implementation decision

Load the active skill's concise v1.0.1 instructions, then relevant source records
and task-specific guidance. Do not preload all nine roles or the entire skill
catalog. Both packages retain richer variants for later controlled comparison;
helpers remain version 1.0.0 because their executable behavior did not change.
Their source hashes were updated for the separate module-description edits.

Keep supported partial findings available to collaborating peers. Record which
claim needs correction, its exact source copy and the critique that prompted a
new attempt. Revisions preserve the selected model and source context. Mechanical
checks can detect invalid IDs or missing fields, but scientific peers decide how
to interpret evidence and what to investigate next. Neither a validator nor this
experiment's one-attempt-per-case protection is a global workflow gate.

The next qualification should test held-out designs and repeated cases, jointly
checking passage support, exact IDs, reported versus derived quantities, method
selection, cross-study attribution and reader clarity. Include a case where added
feedback fixes one defect but introduces another. Broader model/tool loops, live
retrieval, all nine roles and long-running missions remain separate implementation
and testing work. No claim of superiority to another system follows from this test.

## Usage, latency and cost

| Response | Input / completion tokens | Seconds | Finish |
| --- | ---: | ---: | --- |
| 4,096-token evidence pilot | 1,556 / 4,096 | 9.244 | Length; empty final |
| v1.0.0 evidence concise | 1,556 / 5,562 | 12.266 | Complete |
| v1.0.0 evidence rich | 1,689 / 2,784 | 7.719 | Complete |
| v1.0.0 appraisal concise | 797 / 4,078 | 8.887 | Complete |
| v1.0.0 appraisal rich | 1,002 / 4,356 | 10.624 | Complete |
| v1.0.1 evidence concise | 1,658 / 4,818 | 10.645 | Complete |
| v1.0.1 appraisal concise | 943 / 4,625 | 10.288 | Complete |
| Evidence peer revision | 3,040 / 4,010 | 8.901 | Complete |
| Appraisal peer revision | 2,691 / 2,989 | 6.891 | Complete |

These are single-call elapsed observations, not latency distributions or a speed
claim. One earlier POST was rejected at the edge before a model response. Two
unauthenticated diagnostic requests isolated the default-client rejection; an
honest application User-Agent then reached normal API authentication and allowed
the authorized calls. No model, account or endpoint was substituted.

At the [documented rates](https://console.groq.com/docs/model/openai/gpt-oss-120b)
of $0.15 per million input tokens and $0.60 per million output tokens, the nine
responses equal **$0.02463060**. This is an undiscounted paid-rate estimate, not an
account invoice. Groq offers Free and paid Developer plans; an open-source project
or open-weight model does not itself make hosted inference free. Actual billing
was not inspected. The observed request/token limits were consistent with the
published Free profile, but headers do not establish billing status. Sources:
[Groq billing](https://console.groq.com/docs/billing-faqs) and
[rate limits](https://console.groq.com/docs/rate-limits).

## Reproduce the mechanics

Run the offline preflight without credentials:

```sh
.venv/bin/python experiments/skill_fit/run.py
.venv/bin/python -m pytest -q tests/skill_fit
```

The current `experiments/skill_fit/plan.json` is generated from v1.0.1 instructions;
it is not the original v1.0.0 comparison ledger. The receipt preserves historical
hashes and versions. An explicitly authorized `--live --case X1` call saves below
ignored `.local/skill-fit`. `--prior-result` plus `--feedback-file` prepares a
revision; without `--live`, it prints only a request hash and cost estimate.
Use a separate ignored output folder for each experiment phase. The two committed
feedback files contain synthetic source corrections and public method links.

The helper checks case, requested/returned model, original request hash and a
completed nonempty prior answer. New receipts include a content hash; historical
receipts lacking it remain labeled as such. These hashes detect inconsistencies,
not malicious rewriting of an entire receipt. Raw output destinations inside the
repository must be below `.local`; explicit external paths are also allowed.
Neither tests nor the offline plan reads a credential or calls a provider.

Seven focused tests cover paired inputs, withheld-reference exclusion, A1-only
appraisal, preserved settings/context, changed parent records, one-attempt
behavior and rejection of an unsafe output destination before credential access.
Stubbed transport verifies that keys and returned private reasoning are not saved.
These checks establish experiment mechanics, separately from the negative and
partial scientific results above.
