# Offline package evaluations

These optional comparisons use synthetic inputs and no research-provider
credentials. They are separate from production storage and agent tools.

## Evidence metadata search

```sh
scripts/dev evaluate-fts --output .local/evaluations/evidence-fts.json
```

`benchmarks/evidence_fts/corpus.json` supplies versioned fictional entries and
relevance labels authored separately from the harness. The comparison measures
top-five relevance, ordering, false positives, guarded query time, index bytes
and rebuild time for the existing term index and SQLite FTS5 conjunction/phrase
queries. The fixture includes duplicate sources, stale versions, permission
changes, numerical tokens, phrase scope and abbreviation gaps.

The guard is a synthetic stand-in for current source permission and version
checks. It does not prove production permission enforcement or scientific
relevance. Phrase/ranking gains on these scenarios require a separate permitted
corpus with independent relevance assessment before production adoption. The
local SQLite build must support FTS5; no extension is downloaded or loaded.

## Statistical-library comparison

Install the isolated, pinned experiment explicitly:

```sh
scripts/dev analysis-eval-setup
scripts/dev analysis-eval --output .local/evaluations/statsmodels.json
scripts/dev analysis-eval-test
```

Setup uses the repository-pinned uv/Python and the experiment's own lockfile and
virtual environment. `--offline` restricts setup to cached packages. It does not
modify the root environment or lockfiles. The default repository check does not
install this optional experiment.

The comparison evaluates statsmodels against independent scalar fixed-effect
and nonnegative DerSimonian–Laird reference calculations on fictional effect
sizes and variances. Inspect the reported method, heterogeneity and finite-value
policy: agreement in one example is not agreement across all inputs. The tests
also retain low/zero-heterogeneity discrepancies as explicit findings rather
than silently correcting library output.

Statsmodels is an experimental dependency here, not a production dependency or
research-agent tool. Adoption requires source-linked study effects/variances,
duplicate-study resolution, explicit estimator/interval choices and independent
reference comparisons. See the [experiment instructions](../../experiments/analysis_comparison/README.md)
for its package and method details.

## Durable peer tests

```sh
.venv/bin/python -m pytest -q tests/coordination/test_stateful_peer_recovery.py
```

These Hypothesis tests generate delivery, answer, explicit review, refusal,
replay and reopening sequences against the real store/board/mailbox. Separate
generated cases check claim expiry, stale-result rejection and independent
branch completion. They prove bounded local persistence behavior, not natural
model collaboration, concurrency stress or production endurance.
