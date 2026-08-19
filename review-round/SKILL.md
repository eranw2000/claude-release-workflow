---
model: fable
name: review-round
description: "Run the codified three-reviewer loop on a diff or PR: author adversarial per-agent prompts pointing at the riskiest spots, launch code-reviewer + deploy-guard + pr-validator in parallel, reproduce every finding by execution before fixing, prove each fix with a non-vacuous regression test, and post the PR verdict comment that /release gates on. Use before shipping, or when the user says 'review round', 'run the reviewers', 'full review pass', or wants the pre-release review loop. For a spec-anchored review against your requirements or spec docs, run a spec-review pass instead."
---

# Review round

The codified review->fix->re-verify->verdict loop. Three agents driven by
hand-authored adversarial prompts catch Criticals that authoring and testing miss;
this skill makes the discipline repeatable instead of re-invented per PR. It drives
the companion review agents (code-reviewer, deploy-guard, pr-validator), published
separately at https://github.com/eranw2000/claude-review-agents.

Model note: the load-bearing work here is judgment (adversarial prompt authoring,
adjudicating findings), hence `model: fable`. If the fix loop turns into a long
implementation session, switch explicitly with `/model opus`.

## Two modes

- **Pre-release mode (default):** the full loop, steps 1-8. Criticals get fixed or
  formally declined before the verdict posts.
- **Checkpoint mode** (invoked from `/pr-checkpoint` step 6): steps 1-3 and 8 always;
  the fix loop (4-7) is optional and NOTHING blocks the PR. The checkpoint stays
  advisory by design; the hard gate is `/release`. Its step-8 verdict says
  `CHECKPOINT-ADVISORY`, never `CLEAN`, so the mode is visible to the gate that
  reads it later. See step 8 for why the two must not share a word.

## The loop

### 1. Scope

Establish what is under review: `git diff "$BASE"...HEAD` for a branch,
`gh pr view` / `gh pr diff` for a PR. Record the reviewed HEAD SHA
(`git rev-parse HEAD` on the branch); the verdict in step 8 binds to it.

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

Write a per-agent prompt that NAMES the 2-3 riskiest spots in this diff and the
specific claims each agent should verify by execution. Point code-reviewer at the
paths you are least sure of; point deploy-guard at the config/prose/topology angle;
tell pr-validator which added tests must be proven to have run. A generic "review
this diff" prompt is a protocol violation: the wins come from aiming the reviewers
at the weak spots.

### 3. Launch all three in ONE message

Spawn `code-reviewer`, `deploy-guard`, and `pr-validator` in a single message so
they run in parallel; they are independent. Tell each which mode this is
(checkpoint: report only; pre-release: findings will be driven to resolution).

*(Checkpoint mode: skip to step 8 unless the caller asked for the fix loop.)*

### 4. Reproduce before fixing

Every Critical / Blocker / Warning gets REPRODUCED by executing the failing input
through the real path before any fix is written. A finding that does not reproduce
is marked **disputed**, with the exact command you ran; it is never silently
accepted (fixing a non-bug) or silently dropped (losing a real one). Respect the
side-effect guard: no reproduction against production, live services, or real
credentials; a side-effectful reproduction stays unreproduced and is adjudicated by
reading, with that stated.

### 5. Verify suggestions against the authoritative gate

A reviewer's proposed fix is a hypothesis. Check it against the authoritative
release gate or the code before applying (one checker's complaint can be another
gate's by-design zero). Declining a finding with a stated reason is a legitimate
outcome and goes in the verdict comment.

### 6. Every fix gets a non-vacuous regression test

Prove each new test by mutation (break the guard, confirm exactly that test fails,
restore by re-applying the code, never `git checkout`) or by contrast (the same
input with the gate un-armed produces the opposite outcome). Confirm the printed
suite total increased by exactly the number of tests added. Never add tests via
shell redirection (`cat >>`, `tee -a`); use Edit/Write so the test-integrity hook
(shipped in this pack) sees them.

**Derive the mutation set from the DIFF, not from the fixes you remember making.**
Read the guard-shaped lines the branch actually added:

```bash
git diff -U0 --no-color "$BASE"...HEAD -- '*.py' '*.js' '*.ts' '*.sh' \
  | grep -E '^\+[[:space:]]*(if |elif |assert |raise |return (None|False|0|\[\]|""|\x27\x27))'
```

A set assembled from memory covers the lines you were already thinking about, which
are the least likely to be wrong. On the PR that produced this rule an author-chosen
set of six missed five real guards, and one of the five was that PR's Critical. If a
listed line has no test that can fail, say so in the verdict rather than dropping it.

Two traps in the mutation run itself. Purge `__pycache__` first and require a zero-fail
baseline before mutating, because a same-second mutate and restore can leave stale
bytecode active, and then the check lies in either direction. And restore from a
snapshot taken before the run, never with `git checkout`: a reviewer killed mid-mutation
leaves the tree mutated, and a fix built on that state ships the mutation.

### 7. Re-run

Touched suites first, then the FULL suite. A subset of suites is not "green".

### 8. Post the verdict comment

One PR comment (`gh pr comment`) containing:
- Per-agent finding counts (code-reviewer Critical/Warning/Suggestion, deploy-guard
  Blocker/Warning/Note, pr-validator verdict + totals).
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
only in this transcript, and `/release` will later see a PR that looks unreviewed. Say
so in your final message, in those terms, and hand the exact comment body to the user
to post. Never end a round reporting a verdict you could not confirm is on the PR.

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

## Guardrails

- Never merge, deploy, or push to the base branch from this skill; the ship verb is
  `/release`.
- Never post a CLEAN verdict while any Critical/Blocker is unresolved and
  undeclined.
- Never bind a verdict to a SHA you did not actually review and test.
- In checkpoint mode, nothing here refuses, blocks, or rolls back the PR.
