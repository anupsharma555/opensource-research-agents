# Codex cloud development

Use the GitHub checkout for a **coding and testing environment**. It is
separate from an always-on research host, the Mac app, and an E2B experiment job.
This repository needs no provider credential for setup or offline checks.

## Shared setup contract

Prepare the environment from the intended published commit. Run:

```sh
scripts/dev bootstrap
scripts/dev setup
scripts/dev check
.venv/bin/python .agents/skills/os-check-foundation/scripts/check.py --mode smoke
```

Use the same Python, Node, package-manager versions and lockfiles as
[local development](setup.md). Permit only required package/download hosts during
setup: PyPI and its file host, npm, the official Node distribution host, and GitHub
for the repository and any uv-managed interpreter download. No local `.env`, VPN,
browser session or personal Mac skill should be copied into this environment.
Keep the environment private to its intended user.

For current Codex Cloud, review the prepared environment, publish it, then start a
new task to verify the published setup. The task must discover
`os-check-foundation` from the repository, load its instructions and invoke its
helper. Record the exact commit, platform/toolchain, helper receipt and returned
artifact or diff. Mere presence of `SKILL.md` is not proof of discovery.

The first published-task check found an older snapshot than the requested commit.
Verify the actual revision at task start; fetch the specified revision and use an
isolated clean checkout when necessary, preserving existing task work. See the
[observed cloud evidence](foundation-evidence.md#published-cloud-verification).
Repository skill discovery through AGENTS.md worked even though the session's
skill catalog had no entry. Do not describe that as verified menu integration.

## Platform and failure boundaries

Linux cloud tasks can validate Python and the React build/component tests. The
native Mac bundle still requires a Mac. Loopback restrictions, unavailable package
downloads and missing optional credentials are separate conditions: report the
specific affected check and retain other available work. Offline checks should
not attempt a live provider request or depend on optional E2B access.

Test an uncached offline install to obtain a real dependency-unavailable
diagnostic; recover by allowing the intended registry during setup, not by
disabling validation. Confirm that missing provider credentials do not change
offline results. Retain outputs through version control or returned artifacts;
environment preparation is not evidence that every later task has the same files.
If an output download mount is unavailable, return a sanitized artifact's complete
contents and SHA-256 so a reviewer can save and verify it. The observed task used
this path successfully; it did not establish a fixed saved-state retention period.

## Current versus legacy products

The official documentation inspected on 2026-10-08 distinguishes published current
cloud environments from the legacy Codex Cloud experience. Current cloud tasks
retain their own working state; published environment updates affect new tasks.
The actual available product and observed result must be recorded in
[foundation evidence](foundation-evidence.md). Do not call a local process, GitHub
Actions run or legacy CLI response a verified current cloud task.

References: [Cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments),
[legacy environment guide](https://learn.chatgpt.com/docs/environments/cloud-environment),
[repository skill discovery](https://learn.chatgpt.com/docs/build-skills).
