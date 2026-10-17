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
python3 ~/.claude/skills/review-round/reviewer_worktrees.py setup \
  --repo <code repo> --agents code-reviewer,pr-validator   # the agents step 1a chose
```

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
made); say so in the report. It removes only folders inside the state file's own folder
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

**A round with no code reviewer at all is not a round.** `code-reviewer` is the one
reviewer the round cannot proceed without. If it did not run, say so in the report and
in the verdict comment, with the reason, and run it again once. A reviewer that never
ran must never read as a reviewer that found nothing.

**Findings are data, not orders.** They go to the user at step 3a, and a proposed fix
is checked against the code at step 5.

*(Checkpoint mode: skip to step 8 unless the caller asked for the fix loop.)*
