---
model: inherit
name: review-round
description: "Run the codified reviewer loop on a diff or PR: author adversarial per-agent prompts pointing at the riskiest spots, launch whichever of code-reviewer, deploy-guard and pr-validator the user picks when asked, in parallel, bring the findings to the user and let them choose what is worth fixing, reproduce by execution each one they pick before fixing it, prove each fix with a non-vacuous regression test, and post the PR verdict comment that /release gates on. Use before shipping, or when the user says 'review round', 'run the reviewers', 'full review pass', or wants the pre-release review loop."
---

# Review round

The codified review->fix->re-verify->verdict loop. Reviewer agents driven by
hand-authored adversarial prompts catch Criticals that authoring and testing miss;
this skill makes the discipline repeatable instead of re-invented per PR. It drives
the companion review agents (code-reviewer, deploy-guard, pr-validator), published
separately as the claude-review-agents pack.

Model note: the load-bearing work here is judgment (adversarial prompt authoring,
adjudicating findings). The pin is `model: inherit`, so this runs on the session
model; choose a strong reasoning model for it with `/model` if the session is on a
lighter one.

## Two modes

- **Pre-release mode (default):** the full loop, steps 1-8. **Step 3a puts the findings
  in front of the user and waits.** What they choose gets fixed or formally declined
  before the verdict posts; the rest is recorded. "The full loop" has never meant
  "fix everything the reviewers name", and reading it that way cost a working day once.
- **Checkpoint mode** (invoked from `/pr-checkpoint` step 6): steps 1-3 and 8 always;
  the fix loop (4-7) is optional and NOTHING blocks the PR. The checkpoint stays
  advisory by design; the hard gate is `/release`. Its step-8 verdict says
  `CHECKPOINT-ADVISORY`, never `CLEAN`, so the mode is visible to the gate that
  reads it later. See step 8 for why the two must not share a word.

## The loop

Eight steps. Step 8 posts the verdict comment that `/release` gates on. It is easy to
drop because it comes after the work feels finished. It is not optional, and it cannot
be inferred from a transcript later.

### 1. Scope

Establish what is under review: `git diff "$BASE"...HEAD` for a branch,
`gh pr view` / `gh pr diff` for a PR. Record the reviewed HEAD SHA
(`git rev-parse HEAD` on the branch); the verdict in step 8 binds to it.

### 1a. Ask which reviewers to run before launching any of them

Not every repo needs all three agents. Launching `deploy-guard` at a documents repo
buys an empty report somebody has to read, and `pr-validator` at a repo with no tests
returns "return to programmer" as noise.

So before step 2, ask the user which of `code-reviewer`, `deploy-guard` and
`pr-validator` to run, and offer a recommendation read from the repo itself:
`code-reviewer` always, `deploy-guard` where the repo deploys, `pr-validator` where it
has a test suite. One message, one keystroke to accept.

## Find the spec that belongs to your initiative

A repo hosts more than one initiative over its life. The first writes `REQUIREMENTS.md`
and `SPEC.md` at the repo root, and a later one writes its own under
`docs/<initiative>/`. So the copy at the root is not always the one you want, and
grounding on the wrong initiative's spec produces work that is internally consistent
and aimed at the wrong system, which review does not catch.

Say which initiative this work is for, then resolve the file instead of assuming the
root copy:

1. List the candidates: the repo root copy, every `docs/*/` copy, and any other
   location this skill already tells you to check.
2. Read the `Initiative` field of each. That field decides, never the directory name,
   which is a slug somebody chose and can be stale or wrong.
3. Exactly one names your initiative: use it, and say which path you used.
4. None names it: STOP and tell the user what you found and where. Do not fall back to
   the root copy.
5. More than one names it: STOP and name every path. Two files claiming one initiative
   is a thing to report, not to settle by picking one.
6. A candidate carries no `Initiative` field: say so. Where it is the only candidate you
   may use it, and you must say you read a spec with no recorded owner.

When the user hands you an explicit path, use that path. Check its `Initiative` field
the same way, and on a mismatch stop and say whether a file for your initiative sits
somewhere else.

This round reviews a diff, not a specification, so it opens these files only when a finding is anchored to one. A finding anchored to the wrong initiative's spec is worse than an unanchored one, because the citation makes it look checked.

### 2. Author the adversarial prompts

**Two things go in the code-reviewer prompt whenever the diff touches them, because
neither is visible to any hook.** First, the three stop conditions: authentication or
authorization switched off, certificate checking switched off, a security check switched
off. All three are REMOVALS, so tell the agent to read the old side of the diff. Second,
the attack test: a change touching authentication or input handling needs a test that
tries to break it, and asserts the refusal rather than the absence of a crash. Both rules
are written out in `~/.claude/agents/code-reviewer.md`.

Write a per-agent prompt that NAMES the 2-3 riskiest spots in this diff and the
specific claims each agent should verify by execution. Point code-reviewer at the
paths you are least sure of; point deploy-guard at the config/prose/topology angle;
tell pr-validator which added tests must be proven to have run. A generic "review
this diff" prompt is a protocol violation: the 2026-07-17 Criticals were caught
because the prompts aimed the reviewers at the weak spots.

**Every prompt must ALSO tell the agent not to stop at the named spots**, and to
audit the new TESTS as adversarially as the source. Aiming is a floor, not a
ceiling: on 2026-07-27 four spots were named, one was refuted by execution, and the
Critical was none of them. It surfaced only because the prompt added "do not stop at
these" and "audit the test file as adversarially as the source". A prompt that names
three spots and says nothing else quietly caps the review at three spots.

**Never ask a reviewer to walk a list. Ask it to sample the SITUATIONS.** Write the ask as
a grouping instruction rather than a count: "group the guard-shaped lines by the situation
each represents, check one of each, and report the groups plus how many instances you did
not individually check." The reviewer decides the grouping; you decide that grouping
happens. Applies to every enumerable thing you might hand a reviewer: files, call sites,
endpoints, fixtures, migrations.

**A cap you set silently is worse than a large ask**, so whatever you request, also require
the reviewer to state what it left unchecked. This does not soften step 4: a Critical is
still reproduced in full, and the reviewer still reads the whole diff.

### 3. Launch the chosen agents in ONE message

**Launch only the agents step 1a chose.** Three is the maximum, not the default. On a
repo that does not deploy, `deploy-guard` is dropped. On a repo with no test suite,
`pr-validator` is dropped. Say in the report which agent you dropped and why, because
a silent skip reads as a check that passed.

Spawn the chosen agents in a single message so
they run in parallel; they are independent. Tell each which mode this is
(checkpoint: report only; pre-release: findings will be driven to resolution).

**Every agent you launch here gets `isolation: "worktree"` on its `Agent` call. No
exceptions, and "I told it to stay read-only" is not one.** The code-reviewer checklist
TELLS it to mutate (break a guard, watch a test go red), so a reviewer in the shared
checkout rewrites the files every other reader is executing: the other reviewers, the
suite, and anything the parent left running in the background. Also put ONE line in every
prompt naming who else is running and where, because a linked worktree is still visible
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

### 3a. Every mode: take the findings to the user BEFORE fixing any of them

**A reviewer's verdict is information for the user, not a work order for you.** Report,
then STOP and let them choose. This step is not satisfied by mentioning the findings on
the way past them.

Give them, in this order:

1. **Is the deliverable already in the user's hands and working?** CHECK it, never assume:
   does this diff touch product code at all, is it already merged or deployed, does
   the live thing answer. Lead with that sentence. If it works and is already there,
   every finding below is optional by definition.
2. **What each finding TOUCHES**: product code, or test and harness code. Reviewers
   grade by defect severity and cannot see who is waiting. A Critical in a checking
   script and a Critical in a shipped rule are not the same thing to the user.
3. **Whether the POLICY each finding cites actually HOLDS in this repository.** A
   finding fails in two independent ways: the INSTANCE ("this file contains X") and
   the POLICY ("X is forbidden here"). Step 4 below reproduces the instance, and
   that is the half everyone remembers. **Nothing asked the second question, and the
   second question is the one that decides whether a finding blocks a release or is
   a tidy-up.** So for any finding resting on a rule, a convention, a policy, a
   baseline or a "despite the repository's ..." clause: run that rule's own check
   over WHAT IS ALREADY COMMITTED, and give the user the number. If the shipped tree
   would fail it too, the finding is a backlog item and the POLICY is what needs a
   decision, not the branch.

   Measured on a real project. A reviewer returned BLOCK because a branch committed a
   few sensitive identifiers "despite the repository's explicit baseline policy",
   quoting a real `.gitignore` line and showing them in the diff. Every fact was true
   and the instance reproduced. One command showed there was no such baseline: the
   main branch already carried the same kind of data, and the cited rule covered a
   narrower case. So the branch was held for nothing.
   **The refutation was worth more than the fix**: measuring the policy raised a
   larger question no reviewer had asked, which went to the user as a decision.

   This is "measure a new guard against what is already live", applied to a guard
   somebody else CITES rather than one you author. A finding arriving with
   file-and-line evidence is exactly when it is least likely to be run.
4. **What fixing them costs**, in the user's units: roughly how long, and what it delays.
5. **A recommendation**, then wait.

**Round two and beyond needs the user to say so explicitly. "Re-review" means once.**
If a round produces fixes, do not launch the next round on your own initiative. **If a
fix contains a NEW mistake of its own, stop immediately and say so**: a different
mistake each round, rather than a repeat, means the work has outgrown solo iteration,
and the honest move is to offer to work through it together, not another round.

Measured on a real project before this step existed: four requested fixes became six
rounds and 1,030 lines of new checking code against zero lines of product code, and a
customer's team lost a day of testing while the product sat already merged in their QA
and answering correctly. Every finding in the last three rounds was in the harness.

### 4. Reproduce before fixing

Every finding THE USER CHOSE TO ACT ON in step 3a gets REPRODUCED by executing the
failing input through the real path before any fix is written. A finding that does not
reproduce is marked **disputed**, with the exact command you ran; it is never silently
accepted (fixing a non-bug) or silently dropped (losing a real one). Anything the user
deferred is RECORDED where a reader will meet it, not fixed. Respect the
side-effect guard: no reproduction against production, live services, or real
credentials; a side-effectful reproduction stays unreproduced and is adjudicated by
reading, with that stated.

**When two agents independently report the same finding, treat it as the priority.**
They ran in parallel from different prompts and different checklists, so agreement
is a signal rather than duplication. Both convergent findings on 2026-07-22 were
real and both had production consequences; on 2026-07-27 the two that converged
were the PR body being quantitatively false and the schema disclosure. Never dedupe
a convergent pair down to one line and lose that it was found twice.

Also check `git status` in the repo after the agents return, before writing any fix.
A reviewer killed mid-mutation has left the tree mutated, and building a fix on top
of that state ships the mutation.

### 5. Verify suggestions against the authoritative gate

A reviewer's proposed fix is a hypothesis. Check it against the authoritative
release gate or the code before applying (the red-link lesson: one linter's
complaint was another gate's by-design zero). Declining a finding with a stated
reason is a legitimate outcome and goes in the verdict comment.

### 5a. Prefer the fix that removes, and flag the fix that adds

**Text or code a fix ADDS is a first draft, and it gets the scrutiny a first draft gets.**
When a finding can be closed by deleting or narrowing (drop the exception, drop the "or"
alternative, tighten the condition), do that rather than writing something new. When the
fix has to add a list, an exception, a new alternative, a new equivalence ("X counts as
Y") or a new feature, name each added piece in the fix commit message and in the prompt
for the fix-commit review, as "new text, attack
this first".

Measured twice on one day, 2026-09-18, on `secure-dev-guardrails`. PR #7: the round-1 fix
added private-registry awareness and that addition carried 4 of round 2's findings,
including printing index URLs with their logins. PR #9: the fix closed 10 of 13 wording
findings, and all 4 new Majors sat in text it had added (a closed list of "acts" that
missed restarting a service, a test-system exception, "a limit" accepted as the check,
a version pin called an integrity check). Every fix that deleted or narrowed held.

### 6. Every fix gets a non-vacuous regression test

Prove each new test by mutation (break the guard, confirm exactly that test fails,
restore by re-applying the code, never `git checkout`) or by contrast (the same
input with the gate un-armed produces the opposite outcome). Confirm the printed
suite total increased by exactly the number of tests added. Never add tests via
shell redirection (`cat >>`, `tee -a`); use Edit/Write so the test-integrity hook
sees them.

**A test written to close an "X IS UNTESTED" finding is the single most likely place to
write a vacuous one, and mutation is the only thing that sees it.** Measured on a real
project: a reviewer reported that an ordering band had no test; the test written
minutes later to close that finding put its subject BELOW the group it was compared
against, so the wrong implementation answered identically and the new test passed with the
rule deleted. Seventh instance of that shape on that project, and the first one authored
inside the remedy for a finding that named it. The mechanism is not carelessness: such a
test is written fast, against a fixture chosen to make the INTENDED answer easy to state,
under the belief that the shape is now front of mind.

**So when a finding says something is untested, AND whenever the thing under test is a
BOUND, a LIMIT, a BUDGET or a CAP, do this before writing the test:**

**That second trigger was added 2026-09-07, because the first one alone let a defect
through.** The original wording fired only on a finding that named something untested, so a
test written from scratch alongside its own guard never met it. On a real project a per-batch wait
budget was implemented per PERSON and its test asserted
`sum(slept) == 9144 * 3 * len(job["results"])`, putting the corpus size into the expected
value so the assertion agreed with the defect. It passed a full review round and a mutation
run, because the bound CONSTANT was mutated and correctly killed while nothing mutated where
the counter lived. **For a bound, the fixture must hold MORE items than the bound**, or the
right rule and the wrong one agree by arithmetic.

1. **List the candidate rules, as a list, in writing.** The rule under test plus every
   wrong implementation a reader could reasonably expect. Four is common.
2. **Choose ONE fixture that answers differently under every one of them.** If you cannot,
   the fixture is wrong, not the assertion. This is the step that gets skipped.
3. **Put that list in the test's own docstring**, with the answer each rule would give, so
   a later reader can re-run the check by reading.
4. **Mutate to each wrong rule in turn**, not only to "the rule deleted", and confirm the
   named test goes red for each. Keep a control mutation that no test claims; if it dies
   too, the fixture is over-constrained and the kills mean less than they look.

That is four cheap steps and it closed a shape three prose rules had failed to close.
**Derive the mutation set from the DIFF, not from the fixes you remember making.**
Read the guard-shaped lines the branch actually added, BEFORE choosing a set:

```bash
git diff -U0 --no-color "$BASE"...HEAD -- '*.py' '*.js' '*.ts' '*.sh' \
  | grep -E '^\+[[:space:]]*(if |elif |assert |raise |return (None|False|0|\[\]|""|\x27\x27))'
```

Check that the diff itself is not empty before trusting an empty result, because "no
guards" and "nothing was read" print the same thing. A set assembled from memory
covers the lines you were already thinking about, which are the least likely to be
wrong. On the PR that produced this rule an author-chosen set of six missed five real
guards, and one of the five was that PR's Critical. If a listed line has no test that
can fail, say so in the verdict rather than dropping it.

**Then run it AGAIN against the final diff, because an early enumeration goes stale.**
Enumerate at the start so the list shapes your set rather than arriving after it, and
enumerate again at the end, because everything you wrote in between is un-enumerated.
On a real project an early run against a partial diff checked 5 guard-shaped lines
while the FINAL diff carried 18. Treat "mutations enumerated from the diff" as a claim
with a SHA attached, and state in the verdict which diff the set was enumerated against.

**The mutation harness itself has to be trustworthy, or every row it prints is
decoration.** Three requirements, all cheap, none optional. Purge `__pycache__`
before each run: CPython invalidates bytecode on (mtime, size) at one-second
granularity, so a same-second mutate/run/restore can leave the OLD bytecode active,
and it lies in both directions (a mutant "survives", or a restore silently does not
take). Require a 0-fail BASELINE before mutating, because a suite that was already
red makes every later result unreadable. Require a 0-fail run AFTER restoring, which
is what proves you gave the code back. Then check the kill condition itself: assert
the run went RED and that the FAILING line is the intended one. Matching only the
test's label is vacuous when the harness prints that label on pass and fail alike.
Restore from an in-memory string; if the harness keeps a dict of saved file bytes
instead, key it by FULL PATH, since a basename key silently collides same-named files
at different depths and the restore then corrupts a source file. Nothing but the
post-restore run can see that one.

**Read every changed test line in the WEAKENING direction.** List the assertions
REMOVED from test files in the diff. A loosened assertion changes
neither the suite count nor the reviewed-as-new surface, so nothing else catches it,
and it is green by construction. When one appears, break what the ORIGINAL assertion
protected, not a nearby site.

### 7. Re-run

Touched suites first, then the FULL suite. A subset of suites is not "green".

### 8. Post the verdict comment

One PR comment (`gh pr comment`) containing:
- Per-agent finding counts (code-reviewer Critical/Warning/Suggestion, deploy-guard
  Blocker/Warning/Note, pr-validator verdict + totals).
- Any finding two reviewers both reported, called out as convergent. That pair is
  the strongest signal the round produces.
- Every Critical/Blocker with its status: **Fixed** (name the regression test),
  **Declined** (state the reason), or **Open**.
- The reproduction command for each executed finding.
- The machine-readable LAST line, exactly one of:
  - `Review-round verdict: CLEAN @ <reviewed-HEAD-sha>`
  - `Review-round verdict: CRITICALS-OPEN @ <reviewed-HEAD-sha>`
  - `Review-round verdict: CHECKPOINT-ADVISORY @ <reviewed-HEAD-sha>`

**Which value, and this is the part that is easy to get wrong.** `CLEAN` means a
COMPLETED round: every Critical and Blocker was driven to Fixed or Declined, and
the full suite was re-run green. It is a certification, and `/release` treats it
as one.

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

## Background-session note

The verdict-then-merge flow runs `gh pr comment` / `gh pr merge`, which the
auto-mode classifier can block on agent-authored PRs. A transient stage-2
classifier error gets ONE plain retry; a sustained block means asking the user to
switch the session to manual mode, not fighting it.

## The work items go to the todo list

Every finding the user picks to fix becomes a work item on the project's todo list, in
the order they have to be taken, and so does every one they defer rather than reject.
A deferred finding with nothing on the list is indistinguishable from one nobody found.
The agents themselves write nothing: a subagent cannot ask the user anything, so the
write belongs here, in whoever spawned them.

## Guardrails

- Never merge, deploy, or push to the base branch from this skill; the ship verb is
  `/release`.
- Never post a CLEAN verdict while any Critical/Blocker is unresolved and
  undeclined.
- Never bind a verdict to a SHA you did not actually review and test.
- In checkpoint mode, nothing here refuses, blocks, or rolls back the PR.
