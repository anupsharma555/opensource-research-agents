# Realistic digital mental-health benchmarks

This suite extends the small simulated skill test with real research tasks around
the REEACT and REEACT-2 trials. Version 0.2.1 provides ten case designs, a source inventory
and an initial private two-abstract input packet. **No model has run these cases.**
The separate simulated v1 catalog now uses real clinical labels with simulated
data; its historical model results still refer to the original input versions.

The immediate objective is useful, inspectable research: answer a focused question,
find complementary evidence, distinguish related reports, appraise methods,
reconcile conflicting source details and improve a report after peer feedback.
The same cases can later test saved-run inspection, study planning and recovery.
They do not impose a fixed number of searches, calls, peers or revision rounds.

## What is ready

- [Case designs](../../benchmarks/realistic_digital_mental_health/cases.json) give
  each task its input opportunity, outputs, review checks, role mapping and scope.
  The first case pins its two abstract views; the progressive-appraisal case
  distinguishes the initial abstract from later methods and correction access.
- [Source inventory](../../benchmarks/realistic_digital_mental_health/sources.json)
  records publication/study identities, observed access depth, acquisition route,
  retrieval date, raw-copy hashes and reuse status.
- Private source copies, retrieval receipts, two rendered PDF pages and the
  `DMH-01.worker.v0.2.1.json` input are retained below ignored
  `.local/benchmarks/digital-mental-health-2026-10-08/`. That input contains only
  the task, two published abstracts and complete abstract locators; no answer key.
  The original 0.1.0 packet is retained separately; its task and source views are
  unchanged by this aim-coverage update.

These are candidate evaluation data, not new production interfaces. The current
`ora.benchmarks` loader deliberately accepts simulated v1 packets only. It does
not yet load this real-source suite, and the existing four-case Groq helper is not
a retrieval or multi-agent runner. Do not rename these records as simulated to
bypass that distinction. A later harness should use the existing execution
boundaries and add an explicit real-source input contract.

## Why this source family is useful

[REEACT](https://doi.org/10.1136/bmj.h5627) compares two computerised CBT programs
added to usual care with usual care alone. [REEACT-2](https://pubmed.ncbi.nlm.nih.gov/28254959/)
compares different levels of support for the same program. They are different
randomized comparisons. Journal articles, detailed reports and an embedded
[patient-experience study](https://doi.org/10.1136/bmjopen-2015-008581) also create
real opportunities to link related publications without counting them as extra
efficacy trials.

A [published correction](https://doi.org/10.1136/bmj.i195) describes a change to
REEACT's primary outcome before the trial began. That lets reviewers test whether
an agent reconciles dated evidence rather than treating a current registry entry
as an exact historical record. Registry links are inventoried, but dated registry
copies have not yet been frozen. That gap affects historical protocol claims,
not the usability of the acquired publications.

The [REEACT-2 detailed report](https://njl-admin.nihr.ac.uk/document/download/2009253)
also contains a numerical discrepancy: Table 10 gives an interval of 1.227 to
3.424, while nearby prose gives 1.23 to 3.24. PDF pages 51 and 52, printed pages
25 and 26, were visually checked. A sound output preserves the source-specific
values, explains the disagreement and labels any calculation separately. A
single forced answer would conceal a source problem.

This is a selected family of 2015–2017 publications inspected in October 2026.
It is not a historical web snapshot, an exhaustive review or a current treatment
recommendation. A present-day evidence search should be a separate dated condition.

## Cases and implementation order

The suite follows the plan's **three connected scientific aims** (Sections
3.1–3.3), rather than treating literature synthesis as the whole project.

| Project aim | Required benchmark outcome | Coverage |
| --- | --- | --- |
| 1. Evidence → questions → ranked hypotheses | Useful synthesis develops into competing, evidence-linked explanations, discriminating predictions and justified priorities | DMH-01–05 provide evidence skills; DMH-07 explicitly tests the hypothesis portfolio |
| 2. Selected hypotheses → public/simulated experiments | A versioned protocol and permitted data lead to executed code, retained results and an independent check | DMH-07 prepares the design; DMH-09 adds actual execution and verification as a later test |
| 3. Findings → project/collaboration proposal or an honest stop | A feasible, readable brief or justified branch decision follows from the evidence and actual result status | DMH-10 tests useful proposals, pending-result briefs and adaptation to null/inconclusive/failed results |

DMH-06 run inspection and DMH-08 persistence support **all three aims**. They do
not replace scientific outcome tests. A later integrated case should follow a
specific evidence/hypothesis version into a checked result and then its project
brief, including backward revisions when evidence or feasibility changes. These
relationships are not compulsory serial execution or a coordinator veto.

| Case | User-visible task | Useful output | Start when |
| --- | --- | --- | --- |
| DMH-01 | Answer a focused question from two real abstracts | Short answer, distinct comparisons, citations and relevant next questions | First controlled model/skill comparison; private input is prepared |
| DMH-02 | Find complementary reports and identify underlying studies | Search receipts, complete source inventory and study/report map | Scholarly retrieval routes can record their work |
| DMH-03 | Improve an abstract appraisal with retrieved methods | Initial and revised appraisal with evidence explaining each change | Retrieval and appraisal can exchange source records |
| DMH-04 | Extract a PDF table and reconcile nearby text | Structured results with arm/time labels and visible discrepancies | Parser/source-fidelity testing; the source pages are already retained |
| DMH-05 | Have peers critique and revise a synthesis | Initial report, source-specific critique, reply and linked revision | Peer communication and model execution exist |
| DMH-06 | Explain a saved run to a new reviewer | Navigable answer, appraisal, sources, attempts and revisions in app/Codex | Connected review API and desktop work |
| DMH-07 | Develop competing hypotheses and a discriminating study | Hypothesis portfolio, prior-work checks, justified priorities and a protocol sketch | Hypothesis, design, data and analysis roles |
| DMH-08 | Continue across waits and interruption | Preserved evidence, model settings, open work and useful resumed output | Persistent runtime and long-run testing |
| DMH-09 | Execute and independently check an analysis | Source-derived input table, versioned plan, executable code, actual job/result record and verification | Shared job and analysis execution are implemented |
| DMH-10 | Turn evidence/results into an actionable project brief | Feasible work packages, resource assumptions, success criteria and justified branch disposition | Project/collaboration planning; checked results added only when available |

Begin with DMH-01 and the source/table preparation for DMH-04. These add real
language, methods and source inconsistencies without pretending the full pipeline
exists. Keep the other cases as reusable inputs for their owning implementations.
Expected latency categories describe complexity and waiting; they are not timers
that terminate research.

For the first Aim 2 exercise, use a declared retrospective analysis of reported
aggregate counts or an explicitly simulated variant. It cannot reproduce a
covariate-adjusted model without its inputs, establish a clinical mechanism, or be
relabeled as a prospective confirmatory experiment. Aim 3 may produce a useful
prospective brief before execution, provided results are marked pending. No test
requires positive findings or authorizes outreach and resource commitments.

## Evaluation that preserves useful work

Record separate findings for factual support, interpretation, study identity,
citation mechanics, reader usefulness, peer contribution, recovery and resource
use. A malformed identifier needs repair but is not automatically a false study
result. A complete abstract can support a fact even when a narrower preselected
passage omitted it. Supply a faithful source-level locator or create a checked
passage rather than falsely labeling that fact invented.

Useful comparisons hold the evidence opportunity constant between concise/rich
instructions or model settings, while native workflow tests allow adaptive search
and record the actual routes used. Do not confuse those conditions or claim
retrieval breadth from a fixed packet. Record all attempts, selected model/skill
versions, source-copy hashes, changes in context, outputs and feedback. Reports
remain readable; request records, timing and raw source data stay in linked
diagnostics. No private model reasoning is needed to explain an evidence decision.

An independent reviewer should establish source-grounded reference judgments
before scoring model outputs, retain disputed labels and accept multiple justified
syntheses. Keep evaluator material outside the model-visible packet. The repository
is not an isolation boundary: give a tested model only its exported input and
permitted source access. One new packet cannot establish a general model advantage.

Independent source/design review corrected three preparation details: explicit
initial versus follow-up views prevent accidental changes in evidence opportunity;
the PDF extraction/reconciliation case is a single-role task rather than merely
retrieval; and month-only report dates retain that precision instead of inventing
a publication day. Other cases still need their exact exposed views declared
before a controlled run. Source metadata alone is not the input packet.

## Acquisition and remaining checks

Seven source copies were acquired through public routes: three article XML files,
three exact-publication metadata/abstract records and the NIHR PDF. The original
REEACT HTA XML route returned HTTP 500; its indexed abstract was acquired instead.
This was assistant-led curation, not a provider benchmark or an agent recovery run.
The scholarly API can provide complementary material when a publisher page is
unavailable; see [Europe PMC's documented service](https://europepmc.org/RestfulWebService).

Raw copies remain local. The original BMJ and qualitative article identify CC BY
4.0, the correction has no reuse license in its captured XML, and the NIHR report
states private-research reproduction terms. Public accessibility alone does not
establish software redistribution rights. The first private input uses the BMJ
and NIHR abstracts under their respective terms; it is not a distributable corpus.

Before a scored run, freeze its allowed source copies, applicable reference
judgments and review rubric, and record its actual model/tool settings. Dated
registry/protocol acquisition, formal method-guide copies, repeated held-out
cases, live provider behavior and app import remain separate qualification work.
Their absence should narrow the corresponding claim, not stop unrelated useful
research.
