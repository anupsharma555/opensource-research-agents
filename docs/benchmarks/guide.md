# Synthetic research benchmark bundle v1.0.4

This packet set tests whether later research-agent implementations can find, preserve, interpret, and explain evidence without hiding contrary findings or turning technical failures into scientific results. Every named study, program, number, and result in the three scenarios is fictional. The packets are **test input**, not published research or evidence about a real treatment.

## What is frozen

`benchmarks/v1/manifest.json` pins the canonical SHA-256 of each scenario, its evaluator-only reference, and the profile, baseline, and rubric definitions. The bundle is v1.0.4: A and C scenarios remain v1.0.1; B is v1.0.2 after exact denominator spans were added to unchanged source bodies. Each scenario has a stable ID and version; each source has its own ID, version, text hash, access state, rights state, and exact passage spans. A span's offsets must reproduce its quoted text. Changing substantive content requires a new scenario version and manifest hash. Formatting JSON alone does not change the canonical hash.

| Scenario | Evidence challenge | Initial worker view |
| --- | --- | --- |
| `syn-abstract-disagreement` | Favorable but imprecise abstract, separate null study, irrelevant citation, unavailable methods/adverse events | Four synthetic source records, including the access notice |
| `syn-route-recovery` | Failed search route, alternate partial result window, registry without posted outcomes, retention versus symptom outcome | Registry record; the fixture driver delivers later route results |
| `syn-null-versus-error` | A checked imprecise null estimate followed by a separate parser failure | Checked result; the fixture driver delivers the later error |

The `ora.benchmarks` library exposes `validate_catalog`, `load_scenario`, `load_event_script`, `make_worker_packet`, `validate_worker_packet`, `export_worker_packet`, `load_reference`, and `validate_run_record`. `load_scenario` and `load_event_script` are for a fixture driver. Give a tested agent **only** the JSON returned by `make_worker_packet` or written by `export_worker_packet`; deliver scripted events one at a time. Do not reveal future events in the initial packet. `load_reference` reads withheld labels for an independent evaluator and must never run in the tested agent's process.

The source checkout contains the evaluator files, so a worker packet alone is not a filesystem security boundary. A later runtime must mount or transmit only the exported worker packet and authorized event payloads, not this repository or `benchmarks/v1/withheld/`. The current offline checks prove exported-packet separation and detect answer-key markers; they do not prove isolation of a future deployed sandbox.

## Profiles and fair comparisons

`profiles.json` separates provider-only, retrieval-only, single-role, peer-exchange, and complete-workflow tests. `baselines.json` separates two conditions:

1. **Controlled equal input:** supply the same question and pinned source opportunity to eligible systems. Predeclare a comparable resource envelope, including calls, tokens, tools, currency, and source access where measurable. This condition tests reasoning and workflow over comparable material, not live discovery breadth.
2. **Native workflow:** supply the same question and decision, then allow each product's ordinary authorized research features. Record exact product/model version, tools, source access, user interventions, and observed resources. Do not disable a frontier product's normal strengths merely to mimic this project. Disclose unmatched features and unmeasured costs.

The comparison arms are this pipeline, one agent using the same selected model, and a strong current ChatGPT or Codex workflow. No model or provider comparison has been executed in this implementation. Exact run-time versions, budgets, account capacity, and product features must be recorded **before** any later live comparison; the fixture set does not invent those observations or impose a scientific search-round deadline. `baselines.json` includes one illustrative controlled-test allowance for the synthetic A single-role profile: up to four model calls, 20,000 total model tokens, zero tool calls, and USD 1.00, with A1–A4 as the fixed source opportunity. It is a proposed evaluation allocation to review and declare before a later comparison, not observed consumption or a scientific duration cap.

## Scoring and interpretation

`rubric.json` keeps scientific support, contrary-evidence retention, severe errors, false blocks, useful unique evidence, recovery, cost, useful latency, and reader usefulness separate. A saved run uses `run-record-template.json` and records its build/configuration, proof type, source access, user interventions, artifacts, claims, citations, a predeclared benchmark allowance, and distinct observed resource units. Measured controlled comparisons require a numeric allowance and comparison-group ID; fixture-only records may leave planned and actual usage unknown. Provider usage stays attached to its provider and unit; unlike credits are not summed. Unknown cost or timing is `null`, not zero. Time to a useful source and a supported partial answer are separate from total scoped completion; active work and waiting are also separate. The latency class is a revisable description of task complexity, not a timeout.

The structural checker verifies hashes, source IDs, locatable spans, recorded source access, resource units, and result lineage. **It does not decide whether a passage actually supports a claim**, whether a null is scientifically informative, or whether prose helps a reader. An independent evaluator makes those judgments with the withheld reference and records any disagreement. A citation that exists but is irrelevant remains a scientific error even if it passes structural checks.

For a reader-usefulness calibration, consider this separate fictional example: two reports both say 10 of 20 participants responded in each group. A correct but opaque sentence is “The aggregate point estimates are nondiscriminating under the measured endpoint.” A clearer sentence with the same support is “Both groups had 10 responses among 20 people, so these reports do not show a difference on the measured outcome.” The evaluator scores whether the intended reader can find the answer, its evidence, the link between them, and important uncertainty. Polished but generic text gains no credit.

An abstract can support its **reported** facts even when full text is unavailable. An unavailable methods table does not supply missing methods, a registry without posted results is not a null finding, and a parser exception is not contrary scientific evidence. A useful qualified answer can be scored while further searches remain open. Distinct reports of one study count as one underlying study where identity is established.

## Reviewer and proof workflow

Reference files contain acceptable qualified claims, contrary and irrelevant spans, severe-error examples, expected partial outcomes, and unresolved gaps. Their illustrative disagreement fields are explicitly demonstrations, not real human reviews. For a benchmark claim, independent reviewers should receive blinded output when practical, record their own labels and reasons, discuss disagreements, and save an adjudicated decision without the tested agent scoring itself. The review record in `evidence/reference-review.md` documents independent science-agent checks of synthetic A and B labels; it is not a human panel or model-performance result.

`validate_run_record` confirms only a run record's structure and traceability. Scored ratings also pin the reference and rubric hashes. `result_applicability` returns `historical_untested_for_current` when the scenario, profile, arm, condition, rubric, reference, build, or configuration changes. A previous pass remains inspectable but does not become a pass for the new case. `evidence/scenario-result-map.json` links current packet identities to contract checks and explicitly untested model/pilot consumers.

## Offline verification

The current `pyproject.toml` pytest configuration provides the focused project check:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q tests/benchmarks
```

The saved verbose standard-library transcript from the same selected `.venv` is under `evidence/validation.txt`. It proves the listed offline fixture behaviors only. No provider, selected model, scientific reviewer panel, Mac UI, or full workflow was run.
