# Runtime roles and skills

Codex development workflows live separately in `.agents/skills/`: use
[`os-check-foundation`](../../.agents/skills/os-check-foundation/SKILL.md) for
offline checks, [`os-qualify-run`](../../.agents/skills/os-qualify-run/SKILL.md)
for research-run diagnostics across stages (with runtime-specific inspection
guidance), [`os-eval-testing`](../../.agents/skills/os-eval-testing/SKILL.md)
for realistic natural-language tasks and progressive evaluation across conditions,
[`os-run-details`](../../.agents/skills/os-run-details/SKILL.md) for an end-to-end
saved-run account in chat or a requested private report, with numbered steps,
diagnostics and linked technical evidence,
[`os-quick-run`](../../.agents/skills/os-quick-run/SKILL.md) for one immediate,
bounded live API case followed by that saved-run account,
[`os-groq-usage`](../../.agents/skills/os-groq-usage/SKILL.md) for daily Groq token
accounting by credential profile and dated quota observations, and
[`os-review-atlas`](../../.agents/skills/os-review-atlas/SKILL.md) for visual
review against exact saved records. These are not instructions loaded into
research agents. The project-local
[`build-with-exa`](../../.agents/skills/build-with-exa/SKILL.md) supplies upstream
Exa API development guidance plus a short OSRA integration reference; use it
when changing the Exa adapter, not when running an ordinary research query.
Its source revision and local additions are recorded in its `UPSTREAM.json`.


This package supplies a small, runnable role-to-skill skeleton. It is separate from Codex development skills and does not implement nine independent runtimes. The nine names in `roles/roles.json` match Section 7 of the current Linear research plan, read on 2026-10-08. Role manifests keep stable IDs, names, plan sections and skill references; they do not copy shared collaboration rules. The central `ora.contracts.models.EvidencePacket` is the input schema authority. The [module map](modules.md) identifies the callers and ownership of the runtime, model adapter, app and shared development commands.

## Implemented boundary

| Component | Caller and input → output | Dependencies, state, effects | Lowest proof |
| --- | --- | --- | --- |
| `roles/roles.json` and `runtime_skills/registry.json` | Package loader; nine plan role IDs and four skill manifest paths → validated role-to-skill references | No external service or durable state; versioned file references | Exact names, duplicate and incompatible-reference tests |
| `ora.skills.load_registry` | Host/parent check wrapper; project root and available capability names → loaded skills plus scoped unavailable reasons | Canonical Pydantic contracts and the selected environment; read-only files and imports | Missing required file, optional specialist, version and dependency tests |
| `ora.skills.prepare_claim_link` | Evidence preparation or critic; canonical `EvidencePacket`, claim, existing locator and relation → candidate link with artifact ID/version/hash and pending semantic review | Project-owned helper, no durable write or network; requires a source-read capability from the eventual host | Example output, missing-locator and wrong-contract tests |
| `ora.skills.prepare_result_appraisal` | Independent methods reviewer; canonical packet and exact result/method/judgments → result-scoped appraisal record pending independent check | Project-owned helper, no durable write or network; specialist capability may be unavailable without disabling shared evidence linking | Four-judgment example, unknown method detail and malformed-record tests |
| `SkillRegistry.start_skill_attempt` | Role instance; declared role/skill and attempt ID → new attempt-local mutable state with pinned skill/helper identities | In-memory per attempt; no shared session or durable effect | Two roles use the same shared skill in serial and parallel attempts with separate state |

The `source_read_by_id` and `study_appraisal` capability names are host-facing requirements, not claims that source retrieval or scientific appraisal tools already exist. The loader reports a missing optional specialist as unavailable and preserves the shared skill. A missing required shared package or corrupt provenance produces a focused error. Parent-owned command wrappers should call these APIs; there is no second workflow engine here.

## The research skill packages

`evidence-linking` v1.0.3 is shared by **Evidence preparation and synthesis** and **Independent methods reviewer and critic**. It requires an exact source locator, preserves source-copy identity and access surface, distinguishes support, challenge and context, and leaves semantic entailment pending. Its instructions now require a final claim-by-claim comparison with inspected source text, including design/timing labels, derived categories and operational judgments. A source-reading tool confirms access to text, not semantic support. `study-appraisal` v1.0.3 belongs to the critic role. It asks the agent to choose a design-appropriate method and keep reporting completeness, risk of bias, applicability and precision separate for one outcome/comparison/time point. The helpers check presence and lineage; they do **not** choose a method or certify study validity. The examples remain synthetic and helper behavior is unchanged.

`general-web-discovery` v2.0.0 supplies short default guidance for all nine roles. It distinguishes targeted `search_web(mode="quick")` lookups from broader `research` rounds, directs agents to read or cancel existing operations, and keeps discovery candidates separate from inspected source passages. The backend implements Exa-first retrieval and configured fallbacks; the skill supplies guidance, not a separate search executor. Agents can explicitly select Groq browsing or render a page after ordinary extraction. Richer guidance is available through `read_skill_resource`. Package tests verify instructions and contracts, not natural model choice or scientific usefulness; historical runs retain the pinned v1 package.

Each package has concise and richer instructions, a simulated input/output example,
and a manifest with stable ID/version, file hashes, input description, required
capabilities and failure handling. Helper-backed packages additionally pin their
canonical input contract and helper provenance. The two helpers are project-owned.
Their manifests record source path, SHA-256, helper version, exact Pydantic
dependency, local-change status and the honest license state: redistribution terms
await the repository's license choice. No third-party helper or unverified
upstream license was copied.

The current concise instructions contain 270 words for evidence linking and 206 for appraisal; their richer variants contain 341 and 254. These are whitespace-delimited source counts, not model tokens. Load the active concise skill and relevant evidence first, with task-specific method guidance or peer feedback when useful. Keep richer variants available for controlled testing rather than loading the whole catalog into every role.

The [selected-model comparison](model-fit.md) tested v1.0.0 concise/rich variants, revised the concise instructions to v1.0.1, and tried source-specific peer feedback. Some errors improved, but none of the eight completed answers was accepted as a final scientific answer. Concise v1.0.1 was a provisional development default, not a scientifically qualified configuration. The v1.0.3 source-fidelity instructions have not undergone that model comparison. The report retains exact settings, actual usage and negative results; broader role qualification remains separate. `runtime_skills/qualification/packet.json` is the original offline fixture template, whose `not_run` field describes that template rather than the later experiment ledger.

## Integration and versioning

`task-planning` v2.0.0 is optional guidance for every research role. It helps
agents frame and revise complex work while leaving simple questions lightweight.
Its `save_plan` (create), `modify_plan` (targeted revision) and `read_plan` tools
use the existing versioned working-note store and deferred capability selection.
A first planning-tool selection or use also delivers the optional concise
template once; explicit activation remains available for persistent guidance. This instruction-only package has no
executable helper; existing helper-backed manifests keep their provenance
requirements. See [runtime planning](stage4-planning.md) for state, churn,
source-use and proof boundaries.

New mission runs pin the role catalog, load general-web-discovery for every role,
and keep evidence-linking instructions loaded where required. Optional study-appraisal instructions are advertised and
loaded only after the peer activates them; explicit release removes those
instructions and associated tools unless independently requested. Older saved
runs keep their eager loading policy. The [context-efficiency guide](stage4-context-efficiency.md)
owns activation, release and the separate model-quality qualification boundary.

The registry resolves required files inside the project root and verifies their SHA-256. A content change requires review, a skill/manifest version decision and updated hashes; it cannot silently replace instructions or helper code for a resumed attempt. The loader checks that role references request the available exact version. Existing attempts keep their recorded skill and helper hash. A helper output is an **internal candidate record** until the parent runtime adopts an appropriate shared wire contract; this package does not invent a competing API schema.

The V1 role/skill diagram remains a useful boundary sketch for this slice: multiple roles share one skill definition, while each attempt owns its mutable state. It should be revised if actual model qualification, runtime capability admission, or source-store integration shows a different ownership boundary. The [setup guide](setup.md) owns shared toolchains and installation. The [foundation evidence](foundation-evidence.md) records clean-install, UI and cloud checks separately from these skill fixtures.

## Verification and remaining qualification

Run the package fixtures with the pinned environment:

```sh
.venv/bin/python -m pytest -q tests/skills tests/skill_fit
```

The package tests exercise missing instructions, duplicate IDs, incompatible role
versions, unavailable optional specialist files/capabilities, incorrect Pydantic
versions, incomplete provenance and invalid source locators. They also check
separate appraisal dimensions and serial/parallel attempts with isolated mutable
state. An early raw missing-file error was improved to name the affected skill
and instruction variant. The experiment tests cover source/context preservation,
receipt consistency, private output handling and mocked provider transport.
Default checks make no live provider calls.

| Requirement | Established | Separate work |
| --- | --- | --- |
| Shared and specialist packages | Four versioned packages with synthetic examples; all roles share web discovery and optional planning, and two roles share evidence linking | Broader held-out scientific qualification |
| Stable references | Exact role/skill IDs and versions, path/hash validation, canonical `EvidencePacket` | Future public helper-output contracts if runtime callers need them |
| Dependency failures | Focused errors and optional-specialist isolation | Qualification of each added optional feature |
| Reuse and isolation | Private mutable state in serial and parallel attempts; retained skill/helper identities | Production persistence, traces and recovery |
| Provenance | Exact helper bytes, project-owned origin, dependency and license status | Repository redistribution license choice; verified origin/license for any future adaptation |
| Model fit experiment | Matched selected-model outputs, blind review and recorded partial/negative follow-ups | All nine roles, repeated held-out tasks, live tools and an integrated scientific pilot |

Both helpers remain version 1.0.0: their executable behavior is unchanged. Expanded
module descriptions changed their source bytes, so manifest hashes were updated.
Existing attempts retain their old versions and hashes; a later file edit must
not silently relabel historical evidence. The package checks and small live
experiment establish the scaffold and an informed next test, not a production
research runtime or automatic scientific validity.
