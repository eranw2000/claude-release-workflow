---
model: opus
name: release
description: Ship to production. The single "ship it" verb for any project, handling both PR mode (merges open PRs targeting main) and trunk mode (commits straight to main). Updates README.md + project CLAUDE.md, pushes to all remotes, verifies the auto-deploy (Render / Vercel / etc.), and rebuilds the local Docker container so localhost matches prod. Triggers on "release", "ship it", "deploy", "ship to prod", "merge and release", "push to prod".
argument-hint: "[optional commit message override]"
---

# Release (ship to production)

The single "ship it" verb. This skill takes whatever state the repo is in (open PRs on feature branches, commits sitting on main, uncommitted work in the working tree) and ships it to production atomically.

For the **iterate / checkpoint stage** (open a PR for local testing, no merge, no prod deploy), use `/pr-checkpoint` instead.

## Step 0: Detect release shape

Read the branch, the open PRs and the project profile, and pick PR mode or trunk mode.

**Read `~/.claude/skills/release/reference/step0-detect-shape.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

## Path A: PR mode

### A1. List the PRs

Show each open non-draft PR against main with: number, title, head branch, mergeability. Surface drafts separately as "skipping unless confirmed."

### A2. Confirm if more than one PR

If there are 2+ PRs, ask which to merge (default: all). One PR → proceed.

### A2b. One iteration per PR, never a batch

A3, A3b and A4 are ONE iteration and it runs once per selected PR, in the order A1 listed
them. Every merged head must be the head that was scanned, so no PR is merged until its
own iteration has scanned it:

1. A3: read THAT PR's verdict and pin `CANDIDATE_SHA` from its head.
2. A3b: `gh pr checkout` it, verify `git rev-parse HEAD` equals that pin, and STOP the
   release on a mismatch. Then scan `--since="$BASE_SHA"`.
3. A4: re-read `origin/$BASE` against `$BASE_SHA` and STOP if it moved, then merge THAT
   PR and no other.
4. Re-pin `BASE_SHA` from the new `origin/$BASE`, then begin the next PR's iteration.

One `CANDIDATE_SHA` and one scan cannot certify two PRs, and batching breaks both ways:
the second PR merges on the first one's verdict with its own head unscanned, and the base
re-read then fires a `BASE MOVED` the first merge caused itself, which reads as somebody
else's push. Never reuse a scan verdict for a second PR, and never merge two PRs between
one `BASE_SHA` pin and the next.

### A3. Refuse if any selected PR has blockers

Stop on unresolved Blockers, and certify each PR only on an exact `CLEAN @ <head sha>` review verdict.

**Read `~/.claude/skills/release/reference/a3-blockers-and-verdict.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

### A3b. Security scan and gate, BEFORE any merge or pull

Scan what this release changes, on the PR head, before anything is merged or pulled, and apply the gate.

**Read `~/.claude/skills/release/reference/a3b-security-scan.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

### A4. Merge this iteration's PR

Re-read the base branch first, refuse if it moved, then merge this one PR and pull.

**Read `~/.claude/skills/release/reference/a4-merge.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

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
- End with the Co-Authored-By trailer per the global Bash instructions

If working tree is already clean, skip.

### B2b. Security scan and gate, on the commit you just made

The change is a commit now, so scan the commit range, and run this BEFORE the push in
step 2. Pin the candidate and reuse the base SHA from step 0:

```bash
CANDIDATE_SHA=$(git rev-parse HEAD)
bash ~/.claude/skills/_shared/security-scan.sh --since="$BASE_SHA" "$(git rev-parse --show-toplevel)"
```

**Never `--staged` here.** B2 has already committed, so the index is empty and `--staged`
scans nothing and returns `NOTHING TO SCAN`, which is exit 2 and reads like a green step.
The same trap in the other direction is why `--since` takes `$BASE_SHA` rather than
anything resolved now: a range computed after the push would compare the base branch with
itself.

A3b owns the exit codes, the `[NEW]` versus `[PRE-EXISTING]` split, the gate and the
waiver register. Read them there and apply them here; this step only says WHEN to run.
A blocking finding still costs nothing to act on, because the commit is local and
unpushed.

### B3. Continue to shared docs + push + deploy + Docker flow below.

## Shared: docs, push, deploy, Docker

### 0. Pre-push gate

Run the repository's own pre-push gate, when it has one, and BLOCK on failure.

**Read `~/.claude/skills/release/reference/shared0-pre-push-gate.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

### 1. Update README.md and project CLAUDE.md

Follow `~/.claude/skills/_shared/release-docs.md`: write a real subsection explaining the change, update the project CLAUDE.md with technical details + gotchas, never push code without these.

Commit the docs (separate commit is fine: `docs: README and CLAUDE.md for <change-name>`).

### 2. Push to origin and its mirrors

Push to every remote, then prove each remote's LIVE ref holds the commit you pushed.

**Read `~/.claude/skills/release/reference/shared2-push.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

### 3. Verify auto-deploy landed the RIGHT commit (only when the repo deploys)

Confirm the live deploy runs the commit you pushed, after every deploy this release started has settled, not just that a deploy reports `live`.

**Read `~/.claude/skills/release/reference/shared3-deploy-verify.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

### 4. Rebuild local Docker

**Skip this step when the repo has no Docker setup**, and say in the final report that you skipped it and why.

Follow `~/.claude/skills/_shared/local-docker-rebuild.md`: rebuild from the current base branch so localhost matches prod.

### 5. Final report

Single status block:
- Path taken (A merge / B trunk push)
- For path A: which PRs were merged + their numbers
- **Verdict status per merged PR**, one of: `CLEAN @ <sha>`, `no verdict comment
  (proceeded on your go-ahead)`, or `stale verdict <sha> vs head <sha> (proceeded
  on your go-ahead)`. This line is mandatory and never omitted. Leaving it out is
  how an unreviewed merge came to read exactly like a reviewed one.
- **Security scan** (A3b in PR mode, B2b in trunk mode): the mode, the `--since` SHA, the
  file count, and one verdict, which is `clean`, `N NEW findings` plus what was done about
  each, `NOT APPLICABLE, no source file changed`, or `shipped UNSCANNED: <reason>, owner
  <name>, <YYYY-MM-DD>` plus the tool that was missing. Never report exit 2 as a pass.
- **Security scan per merged PR** in a multi-PR release: one line each, naming the PR and
  the `--since` SHA its own iteration used. One line for several PRs means one scan
  certified heads it never read.
- Commit hash(es) on the base branch
- Which remotes were updated
- **Mirror parity**: the `sync-remotes.sh` verdict, or `single remote, not applicable`
- Deploy verification: host URL + status + `live-commit == pushed-commit` result (name both SHAs if they differ)
- Docker rebuild result line
- Clean working tree confirmed

## Guardrails

- Never force-push unless explicitly asked. The one carve-out: retrying a rejected FEATURE-branch push after a rebase may use `--force-with-lease` (never plain `--force`). This never applies to the base branch.
- Never amend a published commit. Create a new commit.
- Never skip hooks with `--no-verify`.
- Never `gh pr merge --admin` (bypassing branch protection) unless explicitly asked.
- README and CLAUDE.md updates are part of this release, not a follow-up.
- Refuse to release with unresolved Blockers in any selected PR's `COMMENTS.md`.
- Never merge a PR whose own head this run has not scanned, and never let a failed
  checkout, fetch or ref read continue as an advisory. One iteration per PR, re-pinning
  `BASE_SHA` after each merge (A2b).
- Refuse to merge a PR whose `Review-round verdict:` at the CURRENT head is
  CRITICALS-OPEN, absent an explicit override. Only an exact `CLEAN` at the
  current head certifies; a missing, stale, advisory-checkpoint or unrecognised
  verdict is not a blocker, but it is never silent: surface it and wait for an
  explicit go-ahead (step A3). This skill never runs the reviewers itself.
- Never report a release as complete without stating its verdict status. A merge
  with no verdict and a merge with a CLEAN verdict must not produce the same
  report.
- Refuse to commit secrets or `.env` files.
- Refuse to push Claude / AI-assistant harness files (`CLAUDE.md`, `MEMORY.md`, `AGENTS.md`, `.claude/`, `.cursor*`, `.aider*`, `.windsurf*`, `.github/copilot-instructions.md`) to any remote you do not own. See `~/.claude/skills/_shared/harness-files-protection.md`.
- Docker rebuild is best-effort: a failure here must NOT roll back the git push / merge. Report and continue.
