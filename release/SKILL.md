---
model: opus
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
BASE=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
BASE=${BASE:-main}   # resolve the real base branch; do NOT assume main
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "branch=$CURRENT_BRANCH base=$BASE"
git status --porcelain
gh pr list --base "$BASE" --state open --json number,title,headRefName,isDraft 2>/dev/null
```

Use `$BASE` everywhere below instead of a literal `main` (querying `--base main` on a `master`/`develop` repo returns nothing, so a real open PR would be misread as "nothing to ship"). Decide which of these you are in:

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

Also read each selected PR's latest `Review-round verdict:` comment (posted by the
`review-round` skill, which owns the marker string; this skill reads it verbatim):

```bash
gh pr view <number> --json comments -q '[.comments[].body | select(contains("Review-round verdict:"))] | last'
HEAD_SHA=$(gh pr view <number> --json headRefOid -q .headRefOid)
```

- Take the marker from the LAST line of the most recent comment carrying it, and
  compare its `@ <sha>` to the PR's current head. A SHA mismatch is treated exactly
  like NO comment (a stale CLEAN must not certify commits pushed after the review,
  and a stale CRITICALS-OPEN must not block a fixed head).
- `Review-round verdict: CRITICALS-OPEN @ <current-head-sha>`: treat like the
  review-notes blockers above. Stop, list the open Criticals from that comment, and
  offer to re-run `/review-round` or proceed only on an explicit override.
- NO verdict comment (or only stale ones): NOT a blocker; proceed. The gate binds
  only when a verdict exists for the exact head being merged.

### A4. Merge each PR

**Pre-merge preflight:** `gh pr merge` merges on the remote immediately, so run the harness-files protection check from `~/.claude/skills/_shared/harness-files-protection.md` against the PR's head branch BEFORE merging, not after. If the base repo (origin) is a remote you do not own and any harness file is tracked, STOP here: a merge cannot be undone as cleanly as a withheld push. Fix the tracked files first, then merge.

```bash
gh pr merge <number> --squash   # or --merge per project convention
```

Background-session note: `gh pr comment` / `gh pr merge` can be blocked by the
auto-mode classifier on agent-authored PRs. A transient stage-2 classifier error
gets ONE plain retry; a sustained block means asking the user to switch the session
to manual mode (or run the command themselves), not fighting it.

Then `git checkout "$BASE" && git pull` to sync your local base branch with the new state.

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

Push to every remote in `git remote -v` (origin, company, client fork, etc.). Report the result of each.

**This push targets the base branch, so it trips the `block-git-push-main.sh` guard hook if you installed it.** That is expected here (this is the sanctioned release path). Append the documented marker so the hook lets it through:

```bash
git push origin "$BASE" #allow-push-main
```

Keep the marker unquoted and after the args (quoting it makes git read it as a refspec). If your harness has a separate policy layer that still blocks the push, run the push yourself rather than fighting it.

If a push is rejected because the remote moved, fetch + rebase and retry. A rebased branch needs `git push --force-with-lease` (never a plain `--force`); on the base branch, only do this if you are certain no one else pushed, otherwise stop and surface it.

### 3. Verify auto-deploy

If the project auto-deploys on push to main:
- **Render**: wait briefly, then check the latest deploy status via the Render CLI or API. Confirm the deployed commit SHA matches the commit you just pushed, a live-but-stale deploy means the webhook missed and you should trigger a deploy manually. Surface the URL.
- **Vercel / Netlify**: same, surface the deploy URL and status.
- If no auto-deploy is configured, say so.

### 4. Rebuild local Docker

Follow `~/.claude/skills/_shared/local-docker-rebuild.md`: rebuild from the current base branch so localhost matches prod. Skips silently if the project does not use Docker.

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

- Never force-push unless explicitly asked. The one carve-out: retrying a rejected FEATURE-branch push after a rebase may use `--force-with-lease` (never plain `--force`). This never applies to the base branch.
- Never amend a published commit. Create a new commit.
- Never skip hooks with `--no-verify`.
- Never `gh pr merge --admin` (bypassing branch protection) unless explicitly asked.
- README and project-notes updates are part of this release, not a follow-up.
- Refuse to release with unresolved blockers in any selected PR's review-notes file.
- Refuse to merge a PR whose `Review-round verdict:` at the CURRENT head is
  CRITICALS-OPEN, absent an explicit override. A missing or stale-SHA verdict is not
  a blocker; this skill never runs the reviewers itself.
- Refuse to commit secrets or `.env` files.
- Refuse to push Claude / AI-assistant harness files (`CLAUDE.md`, `MEMORY.md`, `AGENTS.md`, `.claude/`, `.cursor*`, `.aider*`, `.windsurf*`, `.github/copilot-instructions.md`) to any remote you do not own. See `~/.claude/skills/_shared/harness-files-protection.md`.
- Docker rebuild is best-effort: a failure here must NOT roll back the git push / merge. Report and continue.

## The push-to-main guard hook

This framework ships a `hooks/block-git-push-main.sh` PreToolUse hook that deterministically blocks any raw `git push` to `main` / `master`, so a release always goes through this skill instead of an ad-hoc push. See the repo README for wiring it into `~/.claude/settings.json`. When the guard is active and you genuinely need a direct push, the skill's own pushes to a feature branch are unaffected; the trunk-mode push to main is the one the hook intercepts, bypass it per the hook's documented markers only when you mean to.
