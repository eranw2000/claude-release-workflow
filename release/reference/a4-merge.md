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
gh pr merge <number> --squash --match-head-commit "$CANDIDATE_SHA"   # this iteration's number only; --merge per project convention
```

`--match-head-commit` makes GitHub refuse the merge when the PR head is no longer the commit
this iteration pinned, scanned and checked. Without it, a push to the PR after A3 merges a
head nothing here read. On that refusal, STOP: go back to A3 for this PR and run its whole
iteration again on the new head.

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
