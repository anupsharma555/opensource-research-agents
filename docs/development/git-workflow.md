# Git workflow

The project synchronizes reviewed source with its public GitHub repository.
Credentials, research state, and access receipts stay in private local storage
or the private project tracker.

## Normal work

```sh
git status --short
git fetch origin
git switch -c feature/descriptive-change
scripts/dev check
git diff --check
git diff
git add path/to/reviewed-file
git diff --cached
git commit -m "Describe the observable change"
git push -u origin feature/descriptive-change
```

Open a pull request, review its behavior and test evidence, then integrate the
approved change. Fetching remote history does not replace local files. Use
`git pull --ff-only` only on a clean branch that should simply advance. If histories
diverge, preserve the commits and deliberately merge or rebase the feature branch;
do not force-push or reset unrelated work.

The initial publication is an explicitly reviewed foundation on `main`.
Subsequent changes use focused branches. GitHub Actions runs the same
offline `scripts/dev check` entry point; no provider secret is supplied.

## Concurrent work and recovery

Assign disjoint paths or isolated clones/worktrees with a recorded starting
commit. Each worker states files changed, generated outputs, checks and pending
integration inputs. Do not let two workers mutate the same Git index concurrently.
For an overlap, inspect both intentions, resolve the source, regenerate derived
files and rerun affected checks. Preserve unrelated uncommitted edits throughout.

A remote outage or authentication failure leaves local commits intact. Record the
failed operation, continue local checks and retry the same intended push after
access returns. Do not switch accounts, replace credentials or expand repository
visibility to bypass a failure.

## Publication and fresh-clone checks

- Confirm the intended repository and visibility through GitHub before publishing.
- Run `scripts/check-publication.py` with the project Python after staging and
  review the exact index blobs. This pattern check supplements manual review; it is not a
  comprehensive secret detector and never reads `.env`.
- Verify ignore rules using synthetic `.env`, local-run and build-output paths.
  Credentials, raw run artifacts, local toolchains and personal editor settings
  stay outside Git. Lockfiles, generated contracts and permitted fixtures remain.
- Clone the remote into a new directory, confirm the commit and tree IDs,
  run `scripts/dev bootstrap`, `scripts/dev setup` and `scripts/dev check`, and
  verify lock hashes remain unchanged.
- Keep intentional broken-contract tests in an isolated verification branch or
  checkout. Record the actionable failure and restore the original source before
  ordinary integration; do not weaken the contract to make the test pass.

Observed integration and two-worker conflict results are recorded in the
[foundation evidence](foundation-evidence.md), separately from this procedure.
