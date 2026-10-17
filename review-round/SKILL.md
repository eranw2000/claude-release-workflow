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

**Stacked branch (base not main)?** First run this folder's `stacked_base_check.py
--repo <worktree>`; 1: merge base, re-test; 2: stop.

**Did a helper build it? Check it against the user's pick FIRST.** Before any reviewer runs,
read the builder's report for every decision it made itself ("a call I made", "I decided",
"instead", "chose", "not X but Y") AND each design line of the brief you gave it (a builder
obeying a rewritten brief reports no choice), and compare both, word by word,
with the pick or the recorded decision. Anything that narrows, widens or replaces it goes to
the user now, in one question: a reviewer must never be the first to notice.

### 1a. Choose the reviewers before launching any of them

Decide which review agents this repo needs, and settle it before step 2.

**Read `~/.claude/skills/review-round/reference/1a-choose-reviewers.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**


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

Write each reviewer a prompt aimed at the riskiest spots in this diff, naming what it must try to break.

**Read `~/.claude/skills/review-round/reference/2-adversarial-prompts.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

### 3. Launch the chosen reviewers in ONE message

Start every chosen reviewer in one message so they run in parallel, and say which ones were skipped and why.

**Read `~/.claude/skills/review-round/reference/3-launch-reviewers.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

### 3a. Every mode: take the findings to the user BEFORE fixing any of them

Report every finding, then stop and let the user choose what gets fixed.

**Read `~/.claude/skills/review-round/reference/3a-findings-to-the-user.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

### 4. Reproduce before fixing

Reproduce each chosen finding by running the failing input through the real path before any fix is written.

**Read `~/.claude/skills/review-round/reference/4-reproduce.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

### 5. Verify suggestions against the authoritative gate

A reviewer's proposed fix is a hypothesis. Check it against the authoritative
release gate or the code before applying (the red-link lesson: one linter's
complaint was another gate's by-design zero). Declining a finding with a stated
reason is a legitimate outcome and goes in the verdict comment.

### 5a. Prefer the fix that removes, and flag the fix that adds

Close a finding by deleting or narrowing where you can; name every added piece for the re-review; and when a finding names a MECHANISM, list every path the changed function has to that result.

**Read `~/.claude/skills/review-round/reference/5a-prefer-removal.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

### 6. Every fix gets a non-vacuous regression test

Each fix gets a test that fails without the fix, proven by breaking the guard or by contrast, and the suite total must rise by exactly the tests added.

**Read `~/.claude/skills/review-round/reference/6-regression-tests.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

### 7. Re-run

Touched suites first, then the FULL suite. A subset of suites is not "green".

### 8. Post the verdict comment

Post one PR comment with the per-reviewer counts, what was fixed or declined, and the last-line verdict that `/release` reads.

**Read `~/.claude/skills/review-round/reference/8-verdict-comment.md` now, in full, and follow it. It is this step's full text; the line above is only a summary and is not enough to act on.**

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
- Never post CLEAN while the release security scan has an unfixed, unwaived `[NEW]`
  finding at the reviewed head (step 8).
- Never bind a verdict to a SHA you did not actually review and test.
- In checkpoint mode, nothing here refuses, blocks, or rolls back the PR.
