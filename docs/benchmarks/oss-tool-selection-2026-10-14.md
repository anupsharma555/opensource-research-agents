# GPT-OSS default tool-selection checkpoint, 2026-10-14

A bounded development evaluation reached **16 correct choices out of the first
20 observed tool choices (80%)** and paused at its predefined threshold. This is
a call-weighted checkpoint across eleven synthetic tasks. It does not establish
80% task-completion accuracy, scientific validity, or complete tool coverage.

## Tested configuration and provenance

The evaluation used Groq `openai/gpt-oss-120b`, high reasoning, nonstreaming
responses, an 8,192-token configured completion maximum, and disabled parallel
tool calls. The model and credential profile were preserved throughout.

The real mission runtime prepared the requests and managed normal capability
discovery and attachment. The synthesis role's catalog contained 41 capabilities;
the critique role's contained 43. Only normally attached schemas were callable.
No five-tool menu or artificial exposure of all 43 functions was substituted.

The tested source was a **local development snapshot**, based on repository commit
`e2c17d5df0393441093c81564b8388383d6d51c4` plus uncommitted runtime work. All eleven
runs retained the same source-file hash manifest, whose SHA-256 is
`19e24a23bc2c7a5157f6279411e3932f3c75355c78e1964135ff5f97d790be99`.
This report does not publish that runtime work or qualify the base commit alone.
Exact request packets, source manifests, usage receipts and review records remain
private. The aggregate result cannot be independently reproduced from this
sanitized report alone.

## Protocol and scoring

Natural user tasks specified useful goals and constraints. Candidate routes and
scoring rubrics stayed in controller files. An audit of all eleven user tasks and
22 actual request packets found no tool/function names in the user tasks and no
withheld case rubric in the requests.

The tasks were still directed probes. For example, a repository-lead task asked
to retain a supplied synthetic lead without visiting it. The appraisal task
explicitly asked to check detailed guidance before critiquing a synthetic memo,
which favors a guidance workflow. Release tasks used synthetic prior activations;
a passage-reading task supplied a checked source locator. These conditions limit
claims about spontaneous discovery in an unconstrained conversation.

The first two tasks allowed completion. After the scope changed to selection
only, later runs stopped before the intended target handler or at the checkpoint
boundary. Preparatory discovery and context readers supplied real host feedback.
Synthetic setup responses were excluded from live selection and token totals.
There were at most four observed choices in a single run.

Independent automated review scored the saved observable choices, arguments and
state. A useful direct answer was allowed, but it did not exercise an uncalled
tool. Repeated relevant choices could be correct; redundancy was recorded
separately. No private model reasoning was used to establish correctness.

The denominator includes 19 accepted tool selections and one intended choice
identified unambiguously in a provider validation error. The rejected choice
counts as incorrect; it did not execute a tool. Two direct responses are excluded
from the selection denominator. Multiple choices from one task are correlated,
so twenty choices are not twenty independent task trials.

## Observed choices

| # | Task | Choice | Selection grade |
| ---: | --- | --- | --- |
| 1 | Save a working reminder | `request_capability` | Correct |
| 2 | Save a working reminder | `write_note` | Correct |
| 3 | Defer optional follow-up and inspect work | `board_read` | Correct |
| 4 | Defer optional follow-up and inspect work | `request_capability` | Correct |
| 5 | Defer optional follow-up and inspect work | `board_read` | Correct; redundant |
| 6 | Defer optional follow-up and inspect work | `propose_work` | Correct |
| 7 | Read retained follow-up passage | `read_source_window` | Correct |
| 8 | List saved research venues | `request_capability` | Correct |
| 9 | List saved research venues | `list_venues` | Correct |
| 10 | Retain a supplied venue lead | `read_context_page` | Incorrect |
| 11 | Retain a supplied venue lead | `board_read` | Incorrect |
| 12 | Retain a supplied venue lead | `search_scholarly` | Incorrect |
| 13 | Record a duration estimate | `request_capability` | Correct |
| 14 | Record a duration estimate | `estimate_latency` | Correct |
| 15 | Check appraisal guidance | `activate_skill` | Correct |
| 16 | Release arithmetic access | `release_capability` | Correct |
| 17 | Return to ordinary guidance | `read_context_page` | Correct; indirect context check |
| 18 | Return to ordinary guidance | `read_context_record` | Correct; indirect context check |
| 19 | Return to ordinary guidance | `read_source_inventory` | Incorrect; provider rejected before execution |
| 20 | Offer an optional peer review | `request_capability` | Correct |

The final choice requested invitation capability correctly. It did not establish
that the invitation tool itself was selected or executed.

## Coverage and failures

Thirteen of the 43 distinct tool names were observed: twelve had accepted calls,
and one was rejected before acceptance. Across observed cases, nine names had
only correct choices, two had mixed choices, and two had only incorrect choices.
**Thirty tools remain untested.** The 16/20 stopping rule did not require completing
all 43 capabilities.

Three misses came from the supplied venue-lead task: an empty context-page read,
a work-board lookup, and an external scholarly-search choice failed to serve the
requested persistence task. The external search was stopped before its effect.
The intended venue-proposal capability remained unexercised.

The fourth miss attempted `read_source_inventory` before its function schema was
attached. The provider rejected the request with HTTP 400; the normal
`request_capability` route was available. This is a discovery/selection failure,
not proof of a handler defect. The existing host correction path was not followed
through a new live retest after the checkpoint paused.

One repeated board read was correct but redundant. Two context reads while
returning to ordinary guidance were accepted as indirect checks of saved work.
These grades do not establish efficient behavior. A separate arithmetic task
returned correct percentages and formulas directly, leaving calculator selection
unexercised.

## Resource accounting

There were **22 actual API attempts**. Twenty-one have reported usage:

| Usage | Provider-reported tokens |
| --- | ---: |
| Input | 94,910 |
| Completion | 15,337 |
| Total | 110,247 |

The rejected request has unknown usage, not zero. Monetary cost is unknown.
A separate known historical subtotal of 12,710 tokens brings local known usage
to 122,957 plus that unknown attempt. Four earlier reduced-menu calls totaling
7,297 tokens are included in the historical subtotal and excluded from this
checkpoint's coverage.

For conservative planning, the failed run's entire unused original allowance
of 18,444 tokens remained reserved. Known usage plus that reservation was 141,401
against the stated 200,000-token planning limit. A reservation is not measured
consumption or evidence of current account-wide quota. No budget increase,
credential fallback, model switch, or automatic retry of the failed request was
used. Offline preparation, injected fixtures and review made no Groq inference
requests.

## Remaining evidence

No runtime fix or post-fix live retest was made during this checkpoint. Useful
follow-up work, if testing resumes, is to examine supplied-lead persistence
selection, test the existing request-first correction path, and stage normal
starting states for the thirty untested capabilities. End-to-end execution,
external retrieval, complete task outcomes and broader scientific performance
remain separate qualifications.

This publication adds a summary only. It does not include private diagnostics,
credentials, model payloads, local databases, or unrelated development changes.
