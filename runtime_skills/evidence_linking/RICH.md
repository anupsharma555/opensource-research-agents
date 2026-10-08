# Evidence linking · richer variant

Use this skill when a sentence, table cell, hypothesis premise, or critique needs a precise source trail.

1. Read the current `EvidencePacket` and its access surface. Select the exact inspected passage locator, preserving the packet and artifact IDs, versions, and content hash. Do not replace an earlier copy silently when a newer source appears.
2. Write the narrow factual claim. Keep the study population, comparison, outcome definition, observation time, numerical basis and uncertainty attached where relevant. Label an inference separately from what the source directly reports.
3. Mark the relationship as support, challenge, or context. A citation can be locatable yet irrelevant; independent semantic review decides whether the passage actually warrants the claim. Check contrary passages and related reports without counting the same participant group twice.
4. If only an abstract is inspected, use its explicit facts. Leave unreported methods, subgroups and adverse events unknown. Request a permitted same-study companion or independent contextual study where useful, keeping the two kinds of evidence separate.
5. If the source, locator, parser result or access route fails, keep already valid links and open a targeted repair. Do not block unrelated claims or promote a model summary to original source text.

The project-owned `prepare_claim_link` helper only verifies the canonical `EvidencePacket` and locator and emits a pending-review link. It is not a scientific support classifier.
