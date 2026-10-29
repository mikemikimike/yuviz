---
description: Commit the feature and open a PR
allowed-tools: Bash, Read
---

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

Do this yourself — no agents, it is a few shell calls.

1. `git status --short` and `git diff --stat`. Show me the file list and stop here if anything unrelated to the feature is staged.
2. Confirm that `06-test-report.md` shows zero failures outside its `## Pre-existing on base` section (lesson 54), and that `05-review.md` and `05-review-independent.md` have no open blocking/high findings. `05-review-independent.md` is required: if it is missing, run `/sdlc:independent` first. If any of this fails, tell me and stop.
3. Create `feature/<slug>` if we are still on the default branch. Stage the code **and** the `.sdlc/<slug>/` artifacts by name, never `git add -A` (unrelated untracked files stay out).
4. With the files staged, run the repo-wide scans that read `git ls-files` (they cannot see untracked files, so the fan-in run may have missed them): `./venv/bin/python -m pytest -q tests/test_env_catalogue.py`. These need no services. A failure blocks the commit; fix it (artifacts too: an example DSN uses `<password>`, never a literal one).
5. Write the PR body to `<dir>/pr-body.md`: the PRD's Problem section, the design's Approach paragraph, the acceptance criteria as a checklist, the test command with its result, and each review's verdict (review, security, independent). Under 40 lines. Stage it with the other artifacts.
6. Commit and push. Open the PR with `gh pr create --body-file <dir>/pr-body.md --assignee @me` against `base_branch` from `state.json` (`state.base` is a commit SHA, not a branch). If `state.json` has no `base_branch`, stop and ask me which branch to target; never assume `main`. Under `/sdlc:auto` the approver escalates this, never picks a branch.
7. Wait for CI: `gh pr checks <n> --watch`. Report a check that is red on the base branch too as pre-existing, with the run link. For any other red check:
   - Fix the code under test. Never edit a test, scan, allowlist or CI config to turn a check green. If that is the only way, stop and tell me.
   - Before pushing, run `/sdlc:review` on the fix commit's diff, and `/sdlc:security` too if the fix touches auth, tenant scoping, credentials or an unauthenticated path. A blocking/high finding stops the ship.
   - Push, then update the verdict lines with `gh pr edit <n> --body-file <dir>/pr-body.md`, adding one line per fix commit and the review that covered it.
8. Print the PR URL.
9. Once the PR is merged, tell me to run `/sdlc:qa` — the build and review stages test what we thought to test; QA is what finds the rest.

Ask me before pushing if $ARGUMENTS does not contain `--yes`.
