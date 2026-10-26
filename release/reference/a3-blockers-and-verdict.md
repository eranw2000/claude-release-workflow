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
  certifies. It means a completed round, every Critical and Blocker fixed,
  declined, or deferred as rare (review-round step 8, listed with a `TODO.md` id),
  full suite green.
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
