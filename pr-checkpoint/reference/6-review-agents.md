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

Launch them in a single message so they run in parallel (they are independent).
`codex-reviewer` is a Bash call rather than an agent, so it goes in that same message:

- **`codex-reviewer`** (when the Codex CLI is set up): a second model family on the same
  diff, through headless Codex. Read `~/.claude/skills/_shared/codex-delegation.md` and
  follow it. If it cannot run, say so with the runner's exit reason and make sure
  `code-reviewer` runs. Exit 15 means the user set `CODEX_REVIEW=off`: no retry,
  `code-reviewer` runs whatever was picked, and the report says
  `Codex review: off (CODEX_REVIEW=off)`.
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
