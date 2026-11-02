# Synthetic evidence-search comparison

This corpus is entirely fictional. Root authored the relevance labels before the separate worker built the comparison harness. The labels represent stated synthetic information needs, not human scientific adjudication or measured real-world retrieval quality.

Phrase/negation queries distinguish an intervention from incidental words and a negative finding from missing reporting. Abbreviation queries intentionally consider the abbreviated and expanded concepts relevant even when literal matching misses one. Decimal-dose queries distinguish 0.5 from 0.05; they provide no treatment advice. Only current, permitted source versions can be relevant. Duplicate curated entries for the same source count once. Stale, revoked, inaccessible and absent-only queries have empty relevant sets.

Use the exact versioned corpus for comparison; do not tune queries or labels after seeing one index's results. A successful guard check proves isolation in this fixture, not production permission enforcement. Any deployment decision needs a separate permitted corpus and independent relevance assessment.
