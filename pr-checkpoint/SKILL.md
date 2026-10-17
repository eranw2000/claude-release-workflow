---
model: opus
name: pr-checkpoint
description: Snapshot in-progress work as a GitHub PR and rebuild the local Docker container so you can test the feature branch on localhost. Does NOT merge, does NOT touch production, does NOT update README or CLAUDE.md (premature; that happens at /release time). Use when you want to capture a checkpoint of feature work, open a PR for review, and test locally before deciding it's ready to ship. Triggers on "checkpoint", "open a PR", "draft a PR", "snapshot this", "let me test this locally".
---

# PR checkpoint

Capture in-progress work as a GitHub PR and rebuild local Docker so you can test the feature branch on localhost. This is the **iterate stage**: the work is NOT being released yet.

For the **ship-to-prod** verb, use `/release` instead. `/release` is what merges this PR (and any other open PRs) to main and triggers the production deploy. `/pr-checkpoint` only opens the PR and stages it for local testing.

## What this skill does NOT do

- **Does NOT update README.md or project CLAUDE.md**; those are part of the release, not the checkpoint. Update them in `/release`.
- **Does NOT merge** the PR.
- **Does NOT push to main**, deploy to production, or touch Render / Vercel / Netlify.
- **Does NOT gate on the review agents or tests.** It runs the review agents (`code-reviewer`, `deploy-guard` and `pr-validator`) as **advisory** reports (step 6) and folds their findings into the status block, but it never refuses, blocks, or rolls back the PR on what they find. The hard gate is `/release`.

## Flags

- `--no-review` (alias `--skip-review`): skip the review agents in step 6. Use it for a quick snapshot when you don't want to pay for the three-agent pass (pr-validator running the full suite is the slow part). Everything else (commit, push, PR, Docker rebuild) runs as normal. Without the flag, the reviews run.

## Step 0: Mode guard

First resolve the repo's real base branch (do NOT assume `main`, it may be `master` or `develop`), then confirm we're on a feature branch with a diff against it:

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
- `COMMENTS.md` if present: surface unresolved Blockers as a warning (don't refuse; this is a checkpoint, not a release)
- `REQUIREMENTS.md` and `SPEC.md` if they exist: reference FR / NFR / C IDs in the PR body

### Find the spec that belongs to your initiative

Pick the REQUIREMENTS.md / SPEC.md whose `Initiative` field names this work, and stop when none or several do.

**Read `~/.claude/skills/pr-checkpoint/reference/1b-find-the-spec.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

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

If a push is rejected because the remote moved, fetch + rebase from the remote and retry. A rebase rewrites your local branch, so the retry of an already-published feature branch needs `git push --force-with-lease` (never a plain `--force`, which clobbers a teammate's push). `--force-with-lease` on a feature branch is fine and is the one carve-out from the "never force-push" guardrail; it still never applies to the base branch.

### 4. Write the PR body

```markdown
## What
One sentence explaining what this PR does.

## Why
Brief context. If REQUIREMENTS.md exists, reference the IDs (FR-N, NFR-X-N) this PR addresses.

## Status
**Checkpoint**: opened for local testing and review. Not ready to merge.
(Update this when ready: switch to "Ready to merge" and run /release.)

## Changes
- Bullet points of specific changes, grouped by area
- Files deleted or renamed
- Link the OpenSpec change at `openspec/changes/<change-name>/` if applicable

## Test plan
- [ ] Items the reviewer (or future-you at /release time) should verify
```

PR title under 70 characters. Mark as draft if your repo conventions support it (`gh pr create --draft`).

### 5. Create or update the PR

Check for an open PR on this branch first; create a draft PR against `$BASE` only when none exists, and capture its URL.

**Read `~/.claude/skills/pr-checkpoint/reference/5-create-or-update-the-pr.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

### 6. Run the review agents (advisory, non-blocking)

Unless `--no-review`, run the review agents as advisory reports; never post `CLEAN`, and never block or roll back the PR on a finding.

**Read `~/.claude/skills/pr-checkpoint/reference/6-review-agents.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

### 7. Rebuild local Docker

Skip this step when the repo has no Docker setup, and say in the report that you
skipped it and why.

Follow `~/.claude/skills/_shared/local-docker-rebuild.md`: detect the running local container, rebuild from the current working tree (which is on the feature branch), verify it's up.

### 8. Report

Single status block:
- PR URL
- Local URL to test against (usually `http://localhost:<port>` from compose config; check the rebuild output for the actual port)
- Docker rebuild result line
- **Review summary**: one line per reviewer, naming any that could not run: `code-reviewer` (N Critical / N Warning / N Suggestion), `deploy-guard` (N Blocker / N Warning / N Note), `pr-validator` (verdict + N passed / N failed). List any deploy-guard Blockers or secret/PII findings explicitly; link the full reports the agents returned. If invoked with `--no-review` or the agents are not installed, replace this line with `Review skipped.`
- Reminder: `These reviews are advisory. When ready to ship, run /release, which is the hard gate that merges this PR and deploys to prod.`

## Guardrails

- Never push directly to `main`. This skill only ever pushes the feature branch.
- Never merge. Merging is `/release`'s job.
- Never update README or CLAUDE.md here. They're release-time artifacts.
- Refuse to commit secrets or `.env` files.
- Refuse to push Claude / AI-assistant harness files (`CLAUDE.md`, `MEMORY.md`, `AGENTS.md`, `.claude/`, `.cursor*`, `.aider*`, `.windsurf*`, `.github/copilot-instructions.md`) to any remote you do not own. See `~/.claude/skills/_shared/harness-files-protection.md`.
- Docker rebuild is best-effort; a failure here must NOT roll back the PR creation. Report and continue.
- The review agents (step 6) are advisory only. Never refuse, block, or roll back the PR on their findings, and never let an agent crash or a failing test stop the checkpoint. Surface secret/PII Blockers loudly, but leave the PR open. The hard gate is `/release`.
