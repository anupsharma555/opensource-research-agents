# Benchmark packet contract

The Python validator in `src/ora/benchmarks/` is the executable schema authority for this initial package. This page explains its JSON fields for consumers; it does not define a second competing schema. All records carry a `schema` discriminator ending in `.v1`.

| Record | Required identity and contents | Visibility |
| --- | --- | --- |
| Manifest | Packet-set version; scenario and reference paths, versions and canonical SHA-256 hashes; profile/baseline/rubric hashes | Developer and evaluator |
| Scenario | `scenario_id`, `scenario_version`, simulated task with real clinical labels, question/decision/audience/latency class, sources, initially visible source IDs, scripted events | Fixture driver; events are not in initial worker input |
| Source | `source_id`, `source_version`, content surface, access state, `synthetic_project_owned` rights, body SHA-256, exact text and spans | Worker only when initially provided or subsequently delivered |
| Span | `span_id`, exact `quote`, character `start`/`end`, and human-readable section/page locator | With its source copy |
| Scripted event | `event_id`, delivery `step`, `kind`, actor, target, typed payload | Fixture driver until delivery step |
| Worker packet | Scenario identity/version/hash, profile, condition, task, initially permitted sources, and packet hash | Tested agent |
| Reference | Scenario identity/version, acceptable claims and supporting span IDs, severe errors, contrary/irrelevant spans, qualified partial outcome, gaps, withheld marker | Independent evaluator only |
| Run record | Scenario/profile/arm/condition, build/configuration/rubric hashes and proof type; accessed sources, interventions, resource units, artifacts, claims and separate reviewer assessment with reference hash when rated | Evaluator and diagnostics |

Worker packets for `controlled_equal_input` contain only `initial_source_ids`; for `native_workflow`, they contain the same task but no pinned source snapshots. This input difference must be recorded in any comparison. Events may reveal additional source IDs later. The fixture driver should not hand the entire event script to an agent at once.

The resource record keeps calls, tokens, provider usage by named provider and unit, money and currency, active seconds, queue/provider wait, peer wait, retry time, first useful source, first supported partial answer, and scoped completion as distinct nullable fields. Unknown provider usage is `null`; observed usage is a list of `{provider, unit, amount}` records, so unlike credits are never added together. `proof_type` distinguishes `fixture`, `recorded_input`, `selected_model`, and `live_provider`. A citation to a known source span passes only **structural** validation. The separate scientific support rating requires an independent judgment of meaning and scope.

The loader rejects missing fields, duplicated IDs, changed hashes, out-of-root manifest paths, inaccurate span offsets, unknown event sources, mismatched reference versions, unknown profile/condition IDs, and a worker packet augmented with evaluator fields. The run checker also rejects citations to unrecorded sources, unknown inference premises, negative resource values and scored ratings without reviewer IDs. It cannot prove that a named reviewer was independent or that a future runtime hid the withheld directory; those are evaluation and deployment boundaries.
