---
name: os-check-foundation
description: Verify this research-agent repository's offline foundation and explain the evidence from its contracts, benchmark packets, skills and React checks. Use for scoped local or cloud development checks, not live scientific qualification.
metadata:
  version: 0.1.0
---

# Check the research-agent foundation

Read `docs/development/setup.md` when setting up a clean checkout. It owns the
runtime versions, lockfiles and commands. This skill is portable with the repository;
it does not require a personal Mac skill or a provider credential.

Run this skill's helper from the repository root:

```sh
.venv/bin/python .agents/skills/os-check-foundation/scripts/check.py --mode full
```

Use `--mode smoke` for a quick contract, benchmark and skill-reference check.
The helper calls the shared project modules and writes a receipt under ignored
`.local/checks/`. It never reads `.env` or starts a research mission.

Inspect the receipt and any failed command. Distinguish a malformed contract,
a setup or localhost-access problem, a failing behavioral test, and an untested
scientific outcome. Repair the affected layer; do not weaken tests to produce a
passing status or freeze unrelated development.

Report the tested commit, skill version, commands and observed results, plus
what was not exercised. Link useful source or test locations and explain the
reason for a change in plain language. A successful fixture or React build is
not proof of live retrieval, scientific advantage, or long-running uptime.
