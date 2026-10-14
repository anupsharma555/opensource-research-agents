# Evidence linking · richer variant

Use this skill when a sentence, table cell, hypothesis premise, or critique needs a precise source trail.

1. Read the current `EvidencePacket` and its access surface. Select the exact inspected passage locator, preserving the packet and artifact IDs, versions, and content hash. Do not replace an earlier copy silently when a newer source appears.
2. Write the narrow factual claim. Keep the study population, comparison, outcome definition, observation time, numerical basis and uncertainty attached where relevant. Label an inference separately from what the source directly reports.
3. Mark the relationship as support, challenge, or context. A citation can be locatable yet irrelevant; independent semantic review decides whether the passage actually warrants the claim. Check contrary passages and related reports without counting the same participant group twice.
4. If only an abstract is inspected, use its explicit facts. Leave unreported methods, subgroups and adverse events unknown. Request a permitted same-study companion or independent contextual study where useful, keeping the two kinds of evidence separate.
5. If the source, locator, parser result or access route fails, keep already valid links and open a targeted repair. Do not block unrelated claims or promote a model summary to original source text.
6. Audit the final draft claim by claim against the inspected passages, including headings, tables and summary labels. Separate directly reported facts, calculations, and interpretations. Verify design and timing terms against the inspected source or protocol; do not infer pre-specified status from the mere presence of a subgroup result or citation. Report numerical eligibility cutoffs as stated; translate them into severity categories only with an explicit, applicable mapping. Attribute judgments about burden, feasibility or delivery to measured or stated evidence; otherwise identify them as interpretations with their basis, or omit them. Narrow any claim that its cited passage does not warrant, and retain unresolved uncertainty.

The project-owned `prepare_claim_link` helper only verifies the canonical `EvidencePacket` and locator and emits a pending-review link. A source-read tool confirms access to text. Neither certifies the meaning or scientific support of a claim.
