# Working in this repository

Read [README.md](README.md) to understand the implemented foundation and its proof
limits. Read [setup](docs/development/setup.md) before installing or checking code;
it owns supported toolchains and the shared local/CI/cloud commands. Read the
[module map](docs/development/modules.md) before adding an entry point.

- `src/ora/contracts/models.py` is the wire-schema authority. Use
  `scripts/dev generate`; never hand-edit generated schemas or client types.
- `runtime_skills/` contains capabilities used by research agents. The repository's
  Codex development skills live in `.agents/skills/`. Inspect a skill's `SKILL.md`
  before using it; personal Mac skills are not assumed in cloud tasks.
- `scripts/dev check` runs the default offline foundation checks. Providers and
  E2B are optional and are never activated implicitly. A targeted Python check can
  use `.venv/bin/python -m pytest <path>` after setup.
- Scientific peers choose interpretations and follow-up work. Mechanical host
  code owns transactions, versions, resources and replay. An invalid action
  affects its own branch; do not invent a global approval or fixed research cap.
- Explain decisions through the problem, alternatives, evidence, trade-offs and
  observable checks. Keep findings separate from diagnostics. Do not require or
  invent private model reasoning to establish explainability.
- Fixtures are synthetic unless clearly marked otherwise. Preserve source IDs,
  versions, locators, hashes and counterevidence. Citation shape does not prove
  that a passage supports a claim; offline checks do not prove a live mission.
- Never print `.env` values. Keep credentials, personal identifiers, private
  tracker links, raw provider responses and local run state out of tracked files.
  Read configuration names/status only. Live qualification needs its own explicit
  bounded invocation and saved proof; do not silently switch model or account.
- Before publishing, review the exact staged file list and diff. Preserve local
  artifacts and unrelated edits. Record the actual commit and checks in a private
  tracker receipt, with sanitized technical evidence suitable for the repository.

The current project has no production mission host. Placeholder commands say they
are unavailable; do not turn a sample UI or contract fixture into an uptime claim.
