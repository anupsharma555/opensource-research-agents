# Bounded quantitative tools for research peers

The mission host exposes named calculations on explicitly supplied values. Their input authority is the Pydantic models in `src/ora/contracts/models.py`; original implementations and descriptions are in `src/ora/mission/analysis_tools.py`, with additional version-2 tools in `analysis_extended.py`. The host still owns tool attachment, current permissions, durable execution and result receipts. A catalog entry by itself does not make a tool callable in a model turn.

## Operations and boundaries

| Tool | Inputs | Calculation | Deliberate limits |
| --- | --- | --- | --- |
| `analyze_values` with `operation: describe` | One group of 2–200 finite JSON numbers; optional short unit label | Count, arithmetic mean, sample standard deviation, minimum and maximum | No omissions, imputation, source lookup or inference. |
| `analyze_values` with `operation: welch_ttest` | Two independent groups of 2–200 numbers and an explicit `two-sided`, `less` or `greater` alternative | [SciPy Welch independent-samples t-test](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.ttest_ind.html) with unequal variance and `nan_policy="raise"`; group summaries, mean difference A minus B, statistic, p-value and degrees of freedom. A 95% two-sided interval is returned only for a two-sided alternative. | Both-constant groups and nonfinite results are rejected. The host does not infer a test from a paper, establish independent sampling, check distributional assumptions or interpret clinical importance. |
| `convert_units` | One bounded finite number; source and target selected from named mass, length, time and volume units | [Pint registry](https://pint.readthedocs.io/en/latest/getting/tutorial.html) conversion, with factor and dimensional compatibility check | No custom unit definitions, free-form expressions, temperature offsets, compound clinical concentrations, molecular-weight conversions or clinical equivalence claim. |

The canonical models reject bools, strings, nested values, NaN/Inf, magnitudes beyond 10¹², unknown fields and unsupported operations or units. The tools return a SHA-256 hash of normalized typed inputs, actual software versions, a method label, a 12-significant-digit display policy and a statement that supplied values and scientific assumptions were not validated. Results omit the raw arrays; the mission host retains the original call and permission context separately. The hash is an integrity aid for replay comparison, not proof that the input came from a cited source.

New mission runs and follow-ups pin `analysis_tool_version: 2`. Version-1 runs retain only the original two analysis tools; saved runs without that pin do not acquire analysis tools. Original input schemas and results remain unchanged. Catalog visibility alone is insufficient: the selected call must include the schema in its serialized `request.tools`, and the host must validate the exposed schema and current source dependencies before dispatch.

When the pinned run supports either numerical tool, the serialized developer instructions name each supported tool's purpose and ask the peer to check nontrivial numerical claims with an applicable local calculation. An unattached schema still needs `request_capability` before use. The guidance asks the peer to distinguish estimates from computed values, preserve the selected method, alternative, units and reported precision, and avoid treating a calculation as validation of supplied data or study design. It is omitted when the pinned run does not support these tools. This is model-facing guidance, not automatic tool execution or proof that a model selected the tool; check the saved request and actual tool receipt separately.

The numerical tools import SciPy or Pint only when chosen. The module itself and descriptive arithmetic do not load either heavy library. This keeps ordinary research turns from paying the statistical library's first-import cost. The accepted tool result is a calculation, not an accepted report or automatically supported scientific claim; peers must inspect source values and design, then review interpretation in their own work.

## Additional version-2 tools

| Tool | Explicit input and method | Result and limits |
| --- | --- | --- |
| `compare_summary_statistics` | Group sample sizes, means and **sample SDs**; explicit alternative; independent Welch | Difference, SE, statistic, df, p-value and eligible interval. SD is not SE. Source extraction and independence remain unverified. Normalize SDs before variance calculations and the SciPy call; reject uncertainty or standardized differences outside supported floating precision. |
| `analyze_contingency` | Integer event/nonevent counts for groups A/B; selected risk ratio, odds ratio or risk difference; explicit zero-cell policy | Transparent effect and approximate log-Wald/Wald interval/test. OR requires positive cells unless a 0.5 correction is selected; RR requires positive event cells. RD retains raw zero cells. Any correction is recorded with original counts. Sparse counts can make approximations inappropriate. |
| `analyze_paired_values` | Each unique pair ID supplies both A/B values; explicit paired-t alternative | A-minus-B difference, within-pair SD, statistic, df, p-value and eligible interval. The caller establishes pairing; IDs do not validate it. Constant differences and SciPy precision-loss warnings are rejected. |
| `correlate_values` | Complete unique pairs; explicit Pearson method and alternative | Correlation, analytic p-value and eligible Fisher interval. Constant/nearly constant values are rejected; at least four pairs are required. Association does not establish causation. |
| `adjust_pvalues` | Named family of unique test IDs/p-values; explicit BH/BY method and alpha strictly between zero and one | SciPy FDR adjustment preserving original order/IDs. Decision-bearing p-values and alpha retain round-trip precision. Completeness of the submitted family and original test validity remain unverified. |
| `describe_values` | Finite observations and explicit linear-quantile method | Mean, sample SD, median, quartiles and IQR. Sample spread is not inferential uncertainty. |
| `convert_values_units` | Finite value array and source/target units from the existing allowlist | Batch physical conversion with factor and package version; no custom definitions or clinical-equivalence claim. |

Missing values are rejected rather than silently omitted or imputed. Counts, IDs, array sizes and finite numeric ranges are validated before execution. Risk ratio/difference denominators must represent appropriate at-risk groups; sampled case-control counts do not automatically establish cohort risks. No arbitrary Python, SQL, file path or package function name is accepted.

Version-2 intervals preserve round-trip endpoints. If a nonzero interval is narrower than floating-point spacing at its center, the result gives center and half-width with an explicit resolution status and unavailable endpoints. A computed probability that becomes zero at floating precision is unavailable with a status, rather than reported as an exact zero. Other displays retain the stated numeric policy. These distinctions must survive interpretation in the final answer.

Source-derived arguments and results share the permission/replay checks. After source-use revocation, both the result and replayed model arguments are withheld from subsequent context. Availability, execution, source accuracy and scientific interpretation remain separate proof boundaries.

Large version-2 results use the existing `read_tool_result` character-window reader. Long calculation arguments are archived in model-facing history with an explicit SHA/status; exact saved calls and results are unchanged. FDR is computed over the **complete supplied family once**, then its result is paged: calculating independent pages would change the statistical question. The receipt includes a whole-family rejection count and adjusted-p range. The reader is attached when large saved results are available, rechecks task/source permissions, and carries source dependencies into the next prepared model input. Revocation removes both affected result text and assistant prose derived from revoked input.

## Verification

Focused offline checks:

```sh
.venv/bin/python -m pytest -q tests/mission/test_analysis_tools.py
.venv/bin/python -m pytest -q tests/mission/test_analysis_extended.py tests/mission/test_analysis_extended_integration.py
.venv/bin/python -m ruff check src/ora/mission/analysis_tools.py tests/mission/test_analysis_tools.py
```

The focused suite covers known synthetic summaries/comparisons, one-sided direction, exact input hashing, package versions, invalid value classes, incompatible dimensions and both-constant groups. After catalog/service integration, inspect a saved serialized model request to confirm the selected schema is actually in `request.tools`, then verify model selection, host execution, StateStore result, permission recheck and viewer wording as separate steps. A scripted model turn proves mechanical wiring; a natural-model and source-grounded scientific task need their own qualification.

In one local synthetic microbenchmark of 200 observations per group (100 warm calls per operation), module import took 185 ms; first Welch execution took 619 ms and warm median 2.26 ms; first Pint conversion took 118 ms and warm median 0.293 ms; descriptive warm median was 0.114 ms. These are single-process development-machine measurements, not host throughput or provider latency. See the private `stack-next/analysis-benchmark.json` receipt for the workload and p95 values.
