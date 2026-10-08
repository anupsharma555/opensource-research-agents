# Study appraisal · richer variant

Use this specialist skill after a source batch has a stable version. Start from the same original passages available to the evidence preparer, but complete your first judgment before reading the preparer's conclusions. This is independence of first-pass review, not a mandatory human approval gate.

1. Identify the study design and the particular outcome, comparison and measurement time being judged. Select a named, versioned method suitable for that design; do not use one generic quality score for every study.
2. Inspect the exact abstract, methods, result table, registration or permitted companion source available. Record what was reported separately from whether it is credible. For abstract-only evidence, leave unreported risk-of-bias details unknown.
3. Record four distinct judgments: reporting completeness, risk of bias, applicability to this question, and estimate precision. Give a source-linked reason for each, including uncertainty or missingness. A judgment on one result does not automatically transfer to all results from the study.
4. Preserve conflicting denominators, effect signs, outcomes or time points as discrepancies. Compare versions against the original table when claim-critical. A missing posted result is not a null; a technical failure is not a scientific result.
5. Send targeted information needs to acquisition and discuss discrepancies after both first passes are saved. Reopen only claims that depend on a corrected source or changed judgment; keep unaffected evidence usable.

The project-owned `prepare_result_appraisal` helper binds the agent's method and judgments to an exact result and canonical `EvidencePacket`. It does not choose the appraisal method or certify validity.
