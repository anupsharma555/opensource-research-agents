---
name: os-check-foundation
description: Verify this research-agent repository's offline foundation and explain the evidence from its contracts, benchmark packets, skills and React checks. Use for scoped local or cloud development checks, not live scientific qualification.
metadata:
  version: 0.1.1
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

The receipt records source cleanliness before and after the checks, a SHA-256
of the tracked Git diff, and whether untracked paths were present. It stores
neither diff contents nor untracked path names. Checks remain useful in a dirty
checkout, but `commitOnlyProofValid` is true and `commit` names the tested commit
only when the check passed and the source was clean both before and after it.
These snapshots do not detect a file changed and restored during the check. If
the condition is not met, `commit` is `uncommitted`; `headCommit` is context,
not commit-only proof.

Inspect the receipt and any failed command. Distinguish a malformed contract,
a setup or localhost-access problem, a failing behavioral test, and an untested
scientific outcome. Repair the affected layer; do not weaken tests to produce a
passing status or freeze unrelated development.

Report the tested commit only when its receipt says commit-only proof is valid.
Otherwise report the working-tree result and its source-state flags. Include the
skill version, commands and observed results, plus
what was not exercised. Link useful source or test locations and explain the
reason for a change in plain language. A successful fixture or React build is
not proof of live retrieval, scientific advantage, or long-running uptime.
