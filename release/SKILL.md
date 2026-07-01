---
name: release
description: Ship to production. The single "ship it" verb for any project: handles both PR mode (merges open PRs targeting main) and trunk mode (commits straight to main). Updates README.md + project notes, pushes to all remotes, verifies the auto-deploy (Render / Vercel / etc.), and rebuilds the local Docker container so localhost matches prod. Triggers on "release", "ship it", "deploy", "ship to prod", "merge and release", "push to prod".
argument-hint: "[optional commit message override]"
---

# Release (ship to production)

The single "ship it" verb. This skill takes whatever state the repo is in (open PRs on feature branches, commits sitting on main, uncommitted work in the working tree) and ships it to production atomically.

For the **iterate / checkpoint stage** (open a PR for local testing, no merge, no prod deploy), use `/pr-checkpoint` instead.

## Step 0: Detect release shape

Inspect the repo state and pick the path. Run:

```bash
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "branch=$CURRENT_BRANCH"
git status --porcelain
gh pr list --base main --state open --json number,title,headRefName,isDraft 2>/dev/null
```

Decide which of these you are in:

**Path A: PR mode (one or more open PRs targeting main):**
- There are open non-draft PRs against `main`.
- You will merge them (with confirmation if more than one or if any are draft).
- After merge, switch to main and continue.

**Path B: Trunk mode (on main, work to push):**
- Current branch is `main` / `master`.
- There is uncommitted work in the working tree, OR commits sitting locally that are not pushed.
- You will commit (if needed) and push.

**Path C: Mixed (on a feature branch with no open PR):**
- Bail with: `You're on a feature branch but no open PR exists. Either:`
  - `(a) Run /pr-checkpoint first to open a PR, then /release to merge and ship; or`
  - `(b) Check out main and merge the branch yourself if you want a manual flow.`
- Stop. Do not push or merge.

**Path D: Nothing to ship:**
- On main, clean working tree, nothing unpushed, no open PRs.
- Bail with: `Nothing to release.` Stop.

State the path clearly in your first message so the user sees which way this is going.

## Path A: PR mode

### A1. List the PRs

Show each open non-draft PR against main with: number, title, head branch, mergeability. Surface drafts separately as "skipping unless confirmed."

### A2. Confirm if more than one PR

If there are 2+ PRs, ask which to merge (default: all). One PR, proceed.

### A3. Refuse if any selected PR has blockers

For each PR's head branch, check for a review-notes file (e.g. `COMMENTS.md`) with unresolved blockers. If any exist, list them and stop.

### A4. Merge each PR

```bash
gh pr merge <number> --squash   # or --merge per project convention
```

Then `git checkout main && git pull` to sync local main with the new state.

### A5. Update docs on main

You are now on main with the merged code. Continue to the shared docs + push + deploy + Docker flow below (steps 1-5).

## Path B: Trunk mode

### B1. Assess

```bash
git status
git diff
git log --oneline -5
git remote -v
```

### B2. Commit if needed

- Stage specific files by name (never `git add -A` or `git add .`)
- Refuse to commit secrets or `.env` files
- Commit message: use `$ARGUMENTS` as the subject if provided; otherwise summarize the changes
- Append your commit trailer if your workflow uses one

If the working tree is already clean, skip.

### B3. Continue to shared docs + push + deploy + Docker flow below.

## Shared: docs, push, deploy, Docker

### 0. Project-specific pre-push gate (optional)

If your repo defines a pre-push validation of its own (a health check, a link checker, an index or asset builder, a content lint), run it here and BLOCK on failure. This is the place to catch anything that would ship broken content or a broken build to prod.

```bash
# examples: replace with your project's actual gate, skip if none
make check        # or: npm run lint && npm test
python3 tools/validate.py --check
```

If the gate regenerated any tracked artifact (an index, a lockfile, a build manifest), stage and commit it as part of this release. If the gate fails, STOP, fix it, rerun until it passes, then continue. Skip silently for projects that define no such gate.

### 1. Update README.md and project notes

Follow `~/.claude/skills/_shared/release-docs.md`: write a real subsection explaining the change, update the project notes with technical details + gotchas, never push code without these.

Commit the docs (separate commit is fine: `docs: README and notes for <change-name>`).

### 2. Push to all remotes

**Preflight (mandatory):** before any push, run the harness-files protection check from `~/.claude/skills/_shared/harness-files-protection.md`. For every remote you do not own personally, verify that no Claude / AI-assistant harness files (`CLAUDE.md`, `MEMORY.md`, `AGENTS.md`, `.claude/`, etc.) are tracked, and that `.gitignore` covers the full set. If any harness file is tracked on a client remote, STOP the push and surface the file list. Never push harness files to a repo you do not own.

Push to every remote in `git remote -v` (origin, company, client fork, etc.). If a push is rejected, fetch + rebase and retry. Report the result of each.

### 3. Verify auto-deploy

If the project auto-deploys on push to main:
- **Render**: wait briefly, then check the latest deploy status via the Render CLI or API. Confirm the deployed commit SHA matches the commit you just pushed, a live-but-stale deploy means the webhook missed and you should trigger a deploy manually. Surface the URL.
- **Vercel / Netlify**: same, surface the deploy URL and status.
- If no auto-deploy is configured, say so.

### 4. Rebuild local Docker

Follow `~/.claude/skills/_shared/local-docker-rebuild.md`: rebuild from the freshly-merged main so localhost matches prod. Skips silently if the project does not use Docker.

### 5. Final report

Single status block:
- Path taken (A merge / B trunk push)
- For path A: which PRs were merged + their numbers
- Commit hash(es) on main
- Which remotes were updated
- Deploy verification: host URL + status + commit-match result
- Docker rebuild result line
- Clean working tree confirmed

## Guardrails

- Never force-push unless explicitly asked.
- Never amend a published commit. Create a new commit.
- Never skip hooks with `--no-verify`.
- Never `gh pr merge --admin` (bypassing branch protection) unless explicitly asked.
- README and project-notes updates are part of this release, not a follow-up.
- Refuse to release with unresolved blockers in any selected PR's review-notes file.
- Refuse to commit secrets or `.env` files.
- Refuse to push Claude / AI-assistant harness files (`CLAUDE.md`, `MEMORY.md`, `AGENTS.md`, `.claude/`, `.cursor*`, `.aider*`, `.windsurf*`, `.github/copilot-instructions.md`) to any remote you do not own. See `~/.claude/skills/_shared/harness-files-protection.md`.
- Docker rebuild is best-effort: a failure here must NOT roll back the git push / merge. Report and continue.

## The push-to-main guard hook

This framework ships a `hooks/block-git-push-main.sh` PreToolUse hook that deterministically blocks any raw `git push` to `main` / `master`, so a release always goes through this skill instead of an ad-hoc push. See the repo README for wiring it into `~/.claude/settings.json`. When the guard is active and you genuinely need a direct push, the skill's own pushes to a feature branch are unaffected; the trunk-mode push to main is the one the hook intercepts, bypass it per the hook's documented markers only when you mean to.
