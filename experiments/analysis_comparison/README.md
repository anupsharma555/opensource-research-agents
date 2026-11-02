# Isolated statistical-library comparison

This experiment compares statsmodels 0.15.0 fixed-effect and DerSimonian–Laird
random-effects estimates with independent inverse-variance algebra. The inputs
in `fixture.json` are synthetic unitless numbers, not extracted study results.
The five-study fixture agrees numerically. Low-heterogeneity and identical-effect
fixtures show that statsmodels returns a negative raw tau² for this method,
while the reference truncates tau² to zero. Identical effects can make the raw
random-effects estimate nonfinite. The result records these as mismatches rather
than silently changing statsmodels output. See the [statsmodels implementation](https://www.statsmodels.org/stable/_modules/statsmodels/stats/meta_analysis.html).
The comparison does not assess study quality, effect-scale compatibility,
independence or clinical interpretation.

Use the repository wrapper and pinned Python 3.12.12. From the repository root,
install **only this project's** locked virtual environment, then run the
comparison and its isolated tests:

```sh
scripts/dev analysis-eval-setup
scripts/dev analysis-eval \
  --output .local/reviews/stack-next2-20261031/statsmodels-comparison.json
scripts/dev analysis-eval-test
```

The locked environment is separate from the root `.venv` and `uv.lock`.
`statsmodels` remains an evaluated candidate, not a production dependency or
research-agent tool. A useful future meta-analysis tool would need verified
study-level effect and variance extraction, duplicate-study resolution, a
specified estimand, and independent scientific review.
