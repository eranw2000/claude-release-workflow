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
  advisory by design; the hard gate is `/release`.

## The loop

### 1. Scope

Establish what is under review: `git diff "$BASE"...HEAD` for a branch,
`gh pr view` / `gh pr diff` for a PR. Record the reviewed HEAD SHA
(`git rev-parse HEAD` on the branch); the verdict in step 8 binds to it.

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

This marker string is OWNED by review-round; `/release` reads it verbatim and
compares the SHA to the PR head at release time. The SHA binding is load-bearing:
without it, a CLEAN posted at checkpoint time would silently certify commits pushed
AFTER the review. If you fixed anything in steps 4-7, the reviewed SHA is the
post-fix HEAD you re-ran the suite on; push first, then post.

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
