# Runtime roles and skills: runtime-skill vertical slice

This package supplies a small, runnable role-to-skill skeleton. It is separate from Codex development skills and does not implement nine independent runtimes. The nine names in `roles/roles.json` match Section 7 of the current Linear research plan, read on 2026-10-08. Role manifests keep stable IDs, names, plan sections and skill references; they do not copy shared collaboration rules. The central `ora.contracts.models.EvidencePacket` is the input schema authority. The selected runtime, model/provider setup, app UI and standard project command wrappers belong to other Stage 1 owners.

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

`evidence-linking` v1.0.0 is shared by **Evidence preparation and synthesis** and **Independent methods reviewer and critic**. It requires an exact source locator, preserves source-copy identity and access surface, distinguishes support, challenge and context, and leaves semantic entailment pending. `study-appraisal` v1.0.0 belongs to the critic role. It asks the agent to choose a design-appropriate method and keep reporting completeness, risk of bias, applicability and precision separate for one outcome/comparison/time point. The helper checks presence and lineage; it does **not** choose the method or certify study validity.

Each package has concise and richer instructions, a synthetic input/output example, and a manifest with stable ID/version, file hashes, canonical input reference, required capabilities, failure handling and helper provenance. The two helpers are project-owned. Their manifests record source path, SHA-256, helper version, exact Pydantic dependency, local-change status and the honest license state: redistribution terms await the repository's license choice. No third-party helper or unverified upstream license was copied.

The concise evidence-linking file is 123 whitespace-delimited words; its richer counterpart is 226. The appraisal files are 92 and 254 words respectively. These are **offline structure measurements**, not model token counts or proof of fit. `runtime_skills/qualification/packet.json` keeps the same fictional source task for both variants, names structural checks and leaves the selected model/settings and outcome as `not_run`. A later matched selected-model comparison must measure actual source support, false blocks, severe errors, coverage, tokens and latency before choosing a default variant. It should not infer value from a successful import or a fluent example.

## Integration and versioning

The registry resolves required files inside the project root and verifies their SHA-256. A content change requires review, a skill/manifest version decision and updated hashes; it cannot silently replace instructions or helper code for a resumed attempt. The loader checks that role references request the available exact version. Existing attempts keep their recorded skill and helper hash. A helper output is an **internal candidate record** until the parent runtime adopts an appropriate shared wire contract; this package does not invent a competing API schema.

The V1 role/skill diagram remains a useful boundary sketch for this slice: multiple roles share one skill definition, while each attempt owns its mutable state. It should be revised if actual model qualification, runtime capability admission, or source-store integration shows a different ownership boundary. The root repository skeleton, lockfiles, package-manager commands, React/desktop boundaries and clean-install proof are intentionally parent-owned and are not established by these files.

## Offline verification and acceptance boundary

Run the focused check with the selected project environment:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/skills -v
```

The first run found that a missing required instruction file produced only a raw path error. The loader now names the affected skill and instruction variant; the final run passed all nine tests. Tests also cover duplicate IDs, incompatible role versions, missing optional specialist files/capability, wrong Pydantic version, incomplete helper provenance, exact canonical packet input, invalid locators, separate appraisal dimensions, qualification-packet references and parallel attempt-state isolation. No selected model, provider, live source, E2B job or desktop UI was called.

The current root `pyproject.toml` config also ran both owned suites together with `.venv/bin/pytest -q tests/benchmarks tests/skills`: **24 passed in 0.17s**. Ruff passed on the owned Python source/tests. This is local fixture proof in the selected `.venv`, not a fresh-install or scientific-performance result.

```text
Ran 9 tests in 0.054s

OK
```

| runtime-skill requirement | This slice proves | Still needed elsewhere |
| --- | --- | --- |
| Representative shared and specialist package | Two versioned manifests, instructions, examples and project-owned helpers; two roles share evidence linking | Qualify actual scientific benefit on the selected OSS model |
| Stable references and generated copies | Exact role/skill IDs and versions, path/hash checks, canonical Pydantic `EvidencePacket`; no generated duplicate schema | Parent's schema generator/check wrapper and installed clean-checkout proof |
| Missing/duplicate/incompatible dependencies | Offline tests show focused error or optional specialist isolation | Parent's selected runtime and package/lockfile version checks |
| Reuse and isolation | Separate mutable state in serial and parallel role attempts; skill/helper version retained | Durable runtime trace and restart proof |
| Helper provenance | Path, SHA-256, project-owned origin, dependency and pending redistribution license in manifests | Repository license decision; any future adapted helper needs verified upstream license and pin |
| Module/helper scaffold | Ownership, caller, input/output, state and side effects listed above; helpers are directly importable | Parent's setup/mission/source/store/export entrypoints and UI/runtime integration |

This is a functional package and offline fixture proof, not completion of the full runtime-skill issue. In particular, no lockfile, fresh-checkout installation, supported Node/Desktop toolchain or selected-model fit result is claimed here.
