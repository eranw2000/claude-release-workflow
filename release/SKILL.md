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

Inspect the repo state and pick the path. Run:

```bash
BASE=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
BASE=${BASE:-main}   # resolve the real base branch; do NOT assume main
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
git fetch origin "$BASE" || { echo "STOP: fetch of origin/$BASE failed"; exit 1; }
BASE_SHA=$(git rev-parse "origin/$BASE") || { echo "STOP: origin/$BASE unreadable"; exit 1; }   # PIN the base commit here, once
echo "branch=$CURRENT_BRANCH base=$BASE base_sha=$BASE_SHA"
git status --porcelain
gh pr list --base "$BASE" --state open --json number,title,headRefName,isDraft 2>/dev/null
```

Use `$BASE` everywhere below instead of a literal `main` (querying `--base main` on a `master`/`develop` repo returns nothing, so a real open PR would be misread as "nothing to ship"). Decide which of these you're in:

**Two commits are pinned here and named by every later step. Pin each once and never re-resolve it mid-run:**

- **`BASE_SHA`**, above: the base branch's commit as it stood when this release started. It is the security scan's `--since` ref. It is a SHA, and `$BASE` is a branch NAME, so the two are different variables on purpose: the scan step once reused `BASE` for its own SHA and shadowed the branch name the rest of the run needed.
- **`CANDIDATE_SHA`**: the commit that is actually shipping. In PR mode it is the PR head, so pin it in A3 where you already read it: `CANDIDATE_SHA=$(gh pr view <number> --json headRefOid -q .headRefOid)`. In trunk mode it does not exist until B2 has committed, so pin it there: `CANDIDATE_SHA=$(git rev-parse HEAD)`.

The security scan reads the diff between these two, which is why both are pinned before anything merges, pulls or pushes.

**Path A: PR mode (one or more open PRs targeting main):**
- There are open non-draft PRs against `main`.
- You will merge them (with confirmation if more than one or if any are draft).
- After merge, switch to main and continue.

**Path B: Trunk mode (on main, work to push):**
- Current branch is `main` / `master`.
- There's uncommitted work in the working tree, OR commits sitting locally that aren't pushed.
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

For each PR's head branch, check for `COMMENTS.md` with unresolved Blockers. If any exist, list them and stop.

Also read each selected PR's latest `Review-round verdict:` comment (posted by the
`review-round` skill, which owns the marker string; this skill reads it verbatim):

```bash
gh pr view <number> --json comments -q '[.comments[].body | select(contains("Review-round verdict:"))] | last'
HEAD_SHA=$(gh pr view <number> --json headRefOid -q .headRefOid)
CANDIDATE_SHA=$HEAD_SHA    # this iteration's candidate pin: what A3b scans and A4 merges
```

- Take the marker from the LAST line of the most recent comment carrying it, and
  compare its `@ <sha>` to the PR's current head. A SHA mismatch is treated exactly
  like NO comment (a stale CLEAN must not certify commits pushed after the review,
  and a stale CRITICALS-OPEN must not block a fixed head).
- `Review-round verdict: CLEAN @ <current-head-sha>`: the ONLY value that
  certifies. It means a completed round, every Critical and Blocker resolved or
  declined, full suite green.
- `Review-round verdict: CRITICALS-OPEN @ <current-head-sha>`: treat like
  COMMENTS.md Blockers. Stop, list the open Criticals from that comment, and offer
  to re-run `/review-round` or proceed only on an explicit override.
- `Review-round verdict: CHECKPOINT-ADVISORY @ <current-head-sha>`: treat exactly
  like NO verdict below. A checkpoint round is advisory by contract and its fix
  loop is optional, so nothing here was certified. Say which it is, in those
  words, and wait for an explicit go-ahead.
- **Anything else, including a value this list does not name: treat as NO verdict.**
  Permit on an exact `CLEAN` and stop on everything else. Do not read an
  unrecognised value as certified, and do not guess what a new one meant. This is
  the direction that matters: a gate that lists what to REFUSE lets every value
  nobody thought of through, and the value nobody thought of is the one a future
  edit introduces.
- NO verdict comment, or only stale ones: **STOP and surface it, then proceed only
  on an explicit go-ahead.** This is not a blocker and never becomes one, but it
  must never be silent. Print the PR number, its size (`--json additions,deletions,changedFiles`),
  whether the diff touches a migration or a deploy-shaped file, and one of:
  - `no Review-round verdict comment exists for this PR` , or
  - `the only verdict is stale: <sha-in-comment> vs head <current-head>` , or
  - `the only verdict is an advisory checkpoint one, which certifies nothing` , or
  - `the verdict value <value> is not one this gate recognises`

  then say plainly that nothing has certified this head, and wait.

  Why this is not silent: a missing verdict reads exactly like an unreviewed PR,
  and absence of evidence is the one state that looks the same as success, so it
  gets said out loud.

  A missing verdict is legitimate and common (a docs-only fix, a one-line revert).
  Surfacing it costs one confirmation; not surfacing it costs an unreviewed
  production merge nobody notices.

### A3b. Security scan and gate, BEFORE any merge or pull

Run the code scanners over what this release CHANGES, then apply the gate. This runs HERE,
on the PR head, while the diff still exists. Once `gh pr merge` has run and A4's
`git pull` has landed, the branch's commits ARE the base branch, so any range computed
from the base resolves to nothing and the scan reads zero files.

Check out the candidate so the working tree is the thing being shipped, then scan the
range from the base SHA pinned in step 0. Both prerequisites STOP the release on failure,
because a scan that ran on the wrong tree certifies the wrong commit:

```bash
gh pr checkout <number> || { echo "STOP: checkout failed for PR <number>"; exit 1; }
HEAD_NOW=$(git rev-parse HEAD) || { echo "STOP: HEAD unreadable after checkout"; exit 1; }
[ "$HEAD_NOW" = "$CANDIDATE_SHA" ] || { echo "STOP: candidate mismatch, HEAD=$HEAD_NOW pin=$CANDIDATE_SHA"; exit 1; }
bash ~/.claude/skills/_shared/security-scan.sh --since="$BASE_SHA" "$(git rev-parse --show-toplevel)"
```

A failed checkout leaves the old `HEAD` in place, and the scan would then read a tree
nobody is shipping while A4 merges a head nothing scanned. So the guard is `|| exit`, never
an `echo`: an advisory line scrolls past and the run continues into the merge. Without
`gh`, substitute `git fetch origin <headRefName> && git checkout <headRefName>` and guard
it the same way.

`--since` takes `$BASE_SHA`, the SHA pinned in step 0, and never a literal branch name:
a hardcoded base scans nothing on a `master` or `develop` repo, and re-resolving the ref
here would silently move the range if somebody pushed to the base branch meanwhile.

Getting the order wrong produces a result that looks like a pass and is not. Measured
2026-08-28, the first real use of this step, when the scan ran after the merge and after
the commit: the range resolved to `HEAD...HEAD`, so it scanned nothing and returned
`NOTHING TO SCAN`. That is exit 2 and correctly not a pass, but a reader in a hurry sees
a green step. The order is fixed now, and this is why: the scan's input is a DIFF, and
both a merge and a pull destroy that diff by making the candidate and the base the same
commit. Run it while the two still differ, which is before either.

Four exit codes, and they mean four different things:

- **0, clean.** It scanned something and found nothing. Continue.
- **1, findings.** Everything ran. Read them, and continue only when the gate below is
  satisfied. This BLOCKS until the gate is satisfied.
- **2, incomplete.** Nothing was scanned, or a scanner is missing, failed, or did not
  finish. This is NOT a pass, and it is NOT a block either, so say WHICH of the two it
  was:
  - **Nothing to scan**, the scan's own `NOTHING TO SCAN` line, which is what a docs-only
    or config-only release produces because no changed path matched a source name.
    Report it in the final report as `security scan: NOT APPLICABLE, no source file
    changed`, in those words. Never as clean, never as passed, never as a tick.
  - **A scanner is missing or failed.** Install the named tool and run it again. Shipping
    UNSCANNED is a waiver like any other and takes the register's shape: a one-line reason,
    a named owner and the date, written into the release notes as `shipped UNSCANNED:
    <reason>, owner <name>, <YYYY-MM-DD>` and repeated in the final report. Missing any of
    the three, it is not allowed, and the release stops until the tool runs. A self-report
    with no owner is how a skipped scan becomes nobody's backlog item.
- **3, findings AND incomplete.** Both facts are true and neither may hide the other.
  Treat it as a 1 for the gate, so it BLOCKS, and as a 2 for the report.

**FIRST, read the `origin:` line, because a finding here is not automatically yours.**
The scan lists changed FILES from the diff and semgrep then reads each one END TO END, so
every pre-existing line in a file you touched arrives in this report. Measured 2026-09-02 on
a release that changed 31 files: three findings, all three already live on the base branch, none on a
line the change added. Applied without this distinction the gate below either blocks a
correct release or teaches whoever meets it to wave it through, and both are worse than no
gate. Since that date the scan tags every finding and prints the split:

- **`[NEW]`**, on a line this change adds. The gate below applies in full.
- **`[PRE-EXISTING]`**, in a file this change touched but on a line it did not write. It does
  NOT block the release. **Name it in the final report** as a pre-existing finding, with its
  count, so it reads as a backlog item rather than as a check that passed. It belongs in the
  waiver register or in the project TODO, and it is not this release's job.
- **`[UNKNOWN]`**, meaning no added-line map could be built. **Treat these as `NEW`**, which
  is the direction the tagger itself fails in, deliberately.

**Then the gate: a release needs no unresolved blocking `[NEW]` finding, and every accepted
high finding named in the waiver register** at `~/.claude/skills/_shared/security-standards/baseline.yml`,
with an owner, a justification and an expiry. A `[NEW]` finding kept without a waiver entry is
an unresolved blocking finding; a `[PRE-EXISTING]` one is named in the report instead. Bands are in `security-standards/severity-taxonomy.md`.

**The register is read by the scan, so a waiver actually takes effect.** An entry needs a
`rule_id` that matches the finding (the whole rule name, or its last dotted segment), a
`path` glob, an `owner`, a `reason` and an `expires` date. The scan then prints how many it
waived. Three things it deliberately refuses to honour, each reported out loud rather than
silently: an entry past its expiry, an entry with no expiry at all, and an entry carrying
only a `fingerprint` and no `path`. If PyYAML is missing from
the interpreter, the scan says the register went unread and reports every finding rather
than assuming there was nothing to waive.

**Why this reads the diff and not the repository.** Measured 2026-08-28: semgrep's
p/default ruleset returned 57, 28, 8 and 1 findings on four repos already in production.
A whole-repository gate would have blocked every one of them on its first day, and a
guard the present cannot pass gets switched off by whoever meets it late in the day. The
standing backlog belongs in the waiver register, not in a gate nobody can clear.

**A blocking finding here costs nothing to act on, which is the other half of why the step
moved.** Nothing has merged and nothing has been pushed, so fixing it is a commit on the
branch rather than a revert on the base branch.

**Say what you did.** Name the scan mode, the file count and the verdict in the final
report. A silent skip reads as a check that passed.

### A4. Merge this iteration's PR

**Pre-merge preflight, first: RE-READ the base branch.** A3b scanned the range from
`$BASE_SHA`, so a base branch that has moved since invalidates that verdict. The fetch is
guarded because a failed one leaves a stale `origin/$BASE` that still equals the pin, so
the check would print `base unmoved` about a branch it never re-read:

```bash
git fetch origin "$BASE" || { echo "STOP: fetch of origin/$BASE failed"; exit 1; }
BASE_NOW=$(git rev-parse "origin/$BASE") || { echo "STOP: origin/$BASE unreadable"; exit 1; }
[ "$BASE_NOW" = "$BASE_SHA" ] && echo "base unmoved" || { echo "BASE MOVED: origin/$BASE is $BASE_NOW, pinned $BASE_SHA"; exit 1; }
```

On `BASE MOVED`, **STOP. Do not merge.** Somebody pushed to the base branch, so the
range A3b read no longer describes what this merge would ship, and their commits went
unscanned by this release. Go back to step 0, re-pin `BASE_SHA` and `CANDIDATE_SHA`,
re-run A3b, and only then come back here. On a multi-PR release, check first whether the
mover was an earlier merge of this same run: that means the loop in A2b was not followed,
and the fix is to re-pin and re-scan rather than to wave the message through.

**Pre-merge preflight, second: harness files.** `gh pr merge` merges on the remote immediately, so run the harness-files protection check from `~/.claude/skills/_shared/harness-files-protection.md` against the PR's head branch BEFORE merging, not after. If the base repo (origin) is a client / company remote and any harness file is tracked, STOP here: a merge cannot be undone as cleanly as a withheld push. Fix the tracked files first, then merge.

**A DRAFT pull request cannot be merged and the merge command refuses it**, so a run that
followed A1's "surface drafts separately" and then arrived here hits a failure rather than a
decision. When the draft is the one being released, and the user has said to ship it, mark it
ready first and say you did:

```bash
gh pr ready <number>            # a draft refuses to merge; this is not a silent step
gh pr merge <number> --squash   # this iteration's number only; --merge per project convention
```

Background-session note: `gh pr comment` / `gh pr merge` can be blocked by the
auto-mode classifier on agent-authored PRs. A transient stage-2 classifier error
gets ONE plain retry; a sustained block means asking the user to switch the session
to manual mode (or run the command themselves with `! gh ...`), not fighting it.

Then sync the local base branch and RE-PIN `BASE_SHA`, because this merge moved it and the
next PR's scan range starts from the new value:

```bash
git checkout "$BASE" && git pull || { echo "STOP: could not sync $BASE after the merge"; exit 1; }
BASE_SHA=$(git rev-parse "origin/$BASE") || { echo "STOP: origin/$BASE unreadable"; exit 1; }
echo "re-pinned base_sha=$BASE_SHA"
```

If another selected PR remains, go back to A3 with that number and run its whole iteration
against this new `$BASE_SHA`. When none remains, continue to A5.

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

### 0. Project-specific pre-push gate (optional)

If your repo defines a pre-push validation of its own (a health check, a link checker, an index or asset builder, a content lint), run it here and BLOCK on failure. This is the place to catch anything that would ship broken content or a broken build to prod.

```bash
# examples: replace with your project's actual gate, skip if none
make check        # or: npm run lint && npm test
python3 tools/validate.py --check
```

If the gate regenerated any tracked artifact (an index, a lockfile, a build manifest), stage and commit it as part of this release. If the gate fails, STOP, fix it, rerun until it passes, then continue. Skip silently for projects that define no such gate.

### 1. Update README.md and project CLAUDE.md

Follow `~/.claude/skills/_shared/release-docs.md`: write a real subsection explaining the change, update the project CLAUDE.md with technical details + gotchas, never push code without these.

Commit the docs (separate commit is fine: `docs: README and CLAUDE.md for <change-name>`).

### 2. Push to origin and its mirrors

**Preflight (mandatory):** before any push, run the harness-files protection check from `~/.claude/skills/_shared/harness-files-protection.md`. For every remote you do not own personally, verify that no Claude / AI-assistant harness files (`CLAUDE.md`, `MEMORY.md`, `AGENTS.md`, `.claude/`, etc.) are tracked, and that `.gitignore` covers the full set. If any harness file is tracked on a client remote, STOP the push and surface the file list. Never push harness files to a repo you do not own.

Push to origin. Then push to each remote named in `git config --get-all release.mirror`, if there are any. Report the result of each. Never push to any other remote in `git remote -v` (an upstream, a fork, an archive).

**This push targets the base branch, so it trips the `block-git-push-main.sh` guard hook.** That is expected here (this is the sanctioned release path). Append the documented marker to the push so the hook lets it through:

```bash
git push origin "$BASE" #allow-push-main
```

Keep the marker unquoted and after the args (quoting it makes git read it as a refspec). If the auto-mode classifier still blocks the push (it can, independently of the hook), ask the user to run the push themselves with `! git push`, rather than fighting it.

If a push is rejected because the remote moved, fetch + rebase and retry. A rebased FEATURE branch needs `git push --force-with-lease` (never a plain `--force`). There is no equivalent on the base branch: never force-push it, however certain you are that nobody else pushed. Stop and surface it instead. This matches the Guardrails section below, which had said the opposite of the old wording here.

**Then prove every MIRROR actually landed it. Mandatory whenever the repo lists a mirror in `git config release.mirror`:**

```bash
bash ~/.claude/skills/release/sync-remotes.sh <repo path>
```

A mirror is a remote you named once with `git -C <repo> config --add release.mirror <remote-name>`. Every other remote (an upstream, a fork, an archive) is listed in the output and never pushed. Exit 0 means every listed mirror holds the same commit as origin's default branch. Exit 1 means one is behind, has diverged, is not a remote, or is a non-personal remote with Claude harness files tracked. **The release is not done until it exits 0.** Paste its output into the final report.

Why this is not optional: the deploy check in step 3 confirms PRODUCTION is right, and nothing anywhere confirms a mirror, because a mirror has nothing downstream of it to report a problem. Measured once already, a commit went live and never reached the client-facing mirror, which sat a commit behind for two weeks in silence.

It needs no `#allow-push-main`: origin is the source of truth and the script only ever copies the commit origin ALREADY holds, so it cannot introduce unreviewed code to a company or client repo. It reads LIVE refs with `git ls-remote`, never the local tracking branches. It also reports, without changing anything, whether origin carries a second push URL so that every push fans out by itself.

### 3. Verify auto-deploy landed the RIGHT commit (only when the repo deploys)

**Skip this step when the repo has no deploy target.** A repo with no deploy file has
no deploy to verify, so say that in one line and go to step 4. When the repo DOES
deploy, this check is mandatory and the rest of this step applies in full.

Checking `status: live` is not enough. The autoDeploy webhook can silently miss a push, so the host keeps serving the OLD commit while still reporting the deploy `live`. Always verify the live deploy's commit equals the commit you just pushed.

```bash
PUSHED=$(git rev-parse HEAD)          # or: git ls-remote origin "$BASE" | cut -f1
```

- **Render**: wait briefly, then read the latest deploy for the service. Confirm both `status == live` AND `commit.id == $PUSHED`. If the commit does not match within ~1 minute, the webhook missed it: trigger a deploy manually via `POST /v1/services/{id}/deploys` and re-verify. (Deploy GET responses can contain raw `\n`; parse with `json.loads(..., strict=False)`.) Surface the URL.
- **Vercel / Netlify**: same idea, confirm the live deployment's commit SHA matches `$PUSHED`, not just that a deploy is "ready". Surface the URL.
- If no auto-deploy is configured, say so.

A live-but-stale deploy (status live, commit mismatch) is a FAILED release, not a passed one. Report it as such and either re-trigger or hand off.

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
