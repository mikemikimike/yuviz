---
description: Commit the feature and open a PR
allowed-tools: Bash, Read
---

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

Do this yourself — no agents, it is a few shell calls.

1. `git status --short` and `git diff --stat`. Show me the file list and stop here if anything unrelated to the feature is staged.
2. Confirm `06-test-report.md` shows zero failures, and that `05-review.md` and `05-review-independent.md` (if present) have no open blocking/high findings. If not, tell me and stop.
3. Create `feature/<slug>` if we are still on the default branch. Stage the code **and** the `.sdlc/<slug>/` artifacts by name, never `git add -A` (unrelated untracked files stay out).
4. With the files staged, run the repo-wide scans that read `git ls-files` (they cannot see untracked files, so the fan-in run may have missed them): `./venv/bin/python -m pytest -q tests/test_env_catalogue.py`. These need no services. A failure blocks the commit; fix it (artifacts too: an example DSN uses `<password>`, never a literal one).
5. Commit, push, and open the PR with `gh` against `base_branch` from `state.json` (`state.base` is a commit SHA, not a branch; default `main`), with `--assignee @me`.
6. Wait for CI: `gh pr checks <n> --watch`. Fix any red check on the branch and push again before reporting the run as shipped. Report a check that is red on the base branch too as pre-existing, with the run link.
7. PR body: the PRD's Problem section, the design's Approach paragraph, the acceptance criteria as a checklist, the test command with its result, and each review's verdict. Under 40 lines.
8. Print the PR URL.
9. Once the PR is merged, tell me to run `/sdlc:qa` — the build and review stages test what we thought to test; QA is what finds the rest.

Ask me before pushing if $ARGUMENTS does not contain `--yes`.
