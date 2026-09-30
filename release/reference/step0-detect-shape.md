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
