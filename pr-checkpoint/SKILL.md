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

A repo hosts more than one initiative over its life. The first writes `REQUIREMENTS.md`
and `SPEC.md` at the repo root, and a later one writes its own under
`docs/<initiative>/`. So the copy at the root is not always the one you want, and
grounding on the wrong initiative's spec produces work that is internally consistent
and aimed at the wrong system, which review does not catch.

Say which initiative this work is for, then resolve the file instead of assuming the
root copy:

1. List the candidates: the repo root copy, every `docs/*/` copy, and any other
   location this skill already tells you to check.
2. Read the `Initiative` field of each. That field decides, never the directory name,
   which is a slug somebody chose and can be stale or wrong.
3. Exactly one names your initiative: use it, and say which path you used.
4. None names it: STOP and tell the user what you found and where. Do not fall back to
   the root copy.
5. More than one names it: STOP and name every path. Two files claiming one initiative
   is a thing to report, not to settle by picking one.
6. A candidate carries no `Initiative` field: say so. Where it is the only candidate you
   may use it, and you must say you read a spec with no recorded owner.

When the user hands you an explicit path, use that path. Check its `Initiative` field
the same way, and on a mismatch stop and say whether a file for your initiative sits
somewhere else.

IDs pulled from the wrong file describe another workstream's contract, and the PR body carries them onward.

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

This skill is meant to be re-run on the same branch as you keep working, so **check for an existing PR first**. `gh pr create` errors out (`a pull request for branch ... already exists`) on the second run otherwise.

```bash
EXISTING=$(gh pr view "$BRANCH" --json number,url,state -q 'select(.state=="OPEN") | .url' 2>/dev/null)
```

- **A PR already exists** (`$EXISTING` is non-empty): the branch push in step 3 already updated it. Do NOT create a new one. Optionally refresh the PR body with `gh pr edit "$BRANCH" --body "..."` if the changes since last time warrant it. Reuse `$EXISTING` as the PR URL and continue.
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

If the project doesn't use draft PRs, drop `--draft`.

Capture the PR URL (the new one, or `$EXISTING`).

### 6. Run the review agents (advisory, non-blocking)

**Skip this entire step if invoked with `--no-review` / `--skip-review`, or if the review agents are not installed.** Go straight to step 7 and note in the report that the review was skipped.

This step pairs with a companion set of review agents, published separately as the
claude-review-agents pack (https://github.com/eranw2000/claude-review-agents):
`code-reviewer` (code quality), `deploy-guard` (pre-ship
checks) and `pr-validator` (runs the tests). Install them for this step to do
something, or plug in your own.

This step is the `review-round` skill in **checkpoint mode**
(`~/.claude/skills/review-round/SKILL.md`): author per-agent adversarial prompts
that name the 2-3 riskiest spots in this diff (a generic "review this diff" prompt
is a protocol violation), launch the three agents in one message, and post the PR
verdict comment ending `Review-round verdict: <CHECKPOINT-ADVISORY|CRITICALS-OPEN> @ <sha>`.
The fix loop stays optional at a checkpoint.

**A checkpoint may never post `CLEAN`.** That value certifies a completed round to
`/release`, and this one is advisory by contract with an optional fix loop, so it
has not earned it. `CHECKPOINT-ADVISORY` is the value for "nothing blocking here".
The failure it prevents is quiet: a checkpoint posts a verdict, the round later
is interrupted and posts nothing more, and that first verdict is still
standing when the release gate reads it. `CRITICALS-OPEN` is unchanged and stays
correct here, because a Critical found at a checkpoint should stop a later release
even though it does not stop this PR.

The advisory rules below are LOCAL to this
skill and always hold here, regardless of any future review-round edit:

Spawn the reviewers and collect their reports. These are advisory: a
checkpoint is the iterate stage, so **no finding from any of them refuses, blocks,
or rolls back the PR**. The hard gate is `/release`.

Launch them in a single message so they run in parallel (they are independent):

- **`code-reviewer`**: quality and maintainability on the branch diff against the base.
- **`deploy-guard`**: secrets/PII, config and prod hygiene, deploy-target safety,
  and the stack-specific checks for whatever stack it detects.
- **`pr-validator`**: finds and runs the test suite, returns a pass/fail verdict.

Each agent reads the branch itself (`git diff "$BASE"...HEAD`, `gh pr view`/`gh pr diff`).
Tell each one this is a checkpoint, not a release, so it reports rather than gates.

Handle their results as advice only:
- An agent failing, timing out, or finding Blockers must NOT undo the PR. Capture
  what it returned and move on.
- `pr-validator` on a half-finished branch will often return RETURN TO PROGRAMMER
  (failing or missing tests is expected mid-feature). Report it; do not act on it.
- If `deploy-guard` flags a real secret/PII Blocker, surface it loudly at the top of
  the report so it gets fixed before `/release`, but still leave the PR open.

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
