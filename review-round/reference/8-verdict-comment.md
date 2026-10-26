One PR comment (`gh pr comment`) containing:
- Per-agent finding counts (code-reviewer Critical/Warning/Suggestion, deploy-guard
  Blocker/Warning/Note, pr-validator verdict + totals). **Every number here, and every
  total, fixed or declined count anywhere in the comment, is COUNTED from the saved
  findings by a command, never typed from memory.** Say the same number in chat only
  after that. A long session drifts.
- `codex-reviewer`'s counts under the same three bands, named as `codex/<model>` with
  the model read from its receipt, so a reader can see which family found what. If it
  did not run, one line saying so and why, and which reviewer stood in, never silence.
  When the user switched it off (exit 15) that line is exactly
  `Codex review: off (CODEX_REVIEW=off)`.
- Any finding two reviewers both reported, called out as convergent. A finding both
  model families reported is the strongest signal the round produces.
- Every Critical/Blocker with its status: **Fixed** (name the regression test),
  **Declined** (state the reason), **Deferred** (a `RARE` finding outside the protected
  class that leaves the goal holding, step 3a: give its `TODO.md` id and its likelihood
  line), or **Open**. List the Deferred ones together under a `Deferred` heading.
- The reproduction command for each executed finding.
- **A `Security scan:` line, on the head you are certifying**, from the same scan
  `/release` runs at A3b:
  `bash ~/.claude/skills/_shared/security-scan.sh --since=<base sha> "$(git rev-parse --show-toplevel)"`.
  A `[NEW]` finding means the head is not `CLEAN` yet: fix it or waive it in the
  register, then scan again. Write e.g. `Security scan: clean since <base sha>, 9 files`.
- The machine-readable LAST line, exactly one of:
  - `Review-round verdict: CLEAN @ <reviewed-HEAD-sha>`
  - `Review-round verdict: CRITICALS-OPEN @ <reviewed-HEAD-sha>`
  - `Review-round verdict: CHECKPOINT-ADVISORY @ <reviewed-HEAD-sha>`

**Which value, and this is the part that is easy to get wrong.** `CLEAN` means a
COMPLETED round: every Critical and Blocker was driven to Fixed, Declined or
Deferred, and the full suite was re-run green. It is a certification, and `/release` treats it
as one.

**CLEAN also means `/release`'s security scan gate will pass at this head.** Run it on
the reviewed head before choosing the value, with the base SHA the PR targets:

```bash
bash ~/.claude/skills/_shared/security-scan.sh --since=<base sha> <repo>
```

Exit 0, or exit 2 with `NOTHING TO SCAN`, allows CLEAN. Exit 1 or 3 does not, until every
`[NEW]` finding is fixed in the code, or sits in the waiver register
(`~/.claude/skills/_shared/security-standards/baseline.yml`) with the owner's approval; fix
it in the code by default. **A reviewer judging a scan finding a false positive, and the
comment saying so, does NOT clear it**: `/release` A3b reads the scan, never the comment.
With a finding still open, ask fix-or-waive first; when nobody can answer, post
`CRITICALS-OPEN` and name the findings.

**In checkpoint mode you may not post `CLEAN`.** Post `CHECKPOINT-ADVISORY`
instead whenever the round found nothing blocking. A checkpoint's findings are
advisory by contract and its fix loop is optional, so "nothing blocked a
checkpoint" is a much weaker claim than "everything was resolved" and must not
wear the same word. `CRITICALS-OPEN` is unchanged and correct in either mode: if
a checkpoint round finds an unresolved Critical, saying so should stop a later
release, and it does.

The one thing that makes this a gate rather than a convention: a round that ends
without completing, because it was interrupted, ran out of budget, or stopped to
ask a question nobody answered, leaves whatever it last posted standing. If that
was `CLEAN`, the PR now carries a certification nobody earned.
`CHECKPOINT-ADVISORY` fails safe there, because the release gate stops on it and
asks.

This marker string is OWNED by review-round; `/release` reads it verbatim and
compares the SHA to the PR head at release time. The SHA binding is load-bearing:
without it, a verdict posted at checkpoint time would silently certify commits
pushed AFTER the review. If you fixed anything in steps 4-7, the reviewed SHA is
the post-fix HEAD you re-ran the suite on; push first, then post.

Adding a fourth value is allowed, but it lands in `/release`'s A3 in the same
change or it is inert: that reader permits on `CLEAN` alone and stops on anything
else, so a new value stops releases until A3 learns it.

**Verify the comment actually landed.** Posting is not the same as posted, and the
difference is invisible from here:

```bash
gh pr view <number> --json comments -q '[.comments[].body | select(contains("Review-round verdict:"))] | last'
```

If that comes back empty, the round produced NO durable evidence: its findings live
only in this transcript, and `/release` will later see a PR that looks unreviewed.
Say so in your final message, in those terms, and hand the exact comment body to the
user to post. Never end a round reporting a verdict you could not confirm is on the PR.

This is the failure that changed the rule. A real review round ran, its fixes were the
PR's head commit, and no comment ever reached the PR. `/release` read the absence as
"no gate applies" and merged 32 files and a data migration to production. Both halves
of the contract were self-reported and nothing reconciled them.

Post with `gh pr comment <n> --body "$(cat verdict.md)"`, not `--body-file`.
`--body-file` appends a trailing newline, so the gate's last-line read returns EMPTY on
a comment that looks perfectly posted. Command substitution strips it.
