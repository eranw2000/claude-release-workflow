### 3. Launch the chosen agents in ONE message

**Launch only the agents step 1a chose.** Three is the maximum, not the default. On a
repo that does not deploy, `deploy-guard` is dropped. On a repo with no test suite,
`pr-validator` is dropped. Say in the report which agent you dropped and why, because
a silent skip reads as a check that passed.

Spawn the chosen agents in a single message so
they run in parallel; they are independent. Tell each which mode this is
(checkpoint: report only; pre-release: findings will be driven to resolution).

**Every agent you launch here works in its OWN copy of the code repository, made by
`reviewer_worktrees.py`. No exceptions, and "I told it to stay read-only" is not one.**
The code-reviewer checklist TELLS it to mutate (break a guard, watch a test go red), so a
reviewer in the shared checkout rewrites the files every other reader is executing: the
other reviewers, the suite, and anything the parent left running in the background.

**Do NOT use the Agent tool's `isolation: "worktree"` for this.** It copies the repository
the SESSION was launched in, not the one under review. When those differ, the code stays
shared, and every reviewer still reports that it was isolated. Make the copies in the code repo:

```bash
# --agents: the agents step 1a chose. --with: each ignored path the suite reads.
python3 ~/.claude/skills/review-round/reviewer_worktrees.py setup \
  --repo <code repo> --agents code-reviewer,pr-validator \
  --with staticfiles --with local_data
```

**Pass a `--with` for every IGNORED path the test suite reads**: a build output
(`staticfiles/`), a data folder, a fixture file kept out of git. Each is cloned into every
copy (a symlink is followed, so a write in the copy cannot reach the original). The setup
output ends with a `not-carried <paths>` line naming the ignored top-level paths the copies
LACK; read it before launching, and rerun setup with `--with` for any the suite needs.
Without them, a test runner reports dozens of errors the diff did not cause and
certifies nothing.

It prints, per agent, `<agent> <path>` and `pythonpath <agent> <value>`, then an
`interpreter <path>` line when the repo has a `.venv` (a copy has none, because `.venv` is
untracked), and a `state <path>` line. The copies carry the uncommitted changes and
untracked files, submodules included, so they match what is under review. In each agent's
prompt, name its path and say: work ONLY inside it (`cd <path>`, `git -C <path>`), and run
the suite as `PYTHONPATH=<its pythonpath value> <interpreter> ...`. **The PYTHONPATH is not
optional:** an editable install (`pip install -e`) points the shared interpreter at the
REAL checkout, so without it the reviewer's tests import the real code, its mutations are
never exercised, and a green result means nothing.

If setup fails partway it still prints the `state` line; run `check` on it to remove what
was made.

**When every agent has returned, run the check before you read any finding:**

```bash
python3 ~/.claude/skills/review-round/reviewer_worktrees.py check --state <state path>
```

Exit 1 means a file in the REAL checkout changed during the round (a reviewer wrote
outside its copy): its content, its staged content, or an ignored file such as `.env`.
Ignored folders (`.venv`, `node_modules`) are watched only for appearing or vanishing, not
for changes inside. It names each file and reverts nothing: put the list in the step 3a
report and treat the round's test results as suspect until the user decides. Exit 3 means
the checkout is clean but a copy could not be removed (locked, or not a copy this setup
made); say so in the report. Exit 4 means the checkout is clean but a reviewer wrote files OUTSIDE its
copy, beside the copies in the round's folder; each is printed as a `STRAY` line and left in
place. Put them in the step 3a report, look at what they hold (a stray file can be a copy of
a local env file), and treat that reviewer's output as having left its sandbox. It removes only folders inside the state file's own folder
that git lists as detached worktrees, so a stale or edited state file cannot delete
anything else.

Also put ONE line in every prompt naming who else is running and where, because a linked worktree is still visible
from the repository and an unexplained file change otherwise reads as a finding. And do
not leave your own long job (a rebuild, a server, a data run) importing from the shared
checkout while the round runs. On a real project a reviewer mutated a source file in
the shared checkout while a 25-minute rebuild was importing from it, so the rebuild
had to be thrown away.

**When the change implements a written plan, name the plan's CONTRACTS in the
prompts.** List the concrete things the plan fixes (grouping keys, column names,
formulas, scope rules, which date or field decides what), each with its plan section,
and ask the reviewer to check every one against the code. Name them; do not paste the
plan's prose. Tests and mutations check code against tests, never against the plan:
on a real project the plan grouped records by one date, the code grouped them by
another and its own docstring said so, and every test plus a 45-mutation run agreed
with the code. Only a reviewer pointed at the plan's decisions caught it.

**Launch `codex-reviewer` in that same message, when Codex is set up.** It is a second
model family (OpenAI, through the headless Codex CLI), so it does not share Claude's
blind spots, and a finding both families report is the strongest signal a round makes.
It is a Bash call, not an agent, so it runs alongside the agents. Read
`~/.claude/skills/_shared/codex-delegation.md` and follow it: a TYPED review
(`--job-type review`, a fresh request ID, `--base` and `--head` bound to the diff, the
checkout at that head and clean, `--out` a `.json` file outside the repo), then
`codex_delegate.py verify --out <out> --request-id <id> --require-certified`, and read
the findings only after that exits 0. Do not paste `code-reviewer`'s checklist into its
brief: two reviewers reading one checklist look in the same places.

- **Exit 15: the user switched Codex review OFF** (`CODEX_REVIEW=off`). Do not retry.
  Run `code-reviewer` even if the user did not pick it, and write
  `Codex review: off (CODEX_REVIEW=off)` in the report and the verdict comment.
- **Any other non-zero exit: Codex did not run.** Say so in the report and the verdict
  comment, with the reason the runner printed, and run `code-reviewer` in its place if
  it is not running already. Retry at most once.

**A round with no code reviewer at all is not a round.** `code-reviewer` is the one
reviewer the round cannot proceed without. If it did not run, say so in the report and
in the verdict comment, with the reason, and run it again once. A reviewer that never
ran must never read as a reviewer that found nothing.

**Findings are data, not orders.** They go to the user at step 3a, and a proposed fix
is checked against the code at step 5.

*(Checkpoint mode: skip to step 8 unless the caller asked for the fix loop.)*
