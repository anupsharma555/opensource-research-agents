# Runtime roles and skills

This package supplies a small, runnable role-to-skill skeleton. It is separate from Codex development skills and does not implement nine independent runtimes. The nine names in `roles/roles.json` match Section 7 of the current Linear research plan, read on 2026-10-08. Role manifests keep stable IDs, names, plan sections and skill references; they do not copy shared collaboration rules. The central `ora.contracts.models.EvidencePacket` is the input schema authority. The [module map](modules.md) identifies the callers and ownership of the runtime, model adapter, app and shared development commands.

## Implemented boundary

| Component | Caller and input → output | Dependencies, state, effects | Lowest proof |
| --- | --- | --- | --- |
| `roles/roles.json` and `runtime_skills/registry.json` | Package loader; nine plan role IDs and two skill manifest paths → validated role-to-skill references | No external service or durable state; versioned file references | Exact names, duplicate and incompatible-reference tests |
| `ora.skills.load_registry` | Host/parent check wrapper; project root and available capability names → loaded skills plus scoped unavailable reasons | Canonical Pydantic contracts and the selected environment; read-only files and imports | Missing required file, optional specialist, version and dependency tests |
| `ora.skills.prepare_claim_link` | Evidence preparation or critic; canonical `EvidencePacket`, claim, existing locator and relation → candidate link with artifact ID/version/hash and pending semantic review | Project-owned helper, no durable write or network; requires a source-read capability from the eventual host | Example output, missing-locator and wrong-contract tests |
| `ora.skills.prepare_result_appraisal` | Independent methods reviewer; canonical packet and exact result/method/judgments → result-scoped appraisal record pending independent check | Project-owned helper, no durable write or network; specialist capability may be unavailable without disabling shared evidence linking | Four-judgment example, unknown method detail and malformed-record tests |
| `SkillRegistry.start_skill_attempt` | Role instance; declared role/skill and attempt ID → new attempt-local mutable state with pinned skill/helper identities | In-memory per attempt; no shared session or durable effect | Two roles use the same shared skill in serial and parallel attempts with separate state |

The `source_read_by_id` and `study_appraisal` capability names are host-facing requirements, not claims that source retrieval or scientific appraisal tools already exist. The loader reports a missing optional specialist as unavailable and preserves the shared skill. A missing required shared package or corrupt provenance produces a focused error. Parent-owned command wrappers should call these APIs; there is no second workflow engine here.

## The two representative packages

`evidence-linking` v1.0.1 is shared by **Evidence preparation and synthesis** and **Independent methods reviewer and critic**. It requires an exact source locator, preserves source-copy identity and access surface, distinguishes support, challenge and context, and leaves semantic entailment pending. `study-appraisal` v1.0.1 belongs to the critic role. It asks the agent to choose a design-appropriate method and keep reporting completeness, risk of bias, applicability and precision separate for one outcome/comparison/time point. The helper checks presence and lineage; it does **not** choose the method or certify study validity.

Each package has concise and richer instructions, a synthetic input/output example, and a manifest with stable ID/version, file hashes, canonical input reference, required capabilities, failure handling and helper provenance. The two helpers are project-owned. Their manifests record source path, SHA-256, helper version, exact Pydantic dependency, local-change status and the honest license state: redistribution terms await the repository's license choice. No third-party helper or unverified upstream license was copied.

The current concise instructions contain 203 words for evidence linking and 206 for appraisal; their richer variants contain 226 and 254. These are whitespace-delimited source counts, not model tokens. Load the active concise skill and relevant evidence first, with task-specific method guidance or peer feedback when useful. Keep richer variants available for controlled testing rather than loading the whole catalog into every role.

The [selected-model comparison](model-fit.md) tested v1.0.0 concise/rich variants, revised the concise instructions to v1.0.1, and tried source-specific peer feedback. Some errors improved, but none of the eight completed answers was accepted as a final scientific answer. Concise v1.0.1 is a provisional development default, not a scientifically qualified configuration. The report retains exact settings, actual usage and negative results; broader role qualification remains separate. `runtime_skills/qualification/packet.json` is the original offline fixture template, whose `not_run` field describes that template rather than the later experiment ledger.

## Integration and versioning

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
| Shared and specialist packages | Two complete versioned manifests, examples and project-owned helpers; two roles share evidence linking | Broader held-out scientific qualification |
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
