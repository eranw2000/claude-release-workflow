---
name: pr-checkpoint
description: Snapshot in-progress work as a GitHub PR and rebuild the local Docker container so you can test the feature branch on localhost. Does NOT merge, does NOT touch production, does NOT update README or project notes (premature, that happens at /release time). Use when you want to capture a checkpoint of feature work, open a PR for review, and test locally before deciding it's ready to ship. Triggers on "checkpoint", "open a PR", "draft a PR", "snapshot this", "let me test this locally".
---

# PR checkpoint

Capture in-progress work as a GitHub PR and rebuild local Docker so you can test the feature branch on localhost. This is the **iterate stage**, the work is NOT being released yet.

For the **ship-to-prod** verb, use `/release` instead. `/release` is what merges this PR (and any other open PRs) to main and triggers the production deploy. `/pr-checkpoint` only opens the PR and stages it for local testing.

## What this skill does NOT do

- **Does NOT update README.md or project notes**: those are part of the release, not the checkpoint. Update them in `/release`.
- **Does NOT merge** the PR.
- **Does NOT push to main**, deploy to production, or touch any host (Render / Vercel / Netlify / etc.).
- **Does NOT gate on the review agents or tests.** It runs the review agents as **advisory** reports (step 6) and folds their findings into the status block, but it never refuses, blocks, or rolls back the PR on what they find. The hard gate is `/release`.

## Flags

- `--no-review` (alias `--skip-review`): skip the review agents in step 6. Use it for a quick snapshot when you don't want to pay for the review pass (the test-runner agent running the full suite is the slow part). Everything else (commit, push, PR, Docker rebuild) runs as normal. Without the flag, the reviews run if the agents are installed.

## Step 0: Mode guard

First resolve the repo's real base branch (do NOT assume `main`, it may be `master` or `develop`), then confirm we are on a feature branch with a diff against it:

```bash
BASE=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
BASE=${BASE:-main}   # fall back to main if origin/HEAD isn't set
BRANCH=$(git rev-parse --abbrev-ref HEAD)
git diff "$BASE"...HEAD --quiet; echo "diff_exit=$?"
```

Use `$BASE` everywhere below instead of a literal `main`.

- **Current branch is the base branch (`$BASE`, i.e. `main` / `master`)**: bail with `you're on the base branch, nothing to checkpoint. Check out a feature branch first (git checkout -b feature/<name>).` Stop.
- **Feature branch but no diff against `$BASE`**: bail with `feature branch has no commits ahead of the base. Nothing to checkpoint yet.` Stop.
- **Feature branch with diff**: continue.

## Process

### 1. Assess the work

- `git status`: uncommitted work
- `git diff "$BASE"...HEAD`: all changes on the branch (three-dot, against the merge-base)
- `git log "$BASE"..HEAD --oneline`: commit history on the branch
- Any review-notes file your process uses (e.g. `COMMENTS.md`): surface unresolved blockers as a warning, do not refuse (this is a checkpoint, not a release)
- A spec or requirements file if your project keeps one: reference the relevant IDs in the PR body

### 2. Commit any uncommitted work

If there are uncommitted changes that belong in this checkpoint:
- Stage them specifically by name (never `git add -A` or `git add .`)
- Refuse to commit secrets or `.env` files
- Commit with a clear message
- Append your commit trailer if your workflow uses one

If the working tree is already clean, skip this step.

### 3. Push the feature branch

**Preflight (mandatory):** before pushing, run the harness-files protection check from `~/.claude/skills/_shared/harness-files-protection.md`. If the target remote is one you do not own personally, verify no Claude / AI-assistant harness files (`CLAUDE.md`, `MEMORY.md`, `AGENTS.md`, `.claude/`, etc.) are tracked, and that `.gitignore` covers the full set. If any harness file is tracked on a client remote, STOP and surface the file list. Never push harness files to a repo you do not own.

```bash
git push -u origin "$BRANCH"
```

If a push is rejected because the remote moved, fetch + rebase from the remote and retry. A rebase rewrites your local branch, so the retry of an already-published feature branch needs `git push --force-with-lease` (never a plain `--force`, which clobbers a teammate's push). `--force-with-lease` on a feature branch is the one carve-out from the "never force-push" rule; it never applies to the base branch.

### 4. Write the PR body

```markdown
## What
One sentence explaining what this PR does.

## Why
Brief context. If a requirements file exists, reference the IDs this PR addresses.

## Status
**Checkpoint**: opened for local testing and review. Not ready to merge.
(Update this when ready: switch to "Ready to merge" and run /release.)

## Changes
- Bullet points of specific changes, grouped by area
- Files deleted or renamed

## Test plan
- [ ] Items the reviewer (or future-you at /release time) should verify
```

PR title under 70 characters. Mark as draft if your repo conventions support it (`gh pr create --draft`).

### 5. Create or update the PR

This skill is meant to be re-run on the same branch as you keep working, so **check for an existing PR first**. `gh pr create` errors out (`a pull request for branch ... already exists`) on the second run otherwise.

```bash
EXISTING=$(gh pr view "$BRANCH" --json url,state -q 'select(.state=="OPEN") | .url' 2>/dev/null)
```

- **A PR already exists** (`$EXISTING` is non-empty): the branch push in step 3 already updated it. Do NOT create a new one. Optionally refresh the body with `gh pr edit "$BRANCH" --body "..."`. Reuse `$EXISTING` as the PR URL and continue.
- **No PR yet**: create one with a HEREDOC body, pinning the base to the resolved `$BASE`:

```bash
gh pr create --draft --base "$BASE" --head "$BRANCH" --title "<title under 70 chars>" --body "$(cat <<'EOF'
## What
...
## Why
...
## Status
**Checkpoint**: opened for local testing and review.
## Changes
...
## Test plan
- [ ] ...
EOF
)"
```

If the project does not use draft PRs, drop `--draft`.

Capture the PR URL (the new one, or `$EXISTING`).

### 6. Run the review agents (advisory, non-blocking)

**Skip this entire step if invoked with `--no-review` / `--skip-review`, or if the review agents are not installed.** Go straight to step 7 and note in the report that the review was skipped.

This step pairs with a companion set of review agents (a code-quality reviewer, a pre-ship deploy guard, and a test-runner / PR validator). If you have them installed, spawn them and collect their reports. These are advisory: a checkpoint is the iterate stage, so **no finding from any of them refuses, blocks, or rolls back the PR**. The hard gate is `/release`.

Launch the agents in a single message so they run in parallel (they are independent). Each agent reads the branch itself (`git diff "$BASE"...HEAD`, `gh pr view` / `gh pr diff`). Tell each one this is a checkpoint, not a release, so it reports rather than gates.

Handle their results as advice only:
- An agent failing, timing out, or finding blockers must NOT undo the PR. Capture what it returned and move on.
- A test-runner agent on a half-finished branch will often report failing or missing tests (expected mid-feature). Report it; do not act on it.
- If the deploy guard flags a real secret/PII blocker, surface it loudly at the top of the report so it gets fixed before `/release`, but still leave the PR open.

A matching set of agents is published separately at https://github.com/eranw2000/claude-review-agents (code-reviewer, deploy-guard, pr-validator). Install them for this step to do something, or plug in your own.

### 7. Rebuild local Docker

Follow `~/.claude/skills/_shared/local-docker-rebuild.md`: detect the running local container, rebuild from the current working tree (which is on the feature branch), verify it is up. Skips silently if the project does not use Docker.

### 8. Report

Single status block:
- PR URL
- Local URL to test against (usually `http://localhost:<port>` from compose config; check the rebuild output for the actual port)
- Docker rebuild result line
- **Review summary**: one line per agent with its findings count, if the reviews ran. List any deploy-guard blockers or secret/PII findings explicitly; link the full reports the agents returned. If invoked with `--no-review` or the agents are not installed, replace this line with `Review skipped.`
- Reminder: `These reviews are advisory. When ready to ship, run /release, that's the hard gate that merges this PR and deploys to prod.`

## Guardrails

- Never push directly to `main`. This skill only ever pushes the feature branch.
- Never merge. Merging is `/release`'s job.
- Never update README or project notes here. They are release-time artifacts.
- Refuse to commit secrets or `.env` files.
- Refuse to push Claude / AI-assistant harness files (`CLAUDE.md`, `MEMORY.md`, `AGENTS.md`, `.claude/`, `.cursor*`, `.aider*`, `.windsurf*`, `.github/copilot-instructions.md`) to any remote you do not own. See `~/.claude/skills/_shared/harness-files-protection.md`.
- Docker rebuild is best-effort, a failure here must NOT roll back the PR creation. Report and continue.
- The review agents (step 6) are advisory only. Never refuse, block, or roll back the PR on their findings, and never let an agent crash or a failing test stop the checkpoint. Surface secret/PII blockers loudly, but leave the PR open. The hard gate is `/release`.
