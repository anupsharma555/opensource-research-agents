# Selected-model fit check for two runtime skills

This is a small, controlled comparison of the concise and richer instructions for `evidence-linking` and `study-appraisal`. It uses one wholly synthetic benchmark scenario and the same user input within each skill pair. The reference judgments remain outside model-visible prompts. The model run has **not yet occurred**; no model-quality or variant-selection result is claimed.

The selected endpoint is Groq `openai/gpt-oss-120b`, consistent with the current project plan. [Groq's model page](https://console.groq.com/docs/model/openai/gpt-oss-120b) lists $0.15 per million input tokens and $0.60 per million output tokens. Its [API reference](https://console.groq.com/docs/api-reference) lists the chat-completions URL, `max_completion_tokens`, and `reasoning_effort=high` for this model; [reasoning guidance](https://console.groq.com/docs/reasoning) documents `include_reasoning=false`. The harness sets high reasoning, asks for no returned reasoning text, uses no tools or retrieval, and caps each completion at 4,096 tokens. Pricing and API support were checked on 2026-10-08 and must be rechecked before a later run if they change.

## Fixed input and preflight

`experiments/skill_fit/run.py` uses the public, synthetic `syn-abstract-disagreement` worker packet. Evidence-linking cases see the same A1–A4 source copies. Appraisal cases see the same A1 abstract subset so independent review cannot borrow another study. Only the instruction variant differs within a pair. The script loads and hash-verifies skill manifests through `ora.skills.load_registry`, fixes the exact model and endpoint, and outputs four opaque case IDs with prompt and request SHA-256 hashes in `experiments/skill_fit/plan.json`. Its input upper-bound estimate counts UTF-8 prompt bytes plus 1,024 wrapper tokens per call; all four full 4,096-token completions produce a documented-rate bound below $1. This is deliberately conservative for a short text-only prompt, but it is a pricing estimate, not a billing receipt.

The runner defaults to offline plan output. One explicit `--live --case Xn` invocation would reserve that case before a single HTTPS request and refuse an automatic repeat, including after a transport or HTTP failure. It reads `GROQ_API_KEY` from process environment or the existing local `.env` without displaying it. Only final answer text, model/usage/finish status, elapsed time, hashes and an approximate documented-rate charge would be saved. Returned reasoning content is discarded. A transport, authorization, model, or parameter error stops that case; the script has no alternate model, account, endpoint, retry, live search, or escalation path. **Live calls are pending the authorized credential decision. Zero calls have occurred.**

## Blinded content rubric

The case-to-skill mapping is in the runner for reproducibility; a reviewer can score the final text under opaque IDs before inspecting that mapping. Score each dimension separately, using the withheld synthetic reference only on the evaluator side:

| Dimension | What to inspect in the final answer |
| --- | --- |
| Source and claim support | Exact A1/A2 counts, six-week timing, A1 interval, and source/span identifiers support what the answer says. Locator presence alone does not prove entailment. |
| Contrary evidence and uncertainty | A2's 20/50 versus 21/50 and reported lack of clear difference temper a strong benefit reading; A1's interval crosses no difference. Do not call A1's reported counts false. |
| False blocks and useful coverage | The answer should use explicitly reported abstract facts, even without full text, while keeping methods, adverse events and subgroups unknown. A refusal to use A1's supported counts is a false block. |
| Appraisal judgment | For A1, distinguish reporting completeness, risk of bias, applicability and precision for the exact result. An abstract-only randomization statement does not establish allocation concealment or full trial validity. A method/version claim must be checked rather than credited for fluency. |
| Severe error | Fabricated full-text methods, adverse-event rates, pooled estimates, cross-study attribution, wrong denominator/time point, or treating missing information as a null result. |
| Format and resource | Readability, targeted next information need, completion/truncation status, actual prompt/completion tokens, approximate cost and elapsed time. These do not substitute for scientific support. |

For each case, record specific supported and unsupported sentences, span IDs, unknowns preserved, severe errors, false blocks and a provisional usefulness judgment. Compare the two variants within each skill, not across different tasks. Four responses from one fictional packet cannot establish general scientific reliability, latency distribution, or production readiness; a future larger held-out packet and independent review are separate proof.

## Offline verification

`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q tests/skill_fit` passed five focused preflight tests. They check fixed model/settings/tool absence, matched input within each pair, absence of withheld reference markers, A1-only appraisal input, four distinct request hashes and the under-$1 ceiling. A stubbed transport test confirms the exact endpoint, no saved reasoning or key, one saved final answer and no repeated case. The code lint check passed. No provider or model request is part of these tests.
